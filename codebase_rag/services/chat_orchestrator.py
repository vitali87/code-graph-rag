import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic_ai import DeferredToolRequests, DeferredToolResults
from pydantic import ValidationError

from ..constants import HASH_CACHE_FILENAME
from ..config import settings
from ..main import _initialize_services_and_agent
from ..prompts import (
    API_EVIDENCE_PROMPT,
    API_REMEDIATION_PROMPT,
    API_SCORING_PROMPT,
)
from ..services.llm import create_rag_orchestrator
from ..chat_schemas import EvidenceToolOutput, ScoringToolOutput, RemediationToolOutput
from ..utils.token_utils import count_tokens
from ..utils.tool_call_store import new_run_id, store_tool_call


def _compute_repo_state_hash(target_repo_path: str) -> str:
    hash_file = Path(target_repo_path) / HASH_CACHE_FILENAME
    state_hash = target_repo_path
    if hash_file.is_file():
        try:
            state_hash = hashlib.md5(hash_file.read_bytes()).hexdigest()
        except OSError:
            pass
    return state_hash


def _persist_stage(
    *,
    run_id: str,
    cache_key: str,
    repo_path: str,
    repo_state_hash: str,
    stage: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any],
) -> None:
    try:
        store_tool_call(
            run_id=run_id,
            cache_key=cache_key,
            repo_path=repo_path,
            repo_state_hash=repo_state_hash,
            stage=stage,
            tool_input=tool_input,
            tool_output=tool_output,
        )
    except Exception as e:
        logger.warning(f"Failed to persist {stage} stage: {e}")


class ChatStageError(Exception):
    __slots__ = ("code", "message", "run_id")

    def __init__(self, *, code: str, message: str, run_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.run_id = run_id


async def _run_with_timeout(coro, *, timeout_s: float, stage: str, run_id: str):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError as e:
        raise ChatStageError(
            code="TIMEOUT",
            message=f"{stage} stage timed out after {timeout_s} seconds",
            run_id=run_id,
        ) from e


def _parse_and_validate_stage(
    *, stage: str, raw_text: str, run_id: str
) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except Exception as e:
        raise ChatStageError(
            code="LLM_INVALID_JSON",
            message=f"{stage} stage output was not valid JSON",
            run_id=run_id,
        ) from e

    try:
        if stage == "evidence":
            EvidenceToolOutput.model_validate(parsed)
        elif stage == "scoring":
            ScoringToolOutput.model_validate(parsed)
        elif stage == "remediation":
            RemediationToolOutput.model_validate(parsed)
        else:
            raise ValueError(f"Unknown stage: {stage}")
    except ValidationError as e:
        raise ChatStageError(
            code="LLM_SCHEMA_MISMATCH",
            message=f"{stage} stage output did not match expected schema",
            run_id=run_id,
        ) from e

    return parsed


def _usage_from_result(result: Any) -> tuple[int | None, int | None, int | None]:
    """
    Best-effort extraction of token usage from pydantic_ai results across providers.
    Returns (input, output, total) or (None, None, None) if unavailable.
    """
    # Common patterns: result.usage (dict or object), result.model_response.usage (dict)
    candidates: list[Any] = []
    for attr in ("usage", "model_response", "response", "raw_response"):
        if hasattr(result, attr):
            candidates.append(getattr(result, attr))
    candidates.append(result)

    def _as_dict(v: Any) -> dict[str, Any] | None:
        if isinstance(v, dict):
            return v
        if hasattr(v, "usage") and isinstance(getattr(v, "usage"), dict):
            return getattr(v, "usage")
        if hasattr(v, "model_dump"):
            try:
                dumped = v.model_dump()
                if isinstance(dumped, dict):
                    return dumped
            except Exception:
                return None
        return None

    for c in candidates:
        d = _as_dict(c)
        if not d:
            continue

        usage = d.get("usage") if isinstance(d.get("usage"), dict) else d
        if not isinstance(usage, dict):
            continue

        prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
        completion = usage.get("completion_tokens") or usage.get("output_tokens")
        total = usage.get("total_tokens")
        if isinstance(prompt, int) or isinstance(completion, int) or isinstance(total, int):
            in_t = int(prompt) if isinstance(prompt, int) else None
            out_t = int(completion) if isinstance(completion, int) else None
            tot_t = int(total) if isinstance(total, int) else None
            if tot_t is None and in_t is not None and out_t is not None:
                tot_t = in_t + out_t
            return in_t, out_t, tot_t

    return None, None, None


class ChatOrchestratorService:
    """Service class to encapsulate the RAG agent sequence and business logic."""

    @classmethod
    def _get_cache_key(cls, request_query: dict, target_repo_path: str) -> str:
        query_str = json.dumps(request_query, sort_keys=True)
        state_hash = _compute_repo_state_hash(target_repo_path)
        key_material = f"{query_str}|{state_hash}"
        return hashlib.md5(key_material.encode("utf-8")).hexdigest()

    @classmethod
    async def process_query(
        cls, request_query: dict, ingestor: Any, target_repo_path: str
    ) -> dict[str, Any]:
        """
        Executes the logic to extract an evidence pack, score it, and propose remediation.
        
        Args:
            request_query: The incoming query data payload (e.g. findings).
            ingestor: The Memgraph database ingestor instance.
            target_repo_path: Absolute path to the repository being analyzed.
            
        Returns:
            Dictionary containing 'evidence', 'scoring', 'remediation', or an 'error'.
        """
        cache_key = cls._get_cache_key(request_query, target_repo_path)

        repo_state_hash = _compute_repo_state_hash(target_repo_path)

        run_id = new_run_id()
        t0 = time.perf_counter()

        evidence_agent, _ = _initialize_services_and_agent(
            target_repo_path, ingestor, system_prompt=API_EVIDENCE_PROMPT
        )

        message_history = []
        deferred_results = None

        if isinstance(request_query, dict) and isinstance(
            request_query.get("findings"), list
        ):
            findings_payload = request_query
        else:
            findings_payload = {"findings": [request_query]}

        query_payload = json.dumps(findings_payload, ensure_ascii=False)
        evidence_in_tokens = count_tokens(API_EVIDENCE_PROMPT) + count_tokens(query_payload)
        evidence_usage_from_provider: tuple[int | None, int | None, int | None] = (
            None,
            None,
            None,
        )

        async def _run_evidence_once() -> dict[str, Any]:
            nonlocal deferred_results, message_history, evidence_usage_from_provider
            while True:
                result = await evidence_agent.run(
                    query_payload,
                    message_history=message_history,
                    deferred_tool_results=deferred_results,
                )

                if isinstance(result.output, DeferredToolRequests):
                    deferred_results = DeferredToolResults()
                    for call in result.output.approvals:
                        deferred_results.approvals[call.tool_call_id] = True
                    message_history.extend(result.new_messages())
                    continue

                if not isinstance(result.output, str):
                    raise ChatStageError(
                        code="LLM_INVALID_OUTPUT_TYPE",
                        message=f"Unexpected evidence response format: {type(result.output)}",
                        run_id=run_id,
                    )

                evidence_usage_from_provider = _usage_from_result(result)
                return _parse_and_validate_stage(
                    stage="evidence", raw_text=result.output, run_id=run_id
                )

        evidence_timeout = float(settings.CHAT_EVIDENCE_TIMEOUT_SECONDS)
        evidence_attempts = max(1, int(settings.CHAT_SCHEMA_RETRY_ATTEMPTS))

        evidence_json: dict[str, Any] | None = None
        evidence_ms = 0
        for attempt in range(evidence_attempts):
            t_stage = time.perf_counter()
            try:
                evidence_json = await _run_with_timeout(
                    _run_evidence_once(),
                    timeout_s=evidence_timeout,
                    stage="evidence",
                    run_id=run_id,
                )
                evidence_ms = int((time.perf_counter() - t_stage) * 1000)
                break
            except ChatStageError:
                if attempt >= evidence_attempts - 1:
                    raise
                deferred_results = None
                message_history = []

        assert evidence_json is not None
        evidence_items = evidence_json.get("findings", [])
        evidence_out_tokens = count_tokens(json.dumps(evidence_json, ensure_ascii=False))
        ev_in_u, ev_out_u, ev_tot_u = evidence_usage_from_provider
        evidence_usage = {
            "input_tokens": ev_in_u if ev_in_u is not None else evidence_in_tokens,
            "output_tokens": ev_out_u if ev_out_u is not None else evidence_out_tokens,
            "total_tokens": ev_tot_u
            if ev_tot_u is not None
            else (ev_in_u if ev_in_u is not None else evidence_in_tokens)
            + (ev_out_u if ev_out_u is not None else evidence_out_tokens),
        }
        evidence_input = {"findings": findings_payload.get("findings", [])}
        evidence_output = {"findings": evidence_items}
        _persist_stage(
            run_id=run_id,
            cache_key=cache_key,
            repo_path=target_repo_path,
            repo_state_hash=repo_state_hash,
            stage="evidence",
            tool_input=evidence_input,
            tool_output=evidence_output,
        )

        shared_input = {"findings": evidence_items}
        shared_payload = json.dumps(shared_input, ensure_ascii=False)
        scoring_in_tokens = count_tokens(API_SCORING_PROMPT) + count_tokens(shared_payload)
        remediation_in_tokens = count_tokens(API_REMEDIATION_PROMPT) + count_tokens(shared_payload)
        scoring_usage_from_provider: tuple[int | None, int | None, int | None] = (
            None,
            None,
            None,
        )
        remediation_usage_from_provider: tuple[int | None, int | None, int | None] = (
            None,
            None,
            None,
        )

        scoring_agent = create_rag_orchestrator(tools=[], system_prompt=API_SCORING_PROMPT)
        remediation_agent = create_rag_orchestrator(
            tools=[], system_prompt=API_REMEDIATION_PROMPT
        )

        async def _run_scoring_once() -> dict[str, Any]:
            nonlocal scoring_usage_from_provider
            result = await scoring_agent.run(shared_payload)
            if not isinstance(result.output, str):
                raise ChatStageError(
                    code="LLM_INVALID_OUTPUT_TYPE",
                    message=f"Unexpected scoring response format: {type(result.output)}",
                    run_id=run_id,
                )
            scoring_usage_from_provider = _usage_from_result(result)
            return _parse_and_validate_stage(
                stage="scoring", raw_text=result.output, run_id=run_id
            )

        async def _run_remediation_once() -> dict[str, Any]:
            nonlocal remediation_usage_from_provider
            result = await remediation_agent.run(shared_payload)
            if not isinstance(result.output, str):
                raise ChatStageError(
                    code="LLM_INVALID_OUTPUT_TYPE",
                    message=f"Unexpected remediation response format: {type(result.output)}",
                    run_id=run_id,
                )
            remediation_usage_from_provider = _usage_from_result(result)
            return _parse_and_validate_stage(
                stage="remediation", raw_text=result.output, run_id=run_id
            )

        scoring_timeout = float(settings.CHAT_SCORING_TIMEOUT_SECONDS)
        remediation_timeout = float(settings.CHAT_REMEDIATION_TIMEOUT_SECONDS)
        stage_attempts = max(1, int(settings.CHAT_SCHEMA_RETRY_ATTEMPTS))

        async def _timed(stage: str, coro):
            t_stage = time.perf_counter()
            result = await coro
            return result, int((time.perf_counter() - t_stage) * 1000)

        scoring_json: dict[str, Any] | None = None
        remediation_json: dict[str, Any] | None = None
        scoring_ms = 0
        remediation_ms = 0
        for attempt in range(stage_attempts):
            try:
                (scoring_json, scoring_ms), (remediation_json, remediation_ms) = await asyncio.gather(
                    _timed(
                        "scoring",
                        _run_with_timeout(
                            _run_scoring_once(),
                            timeout_s=scoring_timeout,
                            stage="scoring",
                            run_id=run_id,
                        ),
                    ),
                    _timed(
                        "remediation",
                        _run_with_timeout(
                            _run_remediation_once(),
                            timeout_s=remediation_timeout,
                            stage="remediation",
                            run_id=run_id,
                        ),
                    ),
                )
                break
            except ChatStageError:
                if attempt >= stage_attempts - 1:
                    raise

        assert scoring_json is not None
        assert remediation_json is not None

        scoring_out_tokens = count_tokens(json.dumps(scoring_json, ensure_ascii=False))
        remediation_out_tokens = count_tokens(json.dumps(remediation_json, ensure_ascii=False))

        sc_in_u, sc_out_u, sc_tot_u = scoring_usage_from_provider
        re_in_u, re_out_u, re_tot_u = remediation_usage_from_provider

        scoring_usage = {
            "input_tokens": sc_in_u if sc_in_u is not None else scoring_in_tokens,
            "output_tokens": sc_out_u if sc_out_u is not None else scoring_out_tokens,
            "total_tokens": sc_tot_u
            if sc_tot_u is not None
            else (sc_in_u if sc_in_u is not None else scoring_in_tokens)
            + (sc_out_u if sc_out_u is not None else scoring_out_tokens),
        }
        remediation_usage = {
            "input_tokens": re_in_u if re_in_u is not None else remediation_in_tokens,
            "output_tokens": re_out_u if re_out_u is not None else remediation_out_tokens,
            "total_tokens": re_tot_u
            if re_tot_u is not None
            else (re_in_u if re_in_u is not None else remediation_in_tokens)
            + (re_out_u if re_out_u is not None else remediation_out_tokens),
        }

        _persist_stage(
            run_id=run_id,
            cache_key=cache_key,
            repo_path=target_repo_path,
            repo_state_hash=repo_state_hash,
            stage="scoring",
            tool_input=shared_input,
            tool_output=scoring_json,
        )
        _persist_stage(
            run_id=run_id,
            cache_key=cache_key,
            repo_path=target_repo_path,
            repo_state_hash=repo_state_hash,
            stage="remediation",
            tool_input=shared_input,
            tool_output=remediation_json,
        )

        models = {
            "orchestrator": {
                "provider": settings.active_orchestrator_config.provider,
                "model": settings.active_orchestrator_config.model_id,
            },
            "cypher": {
                "provider": settings.active_cypher_config.provider,
                "model": settings.active_cypher_config.model_id,
            },
        }

        return {
            "run_id": run_id,
            "evidence": {
                **evidence_output,
                "timings_ms": evidence_ms,
                "token_usage": evidence_usage,
            },
            "scoring": {
                **scoring_json,
                "timings_ms": scoring_ms,
                "token_usage": scoring_usage,
            },
            "remediation": {
                **remediation_json,
                "timings_ms": remediation_ms,
                "token_usage": remediation_usage,
            },
            "models": models,
            "schema_version": "1",
        }
