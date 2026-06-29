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
// Containment edges (matching how cgr models Java containment):
//
//   DEFINES        : the file module -> every named type (top-level OR nested)
//   DEFINES_METHOD : the method's immediate enclosing named type -> Method
//
// cgr models a Java module per file (keyed at line 0) and DEFINES every named
// type from it (containment is flat, not nested-type-scoped). A method binds to
// its nearest enclosing named type. Methods of an anonymous class are Functions
// (no DEFINES_METHOD), matching the node mapping.
//
// Output is a {nodes, edges} payload joining cgr on (kind, file, line).
//
// Compile: javac Oracle.java ; Run: java -cp <dir> Oracle <dir>

import com.sun.source.tree.ClassTree;
import com.sun.source.tree.CompilationUnitTree;
import com.sun.source.tree.ExpressionTree;
import com.sun.source.tree.IdentifierTree;
import com.sun.source.tree.LambdaExpressionTree;
import com.sun.source.tree.LineMap;
import com.sun.source.tree.MemberSelectTree;
import com.sun.source.tree.MethodInvocationTree;
import com.sun.source.tree.MethodTree;
import com.sun.source.tree.Tree;
import com.sun.source.util.JavacTask;
import com.sun.source.util.SourcePositions;
import com.sun.source.util.TreePath;
import com.sun.source.util.TreePathScanner;
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
    static final List<String> edges = new ArrayList<>();
    static final List<String> nameEdges = new ArrayList<>();
    static final List<String> calls = new ArrayList<>();
    static final long MODULE_LINE = 0;

    static String esc(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    // (H) Simple name of an extends/implements type: drop generics and any
    // (H) package/outer qualifier, matching how cgr resolves bases by simple name.
    static String simpleName(Object typeTree) {
        String s = typeTree.toString();
        int lt = s.indexOf('<');
        if (lt >= 0) {
            s = s.substring(0, lt);
        }
        int dot = s.lastIndexOf('.');
        if (dot >= 0) {
            s = s.substring(dot + 1);
        }
        return s.trim();
    }

    static void emitNameEdge(
            String rel, String file, String skind, long sline, String targetName) {
        nameEdges.add("{\"rel\":\"" + rel + "\",\"source\":{\"kind\":\"" + skind
            + "\",\"file\":\"" + esc(file) + "\",\"line\":" + sline
            + "},\"target_name\":\"" + esc(targetName) + "\"}");
    }

    // (H) A file-level call site: caller file + callee simple name (the method
    // (H) identifier). The Python side keeps only callees whose name is a declared
    // (H) first-party Method/Function, mirroring the Go/Rust call oracles.
    static void emitCall(String file, String name) {
        calls.add("{\"file\":\"" + esc(file) + "\",\"name\":\"" + esc(name) + "\"}");
    }

    static void emit(String kind, String file, long line, long endLine, String name) {
        recs.add("{\"kind\":\"" + kind + "\",\"file\":\"" + esc(file)
            + "\",\"line\":" + line + ",\"end_line\":" + endLine
            + ",\"name\":\"" + esc(name) + "\"}");
    }

    static void emitEdge(
            String rel, String file, String pkind, long pline, String ckind, long cline) {
        edges.add("{\"rel\":\"" + rel + "\",\"parent\":{\"kind\":\"" + pkind
            + "\",\"file\":\"" + esc(file) + "\",\"line\":" + pline
            + "},\"child\":{\"kind\":\"" + ckind + "\",\"file\":\"" + esc(file)
            + "\",\"line\":" + cline + "}}");
    }

    static String classKind(ClassTree node) {
        switch (node.getKind()) {
            case INTERFACE:
                return "Interface";
            case ENUM:
                return "Enum";
            // (H) cgr models an annotation type (@interface) as a Class.
            default:
                return "Class";
        }
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
            System.out.print("{\"nodes\":[],\"edges\":[],\"name_edges\":[]}");
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
            new TreePathScanner<Void, Void>() {
                public Void visitClass(ClassTree node, Void p) {
                    long pos = sp.getStartPosition(unit, node);
                    // (H) Anonymous classes have an empty name and no cgr node.
                    if (pos >= 0 && node.getSimpleName().length() > 0) {
                        long line = lm.getLineNumber(pos);
                        long endLine = lm.getLineNumber(sp.getEndPosition(unit, node));
                        String kind = classKind(node);
                        emit(kind, rel, line, endLine, node.getSimpleName().toString());
                        // (H) Every named type is DEFINEd by the file module,
                        // (H) including nested types (cgr keeps this flat).
                        emitEdge("DEFINES", rel, "Module", MODULE_LINE, kind, line);
                        // (H) extends superclass -> INHERITS (a class only).
                        if (node.getExtendsClause() != null) {
                            emitNameEdge("INHERITS", rel, kind, line,
                                simpleName(node.getExtendsClause()));
                        }
                        // (H) The implements clause holds a class/enum's interfaces
                        // (H) (-> IMPLEMENTS) but an interface's superinterfaces
                        // (H) (-> INHERITS, like cgr).
                        String hrel = node.getKind() == Tree.Kind.INTERFACE
                            ? "INHERITS" : "IMPLEMENTS";
                        for (Tree it : node.getImplementsClause()) {
                            emitNameEdge(hrel, rel, kind, line, simpleName(it));
                        }
                    }
                    return super.visitClass(node, p);
                }

                public Void visitMethod(MethodTree node, Void p) {
                    long pos = sp.getStartPosition(unit, node);
                    if (pos >= 0) {
                        // (H) cgr labels a member a Method only when its nearest
                        // (H) enclosing named class precedes any enclosing method or
                        // (H) lambda body; members of an anonymous class (declared in
                        // (H) a method body) are modelled as standalone Functions.
                        String kind = "Function";
                        ClassTree owner = null;
                        for (TreePath up = getCurrentPath().getParentPath();
                                up != null; up = up.getParentPath()) {
                            Tree t = up.getLeaf();
                            if (t instanceof ClassTree
                                    && ((ClassTree) t).getSimpleName().length() > 0) {
                                kind = "Method";
                                owner = (ClassTree) t;
                                break;
                            }
                            if (t instanceof MethodTree || t instanceof LambdaExpressionTree) {
                                break;
                            }
                        }
                        long line = lm.getLineNumber(pos);
                        long endLine = lm.getLineNumber(sp.getEndPosition(unit, node));
                        emit(kind, rel, line, endLine, node.getName().toString());
                        // (H) A Method binds to its enclosing named type; an
                        // (H) anonymous-class member (Function) has no such edge.
                        if (owner != null) {
                            long opos = sp.getStartPosition(unit, owner);
                            if (opos >= 0) {
                                emitEdge("DEFINES_METHOD", rel, classKind(owner),
                                    lm.getLineNumber(opos), "Method", line);
                            }
                        }
                    }
                    return super.visitMethod(node, p);
                }

                public Void visitMethodInvocation(MethodInvocationTree node, Void p) {
                    // (H) The callee simple name: the trailing identifier of a
                    // (H) member-select (`obj.foo()`, `Type.bar()`) or a bare
                    // (H) identifier (`foo()`, same-class or static-imported). A
                    // (H) `super()`/`this()` constructor call yields "super"/"this"
                    // (H) and is dropped downstream (never a declared method name).
                    ExpressionTree sel = node.getMethodSelect();
                    String name = null;
                    if (sel instanceof MemberSelectTree) {
                        name = ((MemberSelectTree) sel).getIdentifier().toString();
                    } else if (sel instanceof IdentifierTree) {
                        name = ((IdentifierTree) sel).getName().toString();
                    }
                    if (name != null) {
                        emitCall(rel, name);
                    }
                    return super.visitMethodInvocation(node, p);
                }
            }.scan(unit, null);
        }
        System.out.print("{\"nodes\":[" + String.join(",", recs)
            + "],\"edges\":[" + String.join(",", edges)
            + "],\"name_edges\":[" + String.join(",", nameEdges)
            + "],\"calls\":[" + String.join(",", calls) + "]}");
    }
}
