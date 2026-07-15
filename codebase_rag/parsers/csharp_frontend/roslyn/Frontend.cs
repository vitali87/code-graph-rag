using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.MSBuild;
using Microsoft.CodeAnalysis.Text;

namespace CsharpFrontend;

public static class Frontend
{
    private const string KindClass = "class";
    private const string KindInterface = "interface";
    private const string KindUnknown = "unknown";

    public static async Task<int> RunAsync(string[] args)
    {
        if (args.Length < 1)
        {
            await Console.Error.WriteLineAsync("usage: Frontend <repo-root> [project-or-solution]");
            return 2;
        }

        var rootFull = Path.GetFullPath(args[0]);
        var projectOrSolution = args.Length > 1 ? Path.GetFullPath(args[1]) : null;

        var debug = Environment.GetEnvironmentVariable("CGR_FE_DEBUG") == "1";
        using var workspace = MSBuildWorkspace.Create();
        // A failed reference or an unloadable project must not abort the whole run;
        // degrade to whatever loaded and let the Python side fall back per-fact.
        workspace.WorkspaceFailed += (_, e) =>
        {
            if (debug)
            {
                Console.Error.WriteLine($"[WorkspaceFailed] {e.Diagnostic.Kind}: {e.Diagnostic.Message}");
            }
        };

        var projects = await LoadProjectsAsync(workspace, projectOrSolution, rootFull);
        if (debug)
        {
            Console.Error.WriteLine($"[projects] {projects.Count}");
        }

        var collector = new FactCollector(rootFull, IgnoredDirs());
        foreach (var project in projects)
        {
            // GetCompilationAsync runs source generators, so symbols that resolve
            // only through generated members still bind; the generated trees
            // themselves have no first-party path and never become facts.
            var compilation = await project.GetCompilationAsync();
            if (compilation is null)
            {
                continue;
            }
            foreach (var tree in compilation.SyntaxTrees)
            {
                var path = tree.FilePath;
                if (string.IsNullOrEmpty(path) || !collector.IsFirstParty(path))
                {
                    continue;
                }
                collector.CollectTree(compilation.GetSemanticModel(tree), tree);
            }
        }

        var options = new JsonSerializerOptions { DefaultIgnoreCondition = JsonIgnoreCondition.Never };
        Console.WriteLine(JsonSerializer.Serialize(collector.ToPayload(), options));
        return 0;
    }

    // Collects the four fact kinds over every first-party tree of every loaded
    // compilation, deduplicating across compilations (a file shared by
    // multi-targeted projects visits once per target framework).
    private sealed class FactCollector
    {
        private readonly string _rootFull;
        private readonly HashSet<string> _ignoredDirs;
        private readonly List<TypeFact> _types = new();
        private readonly List<CallFact> _calls = new();
        private readonly List<QueryFact> _queries = new();
        private readonly Dictionary<string, List<DeclLoc>> _partials = new(StringComparer.Ordinal);
        private readonly HashSet<(string, int)> _seenTypes = new();
        private readonly HashSet<(string, int, int, string)> _seenCalls = new();
        private readonly HashSet<(string, int, int, string, int, string)> _seenQueries = new();

        public FactCollector(string rootFull, HashSet<string> ignoredDirs)
        {
            _rootFull = rootFull;
            _ignoredDirs = ignoredDirs;
        }

        public bool IsFirstParty(string path)
        {
            // GetRelativePath yields a rooted path when the two are on different
            // volumes and a "../"-prefixed path when `path` sits outside the root, so
            // it decides containment without a case-sensitive prefix compare that a
            // drive/folder casing mismatch on Windows would break.
            var rel = Path.GetRelativePath(_rootFull, Path.GetFullPath(path));
            if (Path.IsPathRooted(rel) || rel == ".." || rel.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal))
            {
                return false;
            }
            foreach (var part in rel.Split(Path.DirectorySeparatorChar, '/'))
            {
                if (_ignoredDirs.Contains(part))
                {
                    return false;
                }
            }
            return true;
        }

        public void CollectTree(SemanticModel model, SyntaxTree tree)
        {
            var rel = Rel(tree.FilePath);
            foreach (var node in tree.GetRoot().DescendantNodes())
            {
                switch (node)
                {
                    case TypeDeclarationSyntax typeDecl:
                        CollectType(model, typeDecl, rel);
                        break;
                    case InvocationExpressionSyntax invocation:
                        CollectCall(model, invocation, rel);
                        break;
                    case QueryExpressionSyntax query:
                        CollectQuery(model, query, rel);
                        break;
                    default:
                        break;
                }
            }
        }

        public Payload ToPayload() => new(_types, _calls, _partials.Values.ToList(), _queries);

        private string Rel(string path) =>
            Path.GetRelativePath(_rootFull, path).Replace(Path.DirectorySeparatorChar, '/');

        private void CollectType(SemanticModel model, TypeDeclarationSyntax typeDecl, string rel)
        {
            if (typeDecl.Modifiers.Any(SyntaxKind.PartialKeyword))
            {
                CollectPartial(model, typeDecl);
            }
            if (typeDecl.BaseList is null)
            {
                return;
            }
            var line = StartOf(typeDecl).Line + 1;
            if (!_seenTypes.Add((rel, line)))
            {
                return;
            }
            _types.Add(new TypeFact(rel, line, typeDecl.Identifier.Text, ClassifyBases(model, typeDecl)));
        }

        // One group of first-party declaration locations per partial symbol. Even a
        // single-declaration group is emitted: it tells the Python side Roslyn KNOWS
        // this symbol's identity, so a syntactic merge with an unrelated same-name
        // type gets undone.
        private void CollectPartial(SemanticModel model, TypeDeclarationSyntax typeDecl)
        {
            if (model.GetDeclaredSymbol(typeDecl) is not INamedTypeSymbol symbol)
            {
                return;
            }
            var decls = new List<DeclLoc>();
            foreach (var reference in symbol.DeclaringSyntaxReferences)
            {
                if (FirstPartyDecl(reference) is { } loc && !decls.Contains(loc))
                {
                    decls.Add(loc);
                }
            }
            if (decls.Count == 0)
            {
                return;
            }
            var canonical = decls
                .OrderBy(d => d.File, StringComparer.Ordinal)
                .ThenBy(d => d.Line)
                .ToList();
            var key = string.Join("|", canonical.Select(d => $"{d.File}:{d.Line}"));
            _partials[key] = canonical;
        }

        // The site is keyed on the callee NAME token, not the expression start:
        // nested invocations (`Make().Handle(x)` and `Make()`) share a start
        // position, but their name tokens never collide.
        private void CollectCall(SemanticModel model, InvocationExpressionSyntax invocation, string rel)
        {
            if (CalleeNameToken(invocation.Expression) is not { } nameToken)
            {
                return;
            }
            if (model.GetSymbolInfo(invocation).Symbol is not IMethodSymbol symbol)
            {
                return;
            }
            if (FirstPartyDecl(DeclaredMethod(symbol)) is not { } target)
            {
                return;
            }
            var location = nameToken.GetLocation();
            var pos = location.GetLineSpan().StartLinePosition;
            var col = ByteCol(location, pos);
            var name = nameToken.ValueText;
            if (!_seenCalls.Add((rel, pos.Line + 1, col, name)))
            {
                return;
            }
            _calls.Add(new CallFact(rel, pos.Line + 1, col, name, target.File, target.Line, target.Col));
        }

        // Query syntax desugars to operator method calls with no invocation nodes;
        // each first-party operator becomes a caller-to-target fact keyed on the
        // enclosing member's declaration location.
        private void CollectQuery(SemanticModel model, QueryExpressionSyntax query, string rel)
        {
            if (EnclosingCallable(query) is not { } caller)
            {
                return;
            }
            var callerLocation = caller.GetLocation();
            var pos = callerLocation.GetLineSpan().StartLinePosition;
            var col = ByteCol(callerLocation, pos);
            foreach (var op in QueryOperators(model, query))
            {
                if (FirstPartyDecl(DeclaredMethod(op)) is not { } target)
                {
                    continue;
                }
                if (!_seenQueries.Add((rel, pos.Line + 1, col, target.File, target.Line, op.Name)))
                {
                    continue;
                }
                _queries.Add(new QueryFact(rel, pos.Line + 1, col, op.Name, target.File, target.Line, target.Col));
            }
        }

        private DeclLoc? FirstPartyDecl(IMethodSymbol method)
        {
            foreach (var reference in method.DeclaringSyntaxReferences)
            {
                if (FirstPartyDecl(reference) is { } loc)
                {
                    return loc;
                }
            }
            return null;
        }

        private DeclLoc? FirstPartyDecl(SyntaxReference reference)
        {
            var path = reference.SyntaxTree.FilePath;
            if (string.IsNullOrEmpty(path) || !IsFirstParty(path))
            {
                return null;
            }
            var location = reference.GetSyntax().GetLocation();
            var pos = location.GetLineSpan().StartLinePosition;
            return new DeclLoc(Rel(path), pos.Line + 1, ByteCol(location, pos));
        }
    }

    // Unwrap to the symbol whose declaration cgr ingested: the static form of a
    // reduced extension method, the generic definition of a constructed method,
    // and the implementation part of a partial method.
    private static IMethodSymbol DeclaredMethod(IMethodSymbol method)
    {
        var declared = method.ReducedFrom ?? method;
        declared = declared.OriginalDefinition;
        return declared.PartialImplementationPart ?? declared;
    }

    private static SyntaxToken? CalleeNameToken(ExpressionSyntax expression) => expression switch
    {
        MemberAccessExpressionSyntax memberAccess => memberAccess.Name.Identifier,
        MemberBindingExpressionSyntax memberBinding => memberBinding.Name.Identifier,
        GenericNameSyntax generic => generic.Identifier,
        IdentifierNameSyntax identifier => identifier.Identifier,
        _ => null,
    };

    private static SyntaxNode? EnclosingCallable(SyntaxNode node)
    {
        foreach (var ancestor in node.Ancestors())
        {
            if (ancestor is LocalFunctionStatementSyntax or BaseMethodDeclarationSyntax)
            {
                return ancestor;
            }
        }
        return null;
    }

    private static IEnumerable<IMethodSymbol> QueryOperators(SemanticModel model, QueryExpressionSyntax query)
    {
        foreach (var clause in query.DescendantNodes().OfType<QueryClauseSyntax>())
        {
            var info = model.GetQueryClauseInfo(clause);
            if (info.OperationInfo.Symbol is IMethodSymbol operation)
            {
                yield return operation;
            }
            if (info.CastInfo.Symbol is IMethodSymbol cast)
            {
                yield return cast;
            }
        }
        foreach (var selectOrGroup in query.DescendantNodes().OfType<SelectOrGroupClauseSyntax>())
        {
            if (model.GetSymbolInfo(selectOrGroup).Symbol is IMethodSymbol projection)
            {
                yield return projection;
            }
        }
        foreach (var ordering in query.DescendantNodes().OfType<OrderingSyntax>())
        {
            if (model.GetSymbolInfo(ordering).Symbol is IMethodSymbol order)
            {
                yield return order;
            }
        }
    }

    private static LinePosition StartOf(SyntaxNode node) =>
        node.GetLocation().GetLineSpan().StartLinePosition;

    // The Python join compares columns against tree-sitter's start_point, which
    // counts BYTES, while Roslyn's LinePosition.Character counts UTF-16 code
    // units. Re-measure the line prefix in UTF-8 bytes so a non-ASCII character
    // earlier on the line cannot desynchronise the key.
    private static int ByteCol(Location location, LinePosition pos)
    {
        var text = location.SourceTree?.GetText();
        if (text is null)
        {
            return pos.Character;
        }
        var line = text.Lines[pos.Line];
        var prefix = text.ToString(new TextSpan(line.Start, pos.Character));
        return System.Text.Encoding.UTF8.GetByteCount(prefix);
    }

    private static List<BaseFact> ClassifyBases(SemanticModel model, TypeDeclarationSyntax typeDecl)
    {
        var bases = new List<BaseFact>();
        foreach (var baseType in typeDecl.BaseList!.Types)
        {
            var symbol = model.GetSymbolInfo(baseType.Type).Symbol as INamedTypeSymbol;
            var name = symbol?.Name ?? SimpleName(baseType.Type);
            if (string.IsNullOrEmpty(name))
            {
                continue;
            }
            var kind = symbol?.TypeKind switch
            {
                TypeKind.Interface => KindInterface,
                TypeKind.Class => KindClass,
                TypeKind.Struct => KindClass,
                _ => KindUnknown,
            };
            bases.Add(new BaseFact(name, kind));
        }
        return bases;
    }

    // The simple name Roslyn could not resolve to a symbol: strip namespace
    // qualifiers and generic arguments to match tree-sitter's base simple names.
    private static string SimpleName(TypeSyntax type)
    {
        var text = type switch
        {
            GenericNameSyntax g => g.Identifier.Text,
            QualifiedNameSyntax q => q.Right.Identifier.Text,
            IdentifierNameSyntax i => i.Identifier.Text,
            _ => type.ToString(),
        };
        return text;
    }

    private static async Task<List<Project>> LoadProjectsAsync(
        MSBuildWorkspace workspace, string? projectOrSolution, string rootFull)
    {
        var input = projectOrSolution ?? FindProjectOrSolution(rootFull);
        if (input is null)
        {
            return new List<Project>();
        }
        try
        {
            if (input.EndsWith(".sln", StringComparison.OrdinalIgnoreCase))
            {
                var solution = await workspace.OpenSolutionAsync(input);
                return solution.Projects.ToList();
            }
            var project = await workspace.OpenProjectAsync(input);
            return new List<Project> { project };
        }
        catch (Exception ex)
        {
            if (Environment.GetEnvironmentVariable("CGR_FE_DEBUG") == "1")
            {
                Console.Error.WriteLine($"[LoadProjects] {input}: {ex.Message}");
            }
            return new List<Project>();
        }
    }

    private static string? FindProjectOrSolution(string rootFull)
    {
        var sln = Directory.EnumerateFiles(rootFull, "*.sln", SearchOption.AllDirectories)
            .OrderBy(p => p.Length).FirstOrDefault();
        if (sln is not null)
        {
            return sln;
        }
        return Directory.EnumerateFiles(rootFull, "*.csproj", SearchOption.AllDirectories)
            .OrderBy(p => p.Length).FirstOrDefault();
    }

    private static HashSet<string> IgnoredDirs()
    {
        var ignored = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            ".git", "node_modules", "bin", "obj", "vendor", "testdata",
        };
        var env = Environment.GetEnvironmentVariable("CGR_IGNORE_DIRS");
        if (!string.IsNullOrEmpty(env))
        {
            ignored = new HashSet<string>(
                env.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries),
                StringComparer.OrdinalIgnoreCase);
        }
        return ignored;
    }

    private record BaseFact(
        [property: JsonPropertyName("name")] string Name,
        [property: JsonPropertyName("kind")] string Kind);

    private record TypeFact(
        [property: JsonPropertyName("file")] string File,
        [property: JsonPropertyName("line")] int Line,
        [property: JsonPropertyName("name")] string Name,
        [property: JsonPropertyName("bases")] List<BaseFact> Bases);

    private record DeclLoc(
        [property: JsonPropertyName("file")] string File,
        [property: JsonPropertyName("line")] int Line,
        [property: JsonPropertyName("col")] int Col);

    private record CallFact(
        [property: JsonPropertyName("file")] string File,
        [property: JsonPropertyName("line")] int Line,
        [property: JsonPropertyName("col")] int Col,
        [property: JsonPropertyName("name")] string Name,
        [property: JsonPropertyName("tfile")] string TargetFile,
        [property: JsonPropertyName("tline")] int TargetLine,
        [property: JsonPropertyName("tcol")] int TargetCol);

    private record QueryFact(
        [property: JsonPropertyName("file")] string File,
        [property: JsonPropertyName("line")] int Line,
        [property: JsonPropertyName("col")] int Col,
        [property: JsonPropertyName("name")] string Name,
        [property: JsonPropertyName("tfile")] string TargetFile,
        [property: JsonPropertyName("tline")] int TargetLine,
        [property: JsonPropertyName("tcol")] int TargetCol);

    private record Payload(
        [property: JsonPropertyName("types")] List<TypeFact> Types,
        [property: JsonPropertyName("calls")] List<CallFact> Calls,
        [property: JsonPropertyName("partials")] List<List<DeclLoc>> Partials,
        [property: JsonPropertyName("queries")] List<QueryFact> Queries);
}
