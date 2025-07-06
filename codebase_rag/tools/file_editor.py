from typing import Optional
from loguru import logger
from tree_sitter import Parser, Node
from pathlib import Path
import difflib
from pydantic import BaseModel
from pydantic_ai import Tool
import diff_match_patch
from ..language_config import get_language_config
from ..parser_loader import load_parsers

LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".scala": "scala",
    ".cpp": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
}


class EditResult(BaseModel):
    """Data model for file edit results."""

    file_path: str
    success: bool
    error_message: str | None = None


class FileEditor:
    def __init__(self, project_root: str = ".") -> None:
        self.project_root = Path(project_root).resolve()
        self.dmp = diff_match_patch.diff_match_patch()
        # Load parsers using the shared parser loader
        self.parsers, _ = load_parsers()
        logger.info(f"FileEditor initialized with root: {self.project_root}")

    def _get_real_extension(self, file_path_obj: Path) -> str:
        """Gets the file extension, looking past a .tmp suffix if present."""
        extension = file_path_obj.suffix
        if extension == ".tmp":
            # Get the extension before .tmp (e.g., test_file.py.tmp -> .py)
            base_name = file_path_obj.stem
            if "." in base_name:
                return "." + base_name.split(".")[-1]
        return extension

    def get_parser(self, file_path: str) -> Optional[Parser]:
        file_path_obj = Path(file_path)
        extension = self._get_real_extension(file_path_obj)

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

    def get_function_source_code(
        self, file_path: str, function_name: str, line_number: Optional[int] = None
    ) -> Optional[str]:
        root_node = self.get_ast(file_path)
        if not root_node:
            return None

        # Get language config for this file
        file_path_obj = Path(file_path)
        extension = self._get_real_extension(file_path_obj)

        lang_config = get_language_config(extension)
        if not lang_config:
            logger.warning(f"No language config found for extension {extension}")
            return None

        # Find all matching functions with their context
        matching_functions = []
        
        def find_function_nodes(node, parent_class=None):
            if node.type in lang_config.function_node_types:
                # Get the function name node using the 'name' field
                name_node = node.child_by_field_name("name")
                if name_node and name_node.text:
                    func_name = name_node.text.decode("utf-8")
                    
                    # Check if this matches our target function
                    qualified_name = f"{parent_class}.{func_name}" if parent_class else func_name
                    
                    # Match either simple name or qualified name
                    if func_name == function_name or qualified_name == function_name:
                        matching_functions.append({
                            'node': node,
                            'simple_name': func_name,
                            'qualified_name': qualified_name,
                            'parent_class': parent_class,
                            'line_number': node.start_point[0] + 1  # 1-based line numbers
                        })
                    
                    # Don't recurse into function bodies for nested functions
                    return
            
            # Check if this is a class node to track context
            current_class = parent_class
            if node.type in lang_config.class_node_types:
                name_node = node.child_by_field_name("name")
                if name_node and name_node.text:
                    current_class = name_node.text.decode("utf-8")

            # Recursively search children
            for child in node.children:
                find_function_nodes(child, current_class)

        find_function_nodes(root_node)

        # Handle different matching scenarios
        if not matching_functions:
            return None
        elif len(matching_functions) == 1:
            return matching_functions[0]['node'].text.decode("utf-8")
        else:
            # Multiple functions found - try different disambiguation strategies
            
            # Strategy 1: Match by line number if provided
            if line_number is not None:
                for func in matching_functions:
                    if func['line_number'] == line_number:
                        return func['node'].text.decode("utf-8")
                logger.warning(f"No function '{function_name}' found at line {line_number}")
                return None
            
            # Strategy 2: Match by qualified name if function_name contains dot
            if '.' in function_name:
                for func in matching_functions:
                    if func['qualified_name'] == function_name:
                        return func['node'].text.decode("utf-8")
                logger.warning(f"No function found with qualified name '{function_name}'")
                return None
            
            # Strategy 3: Log ambiguity warning with details and return first match
            function_details = []
            for func in matching_functions:
                details = f"'{func['qualified_name']}' at line {func['line_number']}"
                function_details.append(details)
            
            logger.warning(
                f"Ambiguous function name '{function_name}' in {file_path}. "
                f"Found {len(matching_functions)} matches: {', '.join(function_details)}. "
                f"Using first match. Consider using qualified name (e.g., 'ClassName.{function_name}') "
                f"or specify line number for precise targeting."
            )
            
            # Return the first match but warn the user
            return matching_functions[0]['node'].text.decode("utf-8")

    def replace_function_source_code(
        self, file_path: str, function_name: str, new_code: str, line_number: Optional[int] = None
    ) -> bool:
        original_code = self.get_function_source_code(file_path, function_name, line_number)
        if not original_code:
            logger.error(f"Function '{function_name}' not found in {file_path}.")
            return False

        with open(file_path, "r", encoding="utf-8") as f:
            original_content = f.read()

        # Create patches using diff-match-patch
        patches = self.dmp.patch_make(original_code, new_code)

        # Apply patches to the original content
        new_content, results = self.dmp.patch_apply(patches, original_content)

        # Check if all patches were applied successfully
        if not all(results):
            logger.warning(
                f"Patches for function '{function_name}' did not apply cleanly."
            )
            return False

        if original_content == new_content:
            logger.warning("No changes detected after replacement.")
            return False

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        logger.success(
            f"Successfully replaced function '{function_name}' in {file_path}."
        )
        return True

    def get_diff(
        self, file_path: str, function_name: str, new_code: str, line_number: Optional[int] = None
    ) -> Optional[str]:
        original_code = self.get_function_source_code(file_path, function_name, line_number)
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
            with open(file_path, "r", encoding="utf-8") as f:
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
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            logger.success(f"Successfully applied patch to {file_path}")
            return True

        except Exception as e:
            logger.error(f"Error applying patch to {file_path}: {e}")
            return False

    def _display_colored_diff(
        self, original_content: str, new_content: str, file_path: str
    ) -> None:
        """Display a concise colored diff with limited context."""
        # Generate diffs using diff-match-patch
        diffs = self.dmp.diff_main(original_content, new_content)
        self.dmp.diff_cleanupSemantic(diffs)

        # ANSI color codes
        RED = "\033[31m"
        GREEN = "\033[32m"
        CYAN = "\033[36m"
        GRAY = "\033[90m"
        RESET = "\033[0m"

        print(f"\n{CYAN}Changes to {file_path}:{RESET}")

        CONTEXT_LINES = 5  # Show 5 lines before/after changes
        
        # Process diffs to show limited context
        for op, text in diffs:
            # Use keepends=True to preserve newlines for accurate rendering
            lines = text.splitlines(keepends=True)
            
            if op == self.dmp.DIFF_DELETE:
                for line in lines:
                    # rstrip to remove the trailing newline for cleaner printing
                    print(f"{RED}- {line.rstrip()}{RESET}")
            elif op == self.dmp.DIFF_INSERT:
                for line in lines:
                    print(f"{GREEN}+ {line.rstrip()}{RESET}")
            elif op == self.dmp.DIFF_EQUAL:
                # For unchanged sections, show limited context
                if len(lines) > CONTEXT_LINES * 2:
                    # Show first few lines
                    for line in lines[:CONTEXT_LINES]:
                        print(f"  {line.rstrip()}")
                    
                    # Show truncation indicator if there are many lines
                    omitted_count = len(lines) - (CONTEXT_LINES * 2)
                    if omitted_count > 0:
                        print(f"{GRAY}  ... ({omitted_count} lines omitted) ...{RESET}")
                    
                    # Show last few lines
                    for line in lines[-CONTEXT_LINES:]:
                        print(f"  {line.rstrip()}")
                else:
                    # Show all lines if not too many
                    for line in lines:
                        print(f"  {line.rstrip()}")
        
        print()  # Extra newline for spacing

    def replace_code_block(self, file_path: str, target_block: str, replacement_block: str) -> bool:
        """Surgically replace a specific code block in a file using diff-match-patch."""
        logger.info(f"[FileEditor] Attempting surgical block replacement in: {file_path}")
        try:
            full_path = (self.project_root / file_path).resolve()
            full_path.relative_to(self.project_root)  # Security check

            if not full_path.is_file():
                logger.error(f"File not found: {file_path}")
                return False
            
            # Read original content
            with open(full_path, "r", encoding="utf-8") as f:
                original_content = f.read()

            # Find the target block in the file
            if target_block not in original_content:
                logger.error(f"Target block not found in {file_path}")
                logger.debug(f"Looking for: {repr(target_block)}")
                return False

            # Create surgical patch - replace only the target block
            modified_content = original_content.replace(target_block, replacement_block, 1)
            
            # Verify only one replacement was made
            if original_content.count(target_block) > 1:
                logger.warning(f"Multiple occurrences of target block found. Only replacing first occurrence.")
            
            if original_content == modified_content:
                logger.warning("No changes detected - target and replacement are identical")
                return False

            # Display the surgical diff
            self._display_colored_diff(original_content, modified_content, file_path)

            # Create and apply surgical patches
            patches = self.dmp.patch_make(original_content, modified_content)
            patched_content, results = self.dmp.patch_apply(patches, original_content)

            if not all(results):
                logger.error("Surgical patches failed to apply cleanly")
                return False

            # Write the surgically modified content
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(patched_content)

            logger.success(f"[FileEditor] Successfully applied surgical block replacement in: {file_path}")
            return True

        except ValueError:
            logger.error("Security risk: Attempted to edit file outside of project root.")
            return False
        except Exception as e:
            logger.error(f"Error during surgical block replacement: {e}")
            return False

    async def edit_file(self, file_path: str, new_content: str) -> EditResult:
        """Overwrites entire file with new content - use for full file replacement."""
        logger.info(f"[FileEditor] Attempting full file replacement: {file_path}")
        try:
            full_path = (self.project_root / file_path).resolve()
            full_path.relative_to(self.project_root)  # Security check

            # Check if the file exists and is a file before proceeding
            if not full_path.is_file():
                error_msg = f"File not found or is a directory: {file_path}"
                logger.warning(f"[FileEditor] {error_msg}")
                return EditResult(file_path=file_path, success=False, error_message=error_msg)
            
            # Read original content to show diff
            with open(full_path, "r", encoding="utf-8") as f:
                original_content = f.read()

            # Display colored diff
            if original_content != new_content:
                self._display_colored_diff(original_content, new_content, file_path)

            # Write new content (full replacement)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            logger.success(f"[FileEditor] Successfully replaced entire file: {file_path}")
            return EditResult(file_path=file_path, success=True)

        except ValueError:
            error_msg = "Security risk: Attempted to edit file outside of project root."
            logger.error(f"[FileEditor] {error_msg}")
            return EditResult(
                file_path=file_path, success=False, error_message=error_msg
            )
        except Exception as e:
            error_msg = f"An unexpected error occurred: {e}"
            logger.error(f"[FileEditor] Error editing file {file_path}: {e}")
            return EditResult(
                file_path=file_path, success=False, error_message=error_msg
            )


def create_file_editor_tool(file_editor: FileEditor) -> Tool:
    """Factory function to create the file editor tool."""

    async def replace_code_surgically(file_path: str, target_code: str, replacement_code: str) -> str:
        """
        Surgically replaces a specific code block in a file using diff-match-patch.
        This tool finds the exact target code block and replaces only that section,
        leaving the rest of the file completely unchanged. This is true surgical patching.
        
        Args:
            file_path: Path to the file to modify
            target_code: The exact code block to find and replace (must match exactly)
            replacement_code: The new code to replace the target with
            
        Use this when you need to change specific functions, classes, or code blocks
        without affecting the rest of the file. The target_code must be an exact match.
        """
        success = file_editor.replace_code_block(file_path, target_code, replacement_code)
        if success:
            return f"Successfully applied surgical code replacement in: {file_path}"
        else:
            return f"Failed to apply surgical replacement in {file_path}. Target code not found or patches failed."

    return Tool(
        function=replace_code_surgically,
        description="Surgically replaces specific code blocks in files. Requires exact target code and replacement. Only modifies the specified block, leaving rest of file unchanged. True surgical patching.",
    )
