"""Shared utilities for extracting source code from files."""

from pathlib import Path
from typing import Optional

from loguru import logger


def extract_source_lines(
    file_path: Path, 
    start_line: int, 
    end_line: int, 
    encoding: str = 'utf-8'
) -> Optional[str]:
    """Extract source code lines from a file.
    
    This utility function provides the common line-based source extraction
    logic used by multiple components in the codebase.
    
    Args:
        file_path: Path to the source file
        start_line: Start line number (1-based indexing)
        end_line: End line number (1-based indexing, inclusive)
        encoding: File encoding (default: utf-8)
        
    Returns:
        Extracted source code as string, or None if extraction fails
        
    Raises:
        None - All exceptions are caught and logged
    """
    if not file_path.exists():
        logger.warning(f"Source file not found: {file_path}")
        return None
        
    if start_line < 1 or end_line < 1 or start_line > end_line:
        logger.warning(f"Invalid line range: {start_line}-{end_line}")
        return None
    
    try:
        with open(file_path, 'r', encoding=encoding) as f:
            lines = f.readlines()
            
            # Validate line range against file content
            if start_line > len(lines) or end_line > len(lines):
                logger.warning(
                    f"Line range {start_line}-{end_line} exceeds file length "
                    f"{len(lines)} in {file_path}"
                )
                return None
            
            # Extract lines (convert to 0-based indexing)
            extracted_lines = lines[start_line-1:end_line]
            return ''.join(extracted_lines).strip()
            
    except Exception as e:
        logger.warning(f"Failed to extract source from {file_path}: {e}")
        return None


def extract_source_with_fallback(
    file_path: Path,
    start_line: int,
    end_line: int,
    qualified_name: Optional[str] = None,
    ast_extractor: Optional[callable] = None,
    encoding: str = 'utf-8'
) -> Optional[str]:
    """Extract source code with AST-based extraction and line-based fallback.
    
    This function provides a pattern commonly used across the codebase:
    1. Try AST-based extraction (if ast_extractor provided)
    2. Fall back to line-based extraction
    
    Args:
        file_path: Path to the source file
        start_line: Start line number (1-based indexing)
        end_line: End line number (1-based indexing, inclusive)
        qualified_name: Function qualified name (for AST extraction)
        ast_extractor: Optional function for AST-based extraction
        encoding: File encoding (default: utf-8)
        
    Returns:
        Extracted source code as string, or None if extraction fails
    """
    # Try AST-based extraction first (if available)
    if ast_extractor and qualified_name:
        try:
            ast_result = ast_extractor(qualified_name, file_path)
            if ast_result:
                return ast_result
        except Exception as e:
            logger.debug(f"AST extraction failed for {qualified_name}: {e}")
    
    # Fallback to line-based extraction
    return extract_source_lines(file_path, start_line, end_line, encoding)


def validate_source_location(
    file_path: Optional[str], 
    start_line: Optional[int], 
    end_line: Optional[int]
) -> tuple[bool, Optional[Path]]:
    """Validate source location parameters.
    
    Args:
        file_path: File path string (may be None)
        start_line: Start line number (may be None) 
        end_line: End line number (may be None)
        
    Returns:
        Tuple of (is_valid, path_object)
    """
    if not all([file_path, start_line, end_line]):
        return False, None
        
    try:
        path_obj = Path(file_path)
        return True, path_obj
    except Exception:
        return False, None