package cgr.trace;

import java.io.IOException;
import java.io.Writer;
import java.lang.StackWalker.StackFrame;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentSkipListSet;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Aggregates caller/callee observations and flushes them to the cgr trace
 * interchange format (JSONL, format version 1) on JVM shutdown.
 *
 * <p>Instrumented method entries call {@link #enter}. The caller is recovered
 * by walking the stack, mirroring the Python tracer's frame introspection:
 * an edge is recorded only when both endpoints belong to instrumented
 * packages, so JDK and third-party frames never appear in the trace.
 */
public final class TraceRecorder {

    /** One aggregated caller/callee pair. */
    private static final class PairStats {
        final AtomicLong count = new AtomicLong();
        final Set<String> workloads = new ConcurrentSkipListSet<>();
        final Set<String> receiverTypes = new ConcurrentSkipListSet<>();
    }

    private record PairKey(
            String callerPath, String callerQualname, int callerLine,
            String calleePath, String calleeQualname, int calleeLine) {}

    private static final int RECEIVER_SAMPLE_LIMIT = 8;
    private static final int FORMAT_VERSION = 1;
    private static final String LANGUAGE = "jvm";
    private static final String TRACER_NAME = "cgr-trace-jvm";

    private static final StackWalker WALKER =
            StackWalker.getInstance(StackWalker.Option.RETAIN_CLASS_REFERENCE);

    private static final Map<PairKey, PairStats> PAIRS = new ConcurrentHashMap<>();

    private static volatile String[] includePrefixes = new String[0];
    private static volatile String repoRoot = "";
    private static volatile Path outputPath = Path.of("cgr-trace.jsonl");
    // Per-thread with inheritance: a test runner labelling its own thread
    // also labels workers it spawns afterwards, and concurrent runners
    // cannot clobber each other's provenance.
    private static final InheritableThreadLocal<String> WORKLOAD =
            new InheritableThreadLocal<>();

    private TraceRecorder() {}

    static void configure(String[] includes, String repo, Path output, String workload) {
        includePrefixes = includes.clone();
        repoRoot = repo;
        outputPath = output;
        if (workload != null) {
            WORKLOAD.set(workload);
        }
        Runtime.getRuntime().addShutdownHook(new Thread(TraceRecorder::write, "cgr-trace-writer"));
    }

    /**
     * Tags calls recorded on this thread (and threads it spawns afterwards)
     * with a workload label (test id, scenario).
     */
    public static void setWorkload(String workload) {
        WORKLOAD.set(workload);
    }

    /**
     * Records entry into an instrumented method. Called from injected bytecode.
     *
     * @param receiver {@code this} for instance methods, {@code null} for static
     * @param className binary class name, e.g. {@code com.example.Foo$Inner}
     * @param methodName method name as compiled, e.g. {@code bar} or {@code lambda$run$0}
     * @param firstLine first line of the method body per LineNumberTable, or -1
     * @param sourcePath package-derived source path, e.g. {@code com/example/Foo.java}
     */
    public static void enter(
            Object receiver, String className, String methodName, int firstLine, String sourcePath) {
        // Frame 0 is enter() itself, frame 1 the instrumented method. The
        // caller is the nearest project frame above that: JDK internals,
        // lambda-metafactory hidden classes, generated proxies, and other
        // uninstrumented glue are walked through, so an edge like
        // list.forEach(this::handle) attributes to the code that really
        // initiated the call. That glue is exactly what static analysis
        // cannot see, which makes these the highest-value edges.
        StackFrame caller = WALKER.walk(frames -> frames
                .skip(2)
                .filter(f -> !f.getClassName().startsWith("cgr.trace."))
                .filter(f -> !isSynthetic(f.getClassName()))
                .filter(f -> included(f.getClassName()))
                .findFirst()
                .orElse(null));
        if (caller == null) {
            return;
        }
        PairKey key = new PairKey(
                sourcePathOf(caller.getClassName(), caller.getFileName()),
                qualname(caller.getClassName(), caller.getMethodName()),
                caller.getLineNumber(),
                sourcePath,
                qualname(className, methodName),
                firstLine);
        PairStats stats = PAIRS.computeIfAbsent(key, k -> new PairStats());
        long seen = stats.count.incrementAndGet();
        String workload = WORKLOAD.get();
        if (workload != null) {
            stats.workloads.add(workload);
        }
        if (receiver != null && seen <= RECEIVER_SAMPLE_LIMIT) {
            stats.receiverTypes.add(receiver.getClass().getName());
        }
    }

    /** Hidden classes the lambda metafactory and proxy machinery generate. */
    private static boolean isSynthetic(String className) {
        return className.contains("$$Lambda") || className.contains("$Proxy");
    }

    private static boolean included(String className) {
        for (String prefix : includePrefixes) {
            // Boundary-aware: include=com.example must not match the sibling
            // package com.exampleevil.
            if (className.equals(prefix)
                    || className.startsWith(prefix + ".")
                    || className.startsWith(prefix + "$")) {
                return true;
            }
        }
        return false;
    }

    /** {@code com.example.Foo$Inner} + {@code bar} form {@code Foo$Inner.bar}. */
    private static String qualname(String className, String methodName) {
        int lastDot = className.lastIndexOf('.');
        String simple = lastDot < 0 ? className : className.substring(lastDot + 1);
        return simple + "." + methodName;
    }

    /** Package directories plus the compilation unit's file name. */
    private static String sourcePathOf(String className, String fileName) {
        int lastDot = className.lastIndexOf('.');
        String packagePath = lastDot < 0 ? "" : className.substring(0, lastDot).replace('.', '/') + "/";
        return packagePath + (fileName == null ? "" : fileName);
    }

    static void write() {
        try {
            Path parent = outputPath.toAbsolutePath().getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            try (Writer out = Files.newBufferedWriter(outputPath, StandardCharsets.UTF_8)) {
                out.write(header());
                out.write('\n');
                for (Map.Entry<PairKey, PairStats> entry : PAIRS.entrySet()) {
                    out.write(record(entry.getKey(), entry.getValue()));
                    out.write('\n');
                }
            }
        } catch (IOException e) {
            System.err.println("cgr-trace-jvm: failed to write trace: " + e);
        }
    }

    private static String header() {
        return "{\"kind\":\"header\",\"version\":" + FORMAT_VERSION
                + ",\"language\":" + Json.string(LANGUAGE)
                + ",\"repo_root\":" + Json.string(repoRoot)
                + ",\"tracer\":" + Json.string(TRACER_NAME) + "}";
    }

    private static String record(PairKey key, PairStats stats) {
        StringBuilder sb = new StringBuilder(256);
        sb.append("{\"kind\":\"call\",\"caller\":")
                .append(frame(key.callerPath(), key.callerQualname(), key.callerLine()))
                .append(",\"callee\":")
                .append(frame(key.calleePath(), key.calleeQualname(), key.calleeLine()))
                .append(",\"count\":").append(stats.count.get())
                .append(",\"workloads\":").append(Json.strings(stats.workloads))
                .append(",\"receiver_types\":").append(Json.strings(stats.receiverTypes))
                .append('}');
        return sb.toString();
    }

    private static String frame(String path, String qualname, int line) {
        return "{\"path\":" + Json.string(path)
                + ",\"qualname\":" + Json.string(qualname)
                + ",\"line\":" + Math.max(line, 0) + "}";
    }
}
