"""Data drift evaluation.

The Shadow Ledger's power comes from computing what a settlement *should* be.
That power is also its exposure: the expectation is built on assumptions -- a 2%
platform fee, 18% GST on it, T+2 settlement -- and the provider is free to
change any of them without telling us.

This measures what happens when the world moves and the controller's assumptions
do not. Each scenario takes the clean lifecycles (which reconcile perfectly
today), rewrites them under a shifted world, and re-runs the unchanged
controller. Every exception it raises is a false positive caused by drift.

Clean lifecycles only, deliberately: mixing in injected faults would confuse
"the controller is wrong about the world" with "the data is genuinely broken".

    python -m ledgerguard.evaluation.drift
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from ..ledger.money import apply_rate, gst_on_fee, platform_fee
from ..pipeline import run
from ..reconciliation.matcher import MATCHED
from ..synthetic.fault_injector import build_dataset
from ..synthetic.generator import Dataset, FaultClass

OUTPUT_DIR = Path(__file__).parent / "outputs"
GST = Decimal("0.18")


@dataclass
class DriftResult:
    scenario: str
    change: str
    cases: int
    matched: int
    exceptions: int
    false_exception_rate: float
    dominant_exception: str
    detected_as: dict
    verdict: str


def _clean_only(dataset: Dataset) -> Dataset:
    ids = [
        lc.lifecycle_id
        for lc in dataset.lifecycles
        if dataset.ground_truth[lc.lifecycle_id].fault_class == FaultClass.NONE
    ]
    return dataset.subset(ids)


def _rebuild(lc, *, fee_rate: Decimal | None = None, lag_days: int | None = None,
             amount_scale: int = 1):
    """Rewrite one clean lifecycle under a shifted world, keeping it consistent."""
    if amount_scale != 1:
        lc.order.amount *= amount_scale
        lc.payment.amount *= amount_scale
        for r in lc.refunds:
            r.amount *= amount_scale

    amount = lc.payment.amount
    fee = apply_rate(amount, fee_rate) if fee_rate is not None else platform_fee(amount)
    tax = apply_rate(fee, GST)
    lc.payment.fee, lc.payment.tax = fee, tax

    refund_total = sum(r.amount for r in lc.refunds)
    net = amount - fee - tax - refund_total

    s = lc.settlement
    if s is None:
        return
    s.gross_amount, s.fees, s.tax = amount, fee, tax
    s.refund_adjustments, s.net_amount = refund_total, net
    if lag_days is not None and lc.payment.captured_at is not None:
        s.settlement_date = lc.payment.captured_at + timedelta(days=lag_days)
    for e in lc.bank_entries:
        e.amount = net
        e.date = s.settlement_date


def evaluate(scenario: str, change: str, **shift) -> DriftResult:
    dataset = _clean_only(build_dataset())
    for lc in dataset.lifecycles:
        _rebuild(lc, **shift)

    report = run(
        dataset.batch(), use_ai=False,
        lifecycle_by_payment=dataset.lifecycle_by_payment(),
    )
    matched = sum(1 for o in report.outcomes if o.state == MATCHED)
    total = len(report.outcomes)
    exceptions = total - matched

    detected: dict[str, int] = {}
    for o in report.outcomes:
        if o.case.exception:
            key = o.case.exception.exception_type.value
            detected[key] = detected.get(key, 0) + 1
    dominant = max(detected, key=detected.get) if detected else "—"
    rate = round(exceptions / total, 4) if total else 0.0

    if rate == 0:
        verdict = "absorbed: the controller is indifferent to this shift"
    elif rate < 0.5:
        verdict = "partially absorbed"
    else:
        verdict = "NOT absorbed: the assumption is load-bearing and now wrong"

    return DriftResult(
        scenario=scenario, change=change, cases=total, matched=matched,
        exceptions=exceptions, false_exception_rate=rate,
        dominant_exception=dominant, detected_as=detected, verdict=verdict,
    )


SCENARIOS = [
    ("baseline", "no change (control)", {}),
    ("fee_rate_2.25pct", "platform fee 2.00% -> 2.25%", {"fee_rate": Decimal("0.0225")}),
    ("fee_rate_2.5pct", "platform fee 2.00% -> 2.50%", {"fee_rate": Decimal("0.025")}),
    ("fee_rate_1.75pct", "platform fee 2.00% -> 1.75% (in our favour)", {"fee_rate": Decimal("0.0175")}),
    ("settlement_T3", "settlement lag T+2 -> T+3", {"lag_days": 3}),
    ("settlement_T4", "settlement lag T+2 -> T+4", {"lag_days": 4}),
    ("amounts_x10", "ticket sizes 10x larger", {"amount_scale": 10}),
]


def render(results: list[DriftResult]) -> str:
    lines = [
        "# Data drift evaluation",
        "",
        "Clean lifecycles only, rewritten under a shifted world and re-run against",
        "the *unchanged* controller. Every exception below is a false positive",
        "caused by the controller's assumptions no longer matching reality.",
        "",
        "Generated by `python -m ledgerguard.evaluation.drift`.",
        "",
        "| Scenario | Change | Cases | Matched | False exceptions | Rate | Raised as |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| `{r.scenario}` | {r.change} | {r.cases} | {r.matched} | {r.exceptions} | "
            f"{r.false_exception_rate:.1%} | {r.dominant_exception.replace('EXCEPTION_', '')} |"
        )
    lines += ["", "## Reading it", ""]
    for r in results:
        lines.append(f"- **`{r.scenario}`** — {r.verdict}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Data drift evaluation").parse_args(argv)
    results = [evaluate(name, change, **shift) for name, change, shift in SCENARIOS]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    md = render(results)
    (OUTPUT_DIR / "drift.md").write_text(md)
    (OUTPUT_DIR / "drift.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2, default=str) + "\n"
    )
    print(md)
    print(f"Artifacts written to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
