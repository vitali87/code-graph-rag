//> using scala 2.13.14
//> using dep org.scalameta::scalameta:4.9.9

// Independent Scala call oracle for cgr's retrieval eval. Parses each .scala
// source with scalameta (NOT tree-sitter, which cgr uses) and emits one JSON
// payload: `nodes` (every def declaration, kind Function/Method + name) and
// `calls` (each call site as relative-file + callee simple name). The retrieval
// driver restricts calls to first-party names (a callee whose simple name is a
// declared def) and grades cgr's Scala CALLS against these sites.
import scala.meta._
import java.nio.file.{Files, Path, Paths, FileVisitResult, SimpleFileVisitor}
import java.nio.file.attribute.BasicFileAttributes
import scala.collection.mutable.ArrayBuffer

object Oracle {
  val IGNORED: Set[String] =
    Set(".git", ".venv", "venv", "node_modules", "target", "__pycache__", ".mypy_cache")

  val nodes = ArrayBuffer.empty[String]
  val calls = ArrayBuffer.empty[String]
  val covered = ArrayBuffer.empty[String]

  def esc(s: String): String =
    s.flatMap {
      case '"'  => "\\\""
      case '\\' => "\\\\"
      case '\n' => "\\n"
      case '\r' => "\\r"
      case '\t' => "\\t"
      case c    => c.toString
    }

  def emitNode(kind: String, file: String, line: Int, endLine: Int, name: String): Unit =
    nodes += s"""{"kind":"${esc(kind)}","file":"${esc(file)}","line":$line,"end_line":$endLine,"name":"${esc(name)}"}"""

  def emitCall(file: String, name: String): Unit =
    calls += s"""{"file":"${esc(file)}","name":"${esc(name)}"}"""

  // The trailing simple name of a callee expression: `foo`, `obj.foo`,
  // `pkg.Obj.foo`, `foo[T]`, curried `foo(a)(b)` all reduce to `foo`.
  def trailingName(fun: Term): Option[String] = fun match {
    case Term.Name(v)            => Some(v)
    case Term.Select(_, name)    => Some(name.value)
    case Term.ApplyType(f, _)    => trailingName(f)
    case Term.Apply(f, _)        => trailingName(f)
    case _                       => None
  }

  // Nearest enclosing template (class/trait/object body) => Method, else Function.
  def process(file: String, tree: Tree): Unit = {
    def walk(t: Tree, inTemplate: Boolean): Unit = {
      t match {
        // Skip package refs and imports: their Term.Select nodes are not calls.
        case Pkg(_, stats) =>
          stats.foreach(walk(_, inTemplate)); return
        case _: Import => return
        case _ =>
      }

      t match {
        case d: Defn.Def =>
          val kind = if (inTemplate) "Method" else "Function"
          emitNode(kind, file, d.pos.startLine + 1, d.pos.endLine + 1, d.name.value)
        case d: Decl.Def =>
          val kind = if (inTemplate) "Method" else "Function"
          emitNode(kind, file, d.pos.startLine + 1, d.pos.endLine + 1, d.name.value)
        case Term.Apply(fun, _)         => trailingName(fun).foreach(emitCall(file, _))
        case Term.ApplyInfix(_, op, _, _) => emitCall(file, op.value)
        case Term.ApplyType(fun, _)     => trailingName(fun).foreach(emitCall(file, _))
        // A bare `x.name` with no application (a standalone Term.Select) is NOT
        // emitted as a call: Scala's uniform access makes a nullary method call and
        // a plain field read syntactically identical, so counting it would treat a
        // same-named `val` read as a first-party call. The eval therefore grades
        // application/infix call sites only, matching cgr's Scala call extraction,
        // which for the same reason does not resolve bare `field_expression` nodes.
        case _ =>
      }

      val childTemplate = t.isInstanceOf[Template] || inTemplate
      t.children.foreach(walk(_, childTemplate))
    }
    walk(tree, inTemplate = false)
  }

  def main(args: Array[String]): Unit = {
    val root = Paths.get(args(0)).toAbsolutePath.normalize
    val files = ArrayBuffer.empty[Path]
    Files.walkFileTree(root, new SimpleFileVisitor[Path] {
      override def preVisitDirectory(d: Path, a: BasicFileAttributes): FileVisitResult = {
        val name = d.getFileName
        if (name != null && IGNORED.contains(name.toString)) FileVisitResult.SKIP_SUBTREE
        else FileVisitResult.CONTINUE
      }
      override def visitFile(f: Path, a: BasicFileAttributes): FileVisitResult = {
        val s = f.toString
        if (s.endsWith(".scala") || s.endsWith(".sc")) files += f
        FileVisitResult.CONTINUE
      }
    })

    files.foreach { f =>
      val rel = root.relativize(f).toString.replace('\\', '/')
      val text = new String(Files.readAllBytes(f), "UTF-8")
      Input.VirtualFile(rel, text).parse[Source] match {
        case Parsed.Success(tree) =>
          covered += s""""${esc(rel)}""""
          process(rel, tree)
        case Parsed.Error(_, _, _) => // skip unparseable file (oracle grades only clean parses)
      }
    }

    print(s"""{"nodes":[${nodes.mkString(",")}],"edges":[],"name_edges":[],"calls":[${calls.mkString(",")}],"covered":[${covered.mkString(",")}]}""")
  }
}
