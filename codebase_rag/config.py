import os
from dotenv import load_dotenv

load_dotenv()


class AppConfig:
    """Application Configuration"""

    MEMGRAPH_HOST = os.getenv("MEMGRAPH_HOST", "localhost")
    MEMGRAPH_PORT = int(os.getenv("MEMGRAPH_PORT", 7687))
    MEMGRAPH_HTTP_PORT = int(os.getenv("MEMGRAPH_HTTP_PORT", 7444))
    LAB_PORT = int(os.getenv("LAB_PORT", 3000))
    GEMINI_MODEL_ID = os.getenv("GEMINI_MODEL_ID", "gemini-2.5-pro-preview-06-05")
    MODEL_CYPHER_ID = os.getenv(
        "MODEL_CYPHER_ID", "gemini-2.5-flash-lite-preview-06-17"
    )

    # Repository path for code retrieval
    TARGET_REPO_PATH = os.getenv("TARGET_REPO_PATH", ".")

    # It's good practice to fail early if a required key is missing.
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        raise ValueError("Configuration Error: GEMINI_API_KEY is not set.")


settings = AppConfig()
