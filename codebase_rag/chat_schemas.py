from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _AllowExtra(BaseModel):
    model_config = ConfigDict(extra="allow")


class EvidenceFinding(_AllowExtra):
    finding_id: str
    tool: str | None = None
    vulnerability_type: str | None = None
    severity_reported: str | None = None
    file: str | None = None
    justification: dict[str, Any]


class ScoringFinding(_AllowExtra):
    finding_id: str
    tool: str | None = None
    vulnerability_type: str | None = None
    severity_reported: str | None = None
    file: str | None = None
    analysis: dict[str, Any]


class RemediationFinding(_AllowExtra):
    finding_id: str
    tool: str | None = None
    vulnerability_type: str | None = None
    severity_reported: str | None = None
    file: str | None = None
    remediation: dict[str, Any]


class TokenUsage(BaseModel):
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    total_tokens: int = Field(..., ge=0)


class EvidenceToolOutput(BaseModel):
    """LLM output contract for evidence stage (no meta fields)."""

    findings: list[EvidenceFinding]


class ScoringToolOutput(BaseModel):
    """LLM output contract for scoring stage (no meta fields)."""

    findings: list[ScoringFinding]


class RemediationToolOutput(BaseModel):
    """LLM output contract for remediation stage (no meta fields)."""

    findings: list[RemediationFinding]


class EvidenceStage(BaseModel):
    findings: list[EvidenceFinding]
    timings_ms: int = Field(..., ge=0)
    token_usage: TokenUsage


class ScoringStage(BaseModel):
    findings: list[ScoringFinding]
    timings_ms: int = Field(..., ge=0)
    token_usage: TokenUsage


class RemediationStage(BaseModel):
    findings: list[RemediationFinding]
    timings_ms: int = Field(..., ge=0)
    token_usage: TokenUsage


class ModelDescriptor(BaseModel):
    provider: str
    model: str


class ChatModels(BaseModel):
    orchestrator: ModelDescriptor
    cypher: ModelDescriptor


class ChatResponsePayload(BaseModel):
    schema_version: str = "1"
    run_id: UUID
    evidence: EvidenceStage
    scoring: ScoringStage
    remediation: RemediationStage
    models: ChatModels


class ApiErrorDetail(BaseModel):
    code: str
    message: str
    run_id: UUID | None = None


class ApiErrorResponse(BaseModel):
    error: ApiErrorDetail

