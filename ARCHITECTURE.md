# Architecture

## Trust boundary

On the Line separates probabilistic interpretation from deterministic authority:

1. **Channels** — curator SPA, public web intake, signed Twilio inbound intake, consent-gated
   outbound verification, HSDS 3.2 HTTP profile.
2. **Agent interpretation** — Nova 2 Sonic handles live speech; a Strands Graph runs a safety
   reader followed by a structured Nova 2 Lite extractor.
3. **Policy** — pure Python classifies complete evidence into Tier 0–3. It validates coverage,
   confidence, readback, canonical schedules, E.164 phones, high-judgment fields, and removals.
4. **Persistence** — short `BEGIN IMMEDIATE` transactions own HSDS mutation, task claims, consent
   consumption, reviews, referrals, failures, sessions, and audit events.

Models receive no database handle, arbitrary SQL, publish tool, delete tool, or consent authority.
That boundary remains true for direct Bedrock and AgentCore execution.

## Loop A: provider verification

`priority queue → consent reservation → provider call → structured evidence → policy → commit/review`

- The scheduler scores field-aware staleness, referral volume, first-hand failures, seasonality,
  volatility, and recent organization contact.
- Claiming a queued task is compare-and-set inside a write transaction.
- Dial reservation consumes one active consent allowance only when the exact local weekday, time,
  validity period, call cap, permanent suppression, retry time, and organization spacing permit it.
- Twilio creates the call through the authenticated REST API and receives signed TwiML, lifecycle,
  and WebSocket endpoints.
- Nova 2 Sonic can look up only the requested fields, confirm unchanged facts, propose a bounded
  change with a verbatim quote, or end the call with an enumerated reason.
- Non-contact outcomes persist exponential retry time. Stop requests permanently suppress calls.
- Tier 0/1 writes occur atomically with their evidence. Tier 2/3 writes become durable review
  records and cannot alter the HSDS service before a curator transaction resolves them.

## Loop B: help-seeker intake

`need → active-service search → referral receipt → reported field failure → priority task`

Both web and voice intake use the same store methods. Search accepts a service need and optional
postal code, returns active services ranked by semantic match and assurance freshness, and does not
ask for identity. Voice intake tools create a referral receipt and can report a failed referral.
The failure and task creation or promotion share one transaction; a direct failure receives
priority 1000 and its field category is merged into any existing open task.

## Strands implementation

- `BidiAgent` with `BidiNovaSonicModel` powers provider and seeker voice conversations with
  interruption handling.
- `GraphBuilder` fixes extraction order: safety reader, then structured extractor. Every call
  ends here, including a live provider call: the finished transcript is read by this graph, which
  took no part in the conversation, and its reading has to agree with what the voice agent
  recorded before a change can publish.
- `structured_output_model=ExtractionResult` enforces the output schema before policy.
- `GoalLoop` retries invalid domain output and verifies status-removal/readback invariants.
- `ontheline.readback` corroborates a claimed readback against the transcript with no model at
  all, so the check holds in every mode including one with no credentials.
- invocation hooks record the agent lifecycle attached to each verification run.
- `FileSessionManager` persists graph state under a task-specific, path-safe session identifier.
- the official Strands Evals SDK executes the versioned provider-persona experiment.

### Evidence for a live call

A live provider call produces two independent readings of the same conversation, and the tier
policy sees the reconciliation of both.

The voice agent records what it heard through its tools while the call is happening. That record
includes `read_back_confirmed`, which the tier policy treats as evidence, and which is produced by
the model that ran the conversation. So it is checked before policy sees it.
`ontheline.readback.corroborate` looks in the transcript for the exchange the flag claims: the
provider raising the value, the assistant reading the whole of it back, and the provider agreeing,
in that order. It uses no model, so it runs on every call in every mode, and it only ever removes
confidence.

Where a model path is configured, the finished transcript is then read by the safety-reader and
extractor graph, which took no part in the call.
`ontheline.telephony.reconcile_extractions` requires the two readings to agree on a field and its
value before that field can publish without a curator; a disagreement, a change only one reading
saw, or an extraction that could not be produced all send the change to the decision inbox with
the reason recorded on the run. A safety outcome from either reading stops the call; neither can
lift one.

The plan's human-interrupt requirement is implemented as a database-backed interrupt boundary.
This makes a review independent of a model process, keeps it visible across deployments, and lets
approve/edit/reject transactions revalidate the proposed field before publication. Campaign
parallelism is implemented through atomic claims and independent AgentCore invocations; this
prevents emergent multi-agent handoffs from double-calling an organization.

## HSDS 3.2 data contract

The canonical schema normalizes organizations, services, locations, service-at-location links,
phones, and schedules. Public export uses stable RFC 4122 identifiers and dereferenced HSDS 3.2
objects. Import validates the complete payload with Pydantic, upserts idempotently, preserves
records absent from incremental imports, queues active services, and appends one directory import
event.

Operational tables—tasks, consents, calls, runs, proposals, safety flags, referrals, failures,
login sessions, and audit events—remain outside HSDS fields. `assured_date` changes only after all
requested fields are covered and the complete verification has no pending judgment.

## Integrity and concurrency

- SQLite foreign keys, WAL, `synchronous=FULL`, secure deletion, and five-second busy timeouts are
  applied on every connection.
- A partial unique index permits one open verification task per service.
- Task claims and consent call counters are transactional, preventing duplicate workers and calls.
- Proposal resolution is single-use. `_apply_change` is the only write path into a directory
  field, so an autonomous change and a curator's edit meet the same canonical-form validators:
  HSDS status values, canonical schedules, and E.164 phone numbers.
- Audit triggers reject every `UPDATE` and `DELETE`. Each hash covers the prior hash, canonical
  payload, event type, actor, entity, and timestamp.
- Online backups use SQLite's backup API, run `PRAGMA integrity_check`, and publish by atomic rename.
- HTTP bodies, transcript turns, model changes, Twilio control messages, audio frames, and call
  duration are independently bounded.

## Deployment topology

The application container is the sole SQLite writer and mounts `/app/data` on durable encrypted
storage. The bundled single-host deployment uses Caddy for automatic TLS on EC2 with encrypted
EBS; a trusted AWS load balancer or CloudFront edge is also supported. Production settings require
HTTPS, HSTS, an exact hostname, unique credentials, no sample data, live extraction, and Twilio.

The AgentCore image is stateless. It exposes `/ping` and `/invocations`, runs the Strands extraction
graph, and returns only schema-valid extraction plus lifecycle trace. The application invokes it
with a deterministic session identifier, then performs policy and persistence locally. The runtime
IAM role grants Nova model invocation and telemetry; it has no directory database credentials.
