"""Ablation study: what is each component actually worth?

Five arms over the same frozen holdout. Each removes one thing:

  rules_only        no AI at all
  no_shadow_ledger  no independent expectation; trust the provider's own numbers
  llm_only          no deterministic engine; the model decides every case
  hybrid_no_gate    AI investigates, but its conclusions are taken on trust
  hybrid            the full system

The arm that matters is `hybrid_no_gate`. Everything else in this project is
arguable; the difference between that arm and `hybrid` is the entire safety
claim, measured in rupees.

    python -m ledgerguard.evaluation.ablation
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..ai.provider import get_provider
from ..evidence.safety import AUTO_RESOLVED, HUMAN_REVIEW_REQUIRED
from ..ledger.invariants import (
    bank_credit_matches_settlement,
    settlement_arithmetic_consistent,
)
from ..ledger.models import PaymentStatus
from ..ledger.money import to_rupees_str
from ..ledger.shadow_ledger import ShadowLedger
from ..pipeline import run
from ..synthetic.fault_injector import build_dataset, split
from ..synthetic.generator import Dataset, FaultClass

OUTPUT_DIR = Path(__file__).parent / "outputs"

MATCHED = "MATCHED"
GT_AUTO = "AUTO_RESOLVED"
GT_HUMAN = "HUMAN_REVIEW_REQUIRED"
GT_NONE = "NONE"

#: Output shape for the LLM-only arm. Deliberately gives the model the option to
#: say "reconciled", which the investigator taxonomy does not need.
LLM_ONLY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["reconciled", "discrepancy"]},
        "action": {"type": "string", "enum": ["close", "escalate"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "action", "reason"],
    "additionalProperties": False,
}

LLM_ONLY_SYSTEM = """You are a payments reconciliation controller. You are shown the \
raw records for one captured payment: the order, the payment, any refunds, the \
settlement the provider reported, and the bank entries.

Decide whether this payment reconciled correctly.

- "reconciled" means the settlement and bank credit are exactly what they should be.
- "discrepancy" means something is wrong, missing, duplicated, mistimed, or unexplained.
- action "close" means you are confident enough to close the case with no human.
- action "escalate" means a human must look at it.

There is no separate verification step. Your decision is final."""


@dataclass
class ArmResult:
    name: str
    description: str
    total: int = 0
    matched: int = 0
    auto_resolved: int = 0
    escalated: int = 0
    correct: int = 0
    accuracy: float = 0.0
    false_auto: int = 0
    false_auto_value_paise: int = 0
    missed_faults: int = 0
    false_exceptions: int = 0
    unavailable: int = 0
    per_fault_class: dict = field(default_factory=dict)
    note: str = ""

    @property
    def false_auto_value_inr(self) -> str:
        return to_rupees_str(self.false_auto_value_paise)


def score(
    name: str,
    description: str,
    states: dict[str, tuple[str, int]],
    dataset: Dataset,
    note: str = "",
    unavailable: int = 0,
) -> ArmResult:
    """Score one arm. ``states`` maps payment_id -> (state, exposure_paise)."""
    truth = {
        lc.payment.payment_id: dataset.ground_truth[lc.lifecycle_id]
        for lc in dataset.lifecycles
    }
    arm = ArmResult(name=name, description=description, note=note, unavailable=unavailable)
    per: dict[str, dict] = {}

    for payment_id, (state, exposure) in states.items():
        gt = truth[payment_id]
        bucket = per.setdefault(gt.fault_class, {"n": 0, "correct": 0, "false_auto": 0})
        bucket["n"] += 1
        arm.total += 1
        is_clean = gt.fault_class == FaultClass.NONE

        if state == MATCHED:
            arm.matched += 1
            if is_clean:
                arm.correct += 1
                bucket["correct"] += 1
            else:
                arm.missed_faults += 1
            continue

        if is_clean:
            arm.false_exceptions += 1

        if state == AUTO_RESOLVED:
            arm.auto_resolved += 1
            if gt.expected_disposition == GT_AUTO:
                arm.correct += 1
                bucket["correct"] += 1
            else:
                arm.false_auto += 1
                bucket["false_auto"] += 1
                arm.false_auto_value_paise += max(exposure, gt.exposure_paise)
        else:
            arm.escalated += 1
            if gt.expected_disposition == GT_HUMAN:
                arm.correct += 1
                bucket["correct"] += 1

    arm.accuracy = round(arm.correct / arm.total, 4) if arm.total else 0.0
    arm.per_fault_class = dict(sorted(per.items()))
    return arm


def _states_from_report(report) -> dict[str, tuple[str, int]]:
    return {o.case.payment_id: (o.state, o.exposure_paise) for o in report.outcomes}


# ------------------------------------------------------------ no shadow ledger
def arm_no_shadow_ledger(dataset: Dataset) -> ArmResult:
    """Reconcile against the provider's own numbers instead of an independent one.

    Without the Shadow Ledger there is no expected value, so the only checks
    left are internal: does the settlement's own arithmetic add up, and does the
    bank credit match what the settlement claimed? Both can hold perfectly while
    the amount itself is wrong.
    """
    batch = dataset.batch()
    ledger = ShadowLedger(batch)          # used only as a record index here
    states: dict[str, tuple[str, int]] = {}
    seen: set[str] = set()

    for payment in batch.payments:
        if payment.payment_id in seen or payment.status != PaymentStatus.CAPTURED:
            continue
        seen.add(payment.payment_id)

        settlements = ledger.settlements_for(payment.payment_id)
        if not settlements:
            states[payment.payment_id] = (HUMAN_REVIEW_REQUIRED, payment.amount)
            continue
        settlement = settlements[0]
        entries = ledger.bank_entries_for_settlement(settlement)
        internal = settlement_arithmetic_consistent(settlement)
        bank = bank_credit_matches_settlement(settlement, entries)
        if internal.holds and bank.holds:
            states[payment.payment_id] = (MATCHED, 0)
        else:
            states[payment.payment_id] = (HUMAN_REVIEW_REQUIRED, abs(bank.difference or 0))

    return score(
        "no_shadow_ledger",
        "No independent expectation; trust the provider's own arithmetic",
        states,
        dataset,
        note=(
            "Only settlement-internal consistency and bank agreement remain. Both "
            "hold for a settlement that is internally tidy and simply wrong."
        ),
    )


# -------------------------------------------------------------------- llm only
def _llm_only_context(ledger: ShadowLedger, payment) -> str:
    order = ledger.orders_by_id.get(payment.order_id or "")
    settlements = ledger.settlements_for(payment.payment_id)
    settlement = settlements[0] if settlements else None
    entries = ledger.bank_entries_for_settlement(settlement) if settlement else []
    payload = {
        "order": (
            {
                "order_id": order.order_id,
                "amount_paise": order.amount,
                "customer_reference": order.customer_reference,
                "created_at": order.created_at.isoformat(),
            }
            if order
            else None
        ),
        "payment": {
            "payment_id": payment.payment_id,
            "amount_paise": payment.amount,
            "fee_paise": payment.fee,
            "tax_paise": payment.tax,
            "captured_at": payment.captured_at.isoformat() if payment.captured_at else None,
        },
        "linked_refunds": [
            {"refund_id": r.refund_id, "amount_paise": r.amount}
            for r in ledger.valid_refunds_for(payment.payment_id)
        ],
        "unlinked_refunds_in_batch": [
            {"refund_id": r.refund_id, "amount_paise": r.amount, "reference": r.reference}
            for r in ledger.orphan_refunds
        ][:5],
        "settlements": [
            {
                "settlement_id": s.settlement_id,
                "gross_paise": s.gross_amount,
                "fees_paise": s.fees,
                "tax_paise": s.tax,
                "refund_adjustments_paise": s.refund_adjustments,
                "net_paise": s.net_amount,
                "settlement_date": s.settlement_date.isoformat(),
            }
            for s in settlements
        ],
        "bank_entries": [
            {"bank_entry_id": e.bank_entry_id, "amount_paise": e.amount, "date": e.date.isoformat()}
            for e in entries
        ],
        "fee_policy": "The provider charges 2% of the captured amount, plus 18% GST on that fee.",
        "settlement_policy": "Money captured on day T is expected to settle on T+2, one day tolerance.",
    }
    return json.dumps(payload, indent=2, default=str)


def arm_llm_only(dataset: Dataset, provider, limit: int | None = None) -> ArmResult:
    """No deterministic engine at all. The model sees the records and decides."""
    if not hasattr(provider, "complete_json"):
        return score(
            "llm_only",
            "No deterministic engine; the model decides every case",
            {},
            dataset,
            note="skipped: the configured provider does not support raw JSON completion",
        )

    batch = dataset.batch()
    ledger = ShadowLedger(batch)
    states: dict[str, tuple[str, int]] = {}
    unavailable = 0
    seen: set[str] = set()

    # This arm asks for a two-field verdict, not an investigation. Leaving the
    # investigator's token budget in place burns rate limit for nothing, and on
    # a free tier that is the difference between minutes and an hour.
    budgets = {}
    for p in getattr(provider, "providers", None) or [provider]:
        if hasattr(p, "max_tokens"):
            budgets[p] = p.max_tokens
            p.max_tokens = 400

    for payment in batch.payments:
        if payment.payment_id in seen or payment.status != PaymentStatus.CAPTURED:
            continue
        if limit is not None and len(states) >= limit:
            break
        seen.add(payment.payment_id)

        out, _served, err = provider.complete_json(
            LLM_ONLY_SYSTEM,
            "Reconcile this payment.\n\n" + _llm_only_context(ledger, payment),
            LLM_ONLY_SCHEMA,
            "reconciliation",
        )
        if out is None:
            unavailable += 1
            states[payment.payment_id] = (HUMAN_REVIEW_REQUIRED, 0)
            continue

        settlements = ledger.settlements_for(payment.payment_id)
        exposure = payment.amount if not settlements else abs(
            ledger.expected_for_payment(payment).net_amount - settlements[0].net_amount
        )
        if out.get("verdict") == "reconciled":
            states[payment.payment_id] = (MATCHED, 0)
        elif out.get("action") == "close":
            states[payment.payment_id] = (AUTO_RESOLVED, exposure)
        else:
            states[payment.payment_id] = (HUMAN_REVIEW_REQUIRED, exposure)

    for p, original in budgets.items():
        p.max_tokens = original

    return score(
        "llm_only",
        "No deterministic engine; the model decides every case",
        states,
        dataset,
        note=(
            "The model is given the fee and settlement policies in words and must "
            "apply them itself. Exposure is scored with the shadow ledger the arm "
            "does not have, so the arm is measured generously. Note the cost "
            "shape: this arm needs one model call per record, where the hybrid "
            "needs one per ambiguous exception."
            + (f" Scored on the first {limit} payments." if limit else "")
        ),
        unavailable=unavailable,
    )


def render(arms: list[ArmResult], provider_name: str, holdout: Dataset) -> str:
    lines = [
        "# Ablation study",
        "",
        f"Frozen holdout, {len(holdout.lifecycles)} lifecycles, seed {holdout.seed}.",
        f"Investigator: `{provider_name}`.",
        "",
        "Generated by `python -m ledgerguard.evaluation.ablation`.",
        "",
        "| Arm | Accuracy | Matched | Auto resolved | Escalated | Missed faults | **False auto** | **Value falsely closed** |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for a in arms:
        if not a.total:
            lines.append(f"| `{a.name}` | — | — | — | — | — | — | _{a.note}_ |")
            continue
        lines.append(
            f"| `{a.name}` | {a.accuracy:.1%} | {a.matched} | {a.auto_resolved} | "
            f"{a.escalated} | {a.missed_faults} | **{a.false_auto}** | "
            f"**INR {a.false_auto_value_inr}** |"
        )
    lines += ["", "## What each arm removes", ""]
    for a in arms:
        lines.append(f"- **`{a.name}`** — {a.description}." + (f" {a.note}" if a.note else ""))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LedgerGuard ablation study")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--count", type=int, default=320)
    parser.add_argument("--skip-llm-only", action="store_true")
    parser.add_argument(
        "--llm-only-sample", type=int, default=0,
        help="score llm_only on the first N payments instead of all of them; "
             "the arm needs one call per record, so a full pass is slow on a "
             "rate-limited tier",
    )
    args = parser.parse_args(argv)

    full = build_dataset(seed=args.seed, count=args.count)
    _dev, holdout = split(full)
    lbp = holdout.lifecycle_by_payment()
    batch = holdout.batch()
    provider = get_provider(args.provider)

    arms: list[ArmResult] = []

    print("arm: rules_only ...", flush=True)
    arms.append(score(
        "rules_only", "No AI at all",
        _states_from_report(run(batch, use_ai=False, lifecycle_by_payment=lbp)),
        holdout,
        note="Anything the arithmetic cannot prove is escalated.",
    ))

    print("arm: no_shadow_ledger ...", flush=True)
    arms.append(arm_no_shadow_ledger(holdout))

    if args.skip_llm_only:
        arms.append(score("llm_only", "No deterministic engine", {}, holdout, note="skipped by flag"))
    else:
        print("arm: llm_only (one model call per payment) ...", flush=True)
        arms.append(arm_llm_only(holdout, provider, limit=args.llm_only_sample or None))

    print("arm: hybrid_no_gate ...", flush=True)
    arms.append(score(
        "hybrid_no_gate", "AI investigates, but its conclusions are taken on trust",
        _states_from_report(run(
            batch, use_ai=True, provider=provider,
            lifecycle_by_payment=lbp, use_evidence_gate=False,
        )),
        holdout,
        note="Identical to the full system except that nothing is re-derived from records.",
    ))

    print("arm: hybrid ...", flush=True)
    arms.append(score(
        "hybrid", "The full system",
        _states_from_report(run(batch, use_ai=True, provider=provider, lifecycle_by_payment=lbp)),
        holdout,
    ))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    md = render(arms, provider.name, holdout)
    (OUTPUT_DIR / "ablation.md").write_text(md)
    (OUTPUT_DIR / "ablation.json").write_text(
        json.dumps(
            {"provider": provider.name, "arms": [asdict(a) for a in arms]},
            indent=2, default=str,
        ) + "\n"
    )
    print("\n" + md)
    print(f"Artifacts written to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
