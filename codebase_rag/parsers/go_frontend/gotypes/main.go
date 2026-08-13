// Go semantic frontend for cgr's hybrid mode (issue #1179). Loads the target
// module with go/packages (NeedTypes|NeedTypesInfo|NeedSyntax) and emits
// location-keyed facts the tree-sitter name trie cannot derive: the exact
// first-party callee of every call expression (from types.Info.Uses, so
// embedded-struct method promotion and scope shadowing resolve by the real type
// rules), the call sites whose callee resolves OUTSIDE the module (stdlib,
// deps) so the fallback must not fabricate a first-party edge there, and the
// implementer->interface pairs that types.Implements proves (Go interfaces are
// satisfied structurally, so there is no syntactic base list to read -- the
// compiler is the only source of these IMPLEMENTS edges). A call through a
// variable (a method-value or func-value binding) resolves to a *types.Var,
// not a *types.Func, so it is left to tree-sitter -- never a wrong edge.
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

// implementsFact is a position-join, never a name-map: Go reuses simple type
// names across packages (Reader, Writer, Handler), so both sides carry the
// declaring identifier's (file, line, col) and the consumer resolves each to a
// graph node through the type-location map, never by name.
type implementsFact struct {
	File  string `json:"file"`
	Line  int    `json:"line"`
	Col   int    `json:"col"`
	Name  string `json:"name"`
	IFile string `json:"ifile"`
	ILine int    `json:"iline"`
	ICol  int    `json:"icol"`
	IName string `json:"iname"`
}

type payload struct {
	Calls      []callFact       `json:"calls"`
	Externals  []externalFact   `json:"externals"`
	Implements []implementsFact `json:"implements"`
}

// typeEntry is a first-party named type gathered for the implements pass: the
// resolved declaration position plus the *types.Named needed by
// types.Implements.
type typeEntry struct {
	named *types.Named
	rel   string
	line  int
	col   int
	name  string
}

type collector struct {
	root       string
	mainPaths  map[string]bool
	ignored    map[string]bool
	out        *payload
	namedTypes []typeEntry
	interfaces []typeEntry
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
		out: &payload{
			Calls:      []callFact{},
			Externals:  []externalFact{},
			Implements: []implementsFact{},
		},
	}
	for _, pkg := range pkgs {
		col.collectPackage(pkg)
	}
	col.collectImplements()
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
	c.gatherTypes(pkg)
}

// gatherTypes records this package's first-party named types, split into
// interfaces (candidate IMPLEMENTS targets) and concrete named types
// (candidate implementers), for the cross-package implements pass. Only
// cleanly-typed, in-repo declarations are kept; empty interfaces are dropped
// because everything satisfies them and the edge carries no signal.
func (c *collector) gatherTypes(pkg *packages.Package) {
	scope := pkg.Types.Scope()
	for _, name := range scope.Names() {
		obj, ok := scope.Lookup(name).(*types.TypeName)
		if !ok {
			continue
		}
		named, ok := obj.Type().(*types.Named)
		if !ok {
			continue
		}
		// Uninstantiated generic types: types.Implements is not contractually
		// specified for a type with live type parameters (it happens not to
		// panic on go1.23, but the result is not part of the API guarantee), so
		// skip them on BOTH the implementer and the interface side rather than
		// emit a version-dependent edge -- the frontend degrades to tree-sitter.
		if named.TypeParams().Len() > 0 {
			continue
		}
		rel, line, col, ok := c.position(pkg.Fset, obj.Pos())
		if !ok {
			continue
		}
		entry := typeEntry{named: named, rel: rel, line: line, col: col, name: obj.Name()}
		if iface, ok := named.Underlying().(*types.Interface); ok {
			if iface.NumMethods() > 0 {
				c.interfaces = append(c.interfaces, entry)
			}
			continue
		}
		c.namedTypes = append(c.namedTypes, entry)
	}
}

// collectImplements pairs every first-party concrete type with every
// first-party interface it satisfies (value- or pointer-receiver method set).
// Both sides are proven by types.Implements, so an edge is emitted only when
// the compiler agrees -- no structural guessing on the tree-sitter side.
func (c *collector) collectImplements() {
	for _, t := range c.namedTypes {
		for _, i := range c.interfaces {
			if t.named == i.named || !implementsIface(t.named, i.named) {
				continue
			}
			c.out.Implements = append(c.out.Implements, implementsFact{
				File: t.rel, Line: t.line, Col: t.col, Name: t.name,
				IFile: i.rel, ILine: i.line, ICol: i.col, IName: i.name,
			})
		}
	}
}

// implementsIface is true when t satisfies iface through either its value or
// its pointer method set (a pointer-receiver method only lands on *T).
func implementsIface(t *types.Named, ifaceNamed *types.Named) bool {
	iface, ok := ifaceNamed.Underlying().(*types.Interface)
	if !ok {
		return false
	}
	return types.Implements(t, iface) || types.Implements(types.NewPointer(t), iface)
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
