"""Deterministic decision replay.

Every decision the controller makes is a pure function of the records it saw.
This records that claim as an artifact and then checks it: capture the inputs
and outputs of a run, re-run from the captured inputs alone, and assert the
decisions are identical.

Why it matters in a finance controller: "why did you close that case in March"
has to be answerable in August, from the record, without the answer depending on
which model was reachable that day. Replay covers the deterministic surface --
matcher, Shadow Ledger, invariants, Evidence Gate, safety gate. The investigator
is not deterministic, so its output is *captured* and replayed as data rather
than regenerated.

    python -m ledgerguard.evaluation.replay
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ..ai.schemas import InvestigationResult
from ..evidence.safety import decide
from ..evidence.verifier import resolve_evidence_conflicts, verify
from ..ledger.models import Batch
from ..ledger.shadow_ledger import ShadowLedger
from ..pipeline import RunReport, run
from ..reconciliation.matcher import reconcile_batch
from ..synthetic.fault_injector import build_dataset, split

OUTPUT_DIR = Path(__file__).parent / "outputs"


def capture(report: RunReport, batch: Batch) -> dict:
    """Everything needed to reproduce this run's decisions, and nothing else."""
    return {
        "batch_sha256": hashlib.sha256(batch.model_dump_json().encode()).hexdigest(),
        "batch": json.loads(batch.model_dump_json()),
        "investigations": {
            o.case_id: o.investigation.result.model_dump()
            for o in report.outcomes
            if o.investigation is not None
        },
        "decisions": {
            o.case_id: {
                "state": o.state,
                "exception_type": (
                    o.case.exception.exception_type.value if o.case.exception else None
                ),
                "verdict": o.verification.verdict if o.verification else None,
                "verification_score": (
                    o.verification.verification_score if o.verification else None
                ),
                "reason": o.decision.reason if o.decision else None,
                "exposure_paise": o.exposure_paise,
            }
            for o in report.outcomes
        },
    }


def replay(captured: dict) -> dict:
    """Re-derive every decision from the captured records and investigations."""
    batch = Batch.model_validate(captured["batch"])
    digest = hashlib.sha256(batch.model_dump_json().encode()).hexdigest()
    if digest != captured["batch_sha256"]:
        raise SystemExit(
            "Captured batch does not round-trip: "
            f"{digest} != {captured['batch_sha256']}"
        )

    cases, ledger = reconcile_batch(batch)
    stored = captured["investigations"]

    verifications = {}
    for case in cases:
        if case.case_id not in stored:
            continue
        result = InvestigationResult.model_validate(stored[case.case_id])
        verifications[case.case_id] = verify(ledger, case, result)
    resolve_evidence_conflicts(verifications)

    replayed = {}
    for case in cases:
        if not case.is_exception:
            replayed[case.case_id] = {
                "state": "MATCHED",
                "exception_type": None,
                "verdict": None,
                "verification_score": None,
                "reason": None,
                "exposure_paise": 0,
            }
            continue
        result = (
            InvestigationResult.model_validate(stored[case.case_id])
            if case.case_id in stored
            else None
        )
        ver = verifications.get(case.case_id)
        decision = decide(case, result, ver)
        replayed[case.case_id] = {
            "state": decision.state,
            "exception_type": case.exception.exception_type.value,
            "verdict": ver.verdict if ver else None,
            "verification_score": ver.verification_score if ver else None,
            "reason": decision.reason,
            "exposure_paise": abs(case.exception.difference or 0),
        }
    return replayed


def diff(original: dict, replayed: dict) -> list[dict]:
    mismatches = []
    for case_id, want in original.items():
        got = replayed.get(case_id)
        if got is None:
            mismatches.append({"case_id": case_id, "field": "*", "original": want, "replayed": None})
            continue
        for field, value in want.items():
            if got.get(field) != value:
                mismatches.append(
                    {"case_id": case_id, "field": field, "original": value, "replayed": got.get(field)}
                )
    for case_id in replayed:
        if case_id not in original:
            mismatches.append({"case_id": case_id, "field": "*", "original": None, "replayed": replayed[case_id]})
    return mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic decision replay")
    parser.add_argument("--provider", default="stub")
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--count", type=int, default=320)
    args = parser.parse_args(argv)

    from ..ai.provider import get_provider

    full = build_dataset(seed=args.seed, count=args.count)
    _dev, holdout = split(full)
    batch = holdout.batch()

    report = run(
        batch, use_ai=True, provider=get_provider(args.provider),
        lifecycle_by_payment=holdout.lifecycle_by_payment(),
    )
    captured = capture(report, batch)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "decision_log.json"
    path.write_text(json.dumps(captured, indent=2, default=str) + "\n")

    # Replay from the written file, not from memory -- otherwise the test only
    # proves the objects are still in scope.
    reloaded = json.loads(path.read_text())
    mismatches = diff(reloaded["decisions"], replay(reloaded))

    print(f"decision log: {path}")
    print(f"batch sha256: {captured['batch_sha256'][:16]}")
    print(f"cases replayed: {len(reloaded['decisions'])}")
    print(f"investigations replayed as data: {len(reloaded['investigations'])}")
    if mismatches:
        print(f"\nREPLAY FAILED: {len(mismatches)} mismatches")
        for m in mismatches[:10]:
            print(f"  {m['case_id']} {m['field']}: {m['original']!r} -> {m['replayed']!r}")
        return 1
    print("\nREPLAY OK: every decision re-derived identically from the record")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
