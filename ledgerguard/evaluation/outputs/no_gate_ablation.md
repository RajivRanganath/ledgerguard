# With the gate versus without it

Recomputed from `model_comparison.json`, an already-recorded run. No model
calls were made and no benchmark was re-run to produce this file.

Every investigator's raw hypothesis for every wrong-linkage (F6) case was
recorded before the Evidence Gate ruled on it. Trusting those hypotheses
directly is what a system without a gate would do.

| | Count |
|---|---|
| Investigators compared | 5 |
| F6 cases each | 6 |
| **Attempts** | **30** |
| Would have auto closed with **no gate** | **25 of 30** |
| Actually auto closed **with the gate** | **0 of 30** |

## Per investigator

| Investigator | Asked to close an F6 | Closed with the gate |
|---|---|---|
| `groq:openai/gpt-oss-20b` | 6/6 | 0 |
| `gemini:gemini-2.5-flash` | 5/6 | 0 |
| `nvidia:openai/gpt-oss-120b` | 4/6 | 0 |
| `omniroute:mistral/mistral-medium-3-5` | 4/6 | 0 |
| `heuristic_stub` | 6/6 | 0 |

## What it would have cost

The count above is not the money. With the gate in place nothing was
falsely closed, so the value of what it blocked is never realised in
that run. `evaluation/ablation.py` measures it directly by running a
`hybrid_no_gate` arm end to end with `omniroute:mistral/mistral-large-latest`: **6 false auto resolutions, INR 25754.40 falsely closed** on the same holdout.

## What this does and does not show

It shows the gate is load-bearing rather than decorative: the investigators
did propose closing the adversarial cases, repeatedly, and the gate is the
only reason none of them closed. It does not show the gate is sufficient.
The denominator is small and one-sided -- 6 hand-built wrong-linkage cases
in a single fault taxonomy -- so this is evidence that the gate catches the
failure it was designed for, not that it catches every failure.
