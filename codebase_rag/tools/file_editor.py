from pathlib import Path
from typing import List, Tuple
import time
import psutil
import os
import ast

import diff_match_patch as dmp
from loguru import logger
from pydantic import BaseModel
from pydantic_ai import RunContext, Tool
from tree_sitter import Language, Parser

from codebase_rag.language_config import get_language_config

# Import available Tree-sitter languages
try:
    from tree_sitter_python import language as python_language_so
except ImportError:
    python_language_so = None

try:
    from tree_sitter_javascript import language as javascript_language_so
except ImportError:
    javascript_language_so = None

try:
    from tree_sitter_typescript import language_typescript as typescript_language_so
except ImportError:
    typescript_language_so = None

try:
    from tree_sitter_rust import language as rust_language_so
except ImportError:
    rust_language_so = None

try:
    from tree_sitter_go import language as go_language_so
except ImportError:
    go_language_so = None

try:
    from tree_sitter_scala import language as scala_language_so
except ImportError:
    scala_language_so = None

try:
    from tree_sitter_java import language as java_language_so
except ImportError:
    java_language_so = None

# Language mapping for tree-sitter parsers
LANGUAGE_PARSERS = {
    "python": python_language_so,
    "javascript": javascript_language_so,
    "typescript": typescript_language_so,
    "rust": rust_language_so,
    "go": go_language_so,
    "scala": scala_language_so,
    "java": java_language_so,
}


class EditResult(BaseModel):
    """Data model for file edit results."""

    file_path: str
    success: bool
    error_message: str | None = None
    edit_type: str | None = None  # 'full' or 'chunk'
    changes_applied: int | None = None  # Number of diff patches applied
    performance_metrics: dict | None = None  # Performance tracking data
    validation_passed: bool | None = None  # Whether syntax validation passed


class PerformanceMonitor:
    """Performance monitoring for file edit operations."""
    
    def __init__(self):
        self.start_time = None
        self.start_memory = None
        self.process = psutil.Process(os.getpid())
    
    def start_monitoring(self):
        """Start performance monitoring."""
        self.start_time = time.time()
        self.start_memory = self.process.memory_info().rss / 1024 / 1024  # MB
    
    def get_metrics(self) -> dict:
        """Get current performance metrics."""
        if self.start_time is None:
            return {}
        
        elapsed_time = time.time() - self.start_time
        current_memory = self.process.memory_info().rss / 1024 / 1024  # MB
        memory_delta = current_memory - self.start_memory if self.start_memory else 0
        
        return {
            "elapsed_time_seconds": round(elapsed_time, 3),
            "memory_usage_mb": round(current_memory, 2),
            "memory_delta_mb": round(memory_delta, 2),
            "cpu_percent": round(self.process.cpu_percent(), 2)
        }


class FileEditor:
    """Service to edit files in the filesystem."""

    def __init__(self, project_root: str = ".", chunk_threshold_kb: int = 10, chunk_threshold_lines: int = 500, enable_performance_monitoring: bool = True):
        self.project_root = Path(project_root).resolve()
        self.chunk_threshold_kb = chunk_threshold_kb
        self.chunk_threshold_lines = chunk_threshold_lines
        self.enable_performance_monitoring = enable_performance_monitoring
        self.dmp = dmp.diff_match_patch()
        
        # Initialize tree-sitter parsers for syntax validation
        self.parsers = {}
        self._init_tree_sitter_parsers()
        
        logger.info(f"FileEditor initialized with root: {self.project_root}")
        logger.info(f"Chunk thresholds: {chunk_threshold_kb}KB, {chunk_threshold_lines} lines")
        logger.info(f"Performance monitoring: {'enabled' if enable_performance_monitoring else 'disabled'}")
        logger.info(f"Syntax validation available for: {list(self.parsers.keys())}")

    def _init_tree_sitter_parsers(self):
        """Initialize tree-sitter parsers for available languages."""
        for lang_name, language_so in LANGUAGE_PARSERS.items():
            if language_so is not None:
                try:
                    parser = Parser()
                    language = Language(language_so())  # Call the function to get the language
                    parser.language = language  # Use parser.language instead of parser.set_language
                    self.parsers[lang_name] = parser
                    logger.debug(f"Initialized tree-sitter parser for {lang_name}")
                except Exception as e:
                    logger.warning(f"Failed to initialize tree-sitter parser for {lang_name}: {e}")

    def _should_use_chunk_edit(self, file_path: Path, current_content: str) -> bool:
        """Determine if chunk editing should be used based on file size and content."""
        # Check file size
        file_size_kb = file_path.stat().st_size / 1024
        if file_size_kb > self.chunk_threshold_kb:
            return True
        
        # Check line count
        line_count = len(current_content.splitlines())
        if line_count > self.chunk_threshold_lines:
            return True
        
        return False

    def _apply_chunk_edit(self, current_content: str, new_content: str) -> Tuple[str, int]:
        """Apply chunk-based editing using diff-match-patch."""
        # Generate diff patches
        diffs = self.dmp.diff_main(current_content, new_content)
        self.dmp.diff_cleanupSemantic(diffs)
        
        # Create patches
        patches = self.dmp.patch_make(current_content, diffs)
        
        # Apply patches
        result, results = self.dmp.patch_apply(patches, current_content)
        
        # Count successful patch applications
        successful_patches = sum(1 for success in results if success)
        
        return result, successful_patches

    def _validate_syntax(self, content: str, file_path: str) -> tuple[bool, str | None]:
        """Validate syntax using tree-sitter for supported languages or AST for Python."""
        file_path_obj = Path(file_path)
        file_extension = file_path_obj.suffix
        
        # Get language configuration
        lang_config = get_language_config(file_extension)
        if not lang_config:
            logger.debug(f"[FileEditor] No language config found for {file_extension}, skipping validation")
            return True, None
        
        lang_name = lang_config.name
        
        # Special case for Python: use AST parser for better error messages
        if lang_name == "python":
            try:
                ast.parse(content)
                logger.debug(f"[FileEditor] Python AST validation passed for {file_path}")
                return True, None
            except SyntaxError as e:
                error_msg = f"Python syntax error: {e.msg} at line {e.lineno}"
                logger.warning(f"[FileEditor] Python AST validation failed for {file_path}: {error_msg}")
                return False, error_msg
        
        # Use tree-sitter for other languages
        if lang_name in self.parsers:
            try:
                parser = self.parsers[lang_name]
                tree = parser.parse(content.encode('utf-8'))
                
                # Check for parse errors
                if tree.root_node.has_error:
                    # Find the first error node
                    error_node = self._find_error_node(tree.root_node)
                    if error_node:
                        start_line = error_node.start_point[0] + 1  # Convert to 1-based
                        error_msg = f"{lang_name.title()} syntax error at line {start_line}"
                    else:
                        error_msg = f"{lang_name.title()} syntax error detected"
                    
                    logger.warning(f"[FileEditor] Tree-sitter validation failed for {file_path}: {error_msg}")
                    return False, error_msg
                
                logger.debug(f"[FileEditor] Tree-sitter validation passed for {file_path} ({lang_name})")
                return True, None
                
            except Exception as e:
                logger.warning(f"[FileEditor] Tree-sitter validation error for {file_path}: {e}")
                # Don't fail on parser errors, just skip validation
                return True, None
        
        # Language not supported for validation
        logger.debug(f"[FileEditor] No parser available for {lang_name}, skipping validation")
        return True, None

    def _find_error_node(self, node):
        """Recursively find the first error node in the parse tree."""
        if node.is_error:
            return node
        for child in node.children:
            error_node = self._find_error_node(child)
            if error_node:
                return error_node
        return None

    async def edit_file(self, file_path: str, new_content: str) -> EditResult:
        """Edits a file using either full replacement or chunk-based editing."""
        logger.info(f"[FileEditor] Editing file: {file_path}")
        
        # Start performance monitoring
        monitor = PerformanceMonitor() if self.enable_performance_monitoring else None
        if monitor:
            monitor.start_monitoring()
            
        try:
            # Resolve the path to prevent traversal attacks
            full_path = (self.project_root / file_path).resolve()

            # Security check: Ensure the resolved path is within the project root
            full_path.relative_to(self.project_root)

            if not full_path.is_file():
                err_msg = f"File not found at path: {full_path}"
                logger.warning(err_msg)
                return EditResult(
                    file_path=file_path, success=False, error_message=err_msg
                )

            # Read current content
            current_content = full_path.read_text(encoding="utf-8")
            
            # Determine edit strategy
            use_chunk_edit = self._should_use_chunk_edit(full_path, current_content)
            
            if use_chunk_edit:
                # Apply chunk-based editing
                final_content, patches_applied = self._apply_chunk_edit(current_content, new_content)
                edit_type = "chunk"
                logger.info(f"[FileEditor] Applied {patches_applied} patches using chunk editing")
            else:
                # Use full replacement
                final_content = new_content
                edit_type = "full"
                patches_applied = None
                logger.info(f"[FileEditor] Using full replacement for small file")

            # Validate syntax before writing
            validation_passed, validation_error = self._validate_syntax(final_content, file_path)
            if not validation_passed:
                logger.error(f"[FileEditor] Syntax validation failed for {file_path}: {validation_error}")
                return EditResult(
                    file_path=file_path, 
                    success=False, 
                    error_message=f"Syntax validation failed: {validation_error}",
                    edit_type=edit_type,
                    validation_passed=False
                )

            # Write the final content
            full_path.write_text(final_content, encoding="utf-8")
            logger.info(
                f"[FileEditor] Successfully wrote {len(final_content)} characters to {file_path} ({edit_type} edit)"
            )
            
            # Get performance metrics
            metrics = monitor.get_metrics() if monitor else None
            if metrics:
                logger.info(f"[FileEditor] Performance metrics: {metrics}")
            
            return EditResult(
                file_path=file_path, 
                success=True, 
                edit_type=edit_type, 
                changes_applied=patches_applied,
                performance_metrics=metrics,
                validation_passed=validation_passed
            )

        except ValueError:
            err_msg = (
                f"Security risk: Attempted to edit file outside of project root: {file_path}"
            )
            logger.error(err_msg)
            return EditResult(
                file_path=file_path, success=False, error_message=err_msg
            )
        except Exception as e:
            err_msg = f"Error writing to file {file_path}: {e}"
            logger.error(err_msg)
            return EditResult(
                file_path=file_path, success=False, error_message=err_msg
            )

    async def edit_file_with_chunks(self, file_path: str, new_content: str) -> EditResult:
        """Forces chunk-based editing regardless of file size."""
        logger.info(f"[FileEditor] Force chunk editing file: {file_path}")
        
        # Start performance monitoring
        monitor = PerformanceMonitor() if self.enable_performance_monitoring else None
        if monitor:
            monitor.start_monitoring()
            
        try:
            # Resolve the path to prevent traversal attacks
            full_path = (self.project_root / file_path).resolve()

            # Security check: Ensure the resolved path is within the project root
            full_path.relative_to(self.project_root)

            if not full_path.is_file():
                err_msg = f"File not found at path: {full_path}"
                logger.warning(err_msg)
                return EditResult(
                    file_path=file_path, success=False, error_message=err_msg
                )

            # Read current content
            current_content = full_path.read_text(encoding="utf-8")
            
            # Apply chunk-based editing
            final_content, patches_applied = self._apply_chunk_edit(current_content, new_content)
            
            # Validate syntax before writing
            validation_passed, validation_error = self._validate_syntax(final_content, file_path)
            if not validation_passed:
                logger.error(f"[FileEditor] Syntax validation failed for {file_path}: {validation_error}")
                return EditResult(
                    file_path=file_path, 
                    success=False, 
                    error_message=f"Syntax validation failed: {validation_error}",
                    edit_type="chunk",
                    validation_passed=False
                )
            
            # Write the final content
            full_path.write_text(final_content, encoding="utf-8")
            logger.info(
                f"[FileEditor] Successfully applied {patches_applied} patches to {file_path}"
            )
            
            # Get performance metrics
            metrics = monitor.get_metrics() if monitor else None
            if metrics:
                logger.info(f"[FileEditor] Performance metrics: {metrics}")
            
            return EditResult(
                file_path=file_path, 
                success=True, 
                edit_type="chunk", 
                changes_applied=patches_applied,
                performance_metrics=metrics,
                validation_passed=validation_passed
            )

        except ValueError:
            err_msg = (
                f"Security risk: Attempted to edit file outside of project root: {file_path}"
            )
            logger.error(err_msg)
            return EditResult(
                file_path=file_path, success=False, error_message=err_msg
            )
        except Exception as e:
            err_msg = f"Error writing to file {file_path}: {e}"
            logger.error(err_msg)
            return EditResult(
                file_path=file_path, success=False, error_message=err_msg
            )


def create_file_editor_tool(file_editor: FileEditor) -> Tool:
    """Factory function to create the file editor tool."""

    async def edit_existing_file(
        ctx: RunContext, file_path: str, new_content: str
    ) -> EditResult:
        """
        Edits a file using smart strategy (chunk-based for large files, full replacement for small files).
        Use this to modify existing files. The 'file_path' can be found
        from the 'path' property of nodes returned by the graph query tool.
        """
        return await file_editor.edit_file(file_path, new_content)

    return Tool(
        function=edit_existing_file,
        description="Edits an existing file using smart strategy (chunk-based for large files, full replacement for small files).",
    )


def create_chunk_editor_tool(file_editor: FileEditor) -> Tool:
    """Factory function to create the chunk-only file editor tool."""

    async def edit_file_with_chunks(
        ctx: RunContext, file_path: str, new_content: str
    ) -> EditResult:
        """
        Forces chunk-based editing regardless of file size.
        Use this when you want to apply incremental changes to preserve file structure.
        """
        return await file_editor.edit_file_with_chunks(file_path, new_content)

    return Tool(
        function=edit_file_with_chunks,
        description="Forces chunk-based editing for precise incremental changes.",
    )
