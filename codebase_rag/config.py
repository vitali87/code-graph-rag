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
    # Gemini provider 'gla' or 'vertex'
    GEMINI_PROVIDER = os.getenv("GEMINI_PROVIDER", "gla")

    GEMINI_MODEL_ID = os.getenv("GEMINI_MODEL_ID", "gemini-1.5-pro")
    GEMINI_VISION_MODEL_ID = os.getenv("GEMINI_VISION_MODEL_ID", "gemini-1.5-flash")
    MODEL_CYPHER_ID = os.getenv("MODEL_CYPHER_ID", "gemini-1.5-flash")
    # API key for Gemini.
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    # Thinking budget for Gemini models.
    # - Set to a positive integer (e.g., 1024) to enable a static budget.
    # - Set to 0 to disable thinking.
    # - Set to -1 to enable dynamic thinking.
    # - Default is None (off).
    GEMINI_THINKING_BUDGET = os.getenv("GEMINI_THINKING_BUDGET")

    # Google Cloud project ID for Vertex AI.
    GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
    GCP_REGION = os.getenv("GCP_REGION", "us-central1")
    GCP_SERVICE_ACCOUNT_FILE = os.getenv("GCP_SERVICE_ACCOUNT_FILE")

    LOCAL_MODEL_ENDPOINT = os.getenv("LOCAL_MODEL_ENDPOINT", "http://localhost:11434/v1")
    LOCAL_ORCHESTRATOR_MODEL_ID = os.getenv("LOCAL_ORCHESTRATOR_MODEL_ID", "llama3")
    LOCAL_CYPHER_MODEL_ID = os.getenv("LOCAL_CYPHER_MODEL_ID", "llama3")
    LOCAL_MODEL_API_KEY = os.getenv("LOCAL_MODEL_API_KEY", "ollama")

    TARGET_REPO_PATH = os.getenv("TARGET_REPO_PATH", ".")
    SHELL_COMMAND_TIMEOUT = int(os.getenv("SHELL_COMMAND_TIMEOUT", 30))

    def __init__(self):
        if self.GEMINI_THINKING_BUDGET is not None:
            try:
                self.GEMINI_THINKING_BUDGET = int(self.GEMINI_THINKING_BUDGET)
            except (ValueError, TypeError):
                raise ValueError(
                    "Configuration Error: GEMINI_THINKING_BUDGET must be an integer."
                )

        if self.LLM_PROVIDER == "gemini" and self.GEMINI_PROVIDER == "gla" and not self.GEMINI_API_KEY:
            raise ValueError("Configuration Error: GEMINI_API_KEY is required when GEMINI_PROVIDER is 'gla'.")
        if self.LLM_PROVIDER == "gemini" and self.GEMINI_PROVIDER == "vertex" and not self.GCP_PROJECT_ID:
            raise ValueError("Configuration Error: GCP_PROJECT_ID is required when GEMINI_PROVIDER is 'vertex'.")


settings = AppConfig()
