"""Tree-sitter node types for SQL (PostgreSQL / PL/pgSQL dialect).

The grammar is DerekStride/tree-sitter-sql. Its `create_function` node names the
routine through an `object_reference` child rather than a `name` field, so the
FQN spec pairs these types with a dedicated name extractor.

Stored routines are the unit that matters here: in codebases where the business
logic lives in the database, a `usp_*` function is as much a callable as any
host-language function, and the call graph is incomplete without it.
"""

TS_SQL_PROGRAM = "program"
TS_SQL_CREATE_FUNCTION = "create_function"
TS_SQL_CREATE_PROCEDURE = "create_procedure"
TS_SQL_OBJECT_REFERENCE = "object_reference"
TS_SQL_INVOCATION = "invocation"
TS_SQL_FUNCTION_BODY = "function_body"
TS_SQL_STATEMENT = "statement"
