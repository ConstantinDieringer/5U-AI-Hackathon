# Significant changes

Only major capabilities or behavior changes belong here. Combine related work, newest first, at most eight short entries. Small fixes, test results and detailed history belong in Git and PRs.

## 22 November 2026

* Project context and significant changes now each have one short document.
* Customer replies support versioned offers and explicit approval of revised terms. Execution remains rollout controlled, with shadow evaluation as the default.
* Multilingual requests, attachments and replies use checked translations while preserving shipment facts and consent evidence.
* Arrival constraints and cargo insurance are validated before all quote paths so incomplete or unsuitable offers are held for clarification.
* Tracking invitations are enforced on customer sends, with a guarded recovery send when a notice is missing.

## 21 November 2026

* Blocked requests search shipment history first, then ask the customer and resume from their answers.
* Customer emails use shared personal wording and strict formatting rules. The additional LLM style reviewer is disabled.
* The dashboard shows per RFQ model usage and estimated API cost.
