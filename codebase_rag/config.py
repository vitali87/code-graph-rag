# codebase_rag/config.py
import os
from dotenv import load_dotenv

# Load environment variables from a .env file
load_dotenv()

class AppConfig:
    """Application Configuration"""
    MEMGRAPH_HOST = os.getenv("MEMGRAPH_HOST", "localhost")
    MEMGRAPH_PORT = int(os.getenv("MEMGRAPH_PORT", 7687))
    GEMINI_MODEL_ID = os.getenv("GEMINI_MODEL_ID", "gemini-1.5-pro-preview-0514")
    
    # It's good practice to fail early if a required key is missing.
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        raise ValueError("Configuration Error: GEMINI_API_KEY is not set.")

# Create a single, importable instance of the configuration
settings = AppConfig()