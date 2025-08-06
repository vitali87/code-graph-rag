"""
Shared constants for language parsing.

This module previously contained C++ operator constants and mappings,
but these have been replaced with Tree-sitter native AST node handling
for better performance and maintainability.

All processor components now use Tree-sitter's built-in operator recognition
via operator_name, binary_expression, and update_expression node types.
"""

# This file now serves as a placeholder for any future shared constants
# that cannot be handled directly by Tree-sitter AST nodes.
#
# The previous C++ operator mappings have been moved inline to the processors
# that use them, leveraging Tree-sitter's native operator node types instead
# of manual string parsing and mapping.
