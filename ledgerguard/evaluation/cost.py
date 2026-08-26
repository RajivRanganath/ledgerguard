"""Cost estimation for the AI layer.

Two numbers matter and they are different: what an investigation *costs*, and
how *often* the system needs one. The architecture's cost argument is the second
one -- the deterministic engine resolves most cases for free, so the model is
only paid for genuinely ambiguous exceptions.

Prices are published list rates per million tokens, recorded here with the date
they were read. Every provider actually used in this project was on a free tier,
so the figures below are what the same workload *would* cost at list price, not
what was spent. That distinction is kept explicit rather than rounded away.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: USD per million tokens (input, output), with the date the rate was recorded.
#: Free tiers were used for every run in this project; these are list prices.
PRICES_USD_PER_MTOK: dict[str, tuple[str, str]] = {
    "openai/gpt-oss-120b": ("0.15", "0.75"),
    "openai/gpt-oss-20b": ("0.10", "0.50"),
    "gemini-2.5-flash": ("0.30", "2.50"),
    "mistral-large-latest": ("2.00", "6.00"),
    "mistral-medium-3-5": ("0.40", "2.00"),
    "mistral-small-latest": ("0.10", "0.30"),
}
PRICES_RECORDED = "2026-08-26"
DEFAULT_PRICE = ("0.50", "1.50")     # used when a model is not in the table


@dataclass
class CostEstimate:
    model: str
    prompt_tokens: int
    completion_tokens: int
    investigations: int
    records: int
    priced_at: tuple[str, str]
    is_list_price_estimate: bool = True

    @property
    def usd(self) -> Decimal:
        p_in, p_out = (Decimal(x) for x in self.priced_at)
        return (
            Decimal(self.prompt_tokens) * p_in + Decimal(self.completion_tokens) * p_out
        ) / Decimal(1_000_000)

    @property
    def usd_per_100_records(self) -> Decimal:
        if not self.records:
            return Decimal(0)
        return self.usd * Decimal(100) / Decimal(self.records)

    @property
    def tokens_per_investigation(self) -> int:
        if not self.investigations:
            return 0
        return (self.prompt_tokens + self.completion_tokens) // self.investigations

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "investigations": self.investigations,
            "records": self.records,
            "tokens_per_investigation": self.tokens_per_investigation,
            "list_price_usd_per_mtok": {"input": self.priced_at[0], "output": self.priced_at[1]},
            "prices_recorded": PRICES_RECORDED,
            "estimated_usd": f"{self.usd:.6f}",
            "estimated_usd_per_100_records": f"{self.usd_per_100_records:.6f}",
            "note": (
                "List-price estimate. Every run in this project used a free tier, "
                "so nothing was actually charged."
            ),
        }


def estimate(provider, investigations: int, records: int) -> list[CostEstimate]:
    """Build a per-model estimate from whatever usage the provider reported."""
    providers = getattr(provider, "providers", None) or [provider]
    out: list[CostEstimate] = []
    for p in providers:
        prompt = getattr(p, "prompt_tokens", 0)
        completion = getattr(p, "completion_tokens", 0)
        if not (prompt or completion):
            continue
        model = getattr(p, "model", getattr(p, "name", "unknown")).split("/")[-1]
        out.append(
            CostEstimate(
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                investigations=investigations,
                records=records,
                priced_at=PRICES_USD_PER_MTOK.get(model, DEFAULT_PRICE),
            )
        )
    return out
