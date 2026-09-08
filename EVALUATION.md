# Evaluation

On the Line evaluates deterministic authority independently from probabilistic extraction. A model
response cannot weaken the release gate: every result crosses the same schema, policy, and
transaction tests.

## Committed Strands Evals experiment

`evals/personas.json` is a versioned corpus of 12 provider situations:

- precise unchanged confirmation;
- explicit routine hours change with confirmed readback;
- uncertain new volunteer;
- closed program;
- help-seeker at a shared number;
- hostile gatekeeper;
- distressed help-seeker;
- voicemail;
- incomplete new hours;
- eligibility change;
- missing AI disclosure;
- permanent stop request.

Run the official Strands Evals SDK experiment:

```powershell
python scripts/run_evaluation.py
```

The committed report at `evals/results/policy-evaluation.json` records:

| Metric | Result |
|---|---:|
| Personas / executions | 12 / 12 |
| Exact policy match | 100% |
| False-apply rate | 0% |
| Over-escalation rate | 0% |
| Exact-change precision | 100% |
| Exact-change recall | 100% |
| Tested removals blocked | 100% |
| Missing disclosure blocked | 100% |
| Stop requests blocked | 100% |

The report embeds the complete Strands `EvaluationReport`, SDK case results, timestamp, corpus
version, and runtime identifier. CI can regenerate it without AWS calls.

## Credentialed extraction experiment

The same cases can execute the deployed Nova 2 Lite Graph rather than the gold extraction:

```powershell
python scripts/run_evaluation.py --mode bedrock --runs 3 --output evals/results/bedrock-evaluation.json
```

This mode preserves the same expected policy outcomes while measuring exact proposed-change
precision and recall from live model output. Each case uses the production safety reader,
structured output schema, GoalLoop invariant checks, lifecycle hooks, and task-safe persisted
session. Precision and recall are scored independently per persona execution, so a correct value in
one case cannot mask a wrong assignment in another. `--runs 3` produces 36 isolated executions and
reports both personas and runs per persona.

## Deterministic release suite

```powershell
pytest -q
```

The suite covers:

- all 256 G.711 μ-law bytes and signed 16-bit encode ranges against Python's reference codec;
- framed resampling, anti-aliasing, backpressure, barge-in, and final-frame flushing;
- provider and seeker disclosures, voice tool authority, and inbound referral side effects;
- every tier boundary, partial verification, atomic multi-field behavior, and canonical values;
- safe auto-apply, closure review, curator approve/edit/reject, and permanent opt-out;
- atomic claims, provider consent windows, call spacing, exponential retry, and Twilio lifecycle;
- HSDS 3.2 export/import round-trip with stable UUIDs;
- failure/referral ownership, priority ordering, and semantic referral ranking;
- append-only hash-chain integrity and verified online backup;
- AgentCore request validation and application-to-runtime delegation;
- authentication, CSRF, request limits, signed callbacks, PII redaction, and HTTP product flows.

## Release gates

A release passes only when all tests and static checks succeed, policy exact match is 100%, false
apply is zero, every tested removal is held for a curator, every missing disclosure is blocked, and
every stop request produces Tier 3. The CI workflow runs Python 3.12 and 3.13 and builds both ARM64
images: the stateful application and the dedicated stateless AgentCore runtime.

Recorded release verification on 2026-08-12:

| Gate | Result |
|---|---:|
| Pytest | 124 passed |
| Ruff | All checks passed |
| Python bytecode compilation | Passed |
| Browser JavaScript syntax | Passed |
| Locked dependency graph | 121 packages, consistent |
| Installed dependency consistency | No broken requirements |
| Dependency advisory audit | No known vulnerabilities |
| Strands policy experiment | 12/12 passed |
| Built wheel | `on_the_line-1.0.0-py3-none-any.whl` |
| Wheel SHA-256 | `97ea754b9866b92bf612537bc5b293d9dc76dbdf64969b478ae58122a4de9947` |

## Freshness and cost reporting

Directory freshness is computed from active services whose `assured_date` is within 30 days and is
visible on the dashboard before and after a campaign. Assurance advances only for complete Tier 0/1
verification, so the measure cannot be inflated by partial or pending calls.

Cost per verified record is calculated from provider call duration and the deployment's current
Twilio Voice, Twilio Media Streams, and Bedrock Nova 2 Sonic rate cards. Rate-card values remain in
the billing report rather than source code, keeping the measurement tied to the deployed region,
destination, and account price.
