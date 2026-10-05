/* cgr runtime call tracer shim for C and C++ (issue #1252).
 *
 * Compile the target with instrumentation and link this file:
 *
 *   cc -finstrument-functions -g -O0 your_sources... cgr_trace_shim.c -o app
 *
 * The compiler injects __cyg_profile_func_enter/exit around every function;
 * the shim keeps a per-thread call stack of function addresses and counts
 * exact (caller, callee) pairs in a fixed-size open-addressing table. At
 * process exit it writes cgr-trace.addrs (override with CGR_TRACE_ADDRS):
 *
 *   exe <executable path>
 *   slide <load bias of the main image, decimal; ASLR slide for a PIE>
 *   <caller addr hex> <callee addr hex> <count>
 *
 * `cgr trace convert` symbolises those addresses (atos on macOS, addr2line
 * elsewhere) into the trace interchange format. Everything here is
 * no_instrument_function so the shim never traces itself; overhead is a
 * mutex-guarded table insert per call, acceptable for test workloads.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE /* dl_iterate_phdr on glibc */
#endif

#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#ifdef __APPLE__
#include <mach-o/dyld.h>
#elif defined(__linux__)
#include <link.h>
#include <unistd.h>
#endif

#define CGR_ATTR __attribute__((no_instrument_function))

#define CGR_STACK_MAX 4096
/* The depth counter saturates here instead of growing without bound: exits
 * that never run (longjmp, C++ exceptions under clang++) would otherwise
 * carry a signed int to INT_MAX and past it, and a wrapped negative depth
 * indexed cgr_stack out of bounds (#2264). */
#define CGR_DEPTH_CEILING (CGR_STACK_MAX * 2)
#define CGR_TABLE_BITS 16
#define CGR_TABLE_SIZE (1u << CGR_TABLE_BITS)
#define CGR_TABLE_MASK (CGR_TABLE_SIZE - 1u)

typedef struct {
  void *caller;
  void *callee;
  uint64_t count;
} cgr_edge;

static cgr_edge cgr_table[CGR_TABLE_SIZE];
static pthread_mutex_t cgr_lock = PTHREAD_MUTEX_INITIALIZER;
static _Thread_local void *cgr_stack[CGR_STACK_MAX];
/* The shim's own frame address at each enter. The stack grows down, so a
 * live caller's entry lies at or above every callee's: above for a real
 * call, level for a call the compiler inlined (its hooks still run, from the
 * caller's own frame). An entry strictly below a new call is one a longjmp
 * or exception already left. Equal addresses stay ambiguous, so they are
 * never taken as proof. */
static _Thread_local uintptr_t cgr_frames[CGR_STACK_MAX];
static _Thread_local int cgr_depth = 0;
/* Set while this thread is inside the shim. A signal handler that is itself
 * instrumented would otherwise re-enter cgr_record on a thread that already
 * holds cgr_lock and deadlock; such a nested call is skipped instead. */
static _Thread_local volatile sig_atomic_t cgr_busy = 0;
/* Lock-free flags, so a re-entrant call can set them from a signal handler
 * where taking cgr_lock could deadlock. `dropped`: edges were lost (table
 * full, stack too deep, a nested call skipped). `unwound`: a frame was left
 * without its exit hook, so a later call may have been given a stale caller
 * (#2264). Either makes the converter refuse the trace as inexact. */
static atomic_int cgr_dropped = 0;
static atomic_int cgr_unwound = 0;

CGR_ATTR static uint64_t cgr_hash(void *caller, void *callee) {
  uint64_t h = (uint64_t)(uintptr_t)caller * 0x9E3779B97F4A7C15ull;
  h ^= (uint64_t)(uintptr_t)callee + 0x517CC1B727220A95ull + (h << 6) + (h >> 2);
  return h;
}

CGR_ATTR static void cgr_mark(atomic_int *flag) {
  atomic_store_explicit(flag, 1, memory_order_relaxed);
}

CGR_ATTR static void cgr_record(void *caller, void *callee) {
  uint64_t slot = cgr_hash(caller, callee) & CGR_TABLE_MASK;
  pthread_mutex_lock(&cgr_lock);
  for (uint32_t probe = 0; probe < CGR_TABLE_SIZE; probe++) {
    cgr_edge *edge = &cgr_table[(slot + probe) & CGR_TABLE_MASK];
    if (edge->count == 0) {
      edge->caller = caller;
      edge->callee = callee;
      edge->count = 1;
      pthread_mutex_unlock(&cgr_lock);
      return;
    }
    if (edge->caller == caller && edge->callee == callee) {
      edge->count++;
      pthread_mutex_unlock(&cgr_lock);
      return;
    }
  }
  cgr_mark(&cgr_dropped); /* table full: stop distinguishing, keep running */
  pthread_mutex_unlock(&cgr_lock);
}

#ifdef __linux__
/* The first object dl_iterate_phdr reports is the main program; its
 * dlpi_addr is the load bias (the ASLR slide for a PIE, 0 for -no-pie). */
CGR_ATTR static int cgr_main_load_bias(struct dl_phdr_info *info, size_t size,
                                       void *data) {
  (void)size;
  *(long *)data = (long)info->dlpi_addr;
  return 1;
}
#endif

CGR_ATTR static void cgr_exe_path(char *exe, size_t exe_size, long *slide) {
  exe[0] = '\0';
  *slide = 0;
#ifdef __APPLE__
  uint32_t size = (uint32_t)exe_size;
  if (_NSGetExecutablePath(exe, &size) != 0) {
    exe[0] = '\0'; /* buffer too small: leave empty, the converter rejects it */
  }
  *slide = (long)_dyld_get_image_vmaddr_slide(0);
#elif defined(__linux__)
  ssize_t got = readlink("/proc/self/exe", exe, exe_size - 1);
  /* readlink does not NUL-terminate, and a full buffer means truncation; a
   * truncated path would mis-symbolise, so treat it as unknown. */
  if (got > 0 && (size_t)got < exe_size - 1) {
    exe[got] = '\0';
  } else {
    exe[0] = '\0';
  }
  dl_iterate_phdr(cgr_main_load_bias, slide);
#else
  (void)exe_size; /* other platforms: exe stays empty, targets ELF/Mach-O only */
#endif
}

CGR_ATTR static void cgr_write(void) {
  const char *path = getenv("CGR_TRACE_ADDRS");
  if (path == NULL || path[0] == '\0') {
    path = "cgr-trace.addrs";
  }
  /* Write to a sibling temp file and rename on success, so a crash or a
   * partial/failed write never publishes a truncated trace as complete. */
  char tmp[4200];
  int printed = snprintf(tmp, sizeof tmp, "%s.tmp", path);
  if (printed < 0 || (size_t)printed >= sizeof tmp) {
    return;
  }
  FILE *out = fopen(tmp, "w");
  if (out == NULL) {
    return;
  }
  char exe[4096];
  long slide;
  cgr_exe_path(exe, sizeof exe, &slide);
  fprintf(out, "exe %s\n", exe);
  fprintf(out, "slide %ld\n", slide);
  /* Serialize under the same lock cgr_record takes, so a thread still running
   * at exit cannot mutate the table or dropped flag mid-write. */
  pthread_mutex_lock(&cgr_lock);
  if (atomic_load_explicit(&cgr_dropped, memory_order_relaxed)) {
    fprintf(out, "dropped 1\n");
  }
  if (atomic_load_explicit(&cgr_unwound, memory_order_relaxed)) {
    fprintf(out, "unwound 1\n");
  }
  for (uint32_t index = 0; index < CGR_TABLE_SIZE; index++) {
    if (cgr_table[index].count != 0) {
      fprintf(out, "%llx %llx %llu\n",
              (unsigned long long)(uintptr_t)cgr_table[index].caller,
              (unsigned long long)(uintptr_t)cgr_table[index].callee,
              (unsigned long long)cgr_table[index].count);
    }
  }
  pthread_mutex_unlock(&cgr_lock);
  int ok = (ferror(out) == 0);
  if (fclose(out) != 0) {
    ok = 0;
  }
  if (ok) {
    rename(tmp, path);
  } else {
    remove(tmp);
  }
}

CGR_ATTR void __cyg_profile_func_enter(void *this_fn, void *call_site);
CGR_ATTR void __cyg_profile_func_exit(void *this_fn, void *call_site);

static pthread_once_t cgr_once = PTHREAD_ONCE_INIT;

CGR_ATTR static void cgr_register_atexit(void) { atexit(cgr_write); }

void __cyg_profile_func_enter(void *this_fn, void *call_site) {
  (void)call_site;
  if (cgr_busy) {
    /* An instrumented signal handler interrupted the shim on this thread:
     * its call goes unrecorded, so the counts are incomplete. */
    cgr_mark(&cgr_dropped);
    return;
  }
  cgr_busy = 1;
  pthread_once(&cgr_once, cgr_register_atexit);
  uintptr_t frame = (uintptr_t)__builtin_frame_address(0);
  if (cgr_depth > 0 && cgr_depth <= CGR_STACK_MAX) {
    if (frame > cgr_frames[cgr_depth - 1]) {
      /* The recorded caller lies below this call: it is a frame a longjmp or
       * exception skipped, and the edge below names it wrongly. */
      cgr_mark(&cgr_unwound);
    }
    cgr_record(cgr_stack[cgr_depth - 1], this_fn);
  }
  if (cgr_depth < CGR_STACK_MAX) {
    cgr_stack[cgr_depth] = this_fn;
    cgr_frames[cgr_depth] = frame;
  } else {
    /* Deeper than the stack holds: this frame's callees have no known
     * caller and go unrecorded, so the counts are no longer exact. */
    cgr_mark(&cgr_dropped);
  }
  if (cgr_depth < CGR_DEPTH_CEILING) {
    cgr_depth++;
  }
  cgr_busy = 0;
}

void __cyg_profile_func_exit(void *this_fn, void *call_site) {
  (void)call_site;
  if (cgr_busy) {
    return; /* its enter was skipped too, and marked the trace */
  }
  cgr_busy = 1;
  uintptr_t frame = (uintptr_t)__builtin_frame_address(0);
  if (cgr_depth > CGR_STACK_MAX) {
    cgr_depth--; /* beyond the stored frames; already marked dropped */
  } else if (cgr_depth > 0 && cgr_stack[cgr_depth - 1] == this_fn) {
    if (frame > cgr_frames[cgr_depth - 1]) {
      /* Same function, different invocation: an exit above the top entry's
       * frame belongs to an outer frame of this function, and the top is a
       * recursive frame a jump skipped. */
      cgr_mark(&cgr_unwound);
    }
    cgr_depth--;
  } else if (cgr_depth > 0) {
    /* Not the top: a longjmp or exception skipped the exits above it. Calls
     * made since may have named a skipped frame as their caller, and a
     * recursive function's frames cannot be told apart by address, so the
     * trace is marked. The depth is still resynchronised to the nearest
     * frame of this function, so it cannot grow without bound. */
    cgr_mark(&cgr_unwound);
    for (int index = cgr_depth - 1; index >= 0; index--) {
      if (cgr_stack[index] == this_fn) {
        cgr_depth = index;
        break;
      }
    }
  }
  cgr_busy = 0;
}
