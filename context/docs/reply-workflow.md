# Versioned customer reply workflow

## Entry and ownership

`REPLY_WORKFLOW_MODE` is `shadow` by default. `off` preserves the legacy router. In `enforce`,
customer replies enter a durable queue instead of starting independent booking/clarification
threads. Initial RFQ extraction and pre-quote clarification retain their existing owners.
The post-offer workflow has one renewable 90 s lease per thread, renewed every 20 s,
and a bounded pool of `REPLY_WORKERS` (default 4, maximum 16). Database transactions never span
network calls. Different threads run concurrently; one thread processes mail IDs in order.
A duplicate delivery cannot reset workflow state or replay an action.

`reply_intent.ReplyDecision` carries corrections, questions, current consent, typed conditions
and unresolved issues together. Cheap complete rule paths run first. Complex replies use one
12 s gateway reading, cached per incoming mail; existing translation/composition may have their
own calls. All gateway calls retain the existing session budgets and cost accounting. Shadow
readings also spend that budget, so first review offline and keep shadow observation bounded.
Evidence must occur in the current message or a named current attachment. Older customer messages
provide context only; older consent cannot authorize a booking. Numeric corrections, dates,
locations and price limits are independently checked against their evidence.

## Stored contracts

- `request_revisions`: immutable request JSON, version, source mail and correction evidence.
- `thread_workflow`: current version, active offer, last processed input, phase, open issues and
  version-bound approval. This is exposed additively as `workflow` on session detail.
- Quotes gain `request_version`, `supersedes_id`, frozen request and frozen pricing audit.
  The API exposes `request_snapshot` and `pricing_snapshot`; `quotes` remains oldest first.
- Bookings gain `quote_id`. Tracking and settlement use that quote. Legacy unlinked bookings
  keep their previous behavior. Scoring and score aggregates keep using the first quote.
- `reply_decisions` stores the validated reading. `workflow_actions` stores queued replies and
  unique external effects, keyed by thread, incoming mail, action kind and request version.

New columns and tables are additive and startup migration is repeatable. Existing offer rows
without a reconstructable frozen request are not auto-authorized. They require manual review.
Starting the new worker never scans historical mail for approvals to execute.

## Changes, answers and consent

Unspecified request fields are inherited. Explicit new values are validated, applied as a new
version and immediately invalidate the old offer. Full pricing runs again, including trucking.
Only confirmed transport of the replacement activates it. A correction and approval in one mail
still require fresh approval after the changed total is shown. The original six-minute scoring
deadline is not reset. Revisions target 120 s and have the existing RFQ 360 s ceiling from the
correction message; late or incomplete work is surfaced for review.

Current carrier inquiries are separated from old ones by `[RV-n]` and reference correlation.
Late or uncorrelated old bids cannot enter the revised price, including after asynchronous
attachment/FX reads. Current factual questions reuse the active offer snapshot. A requested copy
of that offer does not recalculate, create another quote or reset scoring.

Natural approval wording is accepted without a required keyword. It must target the current,
sent, unchanged offer. Conditions support cheapest/fastest among verified comparable options,
all-in price caps, included pickup and explicitly confirmed schedule dates. Planned/earliest
dates are not guarantees. Unsupported or unproven conditions stop booking and are surfaced;
prices and conditions are never decided by a model. A fulfilled conditional approval books once
and the confirmation explains that its conditions were checked.

New input invalidates a prepared decision before transport. Unresolved questions, changes,
conditions, deferrals, review, pause and manual control suppress booking reminders. Business
answers do not spend the reminder cap. Repeated identical unresolved issues stop repeated
review notices. The dashboard and exported context show the version, phase and open items;
review items also appear in the attention list.

## External effects and restart behavior

A business guard fails closed immediately before transport. Reservation of an external effect
also fails closed. Once transport may have started, a restart/timeout cannot cause automatic
resending or rebooking. Such work is shown as unknown and requires inspection. A worker whose
lease was replaced fails its next transport guard. Safe work interrupted before transport resumes
with the cached reading and existing revision. Other threads continue normally.

## Rollout and rollback

1. Run the reply regression suite, RFQ/reply replays and full tests with a test-only API key.
2. Deploy in a green deploy window, one production instance, initially `shadow`. Verify build
   revision and `reply_workflow_mode` in `/api/health`. Shadow performs no new sends/bookings.
3. Inspect `reply_shadow`, cost logs, `reply_decision`, `request_revised`, `stale_bid`,
   `reply_needs_review`, `reply_unknown` and the attention list before selecting `enforce`.
4. Track correction-to-offer time, unanswered customer questions, duplicate external effects,
   suppressed reminders, review counts and gateway spend. The 100-thread test is a mocked
   concurrency check, not a production throughput guarantee.
5. For rollback, pause/take over any open versioned threads, then select `shadow` or `off`.
   Pending enforce jobs are not executed in shadow mode. Retain new tables and snapshots;
   never restore an invalidated older offer or replay an uncertain external action.

No live mail, portal booking, deployment or production migration is part of the automated tests.
