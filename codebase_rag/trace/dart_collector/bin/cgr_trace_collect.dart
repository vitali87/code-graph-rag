// cgr runtime call-graph collector for Dart (issue #1255).
//
// Spawns the target under `dart run --pause-isolates-on-exit` with the VM
// service enabled, waits for the main isolate to pause at exit, pulls the
// profiler's CPU samples over the VM Service protocol, and writes observed
// caller/callee pairs in the cgr trace interchange format (JSONL, format
// version 1). Sampled stacks give true observed relationships: dispatch
// through function values, callbacks, and dynamic-typed calls appears
// whenever samples landed there. Counts are sample counts, not call counts.
//
// Usage:
//   dart run cgr_trace_collector:cgr_trace_collect \
//     --repo /abs/repo [--output cgr-trace.jsonl] [--workload label] \
//     -- target.dart [args...]

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:vm_service/vm_service.dart';
import 'package:vm_service/vm_service_io.dart';

final class Frame {
  Frame(this.path, this.qualname, this.line);

  final String path;
  final String qualname;
  final int line;

  String get key => '$path$qualname$line';

  Map<String, Object> toJson() =>
      {'path': path, 'qualname': qualname, 'line': line};
}

String jsonLine(Map<String, Object?> value) => json.encode(value);

Future<void> main(List<String> argv) async {
  var repo = '';
  var output = 'cgr-trace.jsonl';
  String? workload;
  final target = <String>[];
  var index = 0;
  while (index < argv.length) {
    final arg = argv[index];
    if (arg == '--repo' && index + 1 < argv.length) {
      repo = argv[++index];
    } else if (arg == '--output' && index + 1 < argv.length) {
      output = argv[++index];
    } else if (arg == '--workload' && index + 1 < argv.length) {
      workload = argv[++index];
    } else if (arg == '--') {
      target.addAll(argv.sublist(index + 1));
      break;
    } else {
      target.add(arg);
    }
    index++;
  }
  if (repo.isEmpty || target.isEmpty) {
    stderr.writeln(
        'usage: cgr_trace_collect --repo /abs/repo [--output f] [--workload w]'
        ' -- target.dart [args...]');
    exitCode = 2;
    return;
  }
  if (repo.endsWith('/')) {
    repo = repo.substring(0, repo.length - 1);
  }

  // VM flags go to the VM directly; `dart run` would reject them.
  final process = await Process.start(Platform.resolvedExecutable, [
    '--pause-isolates-on-exit',
    '--enable-vm-service=0',
    '--profiler',
    ...target,
  ]);
  final serviceUri = Completer<Uri>();
  final stdoutDone = process.stdout
      .transform(utf8.decoder)
      .transform(const LineSplitter())
      .listen((line) {
    final match =
        RegExp(r'listening on (http[^\s]+)').firstMatch(line);
    if (match != null && !serviceUri.isCompleted) {
      serviceUri.complete(Uri.parse(match.group(1)!));
    } else if (!line.contains('Dart DevTools') &&
        !line.startsWith('The Dart VM service')) {
      stdout.writeln(line);
    }
  }).asFuture<void>();
  // Forward without pipe(): binding our stderr sink would make later
  // writeln calls throw.
  process.stderr
      .transform(utf8.decoder)
      .listen(stderr.write);

  final httpUri = await serviceUri.future
      .timeout(const Duration(seconds: 30), onTimeout: () {
    process.kill();
    stderr.writeln('cgr-trace-dart: VM service never came up');
    exit(1);
  });
  final wsUri = 'ws://${httpUri.authority}${httpUri.path}ws';
  final service = await vmServiceConnectUri(wsUri);

  await service.streamListen(EventStreams.kDebug);
  final edges = <String, (Frame, Frame, int)>{};
  final lineCache = <String, List<List<int>>?>{};
  final sampledIsolates = <String>{};

  // Each isolate is sampled at its own pause-at-exit and then resumed, so
  // programs whose workers must finish for main to proceed (Isolate.run)
  // never deadlock, and every isolate's samples are captured regardless of
  // when it was spawned.
  Future<void> sampleAndResume(String isolateId) async {
    if (!sampledIsolates.add(isolateId)) {
      return;
    }
    try {
      await sampleIsolate(service, isolateId, repo, edges, lineCache);
    } on RPCError {
      // The isolate died before it could be sampled.
    } on SentinelException {
      // Likewise.
    } finally {
      // Always resume, even on an unexpected sampling error, so the paused
      // isolate never leaves the target hung.
      try {
        await service.resume(isolateId);
      } on RPCError {
        // Already gone.
      } on SentinelException {
        // Likewise.
      }
    }
  }

  service.onDebugEvent.listen((event) {
    if (event.kind == EventKind.kPauseExit) {
      final isolateId = event.isolate?.id;
      if (isolateId != null) {
        unawaited(sampleAndResume(isolateId));
      }
    }
  });

  final vm = await service.getVM();
  for (final isolate in vm.isolates ?? <IsolateRef>[]) {
    final isolateId = isolate.id;
    if (isolateId == null) {
      continue;
    }
    try {
      final state = await service.getIsolate(isolateId);
      if (state.pauseEvent?.kind == EventKind.kPauseExit) {
        await sampleAndResume(isolateId);
      }
    } on RPCError {
      // Raced with isolate teardown.
    } on SentinelException {
      // Likewise.
    }
  }

  final targetExit = await process.exitCode
      .timeout(const Duration(minutes: 10), onTimeout: () {
    stderr.writeln('cgr-trace-dart: target never exited; killing it');
    process.kill();
    return 1;
  });

  final sink = File(output).openWrite();
  sink.writeln(jsonLine({
    'kind': 'header',
    'version': 1,
    'language': 'dart',
    'repo_root': repo,
    'tracer': 'cgr-trace-dart',
  }));
  final workloads = workload == null ? <String>[] : [workload];
  for (final (caller, callee, count) in edges.values) {
    sink.writeln(jsonLine({
      'kind': 'call',
      'caller': caller.toJson(),
      'callee': callee.toJson(),
      'count': count,
      'workloads': workloads,
      'receiver_types': <String>[],
    }));
  }
  await sink.close();
  stderr.writeln('cgr-trace-dart: wrote ${edges.length} call records to $output');

  await service.dispose();
  exitCode = targetExit;
  await stdoutDone;
}

/// Pulls one isolate's CPU samples and accumulates project edges.
Future<void> sampleIsolate(
  VmService service,
  String isolateId,
  String repo,
  Map<String, (Frame, Frame, int)> edges,
  Map<String, List<List<int>>?> lineCache,
) async {
  final samples = await service.getCpuSamples(isolateId, 0, 1 << 62);
  final functions = samples.functions ?? [];
  final frames = <int, Frame?>{};

  Future<Frame?> frameOf(int functionIndex) async {
    if (frames.containsKey(functionIndex)) {
      return frames[functionIndex];
    }
    Frame? built;
    final profileFunction = functions[functionIndex];
    final ref = profileFunction.function;
    if (ref is FuncRef) {
      final location = ref.location;
      final scriptUri = location?.script?.uri;
      if (location != null &&
          scriptUri != null &&
          scriptUri.startsWith('file://')) {
        final path = Uri.parse(scriptUri).toFilePath();
        final inRepo = path == repo || path.startsWith('$repo/');
        if (inRepo) {
          final line = await lineFor(service, isolateId, location, lineCache);
          var name = ref.name ?? '';
          // Extension methods compile to `Ext|method`; setters carry a
          // trailing `=`; closures and other synthetics resolve by span.
          final pipe = name.lastIndexOf('|');
          if (pipe >= 0) {
            name = name.substring(pipe + 1);
          }
          if (name.endsWith('=')) {
            name = name.substring(0, name.length - 1);
          }
          if (name.isEmpty || name.contains('<')) {
            name = '<anonymous>';
          }
          built = Frame(path, name, line);
        }
      }
    }
    frames[functionIndex] = built;
    return built;
  }

  for (final sample in samples.samples ?? <CpuSample>[]) {
    final stack = sample.stack;
    if (stack == null) {
      continue;
    }
    Frame? ancestor;
    // Stacks arrive leaf-first; walk root-first.
    for (final functionIndex in stack.reversed) {
      if (functionIndex < 0 || functionIndex >= functions.length) {
        continue;
      }
      final frame = await frameOf(functionIndex);
      if (frame == null) {
        continue;
      }
      if (ancestor != null) {
        final key = '${ancestor.key}\u0001${frame.key}';
        final existing = edges[key];
        edges[key] = existing == null
            ? (ancestor, frame, 1)
            : (existing.$1, existing.$2, existing.$3 + 1);
      }
      ancestor = frame;
    }
  }
}

/// 1-based line for a source location, via the script's token table.
Future<int> lineFor(
  VmService service,
  String isolateId,
  SourceLocation location,
  Map<String, List<List<int>>?> cache,
) async {
  final scriptId = location.script?.id;
  final tokenPos = location.tokenPos;
  if (scriptId == null || tokenPos == null) {
    return 0;
  }
  var table = cache[scriptId];
  if (!cache.containsKey(scriptId)) {
    final script = await service.getObject(isolateId, scriptId) as Script;
    table = script.tokenPosTable
        ?.map((row) => row.map((cell) => cell).toList())
        .toList();
    cache[scriptId] = table;
  }
  if (table == null) {
    return 0;
  }
  for (final row in table) {
    final line = row.first;
    for (var cell = 1; cell + 1 < row.length; cell += 2) {
      if (row[cell] == tokenPos) {
        return line;
      }
    }
  }
  return 0;
}
