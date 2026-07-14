using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.MSBuild;

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
        var ignoredDirs = IgnoredDirs();

        var debug = Environment.GetEnvironmentVariable("CGR_FE_DEBUG") == "1";
        using var workspace = MSBuildWorkspace.Create();
        // A failed reference or an unloadable project must not abort the whole run;
        // degrade to whatever loaded and let the Python side fall back per-type.
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

        var types = new List<TypeFact>();
        var seen = new HashSet<(string, int)>();
        foreach (var project in projects)
        {
            var compilation = await project.GetCompilationAsync();
            if (compilation is null)
            {
                continue;
            }
            foreach (var tree in compilation.SyntaxTrees)
            {
                var path = tree.FilePath;
                if (string.IsNullOrEmpty(path) || !IsFirstParty(path, rootFull, ignoredDirs))
                {
                    continue;
                }
                var rel = Path.GetRelativePath(rootFull, path).Replace(Path.DirectorySeparatorChar, '/');
                var model = compilation.GetSemanticModel(tree);
                foreach (var node in tree.GetRoot().DescendantNodes())
                {
                    if (node is not TypeDeclarationSyntax typeDecl || typeDecl.BaseList is null)
                    {
                        continue;
                    }
                    var line = typeDecl.GetLocation().GetLineSpan().StartLinePosition.Line + 1;
                    // A type appears once per compilation it belongs to; a file shared
                    // by multi-targeted projects would emit duplicate (rel,line) keys.
                    if (!seen.Add((rel, line)))
                    {
                        continue;
                    }
                    types.Add(new TypeFact(rel, line, typeDecl.Identifier.Text, ClassifyBases(model, typeDecl)));
                }
            }
        }

        var options = new JsonSerializerOptions { DefaultIgnoreCondition = JsonIgnoreCondition.Never };
        Console.WriteLine(JsonSerializer.Serialize(new Payload(types), options));
        return 0;
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

    private static bool IsFirstParty(string path, string rootFull, HashSet<string> ignoredDirs)
    {
        var full = Path.GetFullPath(path);
        if (!full.StartsWith(rootFull, StringComparison.Ordinal))
        {
            return false;
        }
        var rel = Path.GetRelativePath(rootFull, full);
        foreach (var part in rel.Split(Path.DirectorySeparatorChar, '/'))
        {
            if (ignoredDirs.Contains(part))
            {
                return false;
            }
        }
        return true;
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

    private record Payload([property: JsonPropertyName("types")] List<TypeFact> Types);
}
