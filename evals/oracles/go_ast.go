// Authoritative Go structure oracle for the cgr eval harness.
//
// Walks a directory of Go sources with the standard library's own go/parser
// and go/ast, and emits one JSON record per top-level declaration. The "kind"
// field uses cgr's NodeLabel vocabulary (Function, Method, Class, Interface,
// Type) so the emitted records can be joined directly against cgr's graph on
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

// Def is a single declaration record. Field order and json tags mirror what
// evals/oracles/go_oracle.py expects.
type Def struct {
	Kind string `json:"kind"`
	File string `json:"file"`
	Line int    `json:"line"`
	Name string `json:"name"`
}

// ignoredDirs are skipped during the walk; they never hold first-party sources.
var ignoredDirs = map[string]bool{
	".git":         true,
	"vendor":       true,
	"node_modules": true,
	"testdata":     true,
}

const (
	kindFunction  = "Function"
	kindMethod    = "Method"
	kindClass     = "Class"
	kindInterface = "Interface"
	kindType      = "Type"
	goSuffix      = ".go"
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

// collectFile visits the whole file (not just top-level Decls) so that
// function-local type declarations are recorded too — cgr captures those by
// default, so the oracle must as well to stay an apples-to-apples ground truth.
// Go has no named nested functions, so every *ast.FuncDecl is top-level.
func collectFile(fset *token.FileSet, file *ast.File, rel string, defs *[]Def) {
	ast.Inspect(file, func(n ast.Node) bool {
		switch d := n.(type) {
		case *ast.FuncDecl:
			kind := kindFunction
			if d.Recv != nil {
				kind = kindMethod
			}
			*defs = append(*defs, Def{kind, rel, fset.Position(d.Name.Pos()).Line, d.Name.Name})
		case *ast.TypeSpec:
			*defs = append(*defs, Def{typeSpecKind(d), rel, fset.Position(d.Name.Pos()).Line, d.Name.Name})
		}
		return true
	})
}

func main() {
	root := os.Args[1]
	defs := []Def{}
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
		collectFile(fset, file, filepath.ToSlash(rel), &defs)
		return nil
	})
	_ = json.NewEncoder(os.Stdout).Encode(defs)
}
