"""Compound failure cases.

Two faults in one lifecycle. These exist to pin one rule:

    a proven cause licenses closing a case only if it accounts for all of the
    money; any unattributed residual blocks closure, while a second finding that
    is itself benign does not.

Both halves matter. Without the first, the controller closes on the fault it
recognised and leaves real money unexplained. Without the second, it escalates
every case that has more than one finding and abstention stops meaning anything.
"""

from __future__ import annotations

from ledgerguard.ai.provider import HeuristicProvider
from ledgerguard.evidence.safety import AUTO_RESOLVED, HUMAN_REVIEW_REQUIRED
from ledgerguard.ledger.money import to_paise
from ledgerguard.pipeline import run
from ledgerguard.synthetic.compound import build_compound_batch


def _run():
    batch, cases = build_compound_batch()
    report = run(batch, use_ai=True, provider=HeuristicProvider())
    return {o.case.payment_id: o for o in report.outcomes}, {c.case_id: c for c in cases}


def test_every_compound_case_reaches_its_designed_disposition():
    outcomes, cases = _run()
    assert len(cases) == 5, "three to five compound cases were planned"

    wrong = []
    for case in cases.values():
        got = outcomes[case.payment_id].state
        if got != case.expected_disposition:
            wrong.append((case.case_id, case.name, got, case.expected_disposition))
    assert not wrong, wrong

    # No compound case may ever be closed against ground truth.
    for case in cases.values():
        if case.expected_disposition == HUMAN_REVIEW_REQUIRED:
            assert outcomes[case.payment_id].state != AUTO_RESOLVED, case.case_id


def test_an_unattributed_residual_blocks_closure_on_the_proven_cause():
    """C2: the fee overcharge is proven; INR 650.00 is still missing."""
    outcomes, _ = _run()
    outcome = outcomes["pay_C2"]

    residual = outcome.case.exception.evidence["residual_paise"]
    assert residual == to_paise("650.00"), residual
    assert outcome.state == HUMAN_REVIEW_REQUIRED
    assert "unattributed" in outcome.decision.reason
    assert "650.00" in outcome.decision.reason

    # C5 is the same shape with the residual explained by a refund that belongs
    # to a different customer -- still blocked, and for the same reason.
    c5 = outcomes["pay_C5"]
    assert c5.case.exception.evidence["residual_paise"] == to_paise("900.00")
    assert c5.state == HUMAN_REVIEW_REQUIRED


def test_a_fully_attributed_case_still_closes_and_reports_the_second_finding():
    """C3: duplicate credit plus a delay. Nothing unexplained, so it closes."""
    outcomes, _ = _run()
    outcome = outcomes["pay_C3"]

    assert outcome.state == AUTO_RESOLVED
    assert not outcome.case.exception.evidence.get("residual_paise")
    # The delay was not the classification, and must still be surfaced.
    assert "I5_settlement_within_window" in outcome.case.exception.evidence["secondary_findings"]
    assert "I5_settlement_within_window" in outcome.decision.reason


def test_a_resolved_shortfall_does_not_swallow_a_concurrent_delay():
    """C1: the refund genuinely explains the gap, but the case is also late."""
    outcomes, _ = _run()
    outcome = outcomes["pay_C1"]

    assert outcome.state == AUTO_RESOLVED
    assert outcome.verification.verdict == "VERIFIED"
    assert "I5_settlement_within_window" in outcome.case.exception.evidence["secondary_findings"]
    assert "I5_settlement_within_window" in outcome.decision.reason


def test_a_matching_orphan_refund_cannot_offset_a_settlement_that_never_arrived():
    """C4: no settlement exists, so there is nothing to net the refund against."""
    outcomes, _ = _run()
    outcome = outcomes["pay_C4"]

    assert outcome.case.exception.exception_type.value == "EXCEPTION_MISSING_SETTLEMENT"
    assert outcome.state == HUMAN_REVIEW_REQUIRED
    assert outcome.investigation is None, "a missing settlement is proven, not investigated"
    # The whole captured net is exposed, not the captured amount less the refund.
    assert outcome.exposure_paise == outcome.case.expected.net_amount
