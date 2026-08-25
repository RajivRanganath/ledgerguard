"""Metrics computed from an actual run against hidden ground truth.

Nothing in this module is allowed to see a headline number in advance. Every
figure the README or dashboard prints is produced here, from a real run.

The two numbers that matter most are deliberately at the top of the dataclass:
false auto resolutions, and the rupee value falsely auto resolved. A system can
look accurate simply because most records are easy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..evidence.safety import (
    AUTO_RESOLVED,
    HUMAN_REVIEW_REQUIRED,
    RECOMMEND_REVIEW,
    UNRESOLVED,
)
from ..ledger.money import to_rupees_str
from ..pipeline import RunReport
from ..synthetic.generator import Dataset, FaultClass

#: Ground-truth dispositions, from the fault injector.
GT_AUTO = "AUTO_RESOLVED"
GT_HUMAN = "HUMAN_REVIEW_REQUIRED"
GT_NONE = "NONE"


@dataclass
class Metrics:
    system: str
    provider: str

    # --- the two that matter most -----------------------------------------
    false_auto_resolutions: int = 0
    false_auto_resolved_value_paise: int = 0

    # --- volume ------------------------------------------------------------
    total_cases: int = 0
    total_value_paise: int = 0
    matched: int = 0
    exceptions: int = 0

    # --- correctness -------------------------------------------------------
    match_rate: float = 0.0
    false_exceptions: int = 0              # clean lifecycle wrongly flagged
    missed_faults: int = 0                 # faulted lifecycle wrongly matched
    exception_type_correct: int = 0        # of exceptions raised, type matches truth
    disposition_correct: int = 0
    accuracy: float = 0.0                  # correct disposition / total cases

    # --- exception handling ------------------------------------------------
    auto_resolved: int = 0
    exceptions_correctly_resolved: int = 0
    exceptions_incorrectly_resolved: int = 0
    recommend_review: int = 0
    human_review_required: int = 0
    unresolved: int = 0
    correct_abstentions: int = 0
    unnecessary_abstentions: int = 0

    # --- exposure ----------------------------------------------------------
    exception_value_paise: int = 0
    unresolved_value_paise: int = 0

    # --- cost / throughput -------------------------------------------------
    wall_seconds: float = 0.0
    throughput_per_second: float = 0.0
    investigations: int = 0
    model_calls: int = 0
    model_calls_per_100_records: float = 0.0
    investigations_per_100_records: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0

    per_fault_class: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["false_auto_resolved_value_inr"] = to_rupees_str(self.false_auto_resolved_value_paise)
        d["unresolved_value_inr"] = to_rupees_str(self.unresolved_value_paise)
        d["total_value_inr"] = to_rupees_str(self.total_value_paise)
        return d


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return round(ordered[idx], 2)


def compute(report: RunReport, dataset: Dataset) -> Metrics:
    truth_by_payment = {
        lc.payment.payment_id: dataset.ground_truth[lc.lifecycle_id]
        for lc in dataset.lifecycles
    }
    m = Metrics(system=report.system, provider=report.provider_name)
    per_class: dict[str, dict] = {}

    for outcome in report.outcomes:
        gt = truth_by_payment[outcome.case.payment_id]
        bucket = per_class.setdefault(
            gt.fault_class,
            {"n": 0, "auto_resolved": 0, "correct": 0, "false_auto": 0, "abstained": 0},
        )
        bucket["n"] += 1
        m.total_cases += 1

        payment = report.ledger.payments_by_id[outcome.case.payment_id]
        m.total_value_paise += payment.amount

        state = outcome.state
        is_clean = gt.fault_class == FaultClass.NONE

        if state == "MATCHED":
            m.matched += 1
            if is_clean:
                m.disposition_correct += 1
                bucket["correct"] += 1
            else:
                m.missed_faults += 1
            continue

        # --- from here on, the case was raised as an exception --------------
        m.exceptions += 1
        m.exception_value_paise += outcome.exposure_paise
        if is_clean:
            m.false_exceptions += 1

        if outcome.case.exception.exception_type.value == (gt.expected_exception or ""):
            m.exception_type_correct += 1

        if state == AUTO_RESOLVED:
            m.auto_resolved += 1
            bucket["auto_resolved"] += 1
            if gt.expected_disposition == GT_AUTO:
                m.exceptions_correctly_resolved += 1
                m.disposition_correct += 1
                bucket["correct"] += 1
            else:
                # Closed a case the evidence did not justify closing. This is
                # the failure mode the whole architecture exists to prevent.
                m.exceptions_incorrectly_resolved += 1
                m.false_auto_resolutions += 1
                m.false_auto_resolved_value_paise += max(
                    outcome.exposure_paise, gt.exposure_paise
                )
                bucket["false_auto"] += 1
            continue

        # --- abstained in some form ----------------------------------------
        if state == RECOMMEND_REVIEW:
            m.recommend_review += 1
        elif state == HUMAN_REVIEW_REQUIRED:
            m.human_review_required += 1
        elif state == UNRESOLVED:
            m.unresolved += 1
        bucket["abstained"] += 1
        m.unresolved_value_paise += outcome.exposure_paise

        if gt.expected_disposition == GT_HUMAN:
            m.correct_abstentions += 1
            m.disposition_correct += 1
            bucket["correct"] += 1
        else:
            # Safe, but a missed opportunity: the evidence was there and the
            # system did not close it.
            m.unnecessary_abstentions += 1

    m.match_rate = round(m.matched / m.total_cases, 4) if m.total_cases else 0.0
    m.accuracy = round(m.disposition_correct / m.total_cases, 4) if m.total_cases else 0.0
    m.wall_seconds = round(report.wall_seconds, 3)
    m.throughput_per_second = (
        round(m.total_cases / report.wall_seconds, 1) if report.wall_seconds else 0.0
    )
    m.investigations = report.investigations
    m.model_calls = report.model_calls
    m.model_calls_per_100_records = (
        round(100 * report.model_calls / m.total_cases, 2) if m.total_cases else 0.0
    )
    m.investigations_per_100_records = (
        round(100 * report.investigations / m.total_cases, 2) if m.total_cases else 0.0
    )
    m.latency_p50_ms = _percentile(report.investigation_latencies_ms, 50)
    m.latency_p95_ms = _percentile(report.investigation_latencies_ms, 95)
    m.per_fault_class = dict(sorted(per_class.items()))
    return m
