#!/usr/bin/env python3
"""
Benchmark suite for comparing chunk-based vs full file editing approaches.
"""

import asyncio
import time
from pathlib import Path
from typing import Dict, List

from codebase_rag.tools.file_editor import FileEditor


class FileEditorBenchmark:
    """Benchmark suite for file editing performance."""
    
    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root)
        self.results: List[Dict] = []
    
    def create_test_file(self, file_path: str, content: str) -> None:
        """Create a test file with given content."""
        full_path = self.project_root / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
    
    def generate_code(self, lines: int, language: str = "python") -> str:
        """Generate code for different languages with specified number of lines."""
        if language == "python":
            return self.generate_python_code(lines)
        elif language == "javascript":
            return self.generate_javascript_code(lines)
        elif language == "rust":
            return self.generate_rust_code(lines)
        elif language == "java":
            return self.generate_java_code(lines)
        else:
            return self.generate_python_code(lines)  # fallback
    
    def generate_python_code(self, lines: int) -> str:
        """Generate Python code with specified number of lines."""
        lines_per_function = 10
        num_functions = max(1, lines // lines_per_function)
        
        code = "#!/usr/bin/env python3\n"
        code += "\"\"\"Generated Python code for benchmarking.\"\"\"\n\n"
        code += "import os\nimport sys\nfrom pathlib import Path\n\n"
        
        for i in range(num_functions):
            code += f"def function_{i}():\n"
            code += f"    \"\"\"Auto-generated function {i}.\"\"\"\n"
            for j in range(lines_per_function - 2):
                code += f"    # Line {j + 1} of function {i}\n"
            code += f"    return {i}\n\n"
        
        code += "if __name__ == '__main__':\n"
        code += "    print('Generated code executed successfully')\n"
        
        return code
    
    def modify_python_code(self, original_code: str, modification_type: str = "add_function") -> str:
        """Modify Python code to simulate realistic editing scenarios."""
        lines = original_code.split('\n')
        
        if modification_type == "add_function":
            # Add a new function at the end
            new_function = [
                "",
                "def new_benchmark_function():",
                "    \"\"\"Newly added function for benchmarking.\"\"\"",
                "    result = []",
                "    for i in range(100):",
                "        result.append(i * 2)",
                "    return result",
                ""
            ]
            # Insert before the main block
            main_index = next((i for i, line in enumerate(lines) if line.startswith("if __name__ == '__main__':")), len(lines) - 2)
            lines = lines[:main_index] + new_function + lines[main_index:]
        
        elif modification_type == "modify_imports":
            # Add new imports
            lines.insert(3, "import json")
            lines.insert(4, "import datetime")
        
        elif modification_type == "add_docstring":
            # Add docstring to first function
            for i, line in enumerate(lines):
                if line.startswith("def function_0"):
                    lines.insert(i + 1, "    \"\"\"Updated docstring for function_0.\"\"\"")
                    break
        
        return '\n'.join(lines)
    
    def generate_javascript_code(self, lines: int) -> str:
        """Generate JavaScript code with specified number of lines."""
        lines_per_function = 8
        num_functions = max(1, lines // lines_per_function)
        
        code = "// Generated JavaScript code for benchmarking\n\n"
        code += "const fs = require('fs');\nconst path = require('path');\n\n"
        
        for i in range(num_functions):
            code += f"function function{i}() {{\n"
            code += f"    // Auto-generated function {i}\n"
            for j in range(lines_per_function - 3):
                code += f"    // Line {j + 1} of function {i}\n"
            code += f"    return {i};\n}}\n\n"
        
        code += "console.log('Generated JavaScript code executed successfully');\n"
        return code
    
    def generate_rust_code(self, lines: int) -> str:
        """Generate Rust code with specified number of lines."""
        lines_per_function = 8
        num_functions = max(1, lines // lines_per_function)
        
        code = "// Generated Rust code for benchmarking\n\n"
        code += "use std::fs;\nuse std::path::Path;\n\n"
        
        for i in range(num_functions):
            code += f"fn function_{i}() -> i32 {{\n"
            code += f"    // Auto-generated function {i}\n"
            for j in range(lines_per_function - 3):
                code += f"    // Line {j + 1} of function {i}\n"
            code += f"    {i}\n}}\n\n"
        
        code += "fn main() {\n    println!(\"Generated Rust code executed successfully\");\n}\n"
        return code
    
    def generate_java_code(self, lines: int) -> str:
        """Generate Java code with specified number of lines."""
        lines_per_function = 10
        num_functions = max(1, lines // lines_per_function)
        
        code = "// Generated Java code for benchmarking\n\n"
        code += "import java.io.*;\nimport java.util.*;\n\n"
        code += "public class GeneratedCode {\n\n"
        
        for i in range(num_functions):
            code += f"    public static int function{i}() {{\n"
            code += f"        // Auto-generated function {i}\n"
            for j in range(lines_per_function - 4):
                code += f"        // Line {j + 1} of function {i}\n"
            code += f"        return {i};\n    }}\n\n"
        
        code += "    public static void main(String[] args) {\n"
        code += "        System.out.println(\"Generated Java code executed successfully\");\n"
        code += "    }\n}\n"
        return code
    
    async def benchmark_edit_method(self, editor: FileEditor, file_path: str, 
                                   new_content: str, method_name: str) -> Dict:
        """Benchmark a specific editing method."""
        start_time = time.time()
        
        if method_name == "smart_edit":
            result = await editor.edit_file(file_path, new_content)
        elif method_name == "chunk_edit":
            result = await editor.edit_file_with_chunks(file_path, new_content)
        else:
            raise ValueError(f"Unknown method: {method_name}")
        
        end_time = time.time()
        
        return {
            "method": method_name,
            "file_path": file_path,
            "success": result.success,
            "edit_type": result.edit_type,
            "changes_applied": result.changes_applied,
            "validation_passed": result.validation_passed,
            "elapsed_time": end_time - start_time,
            "performance_metrics": result.performance_metrics
        }
    
    async def run_benchmark(self, file_sizes: List[int], modifications: List[str]) -> List[Dict]:
        """Run comprehensive benchmark comparing different approaches."""
        results = []
        
        # Create file editor instances
        smart_editor = FileEditor(
            project_root=str(self.project_root),
            chunk_threshold_kb=5,  # Lower threshold for testing
            chunk_threshold_lines=100,
            enable_performance_monitoring=True
        )
        
        for file_size in file_sizes:
            for modification in modifications:
                test_file = f"benchmark_test_{file_size}_{modification}.py"
                
                # Generate original content
                original_content = self.generate_python_code(file_size)
                self.create_test_file(test_file, original_content)
                
                # Generate modified content
                modified_content = self.modify_python_code(original_content, modification)
                
                # Benchmark smart editing
                for method in ["smart_edit", "chunk_edit"]:
                    # Reset file content
                    self.create_test_file(test_file, original_content)
                    
                    # Run benchmark
                    result = await self.benchmark_edit_method(
                        smart_editor, test_file, modified_content, method
                    )
                    result.update({
                        "file_size_lines": file_size,
                        "modification_type": modification,
                        "original_size_chars": len(original_content),
                        "modified_size_chars": len(modified_content)
                    })
                    results.append(result)
        
        return results
    
    def print_benchmark_results(self, results: List[Dict]) -> None:
        """Print formatted benchmark results."""
        print("\n" + "="*80)
        print("FILE EDITOR BENCHMARK RESULTS")
        print("="*80)
        
        # Group by file size and modification type
        grouped_results = {}
        for result in results:
            key = (result["file_size_lines"], result["modification_type"])
            if key not in grouped_results:
                grouped_results[key] = []
            grouped_results[key].append(result)
        
        for (file_size, modification), group in grouped_results.items():
            print(f"\n📊 File Size: {file_size} lines | Modification: {modification}")
            print("-" * 60)
            
            for result in group:
                method = result["method"]
                edit_type = result["edit_type"]
                elapsed = result["elapsed_time"]
                changes = result["changes_applied"]
                
                print(f"  {method:12} | {edit_type:5} | {elapsed:.3f}s | {changes} changes")
                
                if result["performance_metrics"]:
                    metrics = result["performance_metrics"]
                    print(f"                 Memory: {metrics.get('memory_delta_mb', 0):.1f}MB")
        
        print("\n" + "="*80)
        print("PERFORMANCE SUMMARY")
        print("="*80)
        
        # Calculate averages
        smart_times = [r["elapsed_time"] for r in results if r["method"] == "smart_edit"]
        chunk_times = [r["elapsed_time"] for r in results if r["method"] == "chunk_edit"]
        
        print(f"Average Smart Edit Time: {sum(smart_times)/len(smart_times):.3f}s")
        print(f"Average Chunk Edit Time: {sum(chunk_times)/len(chunk_times):.3f}s")
        
        smart_memory = [r["performance_metrics"]["memory_delta_mb"] 
                       for r in results if r["method"] == "smart_edit" and r["performance_metrics"]]
        chunk_memory = [r["performance_metrics"]["memory_delta_mb"] 
                       for r in results if r["method"] == "chunk_edit" and r["performance_metrics"]]
        
        if smart_memory and chunk_memory:
            print(f"Average Smart Edit Memory: {sum(smart_memory)/len(smart_memory):.1f}MB")
            print(f"Average Chunk Edit Memory: {sum(chunk_memory)/len(chunk_memory):.1f}MB")


async def main():
    """Main benchmark execution."""
    benchmark = FileEditorBenchmark()
    
    # Test different file sizes and modification types
    file_sizes = [50, 200, 500, 1000]  # Lines of code
    modifications = ["add_function", "modify_imports", "add_docstring"]
    
    print("Starting File Editor Benchmark...")
    print(f"Testing {len(file_sizes)} file sizes × {len(modifications)} modifications × 2 methods")
    print("This may take a few moments...")
    
    results = await benchmark.run_benchmark(file_sizes, modifications)
    benchmark.print_benchmark_results(results)
    
    print("\n🎉 Benchmark completed!")


if __name__ == "__main__":
    asyncio.run(main())