"""End to end controller run.

    deterministic reconciliation
      -> AI investigation, but only for what could not be proved
      -> Evidence Gate
      -> uniqueness pass
      -> safety and action gate

Running with ``use_ai=False`` gives the rules-only baseline: identical
deterministic layer, no investigation step. That is the comparison the
benchmark makes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .ai.investigator import Investigation, investigate_case, should_investigate
from .ai.provider import InvestigatorProvider, UnavailableProvider, get_provider
from .ai.schemas import InvestigationResult
from .evidence.safety import Decision, HUMAN_REVIEW_REQUIRED, decide
from .evidence.verifier import (
    VerificationOutcome,
    resolve_evidence_conflicts,
    verify,
)
from .ledger.models import Batch
from .ledger.shadow_ledger import ShadowLedger
from .reconciliation.matcher import CaseResult, reconcile_batch


@dataclass
class CaseOutcome:
    case: CaseResult
    investigation: Investigation | None = None
    verification: VerificationOutcome | None = None
    decision: Decision | None = None

    @property
    def case_id(self) -> str:
        return self.case.case_id

    @property
    def state(self) -> str:
        if not self.case.is_exception:
            return "MATCHED"
        return self.decision.state if self.decision else HUMAN_REVIEW_REQUIRED

    @property
    def exposure_paise(self) -> int:
        exc = self.case.exception
        if exc is None or exc.difference is None:
            return 0
        return abs(exc.difference)


@dataclass
class RunReport:
    system: str
    provider_name: str
    outcomes: list[CaseOutcome] = field(default_factory=list)
    ledger: ShadowLedger | None = None
    wall_seconds: float = 0.0
    investigations: int = 0
    model_calls: int = 0
    investigation_latencies_ms: list[float] = field(default_factory=list)

    def by_case_id(self) -> dict[str, CaseOutcome]:
        return {o.case_id: o for o in self.outcomes}


def _accept_without_proof(case: CaseResult, result: InvestigationResult) -> VerificationOutcome:
    """The ablation stand-in for the Evidence Gate: believe the investigator.

    Used only by the ablation study, to measure what the gate is actually
    preventing. It records zero checks, so nothing about it can be mistaken for
    verification after the fact.
    """
    from .evidence.verifier import UNVERIFIED, VERIFIED

    trusted = result.hypothesis != "insufficient_evidence" and result.source in (
        "model",
        "heuristic_stub",
    )
    return VerificationOutcome(
        case_id=case.case_id,
        hypothesis=result.hypothesis,
        verdict=VERIFIED if trusted else UNVERIFIED,
        checks=[],
        claimed_evidence_ids=list(result.candidate_evidence_ids),
        note="ABLATION: Evidence Gate disabled; the investigator was taken at its word",
    )


def run(
    batch: Batch,
    use_ai: bool = True,
    provider: InvestigatorProvider | None = None,
    lifecycle_by_payment: dict[str, str] | None = None,
    use_evidence_gate: bool = True,
) -> RunReport:
    started = time.perf_counter()
    cases, ledger = reconcile_batch(batch, lifecycle_by_payment)

    if use_ai:
        provider = provider or get_provider()
    else:
        provider = UnavailableProvider("rules-only baseline: AI investigation disabled")

    outcomes: list[CaseOutcome] = []
    verifications: dict[str, VerificationOutcome] = {}
    investigations = 0
    model_calls = 0
    latencies: list[float] = []

    for case in cases:
        if not case.is_exception:
            outcomes.append(CaseOutcome(case=case))
            continue

        inv: Investigation | None = None
        ver: VerificationOutcome | None = None

        if should_investigate(case):
            if use_ai:
                inv = investigate_case(ledger, case, provider)
                investigations += 1
                model_calls += 1 if inv.called_model else 0
                latencies.append(inv.latency_ms)
                ver = (
                    verify(ledger, case, inv.result)
                    if use_evidence_gate
                    else _accept_without_proof(case, inv.result)
                )
                verifications[case.case_id] = ver
            else:
                # Baseline: no investigation at all. The deterministic layer
                # already said it cannot prove the cause, so it abstains.
                inv = Investigation(
                    case_id=case.case_id,
                    context={},
                    result=InvestigationResult.unavailable(
                        "rules-only baseline: no investigation performed"
                    ),
                    latency_ms=0.0,
                    called_model=False,
                )
                ver = verify(ledger, case, inv.result)
                verifications[case.case_id] = ver

        outcomes.append(CaseOutcome(case=case, investigation=inv, verification=ver))

    # Uniqueness pass runs across all cases, so it cannot depend on the order
    # in which the investigator happened to look at them. It is part of the
    # gate, so the ablation skips it too.
    if use_evidence_gate:
        resolve_evidence_conflicts(verifications)

    for outcome in outcomes:
        if not outcome.case.is_exception:
            continue
        outcome.verification = verifications.get(outcome.case_id, outcome.verification)
        outcome.decision = decide(
            outcome.case,
            outcome.investigation.result if outcome.investigation else None,
            outcome.verification,
        )

    if not use_ai:
        system = "Rules only (baseline)"
    elif not use_evidence_gate:
        system = "Ablation: no Evidence Gate"
    else:
        system = "LedgerGuard (hybrid)"

    return RunReport(
        system=system,
        provider_name=provider.name if use_ai else "none",
        outcomes=outcomes,
        ledger=ledger,
        wall_seconds=time.perf_counter() - started,
        investigations=investigations,
        model_calls=model_calls,
        investigation_latencies_ms=latencies,
    )
