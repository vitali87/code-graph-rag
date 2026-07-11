// Authoritative Go structure oracle for the cgr eval harness.
//
// Walks a directory of Go sources with the standard library's own go/parser
// and go/ast, and emits a JSON payload {nodes, edges}. Node "kind" fields use
// cgr's NodeLabel vocabulary (Function, Method, Class, Interface, Type) and
// edges use cgr's RelationshipType vocabulary, so both join cgr's graph on
// (kind, file, line).
//
// Mapping (Go declaration -> cgr NodeLabel):
//
//	func without receiver -> Function
//	func with receiver    -> Method
//	type ... struct {}    -> Class
//	type ... interface {} -> Interface
//	type ... (other)      -> Type   (defined types and aliases alike)
//
// Containment edges (matching how cgr models Go containment):
//
//	DEFINES        : Module(file, line 0) -> top-level Function / Class / Interface / Type
//	DEFINES_METHOD : receiver type's node -> Method   (cross-file within a package)
//
// cgr models a Go module per file, so a DEFINES parent is the file's module
// keyed at line 0. A receiver method's parent is the node of its receiver type,
// resolved package-wide (a method may sit in a different file than its type).
//
// Run: GO111MODULE=off go run go_ast.go <dir>
package main

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
)

// Def is a single declaration record. Line is the identifier line (the node's
// start, matching cgr); EndLine is the line of the declaration's last token.
type Def struct {
	Kind    string `json:"kind"`
	File    string `json:"file"`
	Line    int    `json:"line"`
	EndLine int    `json:"end_line"`
	Name    string `json:"name"`
}

// NodeRef identifies an edge endpoint by (kind, file, line).
type NodeRef struct {
	Kind string `json:"kind"`
	File string `json:"file"`
	Line int    `json:"line"`
}

// Edge is a containment relationship between two node references.
type Edge struct {
	Rel    string  `json:"rel"`
	Parent NodeRef `json:"parent"`
	Child  NodeRef `json:"child"`
}

// Call is a call site: the file it appears in and the callee's simple name
// (the bare identifier, or the selector tail for x.Method() / pkg.Func()).
type Call struct {
	File string `json:"file"`
	Name string `json:"name"`
}

// Payload is the oracle's stdout shape.
type Payload struct {
	Nodes []Def  `json:"nodes"`
	Edges []Edge `json:"edges"`
	Calls []Call `json:"calls"`
}

// ignoredDirs are skipped during the walk; they never hold first-party sources.
var ignoredDirs = map[string]bool{
	".git":         true,
	"vendor":       true,
	"node_modules": true,
	"testdata":     true,
}

const (
	kindFunction   = "Function"
	kindMethod     = "Method"
	kindClass      = "Class"
	kindInterface  = "Interface"
	kindType       = "Type"
	kindModule     = "Module"
	relDefines     = "DEFINES"
	relDefinesMeth = "DEFINES_METHOD"
	moduleLine     = 0
	goSuffix       = ".go"
)

func typeSpecKind(spec *ast.TypeSpec) string {
	switch spec.Type.(type) {
	case *ast.StructType:
		return kindClass
	case *ast.InterfaceType:
		return kindInterface
	default:
		return kindType
	}
}

// baseTypeName strips pointer and generic instantiation wrappers off a receiver
// type expression, leaving the bare type name (e.g. *Point[T] -> "Point").
func baseTypeName(expr ast.Expr) string {
	switch t := expr.(type) {
	case *ast.StarExpr:
		return baseTypeName(t.X)
	case *ast.IndexExpr:
		return baseTypeName(t.X)
	case *ast.IndexListExpr:
		return baseTypeName(t.X)
	case *ast.Ident:
		return t.Name
	}
	return ""
}

func recvTypeName(recv *ast.FieldList) string {
	if recv == nil || len(recv.List) == 0 {
		return ""
	}
	return baseTypeName(recv.List[0].Type)
}

// parsedFile bundles a parsed source with its location data for the two passes.
type parsedFile struct {
	fset *token.FileSet
	file *ast.File
	rel  string
	dir  string
}

// collectNodes records every declaration (including function-local types) so the
// node set is an apples-to-apples ground truth for cgr's node capture.
func collectNodes(pf parsedFile, defs *[]Def) {
	ast.Inspect(pf.file, func(n ast.Node) bool {
		switch d := n.(type) {
		case *ast.FuncDecl:
			kind := kindFunction
			if d.Recv != nil {
				kind = kindMethod
			}
			line := pf.fset.Position(d.Name.Pos()).Line
			end := pf.fset.Position(d.End()).Line
			*defs = append(*defs, Def{kind, pf.rel, line, end, d.Name.Name})
		case *ast.TypeSpec:
			line := pf.fset.Position(d.Name.Pos()).Line
			end := pf.fset.Position(d.End()).Line
			*defs = append(*defs, Def{typeSpecKind(d), pf.rel, line, end, d.Name.Name})
		}
		return true
	})
}

// typeKey scopes a type name to its package directory; methods resolve their
// receiver type within the same package, which Go keeps in one directory.
func typeKey(dir, name string) string {
	return dir + "\x00" + name
}

// collectTypes records each top-level type's node so receiver methods can later
// point DEFINES_METHOD at the right (kind, file, line).
func collectTypes(pf parsedFile, types map[string]Def) {
	for _, decl := range pf.file.Decls {
		gen, ok := decl.(*ast.GenDecl)
		if !ok || gen.Tok != token.TYPE {
			continue
		}
		for _, spec := range gen.Specs {
			ts, ok := spec.(*ast.TypeSpec)
			if !ok {
				continue
			}
			line := pf.fset.Position(ts.Name.Pos()).Line
			end := pf.fset.Position(ts.End()).Line
			types[typeKey(pf.dir, ts.Name.Name)] = Def{typeSpecKind(ts), pf.rel, line, end, ts.Name.Name}
		}
	}
}

// collectEdges emits DEFINES for top-level funcs/types and DEFINES_METHOD for
// receiver methods, mirroring cgr's per-file module containment.
func collectEdges(pf parsedFile, types map[string]Def, edges *[]Edge) {
	module := NodeRef{kindModule, pf.rel, moduleLine}
	for _, decl := range pf.file.Decls {
		switch d := decl.(type) {
		case *ast.FuncDecl:
			line := pf.fset.Position(d.Name.Pos()).Line
			// A function-local type parents to the enclosing FuncDecl: Go has
			// no nested named funcs and cgr creates no nodes for func
			// literals, so the FuncDecl is the nearest node-bearing scope.
			parentKind := kindFunction
			if d.Recv != nil {
				parentKind = kindMethod
			}
			funcRef := NodeRef{parentKind, pf.rel, line}
			ast.Inspect(d.Body, func(n ast.Node) bool {
				if ts, ok := n.(*ast.TypeSpec); ok {
					tline := pf.fset.Position(ts.Name.Pos()).Line
					child := NodeRef{typeSpecKind(ts), pf.rel, tline}
					*edges = append(*edges, Edge{relDefines, funcRef, child})
				}
				return true
			})
			if d.Recv == nil {
				child := NodeRef{kindFunction, pf.rel, line}
				*edges = append(*edges, Edge{relDefines, module, child})
				continue
			}
			owner, ok := types[typeKey(pf.dir, recvTypeName(d.Recv))]
			if !ok {
				continue
			}
			parent := NodeRef{owner.Kind, owner.File, owner.Line}
			child := NodeRef{kindMethod, pf.rel, line}
			*edges = append(*edges, Edge{relDefinesMeth, parent, child})
		case *ast.GenDecl:
			if d.Tok != token.TYPE {
				continue
			}
			for _, spec := range d.Specs {
				ts, ok := spec.(*ast.TypeSpec)
				if !ok {
					continue
				}
				line := pf.fset.Position(ts.Name.Pos()).Line
				child := NodeRef{typeSpecKind(ts), pf.rel, line}
				*edges = append(*edges, Edge{relDefines, module, child})
			}
		}
	}
}

// calleeName returns the simple name a call expression targets: the bare
// identifier for foo(), or the selector tail for x.Method() and pkg.Func().
func calleeName(expr ast.Expr) string {
	switch f := expr.(type) {
	case *ast.Ident:
		return f.Name
	case *ast.SelectorExpr:
		return f.Sel.Name
	case *ast.IndexExpr:
		return calleeName(f.X)
	case *ast.IndexListExpr:
		return calleeName(f.X)
	}
	return ""
}

// collectCalls records every call site's (file, callee simple name). First-party
// filtering happens in the Python harness against the declared name set.
func collectCalls(pf parsedFile, calls *[]Call) {
	ast.Inspect(pf.file, func(n ast.Node) bool {
		if call, ok := n.(*ast.CallExpr); ok {
			if name := calleeName(call.Fun); name != "" {
				*calls = append(*calls, Call{pf.rel, name})
			}
		}
		return true
	})
}

func main() {
	root := os.Args[1]
	var parsed []parsedFile
	_ = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		if info.IsDir() {
			if ignoredDirs[info.Name()] {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, goSuffix) {
			return nil
		}
		fset := token.NewFileSet()
		file, perr := parser.ParseFile(fset, path, nil, 0)
		if perr != nil {
			return nil
		}
		rel, rerr := filepath.Rel(root, path)
		if rerr != nil {
			rel = path
		}
		rel = filepath.ToSlash(rel)
		parsed = append(parsed, parsedFile{fset, file, rel, filepath.ToSlash(filepath.Dir(rel))})
		return nil
	})

	types := map[string]Def{}
	for _, pf := range parsed {
		collectTypes(pf, types)
	}

	defs := []Def{}
	edges := []Edge{}
	calls := []Call{}
	for _, pf := range parsed {
		collectNodes(pf, &defs)
		collectEdges(pf, types, &edges)
		collectCalls(pf, &calls)
	}
	_ = json.NewEncoder(os.Stdout).Encode(Payload{Nodes: defs, Edges: edges, Calls: calls})
}
