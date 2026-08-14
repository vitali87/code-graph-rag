package cgr.trace;

import java.lang.instrument.Instrumentation;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * Entry point for {@code -javaagent:cgr-jvm-agent.jar=<args>}.
 *
 * <p>Arguments are semicolon-separated {@code key=value} pairs:
 *
 * <ul>
 *   <li>{@code include=com.example,org.acme} (required): package prefixes to
 *       instrument; instrumenting everything including the JDK is not viable
 *   <li>{@code output=cgr-trace.jsonl}: trace file path
 *   <li>{@code repo=/abs/path}: repository root written to the trace header
 *   <li>{@code workload=label}: workload label for the whole run; tests can
 *       refine it per-case via {@link TraceRecorder#setWorkload}
 * </ul>
 */
public final class CgrTraceAgent {

    private CgrTraceAgent() {}

    public static void premain(String agentArgs, Instrumentation instrumentation) {
        List<String> includes = new ArrayList<>();
        Path output = Path.of("cgr-trace.jsonl");
        String repo = "";
        String workload = null;

        for (String pair : (agentArgs == null ? "" : agentArgs).split(";")) {
            int eq = pair.indexOf('=');
            if (eq < 0) {
                continue;
            }
            String key = pair.substring(0, eq).trim();
            String value = pair.substring(eq + 1).trim();
            switch (key) {
                case "include" -> {
                    for (String prefix : value.split(",")) {
                        if (!prefix.isBlank()) {
                            includes.add(prefix.trim());
                        }
                    }
                }
                case "output" -> {
                    try {
                        output = Path.of(value);
                    } catch (java.nio.file.InvalidPathException e) {
                        System.err.println(
                                "cgr-trace-jvm: invalid output path, keeping default: "
                                        + value + " (" + e + ")");
                    }
                }
                case "repo" -> repo = value;
                case "workload" -> workload = value;
                default -> System.err.println("cgr-trace-jvm: unknown agent arg: " + key);
            }
        }
        if (includes.isEmpty()) {
            System.err.println(
                    "cgr-trace-jvm: no include= packages given; agent disabled "
                            + "(example: -javaagent:cgr-jvm-agent.jar=include=com.example)");
            return;
        }
        String[] prefixes = includes.toArray(String[]::new);
        TraceRecorder.configure(prefixes, repo, output, workload);
        instrumentation.addTransformer(new MethodEntryTransformer(prefixes));
    }
}
