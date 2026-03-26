import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from . import constants as cs
from . import logs as ls
from .config import load_cgrignore_patterns, settings
from .graph_updater import GraphUpdater
from .main import (
    app_context,
    connect_memgraph,
    update_model_settings,
)
from .parser_loader import load_parsers
from .services.chat_orchestrator import ChatOrchestratorService
from .services.chat_orchestrator import ChatStageError
from .chat_schemas import ApiErrorDetail, ApiErrorResponse, ChatResponsePayload
from .utils.dependencies import has_semantic_dependencies

# Configure confirmation prompts in API context based on CLI flag
app_context.session.confirm_edits = not (os.environ.get("CGR_NO_CONFIRM") == "1")


def _require_ingestor(_app: FastAPI):
    ingestor = getattr(_app.state, "ingestor", None)
    if ingestor is None:
        raise HTTPException(status_code=500, detail="Memgraph not initialized")
    return ingestor


def _resolve_repo_path(repo_path: str | None) -> str:
    resolved = repo_path or settings.TARGET_REPO_PATH
    if not resolved:
        raise HTTPException(
            status_code=400, detail="repo_path is required either in request or settings"
        )
    return os.path.abspath(resolved)


def _validate_and_apply_model_overrides(orchestrator: str | None, cypher: str | None):
    try:
        if orchestrator or cypher:
            update_model_settings(orchestrator, cypher)
        settings.active_orchestrator_config.validate_api_key("orchestrator")
        settings.active_cypher_config.validate_api_key("cypher")
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail=f"Model validation failed: {str(e)}"
        ) from e


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ingestor = None
    try:
        ingestor = connect_memgraph(settings.resolve_batch_size(None))
        ingestor.__enter__()
        _app.state.ingestor = ingestor
    except Exception:
        if ingestor is not None:
            ingestor.__exit__(None, None, None)
        raise

    if settings.SEMANTIC_SEARCH_ENABLED and has_semantic_dependencies():
        try:
            from .embedder import prewarm_embeddings

            logger.info(ls.SEMANTIC_PREWARM_START)
            prewarm_embeddings()
            logger.info(ls.SEMANTIC_PREWARM_COMPLETE)
        except Exception as e:
            logger.warning(ls.SEMANTIC_PREWARM_FAILED.format(error=e))
    yield

    ingestor = getattr(_app.state, "ingestor", None)
    if ingestor is not None:
        ingestor.__exit__(None, None, None)


app = FastAPI(
    title="Code Graph RAG API",
    description="API for doing Q&A on codebase using Code Graph RAG",
    version="1.0.0",
    lifespan=lifespan,
)

class ChatRequest(BaseModel):
    query: dict
    repo_path: str | None = None
    orchestrator: str | None = None
    cypher: str | None = None

class ChatResponse(BaseModel):
    response: ChatResponsePayload

@app.post("/api/chat", response_model=ChatResponse | ApiErrorResponse)
async def chat_endpoint(request: ChatRequest):
    target_repo_path = _resolve_repo_path(request.repo_path)
    _validate_and_apply_model_overrides(request.orchestrator, request.cypher)
    ingestor = _require_ingestor(app)

    try:
        response_data = await ChatOrchestratorService.process_query(
            request_query=request.query,
            ingestor=ingestor,
            target_repo_path=target_repo_path
        )
        payload = ChatResponsePayload.model_validate(response_data)
        return ChatResponse(response=payload)
    except ValueError as e:
        logger.warning(f"Validation error in chat endpoint: {e}")
        err = ApiErrorResponse(
            error=ApiErrorDetail(code="BAD_REQUEST", message=str(e))
        )
        return JSONResponse(status_code=400, content=err.model_dump(mode="json"))
    except ChatStageError as e:
        # LLM/tooling failures or contract drift: treat as upstream failure.
        run_id: UUID | None = None
        try:
            run_id = UUID(e.run_id)
        except Exception:
            run_id = None
        err = ApiErrorResponse(
            error=ApiErrorDetail(code=e.code, message=e.message, run_id=run_id)
        )
        return JSONResponse(status_code=502, content=err.model_dump(mode="json"))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Chat API error")
        err = ApiErrorResponse(
            error=ApiErrorDetail(code="INTERNAL_ERROR", message=str(e))
        )
        return JSONResponse(status_code=500, content=err.model_dump(mode="json"))

class IndexRequest(BaseModel):
    repo_path: str = Field(..., description="Absolute path to the repository to index")
    clean: bool = Field(True, description="Whether to clean the database before indexing")
    exclude: list[str] | None = Field(None, description="Paths to exclude")

class IndexResponse(BaseModel):
    status: str
    message: str

def _run_indexer(
    target_repo_path: str,
    clean: bool,
    exclude: list[str] | None,
    ingestor
) -> None:
    try:
        repo_path = Path(target_repo_path).resolve()
        if not repo_path.is_dir():
            logger.error(f"Directory not found: {repo_path}")
            return

        project_name = repo_path.name

        cgrignore = load_cgrignore_patterns(repo_path)
        cli_excludes = frozenset(exclude) if exclude else frozenset()
        exclude_paths = cli_excludes | cgrignore.exclude or None
        unignore_paths = cgrignore.unignore or None

        if clean:
            logger.info("Cleaning graph database and hash cache...")
            ingestor.clean_database()
            cache_path = repo_path / cs.HASH_CACHE_FILENAME
            cache_path.unlink(missing_ok=True)

        ingestor.ensure_constraints()
        parsers, queries = load_parsers()

        updater = GraphUpdater(
            ingestor=ingestor,
            repo_path=repo_path,
            parsers=parsers,
            queries=queries,
            unignore_paths=unignore_paths,
            exclude_paths=exclude_paths,
            project_name=project_name,
        )
        updater.run()
        logger.info(f"Successfully completed background indexing for {target_repo_path}")
    except Exception as e:
        logger.exception(f"Background indexing failed for {target_repo_path}: {e}")

@app.post("/api/index", response_model=IndexResponse)
async def index_endpoint(request: IndexRequest):
    """
    Programmatic endpoint equivalent to `cgr start --update-graph --clean`.
    Indexes a repository into Memgraph in a background daemon thread.
    The response is returned immediately; indexing continues independently.
    """
    ingestor = _require_ingestor(app)

    try:
        repo_path = Path(request.repo_path).resolve()
        if not repo_path.is_dir():
            raise ValueError(f"Directory not found: {request.repo_path}")

        # Use a real daemon thread — unlike FastAPI BackgroundTasks, this is NOT
        # cancelled when the HTTP response is sent (avoids asyncio.CancelledError
        # on long-running GraphUpdater jobs).
        t = threading.Thread(
            target=_run_indexer,
            args=(request.repo_path, request.clean, request.exclude, ingestor),
            daemon=True,
            name=f"indexer-{repo_path.name}",
        )
        t.start()
        return IndexResponse(
            status="accepted",
            message=f"Indexing job for {request.repo_path} has been accepted and is running in the background.",
        )
    except ValueError as e:
        logger.warning(f"Validation error in index endpoint: {e}")
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Indexing API error")
        raise HTTPException(status_code=500, detail=str(e)) from e
