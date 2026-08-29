# LedgerGuard

**A self verifying AI finance controller.** Razorpay AI Buildathon, Track 04.

I built LedgerGuard to answer a question that a reconciliation chatbot does not
answer: an LLM can generate a plausible explanation for a financial discrepancy
in about two seconds — but how do you know whether that explanation is
*financially true*?

**The core claim: LLM reasoning is not financial truth. Financial truth is
deterministic accounting state plus verified evidence. The AI may propose, the
financial system must prove.**

---

## What it does

LedgerGuard reconciles orders, payments, refunds, settlements and bank records.

1. **The Shadow Ledger** independently reconstructs what the money movement
   *should* have been, from first principles:
   `expected_net = captured − refunds − fees − tax ± valid adjustments`.
   It never reads settlement or bank records to decide what to expect. Six named
   invariants (`I1`–`I6`) compare that reconstruction against what the provider
   actually reported. All money is integer paise; no float ever touches it.

2. **A deterministic engine reconciles everything it can prove** — exact
   identifier matching, settlement arithmetic against the fee schedule,
   duplicate detection, timing windows, bank matching. On the frozen holdout it
   resolves 54 of 85 cases with zero AI involvement, and never sends a case to
   the model that it could prove on its own.

3. **The AI investigator only sees what could not be proved.** It gets the
   records, the Shadow Ledger's expected value, the discrepancy, the permitted
   hypothesis taxonomy, and candidate related records. It returns structured
   JSON only. It cannot do arithmetic, cannot edit a record, cannot touch the
   ledger, and cannot close a case.

4. **The Evidence Gate re-proves every claim.** If the model says a shortfall is
   a partial refund, the gate checks whether that refund exists, whether it is
   already owned by another payment, whether its reference identifies *this*
   order's customer, whether the amount matches, whether the timing is possible,
   and whether re-running the Shadow Ledger with that refund applied restores the
   invariant exactly. Verdict: `VERIFIED`, `UNVERIFIED`, or `REJECTED`.

5. **When proof is insufficient, it abstains.** Four states —
   `AUTO_RESOLVED`, `RECOMMEND_REVIEW`, `HUMAN_REVIEW_REQUIRED`, `UNRESOLVED`.
   Abstention is a designed outcome, not a failure, and the system is explicitly
   not optimised toward closing every exception.

### Why this is not just an LLM reconciliation bot

A model reading a CSV and explaining discrepancies is fast to build and
impossible to trust. The three things that make LedgerGuard different:

- The expected financial state is computed by code the model cannot reach.
- Every model conclusion is re-derived from records before it can be acted on.
- The system reports what it could not resolve, with rupee exposure attached.

There is no model-generated confidence number anywhere in the system. The UI
shows a transparent **Verification Score** instead — literally *"5 of 7 evidence
checks passed"*, with each check named and its detail shown.

### What happens when evidence is insufficient

The case is escalated with the specific missing evidence attached, and exported
to `ledgerguard/evaluation/outputs/unresolved_exceptions.csv` with its rupee
exposure, what was known, what was missing, why resolution was blocked, and a
suggested human action.

---

## What the benchmark actually showed

Frozen holdout, 85 lifecycles, seed `20260905`, batch SHA-256 `3e28d4572d4cfc95`.
Investigator: the default chain `fallback(groq->gemini->nvidia->omniroute)`, run
uncached — Groq served all 15 investigations. Both systems share the same
deterministic engine, the same Shadow Ledger and the same safety gate; the only
difference is whether the investigation step runs.

| Metric | Rules only | LedgerGuard |
|---|---|---|
| Total cases | 85 | 85 |
| Match rate | 63.5% | 63.5% |
| Exceptions raised | 31 | 31 |
| False exceptions (clean case flagged) | 0 | 0 |
| Missed faults (fault case matched) | 0 | 0 |
| Disposition accuracy | 89.4% | **97.7%** |
| Exceptions correctly resolved | 12 | **19** |
| Exceptions incorrectly resolved | 0 | 0 |
| Correct abstentions | 10 | 10 |
| Unnecessary abstentions | 9 | **2** |
| Value left unresolved | INR 191,780.59 | INR 135,901.89 |
| **False auto resolutions** | **0** | **0** |
| **Value falsely auto resolved** | **INR 0.00** | **INR 0.00** |
| Investigations run | 0 | 15 |
| Investigation latency p50 / p95 | — | 10.8s / 12.8s |

**What this actually says:** the hybrid closes 7 of the 9 ambiguous refund cases
the rules-only system had to escalate, and gives up none of the baseline's
safety — all six wrong-linkage cases stay escalated and the value falsely auto
resolved stays at zero. The gain is recall on ambiguous exceptions, nothing
more. The hybrid does not beat the baseline on anything the baseline could
already prove, and it is not supposed to.

**The result that matters most is not in that table**, and it is not one model.
On the six wrong-linkage cases, run uncached over the same holdout:

| Investigator | Accuracy | Asked to close a wrong-linkage case | Gate rejected | **Value falsely closed** |
|---|---|---|---|---|
| `gemini-2.5-flash` | 100.0% | **5 of 6** | 5 | **INR 0.00** |
| `openai/gpt-oss-20b` (Groq) | 98.8% | **6 of 6** | 6 | **INR 0.00** |
| `mistral-medium-3-5` (OmniRoute) | 97.7% | 4 of 6 | 6 | **INR 0.00** |
| `openai/gpt-oss-120b` (NVIDIA) | 95.3% | 4 of 6 | 5 | **INR 0.00** |
| `heuristic_stub` (offline) | 100.0% | **6 of 6** | 6 | **INR 0.00** |

Four vendors, four separate free tiers, plus a deliberately naive offline
heuristic. Their reasoning was fluent and specific — naming the right refund,
citing the right amount, correctly observing that applying it balances the
ledger. Every one of those observations is true. The conclusion is still wrong,
because the refund belongs to a different customer. **Without the gate, the
highest-scoring model in this table would have made five silent false
closures.**

Note also that accuracy and safety are not the same axis: the most accurate
investigator is the most eager to close the adversarial cases, and the least
accurate is the most cautious. Ranking by accuracy would pick the one that leans
hardest on the gate.

**Model output is not reproducible.** Five `groq` runs on the identical frozen
holdout resolved between 5 and 7 of the 9 F3 cases (95.3%–97.7% accuracy) at
`temperature: 0`. What did *not* vary: false auto resolutions stayed 0, value
falsely closed stayed INR 0.00, and all six F6 cases were escalated every time.
The variance lands entirely in how much is safely closed, never in whether
something wrong gets closed.

Full detail, including reliability and latency, is in
[`docs/model_comparison.md`](docs/model_comparison.md). Across four vendors on
four separate free tiers, a deliberately naive offline heuristic and no
investigator at all — 5 investigators x 6 wrong-linkage cases, **30
provider-versus-F6 attempts** on an 85-case frozen holdout — the value falsely
auto resolved was INR 0.00 in every one. The per-provider accuracy figures are
single-run samples and move on a rerun; that last column has not.

**With the gate versus without it.** That INR 0.00 is not because the
investigators declined to close the adversarial cases. It is because they were
stopped. Every investigator's raw hypothesis was recorded before the gate ruled
on it, and recomputing over that record —
`python -m ledgerguard.evaluation.no_gate_ablation`, no new model calls —
gives the counterfactual directly:

| | Wrong-linkage cases auto closed |
|---|---|
| Trusting the raw hypothesis, no gate | **25 of 30** |
| LedgerGuard, gate enforced | **0 of 30** |

The count is not the money: with the gate in place the value it blocks is never
realised, so `evaluation/ablation.py` measures that separately by running a
`hybrid_no_gate` arm end to end — **6 false auto resolutions, INR 25,754.40
falsely closed** for a single investigator on the same holdout. Artifact:
[`evaluation/outputs/no_gate_ablation.md`](ledgerguard/evaluation/outputs/no_gate_ablation.md).

### Two claims, two kinds of evidence

These get run together easily, so I keep them apart everywhere:

**1. Architectural — the investigator cannot close a case.** This is a property
of the code, not a measurement. `evidence/safety.py::decide` reaches
`AUTO_RESOLVED` on exactly two routes: a deterministic exception type whose
cause is proven outright with no unexplained residual, or an Evidence Gate
verdict of `VERIFIED`. The model's `recommended_action` can only downgrade a
close to a review; it can never create one. It does not depend on which model
is used, or on any model behaving well.

Stress-tested, not just argued: an investigator that fabricates a record id on
*every* case had all 15 responses flagged and stripped, closed nothing on the
invented evidence, and produced output identical to the rules-only baseline. An
invalid API key produced HTTP 401, retired the provider after route rotation,
and degraded those cases to human review with the batch intact.

**2. Empirical — zero false closures, on this holdout.** On the frozen
85-lifecycle holdout, all **6** wrong-linkage (F6) cases stayed escalated for
every investigator tried: 5 investigators x 6 cases = **30 attempts**, 0 false
auto resolutions, INR 0.00 falsely closed. That is 6 adversarial cases and one
fault taxonomy — it is evidence, not a guarantee. A different fault
distribution could contain a case this taxonomy does not model, and the honest
version of this claim always carries its denominator.

The first claim is why I expect the second to hold on data I have not seen. The
second is not what proves the first.

Regenerate every figure above with:

```bash
python -m ledgerguard.evaluation.benchmark --provider groq
python -m ledgerguard.evaluation.model_comparison --providers groq,gemini,stub
```

---

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # optional; the system runs without a key
```

One free API key is enough, and the system also runs with none at all — the
investigation step degrades to abstention and everything else is unchanged.
The automatic chain is
`groq` → `gemini` → `nvidia` → `omniroute` → offline stub:

| Env var | Provider | Default model | In the auto chain |
|---|---|---|---|
| `GROQ_API_KEY` | OpenAI-compatible | `openai/gpt-oss-120b` | 1st |
| `GEMINI_API_KEY` | `generateContent` | `gemini-2.5-flash` | 2nd |
| `NVIDIA_API_KEY` | OpenAI-compatible | `openai/gpt-oss-120b` | 3rd |
| `OMNIROUTE_API_KEY` | local router on `:20128` | `mistral-large-latest` | 4th |

`OMNIROUTE_API_KEY` drives a local [OmniRoute](https://www.npmjs.com/package/omniroute)
router, which fronts many upstreams (OpenRouter, OpenCode, Mistral, …) behind one
endpoint under its own separate credentials — which is why it stays in the chain
even when the direct keys above are exhausted.

**All four are free tiers, and that is deliberate.** No paid API key is used
anywhere in this repository, and none is needed to reproduce any number in it.
The claim being made is that the Evidence Gate holds regardless of how strong the
investigator is, which four ordinary free models support better than one
expensive one. Any other OpenAI-compatible host works via
`LEDGERGUARD_PROVIDER=openai_compatible` plus `LEDGERGUARD_BASE_URL`,
`LEDGERGUARD_API_KEY` and `LEDGERGUARD_MODEL`.

**Failover is automatic.** With more than one key configured, the default
`auto` provider builds a chain and fails over when one runs out — and *within* a
provider it rotates between model routes. Free tiers die mid-run constantly, and
neither failure should turn a resolvable case into an abstention:

```
fallback(groq -> gemini -> nvidia -> omniroute)        # 4 providers, 13 routes
```

OmniRoute is deliberately last and is the deepest link: it fronts Mistral,
NVIDIA and Groq under *its own* credentials, separate from the direct keys, so
it survives those running out. Every route in the chain was individually
verified by calling it — strict `json_schema` where the host supports it,
`json_object` on NVIDIA — and dead ones are not carried, since a dead route only
buys latency before the rotation gives up on it. Routes dropped after testing:
Claude via OmniRoute (401 / no active credentials), NVIDIA endpoints the account
is not entitled to invoke, and models that answer in prose instead of JSON.

- A route that is rate limited past its retries, out of quota, or emitting
  unparseable output is **retired for the run**, not retried per case.
- A provider that fails twice in a row is dropped from the chain, so later cases
  do not pay its retry budget.
- A `400` naming an optional parameter (`reasoning_effort`, `temperature`) means
  *we* sent something the model does not accept — the parameter is dropped and
  the route retried rather than retired.
- Only the last link waits out rate limits; earlier links fail over immediately.

Failover changes **who gets asked**, never what counts as proof. The Evidence
Gate re-derives every hypothesis from records regardless of which provider
answered, so a weaker fallback can cost recall but cannot cost safety.
`python -m ledgerguard.evaluation.benchmark --provider fallback` prints exactly
who served what and which routes died.

With no key at all the AI investigation step degrades to "cannot resolve" and
everything else — rules, ledger, Evidence Gate, benchmark, dashboard — works
unchanged. Every artifact and the dashboard name the provider that produced
them, so a stub run can never be mistaken for a model run.

## Demo

```bash
./run_demo.sh          # tests, then benchmark, then http://127.0.0.1:8137
```

With the server up, verify every beat of the demo before presenting:

```bash
.venv/bin/python -m ledgerguard.tests.rehearsal_check
```

It walks all five beats of [`docs/demo_script.md`](docs/demo_script.md) three
times and fails if any of them regressed — including the adversarial case.

Or the pieces individually:

```bash
.venv/bin/python -m pytest ledgerguard/tests -q
.venv/bin/python -m ledgerguard.evaluation.benchmark
.venv/bin/python -m uvicorn ledgerguard.backend.app:app --port 8137
```

## Dataset generation

```bash
.venv/bin/python -c "from ledgerguard.synthetic.fault_injector import build_dataset; \
  d = build_dataset(seed=20260905, count=320); print(len(d.lifecycles))"
```

320 lifecycles from a fixed seed. Ground truth is computed before any fault is
injected and stored separately from anything the controller ever sees. Six fault
classes:

| Class | Fault | n | Correct outcome |
|---|---|---|---|
| F1 | Missing settlement | 15 | escalate — the money is outstanding |
| F2 | Duplicate record | 15 | auto close — provable by id and content |
| F3 | Unlinked partial refund | 29 | auto close — but only with verified evidence |
| F4 | Fee or tax mismatch | 15 | auto close — provable against the fee schedule |
| F5 | Delayed settlement | 16 | auto close — amounts correct, timing wrong |
| F6 | Incorrect linkage | 12 | **escalate — plausible but unprovable** |
| — | Clean | 218 | reconcile silently |

F3 and F6 are deliberately indistinguishable to the deterministic layer: both
surface as `EXCEPTION_UNEXPLAINED_SHORTFALL` of exactly the size of an unlinked
refund sitting in the batch. Only the customer linkage separates them.

## Evaluation procedure

Split is 75/25 dev/holdout, stratified by fault class so the holdout cannot end
up with zero F6 cases, and paired F6 lifecycles never straddle the split. The
holdout is frozen to
`ledgerguard/evaluation/frozen/holdout_20260905_320.json`; every subsequent run
re-verifies its SHA-256 and aborts with `FROZEN HOLDOUT MISMATCH` if the
regenerated data no longer matches. No system was tuned on individual holdout
answers.

Artifacts, all generated by a run and none typed by hand. They come from **two
different commands**, so read the investigator line at the top of each file
before comparing numbers across them.

From `python -m ledgerguard.evaluation.benchmark`:

- `evaluation/outputs/benchmark.md` — the comparison table
- `evaluation/outputs/benchmark.json` — every metric, both systems
- `evaluation/outputs/cases.json` — full per-case detail
- `evaluation/outputs/unresolved_exceptions.csv` — the honest failure surface

From `python -m ledgerguard.evaluation.report --provider fallback`:

- `evaluation/outputs/reconciliation_report.md` — exportable summary
- `evaluation/outputs/reconciliation_report.csv` — one row per case
- `evaluation/outputs/evidence_ledger.csv` — one row per evidence check

Both sets are committed from live runs of the same chain, but they are separate
runs, and model output is not reproducible: the committed benchmark closed 19
exceptions leaving INR 135,901.89, the committed report closed 20 leaving
INR 126,017.84. That gap is the sampling variance described above, not a
disagreement — the safety column is identical in both, with all six F6 cases
escalated and INR 0.00 falsely closed. Regenerating either one moves its
accuracy figures and will not reproduce the other's.

## The adversarial case

Built by hand in `ledgerguard/synthetic/adversarial.py` and pinned by two tests.

Two payments are short by exactly INR 1,500.00. One unlinked refund of exactly
INR 1,500.00 exists. Applying it makes the Shadow Ledger balance for *either*
payment — arithmetic cannot separate them. It genuinely belongs to Payment B
(customer `CUST-0077`); Payment A (customer `CUST-0001`) has no explanation at all.

The investigator proposes `unlinked_partial_refund` and asks to resolve — for
both. For Payment A the gate returns:

```
REJECTED | 6 of 7 evidence checks passed
  [PASS] E1_evidence_records_exist
  [PASS] E2_refund_is_processed
  [PASS] E3_refund_not_linked_elsewhere
  [FAIL] E4_reference_identifies_this_customer
         refund reference 'ADJ/CUST-0077/partial' vs order customer 'CUST-0001' -> NO MATCH
  [PASS] E5_amount_equals_shortfall       150000 vs 150000
  [PASS] E6_timing_is_plausible
  [PASS] E7_invariant_restored            shadow net == observed net
-> HUMAN_REVIEW_REQUIRED
```

The two checks that would justify closing the case — the amount agrees exactly,
and the ledger balances once the refund is applied — both pass, and the case is
still refused. **The gate rejects on linkage, not on amount.**

## Failure cases and handling

Handled without crashing the batch, each covered by a test:

| Failure | Behaviour |
|---|---|
| No API key | Offline stand-in; provider named in every report |
| Provider unavailable / times out / returns an error | That case → `HUMAN_REVIEW_REQUIRED`, batch continues |
| Provider raises instead of returning | Caught in the pipeline, same degradation |
| Malformed or unparseable model JSON | `InvestigationResult.invalid` → abstain |
| Rate limit (429) or transient 5xx | Retried up to 4x honouring `Retry-After`, then abstains |
| Truncated response (reasoning tokens ate the budget) | Fails JSON validation → abstain |
| Hypothesis outside the permitted taxonomy | Rejected at schema validation |
| Model returns an inadmissible evidence id | Dropped before the gate sees it, and recorded |
| Two cases claim the same evidence record | Both downgraded, order-independently |
| Missing transaction fields | Pydantic `extra="forbid"` models fail loudly at ingestion |
| Duplicate ingestion | Detected in a pre-pass, collapsed into one case |

## Analyses

Every one writes its own artifact under `evaluation/outputs/` and none of them
tunes anything:

```bash
python -m ledgerguard.evaluation.benchmark          # rules only vs hybrid
python -m ledgerguard.evaluation.ablation           # what each component is worth
python -m ledgerguard.evaluation.model_comparison   # five investigators, same holdout
python -m ledgerguard.evaluation.calibration        # is the Verification Score meaningful?
python -m ledgerguard.evaluation.drift              # which assumptions are load-bearing
python -m ledgerguard.evaluation.replay             # re-derive every decision from the record
python -m ledgerguard.evaluation.scale              # deterministic path at 12,000 lifecycles
python -m ledgerguard.evaluation.report             # exportable reconciliation + audit trail
python -m ledgerguard.qa "how much is still unresolved?"
```

`ablation` is the one to read first — it puts a number on the Evidence Gate.
Removing it turns a system with **zero** false closures into one that closes six
cases it should not have, worth **INR 25,754.40**. Three components, three
distinct failure modes: without the gate it closes what it should not; without
the Shadow Ledger it misses 26 of 31 faults; with neither (LLM only) it escalates
36 of 40 cases and automates nothing. Only the full system avoids all three.
`replay` is the one an auditor would care about: every decision is re-derived
from a written record, with the investigator's output replayed as data rather
than regenerated.

`qa` answers only from reconciled records and refuses anything it cannot derive.
The model may choose the query; the number is always computed by code.

## Tests

48 tests, in four tiers.

```bash
.venv/bin/python -m pytest ledgerguard/tests -q
```

**Five core P0 tests** (7 functions), each protecting something that would
otherwise break silently:

1. Financial arithmetic is exact on a known example
2. Shadow Ledger invariants separate a valid lifecycle from three corruptions
3. Evidence Gate accepts a correctly linked refund
4. **Evidence Gate rejects an amount-matching refund from another payment**
5. Insufficient evidence abstains, in four different ways, and never force-resolves

**P1 extended coverage** — duplicate shapes, settlement window boundaries,
generator reproducibility and split stability, malformed model output (five
shapes), timeouts and transport failure, malformed records, and compound
failures.

**P2 evaluation machinery** — including two tests that exist to stop an analysis
from lying: the ablation must actually change the outcome, and the replay must
be able to *fail* when a decision is tampered with.

**Fallback and route rotation** — seven offline tests prove provider failover,
retirement, complete-JSON failover, full failure reporting, malformed-provider
containment, configuration validation, and attribution to the model that
actually answered.

## Secrets

Provider API keys only — `GROQ_API_KEY`, `GEMINI_API_KEY`, `NVIDIA_API_KEY`,
`OMNIROUTE_API_KEY` — each read from the environment, each used for nothing but
an investigator call, and all four free tier. No other secret exists: there are no
Razorpay credentials, no database password, and no payment rail is touched.
`.env` is gitignored; `.env.example` carries placeholders. No key, token or
credential is committed.

## Repository

```
ledgerguard/
  ledger/          money.py  models.py  shadow_ledger.py  invariants.py
  reconciliation/  matcher.py  exceptions.py
  ai/              provider.py  openai_compatible.py  fallback.py
                   investigator.py  schemas.py
  evidence/        verifier.py  safety.py
  synthetic/       generator.py  fault_injector.py  adversarial.py  compound.py
  evaluation/      baseline.py  benchmark.py  metrics.py  report.py
                   ablation.py  calibration.py  replay.py  drift.py  scale.py
                   cost.py  model_comparison.py  frozen/  outputs/
  backend/         app.py
  frontend/        index.html
  tests/           test_p0_core.py  test_p1_extended.py  test_p1_compound.py
                   test_p2_evaluation.py  rehearsal_check.py
  pipeline.py      qa.py
docs/              architecture.md  evaluation.md  limitations.md  analyses.md
                   model_comparison.md  demo_script.md  panel_defense.md
BUILD_STATUS.md
```

`docs/demo_script.md` is the timed five minute walkthrough;
`docs/panel_defense.md` is the written defense of the design decisions.
`BUILD_STATUS.md` records every checkpoint with the evidence that backs it,
including two bugs found and fixed during evaluation.

---

*Solo project. I designed, built and evaluated this.*
