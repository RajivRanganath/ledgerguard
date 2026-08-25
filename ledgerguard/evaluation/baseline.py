"""Rules-only baseline.

Identical deterministic matcher, identical Shadow Ledger, identical safety
gate. The only difference is that no investigation step runs, so anything the
arithmetic cannot prove is abstained on.

Neither system is tuned using information the other does not have: they share
every module except the investigation call, and both are scored on the same
frozen holdout.
"""

from __future__ import annotations

from ..ledger.models import Batch
from ..pipeline import RunReport, run


def run_baseline(batch: Batch, lifecycle_by_payment: dict[str, str] | None = None) -> RunReport:
    return run(batch, use_ai=False, lifecycle_by_payment=lifecycle_by_payment)
