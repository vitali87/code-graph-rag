import os
import asyncio
from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import List, Any, Dict
from dotenv import load_dotenv

# Pydantic AI imports
from pydantic_ai import Agent, Tool, RunContext
from pydantic_ai.models.gemini import GeminiModel
from pydantic_ai.providers.google_gla import GoogleGLAProvider

# Memgraph client
import mgclient
# import json # Not strictly needed if GraphData validator handles complex types

# --- Load Environment Variables ---
load_dotenv()

# --- Configuration ---
MEMGRAPH_HOST = "localhost"
MEMGRAPH_PORT = 7687
# Use the model ID you have access to and is specified in your .env or here
GEMINI_MODEL_ID = os.getenv("GEMINI_MODEL_ID", "gemini-2.5-pro-preview-06-05")


# --- Graph Schema ---
GRAPH_SCHEMA_DESCRIPTION = """
The knowledge graph contains information about a Python codebase.
Node Labels and their key properties:
- Project: {name: string (unique project name)}
- Package: {qualified_name: string (e.g., 'project.package_name'), name: string, path: string}
- Module: {qualified_name: string (e.g., 'project.package.module_name'), name: string (e.g., 'utils.py'), path: string}
- Class: {qualified_name: string (e.g., 'project.package.module.ClassName'), name: string}
- Function: {qualified_name: string (e.g., 'project.package.module.function_name'), name: string}
- Method: {qualified_name: string (e.g., 'project.package.module.ClassName.method_name'), name: string}
- File: {path: string (unique relative path), name: string, extension: string}
- Folder: {path: string (unique relative path), name: string}
- ExternalPackage: {name: string (e.g., 'requests'), version_spec: string}

Relationship Types (source)-[REL_TYPE]->(target):
- Project -[:CONTAINS_PACKAGE]-> Package
- Project -[:CONTAINS_FOLDER]-> Folder
- Project -[:CONTAINS_MODULE]-> Module
- Project -[:CONTAINS_FILE]-> File
- Project -[:DEPENDS_ON_EXTERNAL]-> ExternalPackage
- Package -[:CONTAINS_SUBPACKAGE]-> Package
- Package -[:CONTAINS_MODULE]-> Module
- Package -[:CONTAINS_FOLDER]-> Folder
- Package -[:CONTAINS_FILE]-> File
- Folder -[:CONTAINS_MODULE]-> Module
- Folder -[:CONTAINS_FOLDER]-> Folder
- Folder -[:CONTAINS_FILE]-> File
- Module -[:DEFINES]-> Class
- Module -[:DEFINES]-> Function
- Module -[:IMPORTS]-> Module
- Module -[:IMPORTS_FROM {item: string, alias: string}]-> Module
- Class -[:DEFINES_METHOD]-> Method
- Class -[:INHERITS_FROM]-> Class
- Function -[:CALLS]-> Function
- Function -[:CALLS]-> Method
- Method -[:CALLS]-> Function
- Method -[:CALLS]-> Method

Focus on generating queries that use `qualified_name` for Packages, Modules, Classes, Functions, and Methods,
and `name` for Project or ExternalPackage when filtering. Use `path` for Files and Folders.
When returning node properties, usually 'name' or 'qualified_name' is desired.
When asked to "describe" or "tell me about" an entity, try to fetch its direct properties and some key connected entities.
For "how many" questions, use COUNT().
For "list" or "show" questions, return relevant properties like name or qualified_name.
Ensure all string values in WHERE clauses are properly quoted.
"""

# --- Pydantic Models ---
class GraphData(BaseModel):
    query_used: str
    results: List[Dict[str, Any]]
    summary: str = Field(description="A brief summary of what the data represents, if any data was found, or an error message.")

    @field_validator('results', mode='before')
    @classmethod
    def _format_results(cls, v):
        if isinstance(v, list):
            clean_results = []
            for row in v:
                clean_row = {}
                for k, val in row.items():
                    if not isinstance(val, (str, int, float, bool, list, dict, type(None))):
                        clean_row[k] = str(val)
                    else:
                        clean_row[k] = val
                clean_results.append(clean_row)
            return clean_results
        return v
    model_config = ConfigDict(extra='forbid')

# --- LLM Setup ---
try:
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise ValueError("GEMINI_API_KEY not found in .env file or environment variables.")

    print(f"Using Gemini Model ID: {GEMINI_MODEL_ID}")

    text_to_cypher_llm = GeminiModel(
        GEMINI_MODEL_ID,
        provider=GoogleGLAProvider(api_key=gemini_api_key)
    )
    answer_synthesis_llm = GeminiModel(
        GEMINI_MODEL_ID,
        provider=GoogleGLAProvider(api_key=gemini_api_key)
    )
except ValueError as ve:
    print(f"Configuration Error: {ve}")
    exit()
except ImportError:
    print("ImportError: Could not import GeminiModel or GoogleGLAProvider.")
    print("Please ensure 'pydantic-ai-slim[google-gla]' or 'pydantic-ai[google]' is installed.")
    exit()
except Exception as e:
    print(f"Error initializing LLMs: {e}")
    exit()


TEXT_TO_CYPHER_SYSTEM_PROMPT = f"""
You are an expert Cypher query writer for a Memgraph database that stores a knowledge graph of Python codebases.
Your goal is to translate a user's natural language question about a codebase into a single, valid Cypher query.
Only output the Cypher query itself. Do not add any explanations, markdown formatting like ```cypher, or any text before or after the query.

Graph Schema:
{GRAPH_SCHEMA_DESCRIPTION} # This uses the schema from your file, which is correct.

General Querying Guidelines:
1.  When searching for specific entities like Packages, Modules, Classes, Functions, or Methods, prioritize using their `qualified_name`. If only a simple name is given, use the `name` property.
2.  **Crucial:** When a user asks about a "function", "how to do something", or "a way to do something", they often mean **either a standalone function (:Function) or a method within a class (:Method)**. Your query should almost always check for both.
    - **Correct Pattern:** `MATCH (n) WHERE (n:Function OR n:Method) AND toLower(n.name) CONTAINS 'keyword'`
    - **Incorrect (Too Specific) Pattern:** `MATCH (f:Function) WHERE ...`
3.  For Files and Folders, use their `path` property for precise matching.
4.  For questions about relationships (e.g., "what does X call?"), use the defined relationship types.
5.  For counting, use `COUNT()`.
6.  Ensure all string literals in Cypher `WHERE` clauses are properly single-quoted.

Example Scenarios:

Natural Language: "List all classes in the module 'my_project.processing.data_parser'."
Cypher: MATCH (m:Module {{qualified_name: 'my_project.processing.data_parser'}})-[:DEFINES]->(c:Class) RETURN c.name;

Natural Language: "Show methods in the 'DataProcessor' class."
Cypher: MATCH (c:Class {{name: 'DataProcessor'}})-[:DEFINES_METHOD]->(m:Method) RETURN m.qualified_name;

Natural Language: "How can I log messages in this system?"
Cypher: MATCH (n) WHERE (n:Function OR n:Method) AND (toLower(n.name) CONTAINS 'log' OR toLower(n.name) CONTAINS 'logger' OR toLower(n.name) CONTAINS 'logging') RETURN n.qualified_name, n.name, labels(n) AS type, [(n)<-[:DEFINES]-(parent_mod) | parent_mod.qualified_name][0] AS in_module, [(n)<-[:DEFINES_METHOD]-(parent_cls) | parent_cls.qualified_name][0] AS in_class;

Natural Language: "Find the function or method to add an episode"
Cypher: MATCH (n) WHERE (n:Function OR n:Method) AND n.name = 'add_episode' RETURN n.qualified_name, labels(n) as type;

Natural Language: "Find different ways to handle user authentication."
Cypher: MATCH (n) WHERE (n:Function OR n:Method) AND ( (toLower(n.name) CONTAINS 'auth') OR (toLower(n.name) CONTAINS 'authenticate') OR (toLower(n.name) CONTAINS 'login') ) RETURN n.qualified_name, n.name, labels(n) AS type;

Natural Language: "Which functions call the 'calculate_discount' function?"
Cypher: MATCH (caller)-[:CALLS]->(callee) WHERE (caller:Function OR caller:Method) AND callee.name = 'calculate_discount' RETURN caller.qualified_name, labels(caller) AS caller_type;

Translate the following natural language question into a single, valid Cypher query based on the schema and guidelines:
"""

cypher_generation_agent = Agent(
    model=text_to_cypher_llm,
    system_prompt=TEXT_TO_CYPHER_SYSTEM_PROMPT,
    output_type=str,
    tools=[],
)

async def generate_cypher_from_nl(natural_language_query: str) -> str | None:
    print(f"  [TextToCypher] Input: {natural_language_query}")
    try:
        result = await cypher_generation_agent.run(natural_language_query)
        if isinstance(result.output, str):
            response_text = result.output
            query = response_text.strip()
            if query.startswith("```cypher"): query = query.split("```cypher")[1].strip()
            if query.startswith("```"): query = query.split("```")[1].strip()
            if query.endswith("```"): query = query.rsplit("```", 1)[0].strip()
            if query and not query.endswith(";"): query += ";"
            
            if query and "MATCH" in query.upper() and "RETURN" in query.upper():
                print(f"  [TextToCypher] Generated Cypher: {query}")
                return query
            else:
                print(f"  [TextToCypher] LLM did not generate a valid-looking Cypher query: {query if query else '<empty response>'}")
                return None
        else:
            print(f"  [TextToCypher] Expected string output, got {type(result.output)}: {result.output}")
            return None
    except Exception as e:
        print(f"  [TextToCypher] Error: {e}")
        import traceback
        traceback.print_exc()
        return None

# --- Memgraph Execution Helper ---
def execute_memgraph_cypher(cypher_query: str) -> List[Dict[str, Any]]:
    if not cypher_query:
        print("  [MemgraphExec] No Cypher query to execute.")
        return []
    conn = None
    rows = []
    try:
        conn = mgclient.connect(host=MEMGRAPH_HOST, port=MEMGRAPH_PORT)
        cursor = conn.cursor()
        print(f"  [MemgraphExec] Executing: {cypher_query}")
        cursor.execute(cypher_query)
        if cursor.description:
            column_names = [desc.name for desc in cursor.description]
            for row_tuple in cursor.fetchall():
                rows.append(dict(zip(column_names, row_tuple)))
        print(f"  [MemgraphExec] Found {len(rows)} results.")
        return rows
    except Exception as e:
        print(f"  [MemgraphExec] Error: {e}")
        return [{"error_executing_query": str(e)}]
    finally:
        if conn:
            conn.close()

# --- Pydantic AI Tool for Querying Memgraph ---
async def query_codebase_knowledge_graph(ctx: RunContext, natural_language_query: str) -> GraphData:
    print(f"[Tool:QueryGraph] Received NL query from agent: {natural_language_query}")
    cypher_query = await generate_cypher_from_nl(natural_language_query)

    if not cypher_query:
        return GraphData(
            query_used="N/A - Failed to generate Cypher",
            results=[],
            summary="I could not translate your request into a database query."
        )
    results = execute_memgraph_cypher(cypher_query)
    summary = ""
    if not results:
        summary = "No specific data found in the codebase graph for your query."
    elif "error_executing_query" in results[0] and len(results) == 1 :
        summary = f"There was an error querying the codebase graph: {results[0]['error_executing_query']}"
    else:
        summary = f"Successfully retrieved {len(results)} item(s) from the codebase graph."
    return GraphData(query_used=cypher_query, results=results, summary=summary)

# --- Main RAG Orchestrator Agent ---
RAG_ORCHESTRATOR_SYSTEM_PROMPT = """
You are a helpful AI assistant expert in explaining Python codebases based *solely* on information retrieved from a knowledge graph.
Your task is to answer the user's questions.

1.  If the question requires specific, structured information about the codebase (like listing classes, methods, package contents, dependencies, or how code elements are connected), you MUST use the 'query_codebase_knowledge_graph' tool.
    *   Provide the tool with a clear, natural language question that it can use to query the graph. The tool expects a single argument named 'natural_language_query'.
2.  After the tool returns data (which will be a summary and a list of results from the graph):
    *   **If the tool found relevant data:** Synthesize this data with the original user question to provide a comprehensive, easy-to-understand natural language answer. **Base your answer strictly on the data provided by the tool.** Do not add information or code examples not present in the tool's output.
    *   **If the tool's summary indicates no data was found or an error occurred:** Inform the user politely that the specific information could not be found in the analyzed codebase graph. Do NOT invent alternative solutions, code examples, or general programming advice. Simply state that the information is not available in the graph based on their query.
3.  If the user's question is very general and clearly does not require looking up specific codebase structures (e.g., "hello", "how are you?", "explain what a Python class is in general terms"), you may try to answer it directly using your general knowledge. However, prioritize using the tool for any codebase-specific inquiry.

Be concise and stick to the facts retrieved from the knowledge graph.
If the tool returns an empty result set for a query about specific functions/methods, state that those specific items were not found in the graph.
Do not suggest alternative Python code or classes unless that code/class information was explicitly returned by the 'query_codebase_knowledge_graph' tool.
"""

rag_orchestrator_agent = Agent(
    model=answer_synthesis_llm,
    system_prompt=RAG_ORCHESTRATOR_SYSTEM_PROMPT,
    tools=[query_codebase_knowledge_graph],
    debug_mode=True # Set to True for verbose agent logging
)

# --- Main Execution Logic ---
async def ask_orchestrator(question: str):
    print(f"\nUser Question to Orchestrator: {question}")
    try:
        response = await rag_orchestrator_agent.run(question)
        print(f"\nFinal Orchestrator Answer:\n{response.output}")
    except Exception as e:
        print(f"Error in RAG Orchestrator agent execution: {e}")
        import traceback
        traceback.print_exc()

async def main():
    print(f"Codebase RAG CLI - Using Model: {GEMINI_MODEL_ID}")
    print("Ask questions about your codebase graph. Type 'exit' or 'quit' to end.")
    
    # Optional: Initial check of graph content
    # print("\nPerforming initial graph check...")
    # execute_memgraph_cypher("MATCH (p:Project) RETURN p.name LIMIT 1;")
    # print("-" * 50)

    while True:
        try:
            user_input = input("\nAsk a question: ")
            if user_input.lower() in ["exit", "quit"]:
                print("Exiting...")
                break
            if not user_input.strip():
                continue
            
            await ask_orchestrator(user_input)
            print("\n" + "="*70 + "\n")

        except KeyboardInterrupt:
            print("\nExiting due to KeyboardInterrupt...")
            break
        except Exception as e:
            print(f"An unexpected error occurred in the main loop: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nApplication terminated by user.")