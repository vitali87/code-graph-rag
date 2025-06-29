import os
from dotenv import load_dotenv

load_dotenv()


class AppConfig:
    """Application Configuration"""

    MEMGRAPH_HOST = os.getenv("MEMGRAPH_HOST", "localhost")
    MEMGRAPH_PORT = int(os.getenv("MEMGRAPH_PORT", 7687))
    MEMGRAPH_HTTP_PORT = int(os.getenv("MEMGRAPH_HTTP_PORT", 7444))
    LAB_PORT = int(os.getenv("LAB_PORT", 3000))
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini")

    GEMINI_MODEL_ID = os.getenv("GEMINI_MODEL_ID", "gemini-2.5-pro-preview-06-05")
    MODEL_CYPHER_ID = os.getenv(
        "MODEL_CYPHER_ID", "gemini-2.5-flash-lite-preview-06-17"
    )
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    LOCAL_MODEL_ENDPOINT = os.getenv("LOCAL_MODEL_ENDPOINT", "http://localhost:11434/v1")
    LOCAL_ORCHESTRATOR_MODEL_ID = os.getenv("LOCAL_ORCHESTRATOR_MODEL_ID", "llama3")
    LOCAL_CYPHER_MODEL_ID = os.getenv("LOCAL_CYPHER_MODEL_ID", "llama3")
    LOCAL_MODEL_API_KEY = os.getenv("LOCAL_MODEL_API_KEY", "ollama")

    TARGET_REPO_PATH = os.getenv("TARGET_REPO_PATH", ".")
    SHELL_COMMAND_TIMEOUT = int(os.getenv("SHELL_COMMAND_TIMEOUT", 30))

    def __init__(self):
        if self.LLM_PROVIDER == "gemini" and not self.GEMINI_API_KEY:
            raise ValueError("Configuration Error: GEMINI_API_KEY is required when LLM_PROVIDER is 'gemini'.")


settings = AppConfig()
