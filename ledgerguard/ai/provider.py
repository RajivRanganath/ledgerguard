"""AI provider interface.

The rest of LedgerGuard depends on exactly one function shape:

    investigate(context: dict) -> InvestigationResult

Several implementations ship. ``AnthropicProvider`` calls Claude through the
Anthropic SDK, ``OpenAICompatibleProvider`` and ``GeminiProvider`` (in
``openai_compatible.py``) cover every other host, and ``FallbackProvider`` (in
``fallback.py``) chains them so one running out of quota does not end the run.
``HeuristicProvider`` is an offline stand-in that lets the whole pipeline,
benchmark and demo run with no API key present -- it is deliberately naive,
because a naive investigator is exactly what the Evidence Gate has to survive.
Every result records which provider produced it; the benchmark report prints
that, so a stub run is never mistaken for a model run.
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

    def __init__(self, error: str = "no investigator provider is configured") -> None:
        self.error = error

    def investigate(self, context: dict) -> InvestigationResult:
        return InvestigationResult.unavailable(self.error)


#: Preference order for the automatic fallback chain, in the order requested.
#:
#: Groq first: fastest of the reliable ones (~1.4s) and it does not cache.
#: Gemini second: fast when its free-tier quota is intact.
#: NVIDIA third: currently unusable with the available key (endpoints are
#:   end-of-life, replacements not provisioned) but it fails in ~0.1s and is
#:   retired after two attempts, so it costs almost nothing to keep in place for
#:   when the entitlement is fixed.
#: OmniRoute last: reaches Mistral, but is the slowest by a wide margin
#:   (p50 ~53s uncached) and caches temperature-0 responses.
#:
#: Anthropic and Cerebras are deliberately not in the chain -- no Anthropic key
#: exists, and the Cerebras key returns HTTP 402. Both remain fully supported as
#: an explicit --provider choice; only the automatic order excludes them.
AUTO_ORDER = ["groq", "gemini", "nvidia", "omniroute"]


def _build(kind: str) -> InvestigatorProvider:
    if kind == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set")
        return AnthropicProvider(model=os.environ.get("LEDGERGUARD_MODEL", DEFAULT_MODEL))
    if kind == "gemini":
        from .openai_compatible import GeminiProvider

        return GeminiProvider(model=os.environ.get("LEDGERGUARD_MODEL", "gemini-2.5-flash"))
    from .openai_compatible import PRESETS, OpenAICompatibleProvider

    if kind == "omniroute":
        # Local router: a configured key proves nothing if the process is not
        # running, so reachability is part of "is this provider available".
        # Without this probe, auto-detection would select a dead endpoint and
        # every investigation would degrade to abstention.
        import httpx

        try:
            httpx.get("http://localhost:20128/v1/models", timeout=3.0).raise_for_status()
        except Exception as exc:
            raise ValueError(f"omniroute is not reachable on localhost:20128 ({exc})")

    if kind in PRESETS:
        return OpenAICompatibleProvider(
            preset=kind, model=os.environ.get("LEDGERGUARD_MODEL") or None
        )
    raise ValueError(f"unknown provider {kind!r}")


def build_chain(order: list[str] | None = None) -> list[InvestigatorProvider]:
    """Every provider that is actually configured, in preference order."""
    providers = []
    for candidate in order or AUTO_ORDER:
        try:
            providers.append(_build(candidate))
        except Exception:
            continue
    return providers


def get_provider(kind: str | None = None) -> InvestigatorProvider:
    """Select an investigator provider.

    ``kind`` (or LEDGERGUARD_PROVIDER) may be:
      anthropic | omniroute | groq | cerebras | gemini | nvidia |
      openai_compatible   -- one specific provider
      fallback            -- chain every configured provider, failing over
      stub                -- the offline stand-in
      none                -- a dead provider, for testing degradation
      auto                -- fallback across everything configured, else the stub

    A provider that is named explicitly but cannot be configured is an error,
    not a silent downgrade: a run that was meant to measure a model must never
    quietly report stub numbers instead.
    """
    kind = (kind or os.environ.get("LEDGERGUARD_PROVIDER") or "auto").lower()
    if kind == "none":
        return UnavailableProvider()
    if kind == "stub":
        return HeuristicProvider()

    if kind not in ("auto", "fallback"):
        return _build(kind)

    from .fallback import FallbackProvider

    chain = build_chain()
    if not chain:
        if kind == "fallback":
            raise ValueError("no providers are configured, so no chain can be built")
        return HeuristicProvider()
    if len(chain) == 1 and kind == "auto":
        return chain[0]
    return FallbackProvider(chain)
