// Java semantic fact provider (issue #1181), bundled and built once like the
// Roslyn and gotypes tools. Java's resolution is the largest pure-heuristic
// stack in the repo -- matching by name and arity where the language demands
// overload resolution BY ARGUMENT TYPE -- and the JDK ships the authoritative
// answer in the Compiler Tree API.
//
// Stage 1 attributes WITHOUT the project classpath: intra-repo binding is
// still exact, and every symbol that does not resolve to repo source becomes
// an external proof. Diagnostics from missing dependencies are expected and
// discarded. Emits one JSON line: {"calls": [...], "externals": [...]},
// each keyed on the callee NAME token as (file, line, byte col, name) --
// the same join contract the C# and Go frontends use.
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.ExpressionTree;
import com.sun.source.tree.IdentifierTree;
import com.sun.source.tree.MemberSelectTree;
import com.sun.source.tree.MethodInvocationTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.Tree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePath;
import com.sun.source.util.TreePathScanner;
import com.sun.source.util.Trees;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import javax.lang.model.element.Element;
import javax.lang.model.element.ExecutableElement;
import javax.tools.DiagnosticCollector;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public final class Frontend {

    private Frontend() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.out.println("{\"calls\":[],\"externals\":[]}");
            return;
        }
        Path root = Paths.get(args[0]).toRealPath();
        Set<String> ignored = ignoredDirs();
        List<Path> sources = javaSources(root, ignored);
        if (sources.isEmpty()) {
            System.out.println("{\"calls\":[],\"externals\":[]}");
            return;
        }
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            System.out.println("{\"calls\":[],\"externals\":[]}");
            return;
        }
        DiagnosticCollector<JavaFileObject> diagnostics = new DiagnosticCollector<>();
        try (StandardJavaFileManager fileManager =
                compiler.getStandardFileManager(diagnostics, null, StandardCharsets.UTF_8)) {
            Iterable<? extends JavaFileObject> units =
                    fileManager.getJavaFileObjectsFromPaths(sources);
            // -proc:none: never run repository-controlled annotation processors.
            JavacTask task = (JavacTask) compiler.getTask(
                    null, fileManager, diagnostics, Arrays.asList("-proc:none"), null, units);
            Iterable<? extends CompilationUnitTree> parsed = task.parse();
            task.analyze();
            Collector collector = new Collector(root, Trees.instance(task));
            for (CompilationUnitTree unit : parsed) {
                collector.collect(unit);
            }
            System.out.println(collector.toJson());
        }
    }

    private static Set<String> ignoredDirs() {
        String raw = System.getenv("CGR_IGNORE_DIRS");
        if (raw == null || raw.isEmpty()) {
            return new HashSet<>();
        }
        return new HashSet<>(Arrays.asList(raw.split(",")));
    }

    private static List<Path> javaSources(Path root, Set<String> ignored) throws IOException {
        try (var stream = Files.walk(root)) {
            return stream
                    .filter(Files::isRegularFile)
                    .filter(p -> p.toString().endsWith(".java"))
                    .filter(p -> {
                        Path rel = root.relativize(p);
                        for (Path part : rel) {
                            if (ignored.contains(part.toString())) {
                                return false;
                            }
                        }
                        return true;
                    })
                    .collect(Collectors.toList());
        }
    }

    private static final class Collector extends TreePathScanner<Void, Void> {
        private final Path root;
        private final Trees trees;
        private final SourcePositions positions;
        private final List<String> calls = new ArrayList<>();
        private final Set<String> externals = new LinkedHashSet<>();
        private final Set<String> seenCalls = new HashSet<>();
        private final Map<String, String> sourceCache = new HashMap<>();
        private final Map<String, int[]> lineStartCache = new HashMap<>();
        private CompilationUnitTree unit;
        private String rel;

        Collector(Path root, Trees trees) {
            this.root = root;
            this.trees = trees;
            this.positions = trees.getSourcePositions();
        }

        void collect(CompilationUnitTree tree) {
            String relative = relativize(tree);
            if (relative == null) {
                return;
            }
            this.unit = tree;
            this.rel = relative;
            scan(tree, null);
        }

        private String relativize(CompilationUnitTree tree) {
            try {
                Path path = Paths.get(tree.getSourceFile().toUri()).toRealPath();
                if (!path.startsWith(root)) {
                    return null;
                }
                return root.relativize(path).toString().replace('\\', '/');
            } catch (IOException | IllegalArgumentException e) {
                return null;
            }
        }

        @Override
        public Void visitMethodInvocation(MethodInvocationTree node, Void ignored) {
            record(node);
            return super.visitMethodInvocation(node, ignored);
        }

        private void record(MethodInvocationTree node) {
            ExpressionTree select = node.getMethodSelect();
            String name = calleeName(select);
            if (name == null || name.equals("super") || name.equals("this")) {
                // Implicit constructor delegation has no name token in the
                // source, so the tree-sitter side has nothing to join to.
                return;
            }
            Element element = trees.getElement(new TreePath(getCurrentPath(), select));
            if (!(element instanceof ExecutableElement)) {
                // Unattributed (a missing dependency): the heuristics stay in
                // charge -- an unresolved site is never an external proof.
                return;
            }
            long start = nameStart(select, name);
            if (start < 0) {
                return;
            }
            Position site = position(unit, rel, start);
            if (site == null) {
                return;
            }
            TreePath declaration = trees.getPath(element);
            if (declaration == null) {
                externals.add(externalJson(site, name));
                return;
            }
            String declRel = relativize(declaration.getCompilationUnit());
            if (declRel == null) {
                externals.add(externalJson(site, name));
                return;
            }
            Position target = declarationPosition(declaration, name, declRel);
            if (target == null) {
                return;
            }
            String key = site.file + ":" + site.line + ":" + site.col + ":" + name;
            if (!seenCalls.add(key)) {
                return;
            }
            calls.add(callJson(site, name, target));
        }

        private Position declarationPosition(TreePath declaration, String name, String declRel) {
            Tree tree = declaration.getLeaf();
            if (!(tree instanceof MethodTree)) {
                return null;
            }
            CompilationUnitTree declUnit = declaration.getCompilationUnit();
            long start = positions.getStartPosition(declUnit, tree);
            long end = positions.getEndPosition(declUnit, tree);
            if (start < 0 || end < 0) {
                return null;
            }
            String source = sourceOf(declUnit);
            if (source == null) {
                return null;
            }
            int at = nameTokenOffset(source, name, (int) start, (int) Math.min(end, source.length()));
            if (at < 0) {
                return null;
            }
            return position(declUnit, declRel, at);
        }

        // The declaration's NAME token, matching the key the tree-sitter side
        // produces. Annotations belong to the method header and may carry
        // parenthesised arguments, so the first '(' is not a reliable bound:
        // take the first identifier-bounded occurrence that a '(' follows.
        private static int nameTokenOffset(String source, String name, int start, int end) {
            for (int at = source.indexOf(name, start); at >= 0 && at < end;
                    at = source.indexOf(name, at + 1)) {
                if (at > 0 && Character.isJavaIdentifierPart(source.charAt(at - 1))) {
                    continue;
                }
                int after = at + name.length();
                while (after < end && Character.isWhitespace(source.charAt(after))) {
                    after++;
                }
                if (after < end && source.charAt(after) == '(') {
                    return at;
                }
            }
            return -1;
        }

        private String calleeName(ExpressionTree select) {
            if (select instanceof MemberSelectTree member) {
                return member.getIdentifier().toString();
            }
            if (select instanceof IdentifierTree identifier) {
                return identifier.getName().toString();
            }
            return null;
        }

        private long nameStart(ExpressionTree select, String name) {
            if (select instanceof MemberSelectTree) {
                long end = positions.getEndPosition(unit, select);
                return end < 0 ? -1 : end - name.length();
            }
            return positions.getStartPosition(unit, select);
        }

        private int[] lineStartsOf(CompilationUnitTree tree, String source) {
            String key = tree.getSourceFile().toUri().toString();
            int[] cached = lineStartCache.get(key);
            if (cached != null) {
                return cached;
            }
            List<Integer> starts = new ArrayList<>();
            starts.add(0);
            for (int i = 0; i < source.length(); i++) {
                if (source.charAt(i) == '\n') {
                    starts.add(i + 1);
                }
            }
            int[] offsets = new int[starts.size()];
            for (int i = 0; i < offsets.length; i++) {
                offsets[i] = starts.get(i);
            }
            lineStartCache.put(key, offsets);
            return offsets;
        }

        private String sourceOf(CompilationUnitTree tree) {
            String key = tree.getSourceFile().toUri().toString();
            String cached = sourceCache.get(key);
            if (cached != null) {
                return cached;
            }
            try {
                String text = tree.getSourceFile().getCharContent(true).toString();
                sourceCache.put(key, text);
                return text;
            } catch (IOException | UncheckedIOException e) {
                return null;
            }
        }

        // (1-based line, 0-based BYTE column) of a character offset: the join
        // key the tree-sitter side produces, so UTF-8 width must be measured,
        // never character counts.
        private Position position(CompilationUnitTree tree, String relPath, long offset) {
            String source = sourceOf(tree);
            if (source == null || offset < 0 || offset > source.length()) {
                return null;
            }
            // Binary search over cached line starts: a linear rescan per call
            // site is quadratic in the size of a large source file.
            int[] lineStarts = lineStartsOf(tree, source);
            int found = Arrays.binarySearch(lineStarts, (int) offset);
            int index = found >= 0 ? found : -found - 2;
            int line = index + 1;
            String prefix = source.substring(lineStarts[index], (int) offset);
            int byteCol = prefix.getBytes(StandardCharsets.UTF_8).length;
            return new Position(relPath, line, byteCol);
        }

        private String callJson(Position site, String name, Position target) {
            return "{\"file\":" + quote(site.file)
                    + ",\"line\":" + site.line
                    + ",\"col\":" + site.col
                    + ",\"name\":" + quote(name)
                    + ",\"tfile\":" + quote(target.file)
                    + ",\"tline\":" + target.line
                    + ",\"tcol\":" + target.col + "}";
        }

        private String externalJson(Position site, String name) {
            return "{\"file\":" + quote(site.file)
                    + ",\"line\":" + site.line
                    + ",\"col\":" + site.col
                    + ",\"name\":" + quote(name) + "}";
        }

        String toJson() {
            return "{\"calls\":[" + String.join(",", calls)
                    + "],\"externals\":[" + String.join(",", externals) + "]}";
        }

        private static String quote(String value) {
            StringBuilder out = new StringBuilder("\"");
            for (int i = 0; i < value.length(); i++) {
                char c = value.charAt(i);
                switch (c) {
                    case '"' -> out.append("\\\"");
                    case '\\' -> out.append("\\\\");
                    case '\n' -> out.append("\\n");
                    case '\r' -> out.append("\\r");
                    case '\t' -> out.append("\\t");
                    default -> {
                        if (c < 0x20) {
                            out.append(String.format("\\u%04x", (int) c));
                        } else {
                            out.append(c);
                        }
                    }
                }
            }
            return out.append('"').toString();
        }
    }

    private record Position(String file, int line, int col) {
    }
}
