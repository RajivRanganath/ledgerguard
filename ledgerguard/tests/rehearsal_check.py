"""Demo rehearsal check.

Walks every beat of docs/demo_script.md against a live server and asserts the
data each beat depends on is actually there. Run it before demoing; if a beat
regresses, this fails instead of the demo.

    .venv/bin/python -m ledgerguard.tests.rehearsal_check
"""

from __future__ import annotations

import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8137"


def get(path: str):
    import json

    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.load(r)


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"  [{'OK ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return condition


def rehearse(run_no: int) -> tuple[bool, float]:
    started = time.perf_counter()
    ok = True
    print(f"\n--- rehearsal run {run_no} ---")
    state = get("/api/state")
    cases = {c["case_id"]: c for c in state["cases"]}

    # 0:30 architecture — overview strip
    o = state["overview"]
    ok &= check("overview strip populated", o["records_processed"] > 0, f"{o['records_processed']} records")
    ok &= check("provider is named", bool(state["provider"]), state["provider"])

    # 1:00 normal case
    demo = state["demo_cases"]
    ok &= check("five demo cases prepared", len(demo) == 5, str(demo))
    clean = cases.get(demo[0]) if demo else None
    ok &= check("beat 1: clean case is MATCHED", bool(clean) and clean["state"] == "MATCHED")
    ok &= check(
        "beat 1: all six invariants hold",
        bool(clean) and len(clean["invariants"]) == 6 and all(i["holds"] for i in clean["invariants"]),
    )
    ok &= check("beat 1: no AI was invoked", bool(clean) and clean["investigation"] is None)

    # 1:30 real exception
    f3 = next((c for c in state["cases"] if c["fault_class"] == "F3_UNLINKED_PARTIAL_REFUND"), None)
    ok &= check("beat 2: an F3 case exists", f3 is not None)
    if f3:
        ok &= check("beat 2: I3 is the broken invariant",
                    any(not i["holds"] and i["name"] == "I3_net_matches_shadow_ledger" for i in f3["invariants"]))
        ok &= check("beat 2: a candidate refund was surfaced",
                    len(f3["records"]["candidate_refunds"]) >= 1)
        ok &= check("beat 2: gate VERIFIED it", f3["verification"]["verdict"] == "VERIFIED",
                    f3["verification"]["verification_score"])
        ok &= check("beat 2: invariant-restoration check passed",
                    any(c["name"] == "E7_invariant_restored" and c["passed"] for c in f3["verification"]["checks"]))
        ok &= check("beat 2: AUTO_RESOLVED", f3["state"] == "AUTO_RESOLVED")

    # 2:30 adversarial — the beat that must never regress silently
    adv = state["adversarial"]
    ok &= check("beat 3: donor VERIFIED", adv["donor"]["verdict"] == "VERIFIED",
                adv["donor"]["verification_score"])
    ok &= check("beat 3: victim REJECTED", adv["victim"]["verdict"] == "REJECTED",
                adv["victim"]["verification_score"])
    ok &= check("beat 3: same hypothesis on both sides",
                adv["donor"]["hypothesis"] == adv["victim"]["hypothesis"] == "unlinked_partial_refund")
    ok &= check("beat 3: investigator asked to resolve the wrong one",
                adv["victim"]["recommended_action"] == "resolve")
    vchecks = {c["name"]: c["passed"] for c in adv["victim"]["checks"]}
    ok &= check("beat 3: rejected on linkage", vchecks.get("E4_reference_identifies_this_customer") is False)
    ok &= check("beat 3: amount check still passes", vchecks.get("E5_amount_equals_shortfall") is True)
    ok &= check("beat 3: ledger still balances", vchecks.get("E7_invariant_restored") is True)
    ok &= check("beat 3: escalated to a human", adv["victim"]["state"] == "HUMAN_REVIEW_REQUIRED")

    # 3:20 benchmark
    b, h = state["benchmark"]["baseline"], state["benchmark"]["hybrid"]
    ok &= check("beat 4: hybrid resolves more", h["exceptions_correctly_resolved"] > b["exceptions_correctly_resolved"],
                f"{b['exceptions_correctly_resolved']} -> {h['exceptions_correctly_resolved']}")
    ok &= check("beat 4: zero false auto resolutions in both",
                b["false_auto_resolutions"] == 0 and h["false_auto_resolutions"] == 0)
    ok &= check("beat 4: no clean case falsely flagged",
                b["false_exceptions"] == 0 and h["false_exceptions"] == 0)

    # 4:15 unresolved
    u = state["unresolved"]
    ok &= check("beat 5: unresolved list is non-empty", len(u) > 0, f"{len(u)} cases")
    ok &= check("beat 5: every row explains itself",
                all(r["blocked_because"] and r["suggested_action"] for r in u))

    # the page itself
    with urllib.request.urlopen(BASE + "/", timeout=20) as r:
        html = r.read().decode()
    ok &= check("dashboard HTML served", r.status == 200 and "LedgerGuard" in html, f"{len(html)} bytes")

    return ok, time.perf_counter() - started


def main() -> int:
    all_ok = True
    for i in range(1, 4):
        ok, secs = rehearse(i)
        print(f"  run {i}: {'PASS' if ok else 'FAIL'} in {secs:.2f}s")
        all_ok &= ok
    print("\n" + ("REHEARSAL PASS — every demo beat verified three times" if all_ok
                  else "REHEARSAL FAIL — fix before demoing"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
