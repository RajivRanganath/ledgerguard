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
| `mistral-large-latest` | 100.0% | **6 of 6** | 6 | **INR 0.00** |
| `gemini-2.5-flash` | 98.8% | **5 of 6** | 5 | **INR 0.00** |
| `openai/gpt-oss-120b` | 96.5% | 2 of 6 | 6 | **INR 0.00** |
| `heuristic_stub` (offline) | 100.0% | **6 of 6** | 6 | **INR 0.00** |

Their reasoning was fluent and specific — naming the right refund, citing the
right amount, correctly observing that applying it balances the ledger. Every one
of those observations is true. The conclusion is still wrong, because the refund
belongs to a different customer. **Without the gate, the highest-scoring model in
this table would have made six silent false closures.**

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
[`docs/model_comparison.md`](docs/model_comparison.md). Across three vendors, a
deliberately naive offline heuristic and no investigator at all, the value
falsely auto resolved was INR 0.00 in every case.

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

One API key is needed, and the system runs without any. Auto-detection order is
`anthropic` → `groq` → `cerebras` → `gemini` → `nvidia` → offline stub:

| Env var | Provider | Default model |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic SDK | `claude-opus-5` (explicit only) |
| `GROQ_API_KEY` | OpenAI-compatible | `openai/gpt-oss-120b` |
| `CEREBRAS_API_KEY` | OpenAI-compatible | `gpt-oss-120b` (explicit only) |
| `GEMINI_API_KEY` | `generateContent` | `gemini-2.5-flash` |
| `NVIDIA_API_KEY` | OpenAI-compatible | `meta/llama-3.3-70b-instruct` |

Plus `OMNIROUTE_API_KEY` for a local [OmniRoute](https://www.npmjs.com/package/omniroute)
router, which fronts many upstreams (OpenRouter, OpenCode, Mistral, …) behind one
endpoint. Any other OpenAI-compatible host works via
`LEDGERGUARD_PROVIDER=openai_compatible` plus `LEDGERGUARD_BASE_URL`,
`LEDGERGUARD_API_KEY` and `LEDGERGUARD_MODEL`.

**Failover is automatic.** With more than one key configured, the default
`auto` provider builds a chain and fails over when one runs out — and *within* a
provider it rotates between model routes. Free tiers die mid-run constantly, and
neither failure should turn a resolvable case into an abstention:

```
fallback(groq -> gemini -> nvidia -> omniroute)        # 4 providers, 12 routes
```

OmniRoute is deliberately last and is the deepest link: it fronts Mistral,
NVIDIA and Groq under *its own* credentials, separate from the direct keys, so
it survives those running out. Every route in the chain was individually
verified to answer with a strict JSON schema before being listed — dead ones
(Claude via OmniRoute, end-of-life NVIDIA endpoints, models that answer in prose)
are not carried, since a dead route only buys latency before the rotation gives
up on it.

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

Artifacts, all generated by the benchmark run and none typed by hand:

- `evaluation/outputs/benchmark.md` — the comparison table
- `evaluation/outputs/benchmark.json` — every metric, both systems
- `evaluation/outputs/cases.json` — full per-case detail
- `evaluation/outputs/unresolved_exceptions.csv` — the honest failure surface

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

## Tests

Five core P0 tests (7 functions), each protecting something that would otherwise
break silently:

```bash
.venv/bin/python -m pytest ledgerguard/tests -q
```

1. Financial arithmetic is exact on a known example
2. Shadow Ledger invariants separate a valid lifecycle from three corruptions
3. Evidence Gate accepts a correctly linked refund
4. **Evidence Gate rejects an amount-matching refund from another payment**
5. Insufficient evidence abstains, in four different ways, and never force-resolves

## Secrets

`ANTHROPIC_API_KEY` only, read from the environment. `.env` is gitignored;
`.env.example` carries placeholders. No key, token or credential is committed.

## Repository

```
ledgerguard/
  ledger/       money.py  models.py  shadow_ledger.py  invariants.py
  reconciliation/  matcher.py  exceptions.py
  ai/           provider.py  investigator.py  schemas.py
  evidence/     verifier.py  safety.py
  synthetic/    generator.py  fault_injector.py  adversarial.py
  evaluation/   baseline.py  benchmark.py  metrics.py  frozen/  outputs/
  backend/      app.py
  frontend/     index.html
  tests/        test_p0_core.py
  pipeline.py
docs/           architecture.md  evaluation.md  limitations.md
BUILD_STATUS.md
```

`docs/demo_script.md` is the timed five minute walkthrough;
`docs/panel_defense.md` is the written defense of the design decisions.
`BUILD_STATUS.md` records every checkpoint with the evidence that backs it,
including two bugs found and fixed during evaluation.

---

*Solo project. I designed, built and evaluated this.*
