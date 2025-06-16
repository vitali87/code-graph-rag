# codebase_rag/schemas.py
from pydantic import BaseModel, Field, field_validator, ConfigDict
from typing import List, Any, Dict

class GraphData(BaseModel):
    """Data model for results returned from the knowledge graph tool."""
    query_used: str
    results: List[Dict[str, Any]]
    summary: str = Field(description="A brief summary of the operation's outcome.")

    # This validator is good practice for sanitizing data from external sources
    # before it enters the core application.
    @field_validator('results', mode='before')
    @classmethod
    def _format_results(cls, v):
        if not isinstance(v, list):
            return v
            
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
        
    model_config = ConfigDict(extra='forbid')