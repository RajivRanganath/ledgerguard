"""P2 evaluation machinery.

These protect the analyses themselves. An ablation that silently stops ablating,
or a replay that passes because it compares an object to itself, is worse than
not having one -- it produces a confident number with nothing behind it.

Offline only: every arm here runs on the deterministic stub.
"""

from __future__ import annotations

import json
from decimal import Decimal

from ledgerguard.ai.provider import HeuristicProvider
from ledgerguard.evaluation.ablation import (
    arm_no_shadow_ledger,
    score,
    _states_from_report,
)
from ledgerguard.evaluation.calibration import _bucketise
from ledgerguard.evaluation.cost import CostEstimate
from ledgerguard.evaluation.drift import evaluate as drift_evaluate
from ledgerguard.evaluation.replay import capture, diff, replay
from ledgerguard.evaluation.report import write_reconciliation_report
from ledgerguard.evaluation.scale import measure
from ledgerguard.pipeline import run
from ledgerguard.qa import ask
from ledgerguard.synthetic.fault_injector import build_dataset, split


def _holdout_run(gate: bool = True):
    full = build_dataset()
    _dev, holdout = split(full)
    batch = holdout.batch()
    report = run(
        batch, use_ai=True, provider=HeuristicProvider(),
        lifecycle_by_payment=holdout.lifecycle_by_payment(),
        use_evidence_gate=gate,
    )
    return holdout, batch, report


# ------------------------------------------------------------------- ablation
def test_removing_the_evidence_gate_causes_measurable_false_closures():
    """The ablation must actually ablate. This is the whole safety claim."""
    holdout, _batch, with_gate = _holdout_run(gate=True)
    _h, _b, without_gate = _holdout_run(gate=False)

    on = score("hybrid", "full", _states_from_report(with_gate), holdout)
    off = score("no_gate", "gate off", _states_from_report(without_gate), holdout)

    assert on.false_auto == 0, "the full system must not close anything falsely"
    assert on.false_auto_value_paise == 0
    assert off.false_auto > 0, "gate-off must produce false closures, or it is not ablating"
    assert off.false_auto_value_paise > 0
    assert off.auto_resolved > on.auto_resolved, "gate-off must close strictly more"
    assert off.accuracy < on.accuracy


def test_removing_the_shadow_ledger_makes_the_controller_blind_not_wrong():
    """Without an independent expectation, faults are missed rather than mis-closed."""
    full = build_dataset()
    _dev, holdout = split(full)
    arm = arm_no_shadow_ledger(holdout)

    assert arm.missed_faults > 0, "trusting the provider must miss faults"
    assert arm.false_auto == 0, "it closes nothing, so it cannot close wrongly"
    assert arm.matched > 0


def test_score_counts_a_wrong_close_and_a_safe_abstention_differently():
    full = build_dataset()
    _dev, holdout = split(full)
    truth = {
        lc.payment.payment_id: holdout.ground_truth[lc.lifecycle_id]
        for lc in holdout.lifecycles
    }
    must_escalate = next(
        pid for pid, gt in truth.items() if gt.expected_disposition == "HUMAN_REVIEW_REQUIRED"
    )
    closed = score("x", "", {must_escalate: ("AUTO_RESOLVED", 5000)}, holdout)
    escalated = score("y", "", {must_escalate: ("HUMAN_REVIEW_REQUIRED", 5000)}, holdout)

    assert closed.false_auto == 1 and closed.correct == 0
    assert escalated.false_auto == 0 and escalated.correct == 1


# --------------------------------------------------------------------- replay
def test_every_decision_replays_identically_from_the_written_record():
    _holdout, batch, report = _holdout_run()
    captured = capture(report, batch)

    # Round-trip through JSON, so this cannot pass by sharing objects in memory.
    reloaded = json.loads(json.dumps(captured, default=str))
    assert not diff(reloaded["decisions"], replay(reloaded))


def test_replay_detects_a_tampered_decision():
    """A replay that cannot fail is not evidence of anything."""
    _holdout, batch, report = _holdout_run()
    captured = json.loads(json.dumps(capture(report, batch), default=str))

    case_id = next(
        cid for cid, d in captured["decisions"].items() if d["state"] != "MATCHED"
    )
    captured["decisions"][case_id]["state"] = "AUTO_RESOLVED_BY_HAND"
    mismatches = diff(captured["decisions"], replay(captured))
    assert any(m["case_id"] == case_id and m["field"] == "state" for m in mismatches)


# ---------------------------------------------------------------------- drift
def test_drift_control_is_clean_and_a_fee_change_is_not_absorbed():
    control = drift_evaluate("control", "no change")
    assert control.exceptions == 0, "clean lifecycles must reconcile before any shift"

    shifted = drift_evaluate("fee", "2.0% -> 2.5%", fee_rate=Decimal("0.025"))
    assert shifted.false_exception_rate == 1.0
    assert shifted.dominant_exception == "EXCEPTION_FEE_MISMATCH"

    # Bigger tickets must not by themselves upset anything.
    scaled = drift_evaluate("scale", "10x", amount_scale=10)
    assert scaled.exceptions == 0


def test_settlement_window_drift_shows_where_the_tolerance_ends():
    assert drift_evaluate("t3", "T+3", lag_days=3).exceptions == 0
    late = drift_evaluate("t4", "T+4", lag_days=4)
    assert late.false_exception_rate == 1.0
    assert late.dominant_exception == "EXCEPTION_DELAYED_SETTLEMENT"


# ---------------------------------------------------------------------- scale
def test_the_deterministic_path_holds_at_scale():
    point = measure(4000)
    assert point.false_exceptions == 0
    assert point.missed_faults == 0
    assert point.fault_classes_present == 6
    assert point.throughput_per_second > 1000


# ---------------------------------------------------------------- calibration
def test_bucketise_reports_rates_not_just_counts():
    buckets = _bucketise({"7/7": [True, True, True], "5/7": [False, False]})
    by_label = {b.label: b for b in buckets}
    assert by_label["7/7"].correct_rate == 1.0
    assert by_label["5/7"].correct_rate == 0.0
    assert [b.label for b in buckets] == ["7/7", "5/7"], "buckets must be ordered"


# ----------------------------------------------------------------------- cost
def test_cost_estimate_is_arithmetically_correct():
    est = CostEstimate(
        model="m", prompt_tokens=1_000_000, completion_tokens=1_000_000,
        investigations=10, records=100, priced_at=("0.15", "0.75"),
    )
    assert est.usd == Decimal("0.90")
    assert est.usd_per_100_records == Decimal("0.90")
    assert est.tokens_per_investigation == 200_000


# --------------------------------------------------------------------- report
def test_the_report_export_reconciles_with_the_run(tmp_path):
    _holdout, _batch, report = _holdout_run()
    totals = write_reconciliation_report(report, output_dir=tmp_path)

    assert totals.cases == len(report.outcomes)
    assert totals.matched + totals.auto_resolved + totals.escalated == totals.cases
    assert totals.evidence_checks_passed <= totals.evidence_checks

    rows = (tmp_path / "reconciliation_report.csv").read_text().splitlines()
    assert len(rows) == totals.cases + 1
    # Every automatic closure must be traceable to named checks in the audit file.
    ledger_rows = (tmp_path / "evidence_ledger.csv").read_text().splitlines()
    assert len(ledger_rows) > 1


# ------------------------------------------------------------------------- qa
def test_qa_answers_from_records_and_refuses_everything_else():
    _holdout, _batch, report = _holdout_run()

    unresolved = ask("How much is still unresolved?", report)
    assert unresolved.grounded and unresolved.query == "unresolved_exposure"
    assert "INR" in unresolved.answer and unresolved.citations

    largest = ask("What is the largest unresolved case?", report)
    assert largest.query == "largest_unresolved", "specific queries must win over aggregates"

    refused = ask("Will this merchant be profitable next quarter?", report)
    assert not refused.grounded
    assert refused.query == "unsupported"
    assert "will not estimate" in refused.answer

    unknown_case = ask("What happened with pay_DOES_NOT_EXIST?", report)
    assert not unknown_case.grounded
