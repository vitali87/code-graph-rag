// Go semantic frontend for cgr's hybrid mode (issue #1179). Loads the target
// module with go/packages (NeedTypes|NeedTypesInfo|NeedSyntax) and emits
// location-keyed call facts the tree-sitter name trie cannot derive: the exact
// first-party callee of every call expression (from types.Info.Uses, so
// embedded-struct method promotion and scope shadowing resolve by the real type
// rules), and the call sites whose callee resolves OUTSIDE the module (stdlib,
// deps) so the fallback must not fabricate a first-party edge there. A call
// through a variable (a method-value or func-value binding) resolves to a
// *types.Var, not a *types.Func, so it is left to tree-sitter -- never a wrong
// edge.
//
// Columns are 0-based BYTE offsets on both the call-site and target sides, to
// match tree-sitter's start_point: go/token reports 1-based byte columns, so
// every emitted column is Column-1. Facts are emitted only from packages that
// type-check cleanly; a missing toolchain, no go.mod, a load error, an
// ill-typed package or an empty result all leave cgr on pure tree-sitter.
//
// Run: gotypes <repo-root>   (honours CGR_IGNORE_DIRS, comma-separated)
package main

import (
	"encoding/json"
	"go/ast"
	"go/token"
	"go/types"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/tools/go/packages"
)

type callFact struct {
	File  string `json:"file"`
	Line  int    `json:"line"`
	Col   int    `json:"col"`
	Name  string `json:"name"`
	TFile string `json:"tfile"`
	TLine int    `json:"tline"`
	TCol  int    `json:"tcol"`
}

type externalFact struct {
	File string `json:"file"`
	Line int    `json:"line"`
	Col  int    `json:"col"`
	Name string `json:"name"`
}

type payload struct {
	Calls     []callFact     `json:"calls"`
	Externals []externalFact `json:"externals"`
}

type collector struct {
	root      string
	mainPaths map[string]bool
	ignored   map[string]bool
	out       *payload
}

func main() {
	if len(os.Args) < 2 {
		emit(payload{Calls: []callFact{}, Externals: []externalFact{}})
		return
	}
	root, err := filepath.Abs(os.Args[1])
	if err != nil {
		os.Exit(1)
	}
	cfg := &packages.Config{
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedSyntax |
			packages.NeedTypes | packages.NeedTypesInfo | packages.NeedImports |
			packages.NeedDeps,
		Dir:   root,
		Tests: false,
	}
	pkgs, err := packages.Load(cfg, "./...")
	if err != nil {
		os.Exit(1)
	}
	mainPaths := map[string]bool{}
	for _, pkg := range pkgs {
		if pkg.PkgPath != "" {
			mainPaths[pkg.PkgPath] = true
		}
	}
	col := &collector{
		root:      root,
		mainPaths: mainPaths,
		ignored:   ignoredDirs(),
		out:       &payload{Calls: []callFact{}, Externals: []externalFact{}},
	}
	for _, pkg := range pkgs {
		col.collectPackage(pkg)
	}
	emit(*col.out)
}

func (c *collector) collectPackage(pkg *packages.Package) {
	// packages.Load returns a nil error while recording parse/type errors on
	// the package itself; its resolutions are then untrustworthy, so skip it
	// and let tree-sitter own its calls. Cleanly-typed packages still emit.
	if pkg.TypesInfo == nil || len(pkg.Errors) > 0 {
		return
	}
	for _, file := range pkg.Syntax {
		fset := pkg.Fset
		ast.Inspect(file, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			c.collectCall(call, pkg, fset)
			return true
		})
	}
}

func (c *collector) collectCall(call *ast.CallExpr, pkg *packages.Package, fset *token.FileSet) {
	name := calleeName(call.Fun)
	if name == nil {
		return
	}
	fn, ok := pkg.TypesInfo.Uses[name].(*types.Func)
	if !ok {
		return
	}
	rel, line, col, ok := c.position(fset, name.Pos())
	if !ok {
		return
	}
	if fn.Pkg() == nil || !c.mainPaths[fn.Pkg().Path()] {
		c.out.Externals = append(c.out.Externals, externalFact{
			File: rel, Line: line, Col: col, Name: name.Name,
		})
		return
	}
	trel, tline, tcol, ok := c.position(fset, fn.Pos())
	if !ok {
		return
	}
	c.out.Calls = append(c.out.Calls, callFact{
		File: rel, Line: line, Col: col, Name: name.Name,
		TFile: trel, TLine: tline, TCol: tcol,
	})
}

// calleeName is the call's name token: the bare identifier for f(), or the
// selector tail for x.M() and pkg.F(). Conversions and computed callees have
// no name token and are skipped.
func calleeName(fun ast.Expr) *ast.Ident {
	switch node := fun.(type) {
	case *ast.Ident:
		return node
	case *ast.SelectorExpr:
		return node.Sel
	case *ast.IndexExpr:
		return calleeName(node.X)
	case *ast.IndexListExpr:
		return calleeName(node.X)
	case *ast.ParenExpr:
		return calleeName(node.X)
	}
	return nil
}

// position returns the repo-relative forward-slash path plus 1-based line and
// 0-based BYTE column of pos, or ok=false when the position is invalid or the
// file falls outside the repo or an ignored directory.
func (c *collector) position(fset *token.FileSet, pos token.Pos) (string, int, int, bool) {
	if !pos.IsValid() {
		return "", 0, 0, false
	}
	p := fset.Position(pos)
	rel, err := filepath.Rel(c.root, p.Filename)
	if err != nil {
		return "", 0, 0, false
	}
	rel = filepath.ToSlash(rel)
	if strings.HasPrefix(rel, "../") {
		return "", 0, 0, false
	}
	for _, part := range strings.Split(rel, "/") {
		if c.ignored[part] {
			return "", 0, 0, false
		}
	}
	return rel, p.Line, p.Column - 1, true
}

func emit(out payload) {
	data, err := json.Marshal(out)
	if err != nil {
		os.Exit(1)
	}
	os.Stdout.Write(data)
	os.Stdout.Write([]byte("\n"))
}

func ignoredDirs() map[string]bool {
	ignored := map[string]bool{}
	for _, part := range strings.Split(os.Getenv("CGR_IGNORE_DIRS"), ",") {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			ignored[trimmed] = true
		}
	}
	return ignored
}
