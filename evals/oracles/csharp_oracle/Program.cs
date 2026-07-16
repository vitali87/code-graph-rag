// Authoritative C# structure oracle for the cgr eval harness.
//
// Parses a directory of C# sources with Roslyn's own syntax parser
// (Microsoft.CodeAnalysis.CSharp, the C# analog of Go's go/ast) and emits a
// JSON payload {nodes, edges, name_edges, calls}. Node "kind" fields use cgr's
// NodeLabel vocabulary and edges use cgr's RelationshipType vocabulary, so both
// join cgr's graph on (kind, file, line).
//
// Mapping (C# declaration -> cgr NodeLabel), matching determine_node_type and
// the C# LanguageSpec class/function queries:
//
//   class / struct / record        -> Class
//   interface                      -> Interface
//   enum                           -> Enum
//   method / constructor /         -> Method
//     destructor / operator /
//     conversion operator / property
//   local function                 -> Function
//
// Containment (matching cgr's observed C# model):
//
//   DEFINES        : Module(file, line 0) -> every type (nested types included)
//   DEFINES_METHOD : enclosing type       -> member function
//   DEFINES        : enclosing member     -> local function
//
// Inheritance name-edges (graded by the base's simple name on the Python side):
//
//   INHERITS   : type -> base class simple name
//   IMPLEMENTS : type -> base interface simple name
//
// Lines are 1-based and use the declaration node's own span (attributes and
// modifiers included), matching cgr's start_point. Run: dotnet run -- <dir>

using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

const string KindClass = "Class";
const string KindInterface = "Interface";
const string KindEnum = "Enum";
const string KindMethod = "Method";
const string KindFunction = "Function";
const string KindModule = "Module";
const string RelDefines = "DEFINES";
const string RelDefinesMethod = "DEFINES_METHOD";
const string RelInherits = "INHERITS";
const string RelImplements = "IMPLEMENTS";
const int ModuleLine = 0;
const string OperatorPrefix = "operator_";
const string DestructorPrefix = "~";

// cgr hands its full IGNORE_PATTERNS set via CGR_IGNORE_DIRS so the file walk (and
// the declared-type universe below) skips exactly what cgr skips; the hardcoded
// set is only the standalone-run fallback. Case-insensitive so a Bin/ or OBJ/ on a
// case-insensitive file system is still skipped.
var ignoredDirs = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
{
    ".git", "node_modules", "bin", "obj", "vendor", "testdata",
};
var ignoreEnv = Environment.GetEnvironmentVariable("CGR_IGNORE_DIRS");
if (!string.IsNullOrEmpty(ignoreEnv))
{
    ignoredDirs = new HashSet<string>(
        ignoreEnv.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries),
        StringComparer.OrdinalIgnoreCase);
}

var root = args.Length > 0 ? args[0] : ".";
var rootFull = Path.GetFullPath(root);

var files = new List<(string Rel, SyntaxNode Root)>();
foreach (var path in EnumerateCsFiles(rootFull, ignoredDirs))
{
    string text;
    try { text = File.ReadAllText(path); }
    catch { continue; }
    var tree = CSharpSyntaxTree.ParseText(NeutralizeConditionalDirectives(text), path: path);
    var rel = Path.GetRelativePath(rootFull, path).Replace(Path.DirectorySeparatorChar, '/');
    files.Add((rel, tree.GetRoot()));
}

// First pass: the declared type-name universe, so a base type can be classed as
// a base class (INHERITS) or interface (IMPLEMENTS) by what it is declared as.
var declaredInterfaces = new HashSet<string>(StringComparer.Ordinal);
var declaredClasses = new HashSet<string>(StringComparer.Ordinal);
foreach (var (_, fileRoot) in files)
{
    foreach (var node in fileRoot.DescendantNodes())
    {
        switch (node)
        {
            case InterfaceDeclarationSyntax iface:
                declaredInterfaces.Add(iface.Identifier.Text);
                break;
            case ClassDeclarationSyntax cls:
                declaredClasses.Add(cls.Identifier.Text);
                break;
            case StructDeclarationSyntax st:
                declaredClasses.Add(st.Identifier.Text);
                break;
            case RecordDeclarationSyntax rec:
                declaredClasses.Add(rec.Identifier.Text);
                break;
        }
    }
}

var nodes = new List<Def>();
var edges = new List<Edge>();
var nameEdges = new List<NameEdge>();
var calls = new List<Call>();

foreach (var (rel, fileRoot) in files)
{
    var module = new NodeRef(KindModule, rel, ModuleLine);
    foreach (var node in fileRoot.DescendantNodes())
    {
        switch (node)
        {
            case BaseTypeDeclarationSyntax typeDecl:
                EmitType(rel, module, typeDecl);
                break;
            case MethodDeclarationSyntax:
            case ConstructorDeclarationSyntax:
            case DestructorDeclarationSyntax:
            case OperatorDeclarationSyntax:
            case ConversionOperatorDeclarationSyntax:
            case PropertyDeclarationSyntax:
                EmitMember(rel, (MemberDeclarationSyntax)node);
                break;
            case LocalFunctionStatementSyntax local:
                EmitLocalFunction(rel, local);
                break;
            case InvocationExpressionSyntax invocation:
                AddCall(rel, InvocationName(invocation));
                break;
            case ObjectCreationExpressionSyntax creation:
                AddCall(rel, TypeSimpleName(creation.Type));
                break;
            case ImplicitObjectCreationExpressionSyntax implicitCreation:
                AddCall(rel, TargetTypedNewName(implicitCreation));
                break;
        }
    }
}

var payload = new Payload(nodes, edges, nameEdges, calls);
var options = new JsonSerializerOptions { DefaultIgnoreCondition = JsonIgnoreCondition.Never };
Console.WriteLine(JsonSerializer.Serialize(payload, options));
return;

void EmitType(string rel, NodeRef module, BaseTypeDeclarationSyntax typeDecl)
{
    var kind = TypeKind(typeDecl);
    var (line, endLine) = Span(typeDecl);
    nodes.Add(new Def(kind, rel, line, endLine, typeDecl.Identifier.Text));
    // Every type parents to the file module, matching cgr (nested types too).
    edges.Add(new Edge(RelDefines, module, new NodeRef(kind, rel, line)));
    EmitBases(rel, kind, line, typeDecl);
}

void EmitBases(string rel, string kind, int line, BaseTypeDeclarationSyntax typeDecl)
{
    if (typeDecl.BaseList is null)
    {
        return;
    }
    var isInterface = typeDecl is InterfaceDeclarationSyntax;
    var source = new NodeRef(kind, rel, line);
    var index = 0;
    foreach (var baseType in typeDecl.BaseList.Types)
    {
        var name = TypeSimpleName(baseType.Type);
        if (!string.IsNullOrEmpty(name))
        {
            nameEdges.Add(new NameEdge(BaseRel(name, index, isInterface), source, name));
        }
        index++;
    }
}

// An interface's bases are all inheritance (interface extends interface),
// matching cgr's model and the Java oracle. Otherwise C# permits at most one
// base class and it must be first; every later base is an interface. For the
// first base, prefer what the sources declare it as, then fall back to the
// I-prefix convention cgr uses for external bases.
string BaseRel(string name, int index, bool isInterface)
{
    if (isInterface)
    {
        return RelInherits;
    }
    if (index > 0)
    {
        return RelImplements;
    }
    if (declaredInterfaces.Contains(name))
    {
        return RelImplements;
    }
    if (declaredClasses.Contains(name))
    {
        return RelInherits;
    }
    return name.Length >= 2 && name[0] == 'I' && char.IsUpper(name[1])
        ? RelImplements
        : RelInherits;
}

void EmitMember(string rel, MemberDeclarationSyntax member)
{
    // A directive-split expression body is ill-formed once the directives are
    // neutralized (two bodies); Roslyn error-recovers the second branch's
    // expression as a phantom member declaration (issue #768). A declaration
    // carrying parse ERRORS is a recovery artifact, never a source fact cgr
    // should be graded against; real repos compile, so genuine members are
    // error-free. Warning-severity diagnostics (a `#warning` in the body) are
    // legal in compiling code and must not suppress the member.
    if (member.ContainsDiagnostics
        && member.GetDiagnostics().Any(d => d.Severity == DiagnosticSeverity.Error))
    {
        return;
    }
    var (line, endLine) = Span(member);
    nodes.Add(new Def(KindMethod, rel, line, endLine, MemberName(member)));
    var owner = EnclosingType(member);
    if (owner is null)
    {
        return;
    }
    var ownerRef = new NodeRef(TypeKind(owner), rel, Span(owner).Line);
    edges.Add(new Edge(RelDefinesMethod, ownerRef, new NodeRef(KindMethod, rel, line)));
}

void EmitLocalFunction(string rel, LocalFunctionStatementSyntax local)
{
    var (line, endLine) = Span(local);
    nodes.Add(new Def(KindFunction, rel, line, endLine, local.Identifier.Text));
    var parent = EnclosingCallable(local);
    // A top-level script function (a Cake build script's helpers) has no
    // enclosing callable; cgr anchors it to the file module, so mirror that
    // instead of dropping the containment edge.
    var parentRef = parent is null
        ? new NodeRef(KindModule, rel, ModuleLine)
        : new NodeRef(
            parent is LocalFunctionStatementSyntax ? KindFunction : KindMethod,
            rel,
            Span(parent).Line);
    edges.Add(new Edge(RelDefines, parentRef, new NodeRef(KindFunction, rel, line)));
}

void AddCall(string rel, string? name)
{
    if (!string.IsNullOrEmpty(name))
    {
        calls.Add(new Call(rel, name!));
    }
}

static string TypeKind(BaseTypeDeclarationSyntax typeDecl) => typeDecl switch
{
    InterfaceDeclarationSyntax => KindInterface,
    EnumDeclarationSyntax => KindEnum,
    _ => KindClass,
};

static string MemberName(MemberDeclarationSyntax member) => member switch
{
    MethodDeclarationSyntax m => m.Identifier.Text,
    ConstructorDeclarationSyntax c => c.Identifier.Text,
    DestructorDeclarationSyntax d => DestructorPrefix + d.Identifier.Text,
    OperatorDeclarationSyntax o => OperatorPrefix + o.OperatorToken.Text,
    ConversionOperatorDeclarationSyntax cv => OperatorPrefix + cv.Type.ToString(),
    PropertyDeclarationSyntax p => p.Identifier.Text,
    _ => "",
};

static BaseTypeDeclarationSyntax? EnclosingType(SyntaxNode node)
{
    for (var current = node.Parent; current is not null; current = current.Parent)
    {
        if (current is BaseTypeDeclarationSyntax typeDecl)
        {
            return typeDecl;
        }
    }
    return null;
}

static SyntaxNode? EnclosingCallable(SyntaxNode node)
{
    for (var current = node.Parent; current is not null; current = current.Parent)
    {
        switch (current)
        {
            case LocalFunctionStatementSyntax:
            case MethodDeclarationSyntax:
            case ConstructorDeclarationSyntax:
            case DestructorDeclarationSyntax:
            case OperatorDeclarationSyntax:
            case ConversionOperatorDeclarationSyntax:
            case PropertyDeclarationSyntax:
                return current;
        }
    }
    return null;
}

static (int Line, int End) Span(SyntaxNode node)
{
    var span = node.GetLocation().GetLineSpan();
    return (span.StartLinePosition.Line + 1, span.EndLinePosition.Line + 1);
}

static string? InvocationName(InvocationExpressionSyntax invocation) =>
    ExpressionName(invocation.Expression);

static string? ExpressionName(ExpressionSyntax expression) => expression switch
{
    IdentifierNameSyntax id => id.Identifier.Text,
    GenericNameSyntax g => g.Identifier.Text,
    MemberAccessExpressionSyntax member => member.Name.Identifier.Text,
    MemberBindingExpressionSyntax binding => binding.Name.Identifier.Text,
    _ => null,
};

// C# 9 target-typed `new()` (issue #773): the constructed type is named by the
// enclosing declaration. Mirrors cgr's syntactic walk exactly (local/field
// initializer, property initializer, return position, expression body) so the
// eval stays symmetric; any other position (an argument, an operand) needs
// overload resolution and is left unresolved on both sides.
static string? TargetTypedNewName(ImplicitObjectCreationExpressionSyntax creation)
{
    SyntaxNode? node = creation.Parent;
    while (node is EqualsValueClauseSyntax
        or VariableDeclaratorSyntax
        or ParenthesizedExpressionSyntax)
    {
        node = node.Parent;
    }
    switch (node)
    {
        case VariableDeclarationSyntax decl when !decl.Type.IsVar:
            return TypeSimpleName(decl.Type);
        case PropertyDeclarationSyntax prop:
            return TypeSimpleName(prop.Type);
        case ReturnStatementSyntax or ArrowExpressionClauseSyntax:
            return EnclosingReturnTypeName(node);
        default:
            return null;
    }
}

// A return position is typed by the nearest enclosing callable: methods and
// local functions name it directly, a property or indexer (or their accessor
// bodies) via its type. A lambda/anonymous method has no syntactic return
// type: unresolvable.
static string? EnclosingReturnTypeName(SyntaxNode node)
{
    for (var ancestor = node.Parent; ancestor is not null; ancestor = ancestor.Parent)
    {
        switch (ancestor)
        {
            case MethodDeclarationSyntax method:
                return TypeSimpleName(method.ReturnType);
            case LocalFunctionStatementSyntax local:
                return TypeSimpleName(local.ReturnType);
            case PropertyDeclarationSyntax prop:
                return TypeSimpleName(prop.Type);
            case IndexerDeclarationSyntax indexer:
                return TypeSimpleName(indexer.Type);
            case AnonymousFunctionExpressionSyntax:
                return null;
        }
    }
    return null;
}

// The simple name of a (possibly qualified, generic, nullable, array) type: the
// rightmost identifier with type arguments stripped (N2.IList<T> -> "IList").
static string TypeSimpleName(TypeSyntax type)
{
    switch (type)
    {
        case IdentifierNameSyntax id:
            return id.Identifier.Text;
        case GenericNameSyntax g:
            return g.Identifier.Text;
        case QualifiedNameSyntax q:
            return TypeSimpleName(q.Right);
        case AliasQualifiedNameSyntax a:
            return TypeSimpleName(a.Name);
        case NullableTypeSyntax n:
            return TypeSimpleName(n.ElementType);
        case ArrayTypeSyntax arr:
            return TypeSimpleName(arr.ElementType);
        default:
            return "";
    }
}

// cgr has no preprocessor: tree-sitter parses every #if/#elif/#else branch, so a
// declaration guarded by an undefined symbol IS in cgr's graph. Roslyn instead
// treats an undefined #if branch as disabled trivia and never yields its
// declarations, which would make every such real declaration read as a cgr false
// positive. Blank the conditional directive LINES (keeping the line so 1-based line
// numbers are stable for the join) so every branch parses as active code, matching
// cgr's structural view. Non-conditional directives (#region, #pragma, #nullable,
// #define/#undef) don't gate code and are left for Roslyn to handle.
static string NeutralizeConditionalDirectives(string text)
{
    if (text.IndexOf("#if", StringComparison.Ordinal) < 0)
    {
        return text;
    }
    var lines = text.Split('\n');
    for (var i = 0; i < lines.Length; i++)
    {
        var trimmed = lines[i].TrimStart();
        if (trimmed.StartsWith("#if", StringComparison.Ordinal)
            || trimmed.StartsWith("#elif", StringComparison.Ordinal)
            || trimmed.StartsWith("#else", StringComparison.Ordinal)
            || trimmed.StartsWith("#endif", StringComparison.Ordinal))
        {
            // ponytail: blanks a line that merely looks like a directive (e.g. `#if`
            // at column 0 inside a multi-line verbatim string); vanishingly rare and
            // cgr's tree-sitter mishandles the same case, so no eval bias.
            lines[i] = "";
        }
    }
    return string.Join('\n', lines);
}

static IEnumerable<string> EnumerateCsFiles(string root, HashSet<string> ignoredDirs)
{
    var stack = new Stack<string>();
    stack.Push(root);
    while (stack.Count > 0)
    {
        var dir = stack.Pop();
        string[] subDirs;
        try { subDirs = Directory.GetDirectories(dir); }
        catch { continue; }
        foreach (var sub in subDirs)
        {
            if (!ignoredDirs.Contains(Path.GetFileName(sub)))
            {
                stack.Push(sub);
            }
        }
        string[] entries;
        try { entries = Directory.GetFiles(dir, "*.cs"); }
        catch { continue; }
        foreach (var file in entries)
        {
            yield return file;
        }
    }
}

record NodeRef(
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("file")] string File,
    [property: JsonPropertyName("line")] int Line);

record Def(
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("file")] string File,
    [property: JsonPropertyName("line")] int Line,
    [property: JsonPropertyName("end_line")] int EndLine,
    [property: JsonPropertyName("name")] string Name);

record Edge(
    [property: JsonPropertyName("rel")] string Rel,
    [property: JsonPropertyName("parent")] NodeRef Parent,
    [property: JsonPropertyName("child")] NodeRef Child);

record NameEdge(
    [property: JsonPropertyName("rel")] string Rel,
    [property: JsonPropertyName("source")] NodeRef Source,
    [property: JsonPropertyName("target_name")] string TargetName);

record Call(
    [property: JsonPropertyName("file")] string File,
    [property: JsonPropertyName("name")] string Name);

record Payload(
    [property: JsonPropertyName("nodes")] List<Def> Nodes,
    [property: JsonPropertyName("edges")] List<Edge> Edges,
    [property: JsonPropertyName("name_edges")] List<NameEdge> NameEdges,
    [property: JsonPropertyName("calls")] List<Call> Calls);
