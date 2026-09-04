# Ethics and safety controls

On the Line automates calls that community resource specialists already make while keeping provider and
help-seeker agency inside enforceable product boundaries.

## Enforced commitments

1. **Consented providers only.** Outbound dialing requires an active consent record tied to the
   organization's exact E.164 business number. The transaction enforces timezone, weekday, local
   hours, validity dates, call cap, organization spacing, and permanent suppression.
2. **Immediate AI disclosure.** The first sentence identifies On the Line as an automated assistant and
   names the directory. Missing disclosure produces Tier 3 and blocks every directory write.
3. **One-step opt-out.** “Stop calling” ends the conversation and permanently sets `do_not_call`.
   There is no confirmation loop or automatic redial.
4. **No audio retention.** Audio exists only in the live media bridge. Structured transcript
   evidence is redacted and recording consent defaults false.
5. **Asymmetric protection.** Closure, suspension, null value, reduced service, status change, and
   eligibility change always require human judgment. No model tool can publish or delete.
6. **Complete-evidence assurance.** An assurance date advances only after every requested field has
   explicit evidence and the entire verification has cleared policy.
7. **Minimum seeker data.** Web and voice intake work without identity. They accept a service need
   and optional postal code. Common phone and email patterns in narrative feedback are redacted
   before persistence.
8. **Human authority.** Curators approve, edit, reject, or acknowledge persisted evidence. Edited
   values pass the same field validators as autonomous changes.
9. **Traceability.** Automatic changes, curator actions, consent, calls, referrals, failures, task
   transitions, and imports append hash-chained events protected against mutation.

## Operational controls

- Consent is obtained by an authorized organization representative and retained with its evidence
  reference, number, window, call allowance, and validity period.
- Calling cadence is configurable per deployment and retry dates are persisted with exponential
  backoff.
- Access uses individual server-side curator sessions with CSRF protection and login throttling.
- Production data lives on encrypted durable storage with verified online backups.
- Audit-chain verification runs after restore and is visible in the curator console.
- Incident handling preserves the verification run, transcript evidence, policy decision, and
  agent trace as one reviewable record.
- Provider and seeker voice prompts prohibit promises about eligibility, availability,
  appointments, or outcomes.

The versioned evaluation corpus continuously asserts disclosure, permanent opt-out, removal review,
partial-evidence review, help-seeker safety stop, and zero false application across its adversarial
policy cases.
