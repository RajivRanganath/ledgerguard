"""Structured contract for the AI investigator.

The model returns JSON in this shape and nothing else. If it returns anything
that does not validate, the investigation is treated as unavailable and the
case abstains -- a malformed model response must never become a resolution.

Note what is deliberately absent: there is no money field the model can write,
no confidence score that the rest of the system consumes as a probability, and
no field that lets the model assert a fact rather than point at a record.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..reconciliation.exceptions import PERMITTED_HYPOTHESES


class RequiredEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str                                 # refund | settlement | bank_entry | payment
    expected_relationship: str                # human readable claim the gate re-checks


class InvestigatorOutput(BaseModel):
    """The exact JSON schema handed to the model as a structured output format.

    Kept separate from InvestigationResult so that the runtime-only fields
    (source, model_name, error) can never be written by the model.
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis: Literal[
        "unlinked_partial_refund",
        "fee_or_tax_variance",
        "duplicate_deduction",
        "timing_difference",
        "unexplained_deduction",
        "insufficient_evidence",
    ]
    reason: str
    required_evidence: list[RequiredEvidence]
    candidate_evidence_ids: list[str]
    recommended_action: Literal["resolve", "review"]


class InvestigationResult(BaseModel):
    """What the investigator is allowed to say."""

    model_config = ConfigDict(extra="ignore")

    hypothesis: str
    reason: str = Field(max_length=600)
    required_evidence: list[RequiredEvidence] = Field(default_factory=list)
    candidate_evidence_ids: list[str] = Field(default_factory=list)
    recommended_action: Literal["resolve", "review"] = "review"

    #: Set by the runtime, never by the model.
    source: Literal["model", "heuristic_stub", "unavailable", "invalid_response"] = "model"
    model_name: Optional[str] = None
    error: Optional[str] = None

    @field_validator("hypothesis")
    @classmethod
    def _known_hypothesis(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in PERMITTED_HYPOTHESES:
            raise ValueError(
                f"hypothesis {v!r} is outside the permitted taxonomy {PERMITTED_HYPOTHESES}"
            )
        return v

    @field_validator("candidate_evidence_ids")
    @classmethod
    def _bounded_candidates(cls, v: list[str]) -> list[str]:
        if len(v) > 10:
            raise ValueError("too many candidate evidence ids")
        return [str(x).strip() for x in v if str(x).strip()]

    @classmethod
    def unavailable(cls, error: str) -> "InvestigationResult":
        """Used when the provider is missing, times out, or fails."""
        return cls(
            hypothesis="insufficient_evidence",
            reason=f"AI investigator unavailable: {error}",
            required_evidence=[],
            candidate_evidence_ids=[],
            recommended_action="review",
            source="unavailable",
            error=error,
        )

    @classmethod
    def invalid(cls, error: str) -> "InvestigationResult":
        """Used when the model returned something that does not validate."""
        return cls(
            hypothesis="insufficient_evidence",
            reason=f"AI response rejected: {error}",
            required_evidence=[],
            candidate_evidence_ids=[],
            recommended_action="review",
            source="invalid_response",
            error=error,
        )
