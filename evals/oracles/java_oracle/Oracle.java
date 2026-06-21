// Authoritative Java structure oracle for the cgr eval harness.
//
// Parses every .java file under a directory with the JDK's own Compiler Tree API
// (javax.tools + com.sun.source) and emits one JSON record per declaration, in
// cgr's NodeLabel vocabulary, so records join cgr's graph on (kind, file, line).
// task.parse() only parses (no resolution), so missing dependencies are fine.
//
// Mapping (Java construct -> cgr NodeLabel):
//
//   class                  -> Class
//   interface / @interface -> Interface  (its method signatures -> Method)
//   enum                   -> Enum
//   method / constructor   -> Method
//
// Compile: javac Oracle.java ; Run: java -cp <dir> Oracle <dir>

import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.LineMap;
import com.sun.source.tree.MethodTree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreeScanner;
import com.sun.source.util.Trees;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public class Oracle {
    static final Set<String> IGNORED =
        new HashSet<>(Arrays.asList(".git", "target", "build", "node_modules", "vendor"));
    static final List<String> recs = new ArrayList<>();

    static String esc(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    static void emit(String kind, String file, long line, String name) {
        recs.add("{\"kind\":\"" + kind + "\",\"file\":\"" + esc(file)
            + "\",\"line\":" + line + ",\"name\":\"" + esc(name) + "\"}");
    }

    public static void main(String[] args) throws Exception {
        Path root = Paths.get(args[0]).toAbsolutePath().normalize();
        List<Path> files = new ArrayList<>();
        Files.walkFileTree(root, new SimpleFileVisitor<Path>() {
            public FileVisitResult preVisitDirectory(Path d, BasicFileAttributes a) {
                Path name = d.getFileName();
                if (name != null && IGNORED.contains(name.toString())) {
                    return FileVisitResult.SKIP_SUBTREE;
                }
                return FileVisitResult.CONTINUE;
            }

            public FileVisitResult visitFile(Path f, BasicFileAttributes a) {
                if (f.toString().endsWith(".java")) {
                    files.add(f);
                }
                return FileVisitResult.CONTINUE;
            }
        });
        if (files.isEmpty()) {
            System.out.print("[]");
            return;
        }

        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        StandardJavaFileManager fm = compiler.getStandardFileManager(null, null, null);
        Iterable<? extends JavaFileObject> units = fm.getJavaFileObjectsFromPaths(files);
        JavacTask task = (JavacTask) compiler.getTask(null, fm, d -> {}, null, null, units);
        SourcePositions sp = Trees.instance(task).getSourcePositions();

        for (CompilationUnitTree unit : task.parse()) {
            Path abs = Paths.get(unit.getSourceFile().toUri());
            String rel = root.relativize(abs).toString().replace('\\', '/');
            LineMap lm = unit.getLineMap();
            new TreeScanner<Void, Void>() {
                public Void visitClass(ClassTree node, Void p) {
                    String kind;
                    switch (node.getKind()) {
                        case INTERFACE:
                            kind = "Interface";
                            break;
                        case ENUM:
                            kind = "Enum";
                            break;
                        // (H) cgr models an annotation type (@interface) as a Class.
                        default:
                            kind = "Class";
                    }
                    long pos = sp.getStartPosition(unit, node);
                    if (pos >= 0 && node.getSimpleName().length() > 0) {
                        emit(kind, rel, lm.getLineNumber(pos), node.getSimpleName().toString());
                    }
                    return super.visitClass(node, p);
                }

                public Void visitMethod(MethodTree node, Void p) {
                    long pos = sp.getStartPosition(unit, node);
                    if (pos >= 0) {
                        emit("Method", rel, lm.getLineNumber(pos), node.getName().toString());
                    }
                    return super.visitMethod(node, p);
                }
            }.scan(unit, null);
        }
        System.out.print("[" + String.join(",", recs) + "]");
    }
}
