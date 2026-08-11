from typing import TYPE_CHECKING, NamedTuple

from loguru import logger

from .cypher_queries import (
    CYPHER_EXAMPLE_CLASS_METHODS,
    CYPHER_EXAMPLE_CODE_SMELLS,
    CYPHER_EXAMPLE_CONTENT_BY_PATH,
    CYPHER_EXAMPLE_DECORATED_FUNCTIONS,
    CYPHER_EXAMPLE_FILES_IN_FOLDER,
    CYPHER_EXAMPLE_FIND_FILE,
    CYPHER_EXAMPLE_FIND_PATTERN,
    CYPHER_EXAMPLE_KEYWORD_SEARCH,
    CYPHER_EXAMPLE_LIMIT_ONE,
    CYPHER_EXAMPLE_PROJECT_SCOPED,
    CYPHER_EXAMPLE_PYTHON_FILES,
    CYPHER_EXAMPLE_README,
    CYPHER_EXAMPLE_SECURITY_ISSUES,
    CYPHER_EXAMPLE_TASKS,
)
from .schema_builder import GRAPH_SCHEMA_DEFINITION
from .tools.tool_descriptions import AgenticToolName
from .types_defs import ToolNames

if TYPE_CHECKING:
    from pydantic_ai import Tool


def extract_tool_names(tools: list["Tool"]) -> ToolNames:
    registered = {t.name for t in tools}

    def resolve_tool_name(canonical: AgenticToolName) -> str | None:
        if canonical not in registered:
            # Updated warning message as per Resolution Plan #2
            logger.warning(
                f"Tool '{canonical}' is not registered on the agent; "
                "its instructions will be omitted from the orchestrator prompt."
            )
            return None
        return str(canonical)

    # All fields must now explicitly handle None, reflecting the types_defs.py change.
    return ToolNames(
        query_graph=resolve_tool_name(AgenticToolName.QUERY_GRAPH),
        read_file=resolve_tool_name(AgenticToolName.READ_FILE),
        semantic_search=resolve_tool_name(AgenticToolName.SEMANTIC_SEARCH),
        create_file=resolve_tool_name(AgenticToolName.CREATE_FILE),
        edit_file=resolve_tool_name(AgenticToolName.REPLACE_CODE),
        shell_command=resolve_tool_name(AgenticToolName.EXECUTE_SHELL),
    )


CYPHER_QUERY_RULES = """**2. Critical Cypher Query Rules**

- **ALWAYS Return Specific Properties with Aliases**: Do NOT return whole nodes (e.g., `RETURN n`). You MUST return specific properties with clear aliases (e.g., `RETURN n.name AS name`).
- **Use `STARTS WITH` for Paths**: When matching paths, always use `STARTS WITH` for robustness (e.g., `WHERE n.path STARTS WITH 'workflows/src'`). Do not use `=`.
- **Use `ENDS WITH` for qualified_name**: The `qualified_name` property contains full paths like `'Project.folder.subfolder.ClassName'`. When users mention a class, function, or method by its short name (e.g., "VatManager"), use `ENDS WITH` to match: `WHERE c.qualified_name ENDS WITH '.VatManager'`. Do NOT use `{name: 'VatManager'}` equality matching.
- **Use `toLower()` for Searches**: For case-insensitive searching on string properties, use `toLower()`.
- **Querying Lists**: To check if a list property (like `decorators`) contains an item, use the `ANY` or `IN` clause (e.g., `WHERE 'flow' IN n.decorators`).
- **NEVER use unbounded variable-length paths**: Patterns like `[:CALLS*]`, `[*]`, `[:CALLS*1..]` enumerate every path in the graph and exhaust memory. Always cap with an upper bound, e.g. `[:CALLS*1..6]`. If you genuinely need unbounded reachability, use a MAGE procedure (see Section 2b) instead of variable-length Cypher.

**2b. Graph Algorithm Procedures (MAGE)**

For algorithmic questions (longest/shortest paths, cycles, recursion clusters, centrality, communities, reachability), prefer calling a MAGE procedure over writing variable-length Cypher. Cypher path patterns enumerate all matches with no memoization, so they OOM on cyclic graphs; MAGE procedures run real graph algorithms in bounded memory.

Use these read-only procedures (call them with `CALL <procedure>(...) YIELD ... RETURN ...`):

- **Strongly connected components / recursion clusters**: `CALL nxalg.strongly_connected_components() YIELD components`
- **Weakly connected components**: `CALL weakly_connected_components.get() YIELD node, component_id` or `CALL wcc.get_components(nodes, edges)`
- **Cycles**: `CALL nxalg.simple_cycles() YIELD cycles` (all cycles), `CALL nxalg.find_cycle() YIELD cycle` (one cycle)
- **All simple paths between two nodes (bounded)**: `CALL nxalg.all_simple_paths(source, target, cutoff)` or `CALL algo.all_simple_paths(source, target, [:CALLS], maxHops)`
- **Shortest path**: `CALL nxalg.shortest_path(source, target)` or `CALL algo.astar(source, target, config)`
- **Reachability**: `CALL graph_util.ancestors(node)`, `CALL graph_util.descendants(node)`
- **Topological order (DAGs only)**: `CALL nxalg.topological_sort() YIELD nodes` or `CALL graph_util.topological_sort()`
- **PageRank**: `CALL pagerank.get() YIELD node, rank` or `CALL nxalg.pagerank() YIELD node, rank`
- **Betweenness centrality**: `CALL betweenness_centrality.get() YIELD node, betweenness_centrality`
- **Degree centrality**: `CALL degree_centrality.get() YIELD node, degree`
- **Communities**: `CALL community_detection.get() YIELD node, community_id`, `CALL leiden_community_detection.get() YIELD node, community_id`
- **Articulation / bridges**: `CALL bridges.get() YIELD ...`, `CALL nxalg.biconnected_components() YIELD nodes`
- **Dominators**: `CALL nxalg.immediate_dominators(start) YIELD node, dominator`
- **Path expansion (bounded BFS over filtered edges)**: `CALL path.expand(start, relationships, labels, minHops, maxHops) YIELD path`

Important: MAGE procedures named `nxalg.*` and several others operate on the **entire graph**, ignoring edge-type filters. To restrict to a specific edge type (e.g., only `CALLS`), follow the procedure call with a `WHERE` clause that checks `EXISTS((a)-[:CALLS]->(b))` or use `path.expand` which accepts a relationship-type filter.

**2c. When Cypher Can't Answer**

If a question cannot be expressed as a bounded Cypher pattern or as a single MAGE procedure call (e.g., "longest call chain in a graph with cycles"), return your best bounded approximation rather than an unbounded path query. Examples:
- "longest call chain" → `CALL nxalg.strongly_connected_components() YIELD components RETURN components` (let the orchestrator post-process), or use `CALL path.expand` with a generous but finite `maxHops`.
- "find a deeply-nested call site" → use a bounded depth such as `[:CALLS*1..10]` with `ORDER BY ... LIMIT 1`."""


def build_graph_schema_and_rules() -> str:
    return f"""You are an expert AI assistant for analyzing codebases using a **hybrid retrieval system**: a **Memgraph knowledge graph** for structural queries and a **semantic code search engine** for intent-based discovery.

**1. Graph Schema Definition**
The database contains information about a codebase, structured with the following nodes and relationships.

{GRAPH_SCHEMA_DEFINITION}

{CYPHER_QUERY_RULES}
"""


GRAPH_SCHEMA_AND_RULES = build_graph_schema_and_rules()


def _format_active_projects_block(active_projects: list[str] | None) -> str:
    if not active_projects:
        return (
            "\n**Project Scope**: This Memgraph database may contain multiple "
            "indexed projects. Call `list_projects` early to enumerate them, then "
            "scope graph queries by filtering on the `qualified_name` prefix "
            "(e.g., `WHERE n.qualified_name STARTS WITH 'projectName.'`).\n"
        )
    if len(active_projects) == 1:
        return (
            f"\n**Project Scope**: This session is focused on the project "
            f"`{active_projects[0]}`. Scope Cypher queries by filtering on "
            f"`WHERE n.qualified_name STARTS WITH '{active_projects[0]}.'` "
            "unless the user explicitly asks about other projects.\n"
        )
    project_list = ", ".join(f"`{p}`" for p in active_projects)
    starts_with_examples = " OR ".join(
        f"n.qualified_name STARTS WITH '{p}.'" for p in active_projects
    )
    return (
        f"\n**Project Scope**: This session spans the following projects: "
        f"{project_list}. When users ask cross-project questions, query across "
        "all of them. To restrict to one project, filter "
        f"`n.qualified_name STARTS WITH '<projectName>.'`. To restrict to the "
        f"active set, filter with `{starts_with_examples}`.\n"
    )


def build_rag_orchestrator_prompt(
    tools: list["Tool"],
    project_instructions: str | None = None,
    active_projects: list[str] | None = None,
) -> str:
    t = extract_tool_names(tools)
    # Determine tool availability
    has_query = t.query_graph is not None
    has_read = t.read_file is not None
    has_semantic = t.semantic_search is not None
    has_create = t.create_file is not None
    has_edit = t.edit_file is not None
    has_shell = t.shell_command is not None

    prompt_sections = []

    # --- Initial Fixed Section ---
    prompt_sections.append("""You are an expert AI assistant for analyzing codebases. Your answers are based **EXCLUSIVELY** on information retrieved using your tools.

**CRITICAL RULES:**
1.  **TOOL-ONLY ANSWERS**: You must ONLY use information from the tools provided. Do not use external knowledge.""")

    # --- Conditional Rule 2: Natural Language Queries for Query Graph ---
    if has_query:
        prompt_sections.append(f"""2.  **NATURAL LANGUAGE QUERIES**: When using the `{t.query_graph}` tool, ALWAYS use natural language questions. NEVER write Cypher queries directly - the tool will translate your natural language into the appropriate database query.""")

    # --- Fixed Rule 3 ---
    prompt_sections.append("""3.  **HONESTY**: If a tool fails or returns no results, you MUST state that clearly and report any error messages. Do not invent answers.""")

    # --- Conditional Rule 4: Choose the Right Tool for File Type ---
    rule_4_lines = ["4.  **CHOOSE THE RIGHT TOOL FOR THE FILE TYPE**:"]
    if has_read:
        rule_4_lines.append(f"    - For source code files (.py, .ts, etc.), use `{t.read_file}`.")
    rule_4_lines.append("    - Images and PDFs the user references are attached inline to the message; read them directly from your own multimodal input.")
    prompt_sections.append("\n".join(rule_4_lines))

    # --- General Approach Fixed Section (up to 2d) ---
    prompt_sections.append("""
**Your General Approach:**
1.  **Inspect Attached Media Directly**: When the user attaches an image or PDF, analyze it from the inline content of the message. Do not call a tool for it.
2.  **Deep Dive into Code**: When you identify a relevant component (e.g., a folder), you must go beyond documentation.
    a. First, check if documentation files like `README.md` exist and read them for context. For configuration, look for files appropriate to the language (e.g., `pyproject.toml` for Python, `package.json` for Node.js).
    b. **Then, you MUST dive into the source code.** Explore the `src` directory (or equivalent). Identify and read key files (e.g., `main.py`, `index.ts`, `app.ts`) to understand the implementation details, logic, and functionality.
    c. Synthesize all this information—from documentation, configuration, and the code itself—to provide a comprehensive, factual answer. Do not just describe the files; explain what the code *does*.
    d. Only ask for clarification if, after a thorough investigation, the user's intent is still unclear.""")

    # --- Conditional General Approach 3: Choose the Right Search Strategy ---
    search_strategy_lines = []
    if has_semantic or has_query or has_read: # Only include this section if there's *any* search/read capability
        search_strategy_lines.append("3.  **Choose the Right Search Strategy**:")

        # 3a. When to use Semantic Search first, or general find code instruction if only read available
        if has_semantic:
            search_strategy_lines.append(f"""    a. **WHEN TO USE SEMANTIC SEARCH FIRST**: Always start with `{t.semantic_search}` for ANY of these patterns:
           - "main entry point", "startup", "initialization", "bootstrap", "launcher"
           - "error handling", "validation", "authentication"
           - "where is X done", "how does Y work", "find Z logic"
           - Any question about PURPOSE, INTENT, or FUNCTIONALITY

           **Entry Point Recognition Patterns**:
           - Python: `if __name__ == "__main__"`, `main()` function, CLI scripts, `app.run()`
           - JavaScript/TypeScript: `index.js`, `main.ts`, `app.js`, `server.js`, package.json scripts
           - Java: `public static void main`, `@SpringBootApplication`
           - C/C++: `int main()`, `WinMain`
           - Web: `index.html`, routing configurations, startup middleware""")
        elif has_read: # If no semantic search, guide to direct read for finding code
            search_strategy_lines.append(f"""    a. **WHEN TO FIND CODE**: Directly identify and read relevant files using `{t.read_file}` or rely on direct context if no other search tool is available.""")

        # 3b. When to use Graph Directly, or how to handle structural queries without graph
        if has_query:
            search_strategy_lines.append(f"""    b. **WHEN TO USE GRAPH DIRECTLY**: Only use `{t.query_graph}` directly for pure structural queries:
           - "What does function X call?" (when you already know X's name)
           - "List methods of User class" (when you know the exact class name)
           - "Show files in folder Y" (when you know the exact folder path)""")
        elif has_semantic and has_read: # No graph, but semantic and read for structural
             search_strategy_lines.append(f"""    b. **STRUCTURAL QUERIES**: For questions about code structure (e.g., "list files in folder Y", "what methods does class X have"), you must first use `{t.semantic_search}` with highly specific terms to locate relevant files or definitions, then use `{t.read_file}` to examine their contents.""")
        elif has_read: # Only read for structural
            search_strategy_lines.append(f"""    b. **STRUCTURAL QUERIES**: For questions about code structure (e.g., "list files in folder Y", "what methods does class X have"), you must identify relevant files and read them using `{t.read_file}`.""")

        # 3c. Hybrid/Combined Approach
        if has_semantic and has_query and has_read:
            search_strategy_lines.append(f"""    c. **HYBRID APPROACH (RECOMMENDED)**: For most queries, use this sequence:
           1. Use `{t.semantic_search}` to find relevant code elements by intent/meaning
           2. Then use `{t.query_graph}` to explore structural relationships
           3. **CRITICAL**: Always read the actual files using `{t.read_file}` to examine source code
           4. For entry points specifically: Look for `if __name__ == "__main__"`, `main()` functions, or CLI entry points""")
        elif has_semantic and has_read:
            search_strategy_lines.append(f"""    c. **SEMANTIC + READ APPROACH (RECOMMENDED)**: For most queries, use this sequence:
           1. Use `{t.semantic_search}` to find relevant code elements by intent/meaning
           2. **CRITICAL**: Always read the actual files using `{t.read_file}` to examine source code
           3. For entry points specifically: Look for `if __name__ == "__main__"`, `main()` functions, or CLI entry points""")
        elif has_query and has_read:
            search_strategy_lines.append(f"""    c. **GRAPH + READ APPROACH (RECOMMENDED)**: For most queries, use this sequence:
           1. Use `{t.query_graph}` to explore structural relationships directly
           2. **CRITICAL**: Always read the actual files using `{t.read_file}` to examine source code
           3. For entry points specifically: Look for `if __name__ == "__main__"`, `main()` functions, or CLI entry points""")
        elif has_read: # Only read available
            search_strategy_lines.append(f"""    c. **DIRECT READ APPROACH (RECOMMENDED)**: For most queries, identify and read relevant files using `{t.read_file}`.""")

        # 3d. Tool Chaining Example
        if has_semantic and has_query and has_read:
            search_strategy_lines.append(f"""    d. **Tool Chaining Example**: For "main entry point and what it calls":
           1. `{t.semantic_search}` for focused terms like "main entry startup" (not overly broad)
           2. `{t.query_graph}` to find specific function relationships
           3. `{t.read_file}` for main.py with targeted sections (use offset/limit for large files)
           4. Look for the true application entry point (main function, __main__ block, CLI commands)
           5. If you find CLI frameworks (typer, click, argparse), read relevant command sections only
           6. Summarize execution flow concisely rather than showing all details""")
        elif has_semantic and has_read:
            search_strategy_lines.append(f"""    d. **Tool Chaining Example**: For "main entry point and what it does":
           1. `{t.semantic_search}` for focused terms like "main entry startup" (not overly broad)
           2. `{t.read_file}` for main.py with targeted sections (use offset/limit for large files)
           3. Look for the true application entry point (main function, __main__ block, CLI commands)
           4. If you find CLI frameworks (typer, click, argparse), read relevant command sections only
           5. Summarize execution flow concisely rather than showing all details""")
        elif has_query and has_read:
            search_strategy_lines.append(f"""    d. **Tool Chaining Example**: For "main entry point and what it calls":
           1. `{t.query_graph}` to find specific function relationships starting from a known entry file or function
           2. `{t.read_file}` for main.py with targeted sections (use offset/limit for large files)
           3. Look for the true application entry point (main function, __main__ block, CLI commands)
           4. If you find CLI frameworks (typer, click, argparse), read relevant command sections only
           5. Summarize execution flow concisely rather than showing all details""")
        elif has_read: # Only read available
            search_strategy_lines.append(f"""    d. **Tool Chaining Example**: For "main entry point":
           1. `{t.read_file}` for main.py with targeted sections (use offset/limit for large files)
           2. Look for the true application entry point (main function, __main__ block, CLI commands)
           3. If you find CLI frameworks (typer, click, argparse), read relevant command sections only
           4. Summarize execution flow concisely rather than showing all details""")

    if search_strategy_lines: # Add this section only if it has content
        prompt_sections.append("\n".join(search_strategy_lines))

    # --- Conditional General Approach 4: Plan Before Writing or Modifying ---
    prompt_sections.append("4.  **Plan Before Writing or Modifying**:")
    create_edit_tools_str = []
    if has_create:
        create_edit_tools_str.append(f"`{t.create_file}`")
    if has_edit:
        create_edit_tools_str.append(f"`{t.edit_file}`")

    if create_edit_tools_str:
        prompt_sections.append(f"    a. Before using {', '.join(create_edit_tools_str)}, or modifying files, you MUST explore the codebase to find the correct location and file structure.")
    else:
        prompt_sections.append("    a. Before modifying files, you MUST explore the codebase to find the correct location and file structure.")

    # --- Conditional General Approach 5: Execute Shell Commands ---
    if has_shell:
        prompt_sections.append(f"""    b. For shell commands: If `{t.shell_command}` returns a confirmation message (return code -2), immediately return that exact message to the user. When they respond "yes", call the tool again with `user_confirmed=True`.
5.  **Execute Shell Commands**: The `{t.shell_command}` tool handles dangerous command confirmations automatically. If it returns a confirmation prompt, pass it directly to the user.""")

    # --- Conditional General Approach 6: Complete the Investigation Cycle ---
    investigation_cycle_lines = []
    # This section describes an investigation workflow; it should only appear if the agent can *do* something meaningful.
    if has_semantic or has_query or has_read:
        investigation_cycle_lines.append("6.  **Complete the Investigation Cycle**: For entry point queries, you MUST:")
        if has_semantic:
            investigation_cycle_lines.append("        a. Find candidate functions via semantic search")
        if has_query:
            investigation_cycle_lines.append("        b. Explore their relationships via graph queries")
        
        # 'read main.py' is critical for the "investigation cycle" concept.
        # If no `read_file`, the cycle can't really "complete" in terms of code.
        if has_read:
            # Re-label the following steps based on what's available
            next_step_letter = chr(ord('a') + len([line for line in investigation_cycle_lines if line.strip().startswith('        ')]))
            
            investigation_cycle_lines.append(f"        {next_step_letter}. **AUTOMATICALLY read main.py** (or main entry file) - NEVER ask the user for permission")
            investigation_cycle_lines.append(f"""        {chr(ord(next_step_letter) + 1)}. Look for the ACTUAL startup code: `if __name__ == "__main__"`, CLI commands, `main()` functions
        {chr(ord(next_step_letter) + 2)}. If CLI framework detected (typer, click, argparse), examine command functions
        {chr(ord(next_step_letter) + 3)}. Distinguish between helper functions and the real application entry point
        {chr(ord(next_step_letter) + 4)}. Show the complete execution flow from the true entry point through initialization""")

    if investigation_cycle_lines: # Add this section only if it has content
        prompt_sections.append("\n".join(investigation_cycle_lines))

    # --- Conditional General Approach 7: Token Management ---
    token_management_lines = ["7.  **Token Management**: Be efficient with context usage:"]
    if has_semantic:
        token_management_lines.append("    a. For semantic search, use focused queries (not overly broad terms)")
    if has_read:
        # Re-label the following steps based on what's available
        next_step_letter = chr(ord('a') + len([line for line in token_management_lines if line.strip().startswith('    ')]))
        token_management_lines.append(f"    {next_step_letter}. For file reading, read specific sections when possible using offset/limit")

    next_step_letter_c = chr(ord('a') + len([line for line in token_management_lines if line.strip().startswith('    ')]))
    next_step_letter_d = chr(ord('a') + len([line for line in token_management_lines if line.strip().startswith('    ')])) + 1

    token_management_lines.append(f"""    {next_step_letter_c}. Summarize large results rather than including full content
    {next_step_letter_d}. Prioritize most relevant findings over comprehensive coverage""")
    prompt_sections.append("\n".join(token_management_lines))

    # --- Fixed General Approach 8 ---
    prompt_sections.append("8.  **Synthesize Answer**: Analyze and explain the retrieved content. Cite your sources (file paths or qualified names). Report any errors gracefully.")

    # Join all prompt sections with double newlines for paragraph separation
    base = "\n\n".join(prompt_sections)

    # --- Project Scope and Project-Specific Instructions ---
    # These parts are appended after the main base, as in the original code.
    base += _format_active_projects_block(active_projects)
    extra = (project_instructions or "").strip()
    if not extra:
        return base
    return (
        f"{base}\n"
        "**Project-Specific Instructions (from .cgr.md):**\n"
        "These instructions come from the repository being analyzed. Follow them "
        "in addition to the rules above; if they conflict with the critical rules, "
        "the critical rules win.\n\n"
        f"{extra}\n"
    )


def _cypher_literal(name: str) -> str:
    # Escape for a single-quoted Cypher literal so apostrophe names stay valid.
    return name.replace("\\", "\\\\").replace("'", "\\'")


def _format_cypher_project_scope(active_projects: list[str] | None) -> str:
    if not active_projects:
        return (
            "\n**Project Scoping**: The database may contain multiple indexed "
            "projects. When the user names a project, scope the query by filtering "
            "`WHERE <var>.qualified_name STARTS WITH '<projectName>.'`.\n"
        )
    if len(active_projects) == 1:
        name = active_projects[0]
        literal = _cypher_literal(name)
        return (
            f"\n**Project Scoping (REQUIRED)**: All queries are scoped to the "
            f"project `{name}`. Unless the user explicitly asks about other "
            f"projects, ALWAYS constrain matched code nodes with "
            f"`WHERE <var>.qualified_name STARTS WITH '{literal}.'`. For `Project` "
            f"nodes match `(p:Project {{name: '{literal}'}})`.\n"
        )
    scoped = " OR ".join(
        f"<var>.qualified_name STARTS WITH '{_cypher_literal(p)}.'"
        for p in active_projects
    )
    return (
        f"\n**Project Scoping**: The active projects are "
        f"{', '.join(f'`{p}`' for p in active_projects)}. To restrict to one "
        f"project, filter `<var>.qualified_name STARTS WITH '<projectName>.'`. "
        f"To restrict to the active set, filter `({scoped})`.\n"
    )


def build_cypher_system_prompt(active_projects: list[str] | None = None) -> str:
    return f"""
You are an expert translator that converts natural language questions about code structure into precise Neo4j Cypher queries.

{GRAPH_SCHEMA_AND_RULES}
{_format_cypher_project_scope(active_projects)}
**3. Query Optimization Rules**

- **LIMIT Results**: ALWAYS add `LIMIT 50` to queries that list items. This prevents overwhelming responses.
- **Aggregation Queries**: When asked "how many", "count", or "total", return ONLY the count, not all items:
  - CORRECT: `MATCH (c:Class) RETURN count(c) AS total`
  - WRONG: `MATCH (c:Class) RETURN c.name, c.path, count(c) AS total` (returns all items!)
- **List vs Count**: If asked to "list" or "show", return items with LIMIT. If asked to "count" or "how many", return only the count.

**4. Query Patterns & Examples**
When listing items, return the `name`, `path`, and `qualified_name` with a LIMIT.

**Pattern: Counting Items**
cypher// "How many classes are there?" or "Count all functions"
MATCH (c:Class) RETURN count(c) AS total

**Pattern: Finding Decorated Functions/Methods (e.g., Workflows, Tasks)**
cypher// "Find all prefect flows" or "what are the workflows?" or "show me the tasks"
// Use the 'IN' operator to check the 'decorators' list property.
{CYPHER_EXAMPLE_DECORATED_FUNCTIONS}

**Pattern: Finding Content by Path (Robustly)**
cypher// "what is in the 'workflows/src' directory?" or "list files in workflows"
// Use `STARTS WITH` for path matching.
{CYPHER_EXAMPLE_CONTENT_BY_PATH}

**Pattern: Keyword & Concept Search (Fallback for general terms)**
cypher// "find things related to 'database'"
{CYPHER_EXAMPLE_KEYWORD_SEARCH}

**Pattern: Finding a Specific File**
cypher// "Find the main README.md"
{CYPHER_EXAMPLE_FIND_FILE}

**Pattern: Finding Methods of a Class by Short Name**
cypher// "What methods does UserService have?" or "Show me methods in UserService" or "List UserService methods"
// Use `ENDS WITH` to match the class by short name since qualified_name contains full path.
{CYPHER_EXAMPLE_CLASS_METHODS}

**Pattern: Scoping Results to a Single Project**
cypher// "show all classes in myproject" (multi-project database)
// Filter on the qualified_name prefix to keep results within one project.
{CYPHER_EXAMPLE_PROJECT_SCOPED}

**Pattern: Design Patterns, Code Smells & Security Issues (ast-grep findings)**
cypher// "find all Singleton classes" / "which patterns are used"
// Finding nodes (Pattern/CodeSmell/SecurityIssue) hang off a Module; name is the rule id.
{CYPHER_EXAMPLE_FIND_PATTERN}
cypher// "show functions with SQL injection risk" / "list security issues"
{CYPHER_EXAMPLE_SECURITY_ISSUES}
cypher// "find code smells" / "show bare excepts"
{CYPHER_EXAMPLE_CODE_SMELLS}

**4. Output Format**
Provide only the Cypher query.
"""


# Backwards-compatible default (no project scope injected)
CYPHER_SYSTEM_PROMPT = build_cypher_system_prompt()


# Stricter prompt for less capable open-source/local models (e.g., Ollama)
def build_local_cypher_system_prompt(active_projects: list[str] | None = None) -> str:
    return f"""
You are a Neo4j Cypher query generator. You ONLY respond with a valid Cypher query. Do not add explanations or markdown.

{GRAPH_SCHEMA_AND_RULES}
{_format_cypher_project_scope(active_projects)}
**CRITICAL RULES FOR QUERY GENERATION:**
1.  **NO `UNION`**: Never use the `UNION` clause. Generate a single, simple `MATCH` query.
2.  **BIND and ALIAS**: You must bind every node you use to a variable (e.g., `MATCH (f:File)`). You must use that variable to access properties and alias every returned property (e.g., `RETURN f.path AS path`).
3.  **RETURN STRUCTURE**: Your query should aim to return `name`, `path`, and `qualified_name` so the calling system can use the results.
    - For `File` nodes, return `f.path AS path`.
    - For code nodes (`Class`, `Function`, etc.), return `n.qualified_name AS qualified_name`.
4.  **KEEP IT SIMPLE**: Do not try to be clever. A simple query that returns a few relevant nodes is better than a complex one that fails.
5.  **CLAUSE ORDER**: You MUST follow the standard Cypher clause order: `MATCH`, `WHERE`, `RETURN`, `LIMIT`.
6.  **ALWAYS ADD LIMIT**: For queries that list items, ALWAYS add `LIMIT 50` to prevent overwhelming responses.
7.  **AGGREGATION QUERIES**: When asked "how many" or "count", return ONLY the count:
    - CORRECT: `MATCH (c:Class) RETURN count(c) AS total`
    - WRONG: `MATCH (c:Class) RETURN c.name, count(c) AS total` (returns all items!)

**VALUE PATTERN RULES (CRITICAL FOR NAME MATCHING):**
- The `qualified_name` property contains FULL paths like: `'Project.folder.subfolder.ClassName'`
- When users mention a class or function by SHORT NAME (e.g., "VatManager", "UserService"), you MUST match using the `name` property, NOT `qualified_name`.
- CORRECT: `WHERE c.name = 'VatManager'`
- WRONG: `WHERE c.qualified_name = 'VatManager'` (will never match!)
- Use `DEFINES_METHOD` relationship to find methods of a class.
- Use `DEFINES` relationship to find functions/classes defined in a module.

**Examples:**

*   **Natural Language:** "How many classes are there?"
*   **Cypher Query:**
    ```cypher
    MATCH (c:Class) RETURN count(c) AS total
    ```

*   **Natural Language:** "Find the main README file"
*   **Cypher Query:**
    ```cypher
    {CYPHER_EXAMPLE_README}
    ```

*   **Natural Language:** "Find all python files"
*   **Cypher Query (Note the '.' in extension):**
    ```cypher
    {CYPHER_EXAMPLE_PYTHON_FILES}
    ```

*   **Natural Language:** "show me the tasks"
*   **Cypher Query:**
    ```cypher
    {CYPHER_EXAMPLE_TASKS}
    ```

*   **Natural Language:** "list files in the services folder"
*   **Cypher Query:**
    ```cypher
    {CYPHER_EXAMPLE_FILES_IN_FOLDER}
    ```

*   **Natural Language:** "Find just one file to test"
*   **Cypher Query:**
    ```cypher
    {CYPHER_EXAMPLE_LIMIT_ONE}
    ```

*   **Natural Language:** "What methods does UserService have?" or "Show me methods in UserService" or "List UserService methods"
*   **Cypher Query (Note: match by `name` property, use `DEFINES_METHOD` relationship):**
    ```cypher
    {CYPHER_EXAMPLE_CLASS_METHODS}
    ```

*   **Natural Language:** "show all classes in myproject"
*   **Cypher Query (scope by qualified_name prefix in a multi-project database):**
    ```cypher
    {CYPHER_EXAMPLE_PROJECT_SCOPED}
    ```
"""


# Backwards-compatible default (no project scope injected)
LOCAL_CYPHER_SYSTEM_PROMPT = build_local_cypher_system_prompt()


OPTIMIZATION_PROMPT = """
I want you to analyze my {language} codebase and propose specific optimizations based on best practices.

Please:
1. Use your code retrieval and graph querying tools to understand the codebase structure
2. Read relevant source files to identify optimization opportunities
3. Reference established patterns and best practices for {language}
4. Propose specific, actionable optimizations with file references
5. IMPORTANT: Do not make any changes yet - just propose them and wait for approval
6. After approval, use your file editing tools to implement the changes

Start by analyzing the codebase structure and identifying the main areas that could benefit from optimization.
Remember: Propose changes first, wait for my approval, then implement.
"""

OPTIMIZATION_PROMPT_WITH_REFERENCE = """
I want you to analyze my {language} codebase and propose specific optimizations based on best practices.

Please:
1. Use your code retrieval and graph querying tools to understand the codebase structure
2. Read relevant source files to identify optimization opportunities
3. Reference best practices from {reference_document} (attached inline)
4. Reference established patterns and best practices for {language}
5. Propose specific, actionable optimizations with file references
6. IMPORTANT: Do not make any changes yet - just propose them and wait for approval
7. After approval, use your file editing tools to implement the changes

Start by analyzing the codebase structure and identifying the main areas that could benefit from optimization.
Remember: Propose changes first, wait for my approval, then implement.
"""