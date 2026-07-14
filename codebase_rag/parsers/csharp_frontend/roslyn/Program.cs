// Roslyn semantic frontend for cgr's C# hybrid mode (issue #738).
//
// Loads the real .csproj/.sln via MSBuildWorkspace (so third-party + BCL types
// resolve), then for every first-party type declaration emits, keyed on
// (file, line) matching cgr's tree-sitter node span, each base classified as
// "class" or "interface" by the resolved symbol -- the fact syntax alone gets
// wrong (~0.85 F1 in the eval). tree-sitter stays the backbone; the Python side
// consults these classifications and falls back to its I-prefix heuristic for any
// base the semantic model could not resolve.
//
// MSBuildLocator.RegisterDefaults() MUST run before any Microsoft.CodeAnalysis
// .MSBuild type is touched, so Main only references the locator + Frontend; the
// workspace types JIT-load when Frontend.RunAsync is first called, after registration.

using Microsoft.Build.Locator;

if (!MSBuildLocator.IsRegistered)
{
    MSBuildLocator.RegisterDefaults();
}

return await CsharpFrontend.Frontend.RunAsync(args);
