from typing import Any, Dict, Optional, Callable
from loguru import logger
from tree_sitter import Language, Parser, Node
from pathlib import Path
import difflib
from pydantic import BaseModel
from pydantic_ai import Tool
import diff_match_patch

# Define a type for the language library loaders
LanguageLoader = Callable[[], object]

# Import available Tree-sitter languages and correctly type them as Optional
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


LANGUAGE_LIBRARIES: Dict[str, Optional[LanguageLoader]] = {
    "python": python_language_so,
    "javascript": javascript_language_so,
    "typescript": typescript_language_so,
    "rust": rust_language_so,
    "go": go_language_so,
    "scala": scala_language_so,
    "java": java_language_so,
}

LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".scala": "scala",
}


class EditResult(BaseModel):
    """Data model for file edit results."""
    
    file_path: str
    success: bool
    error_message: str | None = None


class FileEditor:
    def __init__(self, project_root: str = ".") -> None:
        self.project_root = Path(project_root).resolve()
        self.parsers: Dict[str, Parser] = {}
        self.dmp = diff_match_patch.diff_match_patch()
        self._initialize_parsers()
        logger.info(f"FileEditor initialized with root: {self.project_root}")

    def _initialize_parsers(self) -> None:
        for lang_name, lang_lib in LANGUAGE_LIBRARIES.items():
            if lang_lib:
                try:
                    language = Language(lang_lib())
                    parser = Parser(language)
                    parser.language = language
                    self.parsers[lang_name] = parser
                except Exception as e:
                    logger.warning(f"Failed to load {lang_name} grammar: {e}")

    def get_parser(self, file_path: str) -> Optional[Parser]:
        file_path_obj = Path(file_path)
        extension = file_path_obj.suffix
        
        # Handle .tmp files by looking at the base name before .tmp
        if extension == '.tmp':
            # Get the extension before .tmp (e.g., test_file.py.tmp -> .py)
            base_name = file_path_obj.stem
            if '.' in base_name:
                extension = '.' + base_name.split('.')[-1]
        
        lang_name = LANGUAGE_EXTENSIONS.get(extension)
        if lang_name:
            return self.parsers.get(lang_name)
        return None

    def get_ast(self, file_path: str) -> Optional[Node]:
        parser = self.get_parser(file_path)
        if not parser:
            logger.warning(f"No parser available for {file_path}")
            return None
        
        with open(file_path, "rb") as f:
            content = f.read()
        
        tree = parser.parse(content)
        return tree.root_node

    def get_function_source_code(self, file_path: str, function_name: str) -> Optional[str]:
        root_node = self.get_ast(file_path)
        if not root_node:
            return None
        
        # Recursively search for function definitions
        def find_function_node(node):
            if node.type == "function_definition":
                # Get the function name node (first child after 'def')
                for child in node.children:
                    if child.type == "identifier" and child.text.decode('utf-8') == function_name:
                        return node
            
            # Recursively search children
            for child in node.children:
                result = find_function_node(child)
                if result:
                    return result
            return None
        
        function_node = find_function_node(root_node)
        if function_node:
            return function_node.text.decode('utf-8')
            
        return None

    def replace_function_source_code(self, file_path: str, function_name: str, new_code: str) -> bool:
        original_code = self.get_function_source_code(file_path, function_name)
        if not original_code:
            logger.error(f"Function '{function_name}' not found in {file_path}.")
            return False

        with open(file_path, 'r', encoding='utf-8') as f:
            original_content = f.read()
        
        # Create patches using diff-match-patch
        patches = self.dmp.patch_make(original_code, new_code)
        
        # Apply patches to the original content
        new_content, results = self.dmp.patch_apply(patches, original_content)
        
        # Check if all patches were applied successfully
        if not all(results):
            logger.warning(f"Some patches failed to apply cleanly for function '{function_name}'")
            # Fallback to simple string replacement
            new_content = original_content.replace(original_code, new_code)
        
        if original_content == new_content:
            logger.warning("No changes detected after replacement.")
            return False
            
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        logger.success(f"Successfully replaced function '{function_name}' in {file_path}.")
        return True

    def get_diff(self, file_path: str, function_name: str, new_code: str) -> Optional[str]:
        original_code = self.get_function_source_code(file_path, function_name)
        if not original_code:
            return None

        # Use diff-match-patch for more sophisticated diff generation
        diffs = self.dmp.diff_main(original_code, new_code)
        self.dmp.diff_cleanupSemantic(diffs)
        
        # Convert to unified diff format for readability
        diff = difflib.unified_diff(
            original_code.splitlines(keepends=True),
            new_code.splitlines(keepends=True),
            fromfile=f"original/{function_name}",
            tofile=f"new/{function_name}",
        )
        return "".join(diff)

    def apply_patch_to_file(self, file_path: str, patch_text: str) -> bool:
        """Apply a patch to a file using diff-match-patch."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                original_content = f.read()
            
            # Parse the patch
            patches = self.dmp.patch_fromText(patch_text)
            
            # Apply the patch
            new_content, results = self.dmp.patch_apply(patches, original_content)
            
            # Check if all patches were applied successfully
            if not all(results):
                logger.warning(f"Some patches failed to apply cleanly to {file_path}")
                return False
            
            # Write the updated content
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            logger.success(f"Successfully applied patch to {file_path}")
            return True
            
        except Exception as e:
            logger.error(f"Error applying patch to {file_path}: {e}")
            return False

    async def edit_file(self, file_path: str, new_content: str) -> EditResult:
        """Overwrites the content of a specified file with new content."""
        logger.info(f"[FileEditor] Attempting to edit file: {file_path}")
        try:
            full_path = (self.project_root / file_path).resolve()
            full_path.relative_to(self.project_root)  # Security check
            
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            logger.success(f"[FileEditor] Successfully edited file: {file_path}")
            return EditResult(file_path=file_path, success=True)
            
        except ValueError:
            error_msg = "Security risk: Attempted to edit file outside of project root."
            logger.error(f"[FileEditor] {error_msg}")
            return EditResult(file_path=file_path, success=False, error_message=error_msg)
        except Exception as e:
            error_msg = f"An unexpected error occurred: {e}"
            logger.error(f"[FileEditor] Error editing file {file_path}: {e}")
            return EditResult(file_path=file_path, success=False, error_message=error_msg)


def create_file_editor_tool(file_editor: FileEditor) -> Tool:
    """Factory function to create the file editor tool."""

    async def edit_existing_file(file_path: str, new_content: str) -> str:
        """
        Overwrites the content of a specified file with new content.
        Use this to modify existing files. The 'file_path' can be found
        from the 'path' property of nodes returned by the graph query tool.
        """
        result = await file_editor.edit_file(file_path, new_content)
        if result.success:
            return f"Successfully edited file: {file_path}"
        else:
            return f"Error editing file: {result.error_message}"

    return Tool(
        function=edit_existing_file,
        description="Overwrites an existing file with new content. Use with caution.",
    )
