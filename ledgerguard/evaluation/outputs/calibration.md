# Calibration of the Verification Score

Development split, 235 lifecycles, 26 investigated cases.
Investigator: `heuristic_stub`.

"Closing would be correct" is the counterfactual: if the case were closed
at this point, would ground truth agree? Nothing here tunes the system.

## The investigator's own request

| model asked to | cases | closing would be correct | rate |
|---|---|---|---|
| `resolve` | 26 | 20 | 76.9% |

## The Verification Score

| checks passed | cases | closing would be correct | rate |
|---|---|---|---|
| `7/7` | 20 | 20 | 100.0% |
| `6/7` | 2 | 0 | 0.0% |
| `5/7` | 4 | 0 | 0.0% |

## The gate's verdict

| verdict | cases | closing would be correct | rate |
|---|---|---|---|
| `VERIFIED` | 20 | 20 | 100.0% |
| `REJECTED` | 6 | 0 | 0.0% |

## Reading it

- When the investigator asked to resolve, closing was correct **76.9%** of the time (20/26). Its request is a hypothesis, not a probability, and the numbers say so.
- When every evidence check passed (`7/7`), closing was correct **100.0%** of the time (20/20).
- A partial score is not a weaker yes. The gate treats a failed **linkage**
  check as disproof, so cases below a full score are not 'probably fine' --
  they are cases where something specific was shown to be wrong or absent.
  That is why the score is displayed as a checklist and never as a percentage.

**Caveat:** these rates come from one run on one synthetic dataset with a
single investigator. They describe this fixture, not reconciliation in
general, and the sample per bucket is small. No threshold anywhere in the
system was tuned from this table, and none should be without far more data.
