# Operations runbook

## Production start gate

Production configuration is accepted only when all of these are true:

- `OTL_ENVIRONMENT=production`;
- unique session secret of at least 32 characters;
- unique initial curator password of at least 14 characters;
- `bedrock` or `agentcore` extraction mode;
- Twilio SID, token, and E.164 outbound number;
- sample data disabled;
- HTTPS enforcement and an HTTPS public origin;
- an exact deployed hostname with no wildcard or test host;
- the load balancer's private source addresses in `OTL_FORWARDED_ALLOW_IPS`;
- database and Strands session paths on durable encrypted storage.

Run the external credential and storage gate:

```powershell
python scripts/preflight.py --env-file .env
```

The process runs as a non-root container user. TLS terminates at the trusted AWS edge or load
balancer, which forwards to port 8080. Only that proxy address is trusted for forwarded headers.

## Health

- `GET /health` verifies application database access and returns HTTP 503 on failure.
- AgentCore uses `GET /ping` and returns `{"status":"Healthy"}`.
- `GET /api/system` reports the configured agent and telephony modes plus audit-chain status to an
  authenticated curator.
- The container probes `/ping` every 30 seconds.

## Backup and restore

Create an online, transactionally consistent backup without stopping the service:

```powershell
ontheline backup backups/ontheline-2026-08-11.db
```

The command refuses to overwrite a target, uses SQLite's online backup API, runs
`PRAGMA integrity_check`, and atomically publishes the completed file. Encrypt and replicate the
backup through the platform's storage policy.

Restore sequence:

1. Stop the application writer.
2. Preserve the current database and WAL/SHM files as an incident artifact.
3. Place the verified backup at `OTL_DATABASE_PATH`.
4. Start the application.
5. Run `ontheline audit-verify`.
6. Check `/health`, sign in, and verify the dashboard counts and newest audit event.

## Incident handling

| Signal | Immediate action |
|---|---|
| Audit-chain warning | Stop mutating traffic, preserve database files, verify the latest backup and event boundary |
| Tier 1 evidence dispute | Preserve the run, suspend its service from referral, and resolve through the decision record |
| Repeated media-frame drops | Stop new calls, inspect network and Bedrock stream latency, retain call lifecycle events |
| Multiple failures for one service | Keep priority 1000, disable referral by curator-approved status, verify provider data |
| Twilio signature failure | Reject with 401/1008, compare public origin and proxy URL handling, rotate token after credential exposure |
| AgentCore extraction failure | Task claim is released with audit evidence; verify runtime readiness and invoke permission |

## Data operations

- Import provider records from the curator directory view using dereferenced HSDS 3.2 JSON.
- Record consent after import and before placing any live provider call.
- Run `ontheline audit-verify` after restore, migration, or incident response.
- Run `ontheline backup` on the deployment backup schedule and verify replication completion.
- Curator sessions expire after the configured lifetime; revocation occurs on logout or session-row
  deletion.

## Supported deployment topology

One application container owns the durable SQLite volume and accepts concurrent HTTP readers and
short transactional writes. The stateless AgentCore extraction runtime scales independently and
has no directory credentials. Atomic task claims allow concurrent extraction workers while the
single application writer remains the authority for consent, policy, HSDS mutation, and audit.
The contest runbook places the application and Caddy HTTPS proxy on one EC2 host with encrypted
EBS; do not scale the stateful application past one replica without migrating the store.
