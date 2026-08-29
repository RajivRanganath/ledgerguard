# Reconciliation report

System: LedgerGuard (hybrid). Investigator: `fallback(groq->gemini->nvidia->omniroute)`.

| | |
|---|---|
| Cases reconciled | 85 |
| Captured value | INR 2493357.00 |
| Matched, no exception | 54 |
| Exceptions closed automatically | 20 |
| Escalated to a human | 11 |
| Value under exception | INR 364267.31 |
| Value left unresolved | INR 126017.84 |
| Evidence checks run | 84 |
| Evidence checks passed | 78 |

Every automatic closure above is backed by the named checks in
`evidence_ledger.csv`. Nothing is closed on a model's say-so, and no
money is moved by this system under any state.

Files: `reconciliation_report.csv` (one row per case),
`evidence_ledger.csv` (one row per check), `unresolved_exceptions.csv`
(the cases that stayed open, with what was missing).
