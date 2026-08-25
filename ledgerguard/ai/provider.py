"""AI provider interface.

The rest of LedgerGuard depends on exactly one function shape:

    investigate(context: dict) -> InvestigationResult

Two implementations ship. ``AnthropicProvider`` calls Claude with a structured
output schema. ``HeuristicProvider`` is an offline stand-in that lets the whole
pipeline, benchmark and demo run with no API key present -- it is deliberately
naive, because a naive investigator is exactly what the Evidence Gate has to
survive. Every result records which provider produced it; the benchmark report
prints that, so a stub run is never mistaken for a model run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from .schemas import InvestigationResult, InvestigatorOutput, RequiredEvidence

def _load_dotenv() -> None:
    """Read .env from the repo root if present. Never overrides a real env var."""
    path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_MAX_TOKENS = 2000

SYSTEM_PROMPT = """You are the exception investigator inside LedgerGuard, a payments \
reconciliation controller.

A deterministic engine has already reconciled everything it could prove. You only \
ever see exceptions it could not resolve. Your job is to propose the single most \
likely explanation and point at the specific records that would prove it. You are \
not the decision maker: an independent Evidence Gate re-checks every claim you make \
against the actual records, and it will reject you if the evidence does not hold.

Rules you must follow:
- Never perform monetary arithmetic. The expected values you are given were computed \
by an independent Shadow Ledger; use them, do not recompute them.
- Never invent an identifier. Every id in candidate_evidence_ids must appear verbatim \
somewhere in the context you were given.
- Never force a resolution. If the available records do not contain enough evidence to \
prove an explanation, return hypothesis "insufficient_evidence" and \
recommended_action "review".
- A candidate refund matching the discrepancy on amount is not proof of linkage. Say \
what relationship would have to hold for your hypothesis to be true, and let the gate \
check it.
- recommended_action "resolve" is a request, not a decision. Use it only when you \
believe every required_evidence item can be verified from the records shown."""


class InvestigatorProvider(Protocol):
    name: str

    def investigate(self, context: dict) -> InvestigationResult:  # pragma: no cover
        ...


def _to_result(out: InvestigatorOutput, source: str, model_name: str | None) -> InvestigationResult:
    return InvestigationResult(
        hypothesis=out.hypothesis,
        reason=out.reason,
        required_evidence=out.required_evidence,
        candidate_evidence_ids=out.candidate_evidence_ids,
        recommended_action=out.recommended_action,
        source=source,
        model_name=model_name,
    )


class AnthropicProvider:
    """Claude-backed investigator using structured outputs."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        import anthropic

        self.name = f"anthropic:{model}"
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(timeout=timeout, max_retries=1)

    def investigate(self, context: dict) -> InvestigationResult:
        prompt = (
            "Investigate this reconciliation exception.\n\n"
            + json.dumps(context, indent=2, default=str)
        )
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_format=InvestigatorOutput,
            )
        except self._anthropic.APITimeoutError as exc:
            return InvestigationResult.unavailable(f"timeout after {self.timeout}s: {exc}")
        except self._anthropic.APIStatusError as exc:
            return InvestigationResult.unavailable(f"api error {exc.status_code}")
        except self._anthropic.APIConnectionError as exc:
            return InvestigationResult.unavailable(f"connection error: {exc}")
        except Exception as exc:                      # provider failure of any kind
            return InvestigationResult.unavailable(f"{type(exc).__name__}: {exc}")

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return InvestigationResult.invalid("model returned no parseable output")
        try:
            return _to_result(parsed, source="model", model_name=self.model)
        except Exception as exc:
            return InvestigationResult.invalid(str(exc))


class HeuristicProvider:
    """Offline stand-in. Deliberately naive: matches on amount, not on linkage.

    This is not a model and is never reported as one. It exists so the pipeline
    runs without an API key, and so the adversarial case can be demonstrated
    against the *worst plausible* investigator rather than a lucky one.
    """

    name = "heuristic_stub"

    def investigate(self, context: dict) -> InvestigationResult:
        candidates = context.get("candidate_evidence", []) or []
        shortfall = context.get("discrepancy", {}).get("difference_paise")
        exc_type = context.get("exception", {}).get("type", "")

        refund_matches = [
            c
            for c in candidates
            if c.get("record_type") == "refund" and c.get("amount") == shortfall
        ]
        if refund_matches:
            picked = refund_matches[0]
            out = InvestigatorOutput(
                hypothesis="unlinked_partial_refund",
                reason=(
                    f"Refund {picked['refund_id']} is for {picked['amount']} paise, exactly the "
                    f"unexplained shortfall, and carries no payment linkage. It most likely "
                    f"belongs to this payment and lost its link during ingestion."
                ),
                required_evidence=[
                    RequiredEvidence(
                        type="refund",
                        expected_relationship="refund belongs to this payment (customer and order must agree)",
                    ),
                    RequiredEvidence(
                        type="refund",
                        expected_relationship="refund.amount == shadow ledger shortfall",
                    ),
                    RequiredEvidence(
                        type="refund",
                        expected_relationship="refund.created_at is after capture and before settlement",
                    ),
                ],
                candidate_evidence_ids=[picked["refund_id"]],
                recommended_action="resolve",
            )
            return _to_result(out, source="heuristic_stub", model_name=None)

        if exc_type == "EXCEPTION_UNEXPLAINED_SHORTFALL":
            out = InvestigatorOutput(
                hypothesis="unexplained_deduction",
                reason=(
                    "The settlement deducted an amount the shadow ledger cannot account for, "
                    "and no candidate record explains it."
                ),
                required_evidence=[],
                candidate_evidence_ids=[],
                recommended_action="review",
            )
            return _to_result(out, source="heuristic_stub", model_name=None)

        out = InvestigatorOutput(
            hypothesis="insufficient_evidence",
            reason="No candidate evidence was supplied for this exception type.",
            required_evidence=[],
            candidate_evidence_ids=[],
            recommended_action="review",
        )
        return _to_result(out, source="heuristic_stub", model_name=None)


class UnavailableProvider:
    """Stands in for a completely dead provider. Used by the failure-mode test."""

    name = "unavailable"

    def __init__(self, error: str = "no ANTHROPIC_API_KEY configured") -> None:
        self.error = error

    def investigate(self, context: dict) -> InvestigationResult:
        return InvestigationResult.unavailable(self.error)


#: Auto-detection order. Anthropic first because that is the reference
#: implementation; the rest are ordinary OpenAI-compatible or Gemini hosts.
AUTO_ORDER = ["anthropic", "groq", "cerebras", "gemini", "nvidia"]


def _build(kind: str) -> InvestigatorProvider:
    if kind == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set")
        return AnthropicProvider(model=os.environ.get("LEDGERGUARD_MODEL", DEFAULT_MODEL))
    if kind == "gemini":
        from .openai_compatible import GeminiProvider

        return GeminiProvider(model=os.environ.get("LEDGERGUARD_MODEL", "gemini-2.5-flash"))
    from .openai_compatible import PRESETS, OpenAICompatibleProvider

    if kind in PRESETS:
        return OpenAICompatibleProvider(
            preset=kind, model=os.environ.get("LEDGERGUARD_MODEL") or None
        )
    raise ValueError(f"unknown provider {kind!r}")


def get_provider(kind: str | None = None) -> InvestigatorProvider:
    """Select an investigator provider.

    ``kind`` (or LEDGERGUARD_PROVIDER) may be:
      anthropic | groq | cerebras | gemini | nvidia | openai_compatible
      stub  -- the offline stand-in
      none  -- a dead provider, for testing degradation
      auto  -- first configured provider in AUTO_ORDER, else the stub

    A provider that is named explicitly but cannot be configured is an error,
    not a silent downgrade: a run that was meant to measure a model must never
    quietly report stub numbers instead.
    """
    kind = (kind or os.environ.get("LEDGERGUARD_PROVIDER") or "auto").lower()
    if kind == "none":
        return UnavailableProvider()
    if kind == "stub":
        return HeuristicProvider()

    if kind != "auto":
        return _build(kind)

    for candidate in AUTO_ORDER:
        try:
            return _build(candidate)
        except Exception:
            continue
    return HeuristicProvider()
