# Reconciliation report

System: LedgerGuard (hybrid). Investigator: `heuristic_stub`.

| | |
|---|---|
| Cases reconciled | 85 |
| Captured value | INR 2493357.00 |
| Matched, no exception | 54 |
| Exceptions closed automatically | 21 |
| Escalated to a human | 10 |
| Value under exception | INR 364267.31 |
| Value left unresolved | INR 121899.54 |
| Evidence checks run | 105 |
| Evidence checks passed | 93 |

Every automatic closure above is backed by the named checks in
`evidence_ledger.csv`. Nothing is closed on a model's say-so, and no
money is moved by this system under any state.

Files: `reconciliation_report.csv` (one row per case),
`evidence_ledger.csv` (one row per check), `unresolved_exceptions.csv`
(the cases that stayed open, with what was missing).
