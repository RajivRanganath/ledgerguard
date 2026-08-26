"""Automatic failover across investigator providers.

Free tiers run out, upstream credentials get deactivated mid-run, and routers
resolve aliases to whatever happens to be alive. A single provider is therefore
not a reliable investigator, and the failure is not interesting -- it is just
noise that turns resolvable cases into abstentions.

This wraps an ordered chain. Each case is offered to the first healthy provider;
if that provider reports itself unusable, the case falls through to the next.

Two properties matter and are deliberate:

* **A provider that fails is retired for the run, not retried per case.**
  Without that, every remaining case pays the full retry budget of every dead
  provider ahead of it -- on a 15 case run with 45s of backoff that is the
  difference between a minute and half an hour.
* **Falling back never changes what counts as proof.** The chain only decides
  *who gets asked*. The Evidence Gate re-derives every hypothesis from records
  regardless of which provider produced it, so a weaker fallback provider can
  cost recall but cannot cost safety.
"""

from __future__ import annotations

from collections import Counter

from .schemas import InvestigationResult

#: Consecutive unusable results before a provider is retired for the run.
FAILURES_BEFORE_RETIRING = 2


class FallbackProvider:
    """Try each provider in order until one produces a usable investigation."""

    def __init__(self, providers: list, failures_before_retiring: int = FAILURES_BEFORE_RETIRING):
        if not providers:
            raise ValueError("fallback chain is empty")
        self.providers = list(providers)
        self.failures_before_retiring = failures_before_retiring

        # Waiting out a rate limit only makes sense on the last link. Anywhere
        # else, the cheaper move is to ask the next provider immediately --
        # otherwise a dead free tier costs the full backoff budget on every
        # case before the chain even reaches a working provider.
        for provider in self.providers[:-1]:
            if hasattr(provider, "max_retries"):
                provider.max_retries = 1
        self._consecutive_failures: Counter = Counter()
        self._retired: dict[str, str] = {}
        #: How many investigations each provider actually answered.
        self.served_by: Counter = Counter()
        # Short enough for a metrics table; report() carries the full detail.
        short = [p.name.split(":", 1)[0] for p in self.providers]
        self.name = "fallback(" + "->".join(short) + ")"

    @property
    def active(self) -> list:
        return [
            p for p in self.providers if p.name.split(":", 1)[0] not in self._retired
        ]

    def _retire(self, provider, reason: str) -> None:
        self._retired.setdefault(provider.name.split(":", 1)[0], reason)

    def investigate(self, context: dict) -> InvestigationResult:
        last: InvestigationResult | None = None

        for provider in self.active:
            key = provider.name.split(":", 1)[0]
            try:
                result = provider.investigate(context)
            except Exception as exc:
                # A provider that raises rather than returns is still just an
                # unusable provider; it must not take the batch down with it.
                result = InvestigationResult.unavailable(
                    f"{type(exc).__name__}: {exc}"
                )

            if result.source in ("model", "heuristic_stub"):
                self._consecutive_failures[key] = 0
                # Key on the model that actually answered, not on the provider's
                # starting route -- after rotation those are different things.
                served = result.model_name or provider.name
                self.served_by[f"{key}:{served}"] += 1
                return result

            last = result
            self._consecutive_failures[key] += 1
            if self._consecutive_failures[key] >= self.failures_before_retiring:
                self._retire(provider, result.error or "repeated unusable responses")

        # Nothing in the chain could answer. Abstain, and say why.
        if last is None:
            return InvestigationResult.unavailable(
                "every provider in the fallback chain was already retired"
            )
        detail = "; ".join(f"{name}: {why}" for name, why in self._retired.items())
        return InvestigationResult.unavailable(
            f"all providers exhausted ({detail or last.error})"
        )

    def complete_json(
        self, system: str, user: str, schema: dict, name: str = "response"
    ) -> tuple[dict | None, str | None, str | None]:
        """Structured JSON completion, with the same failover as investigate().

        Providers that cannot do arbitrary structured output (Gemini's
        generateContent path here) are skipped rather than treated as failures,
        so they do not consume the retirement budget for something they were
        never asked to do.
        """
        last_error = "no provider in the chain supports raw JSON completion"
        for provider in self.active:
            if not hasattr(provider, "complete_json"):
                continue
            key = provider.name.split(":", 1)[0]
            try:
                payload, served, error = provider.complete_json(system, user, schema, name)
            except Exception as exc:
                payload, served, error = None, None, f"{type(exc).__name__}: {exc}"
            if payload is not None:
                self._consecutive_failures[key] = 0
                self.served_by[f"{key}:{served or provider.name}"] += 1
                return payload, served, None
            last_error = error or last_error
            self._consecutive_failures[key] += 1
            if self._consecutive_failures[key] >= self.failures_before_retiring:
                self._retire(provider, last_error)
        return None, None, last_error

    def report(self) -> dict:
        """Who answered, and who dropped out. Printed alongside every run."""
        return {
            "chain": [p.name for p in self.providers],
            "served_by": dict(self.served_by),
            "retired": dict(self._retired),
            "tokens": {
                p.name.split(":", 1)[0]: {
                    "prompt": p.prompt_tokens,
                    "completion": p.completion_tokens,
                }
                for p in self.providers
                if getattr(p, "prompt_tokens", 0) or getattr(p, "completion_tokens", 0)
            },
            "cache_hits": {
                p.name.split(":", 1)[0]: p.cache_hits
                for p in self.providers
                if getattr(p, "cache_hits", 0)
            },
            "routes_exhausted": {
                p.name.split(":", 1)[0]: dict(getattr(p, "exhausted", {}) or {})
                for p in self.providers
                if getattr(p, "exhausted", None)
            },
        }
