# Significant changes

Only major capabilities or behavior changes belong here. Combine related work, newest first, at most eight short entries. Small fixes, test results and detailed history belong in Git and PRs.

## 22 November 2026

* Fixed simulation identities: mailbox changes, lost mail access and communication rerouting claims are quarantined before workflow actions, including from known senders. Automatic contact changes and historical redirects are disabled by default.
* GPT-5.6 Luna is the selected default. Recovery can interpret unfamiliar requests, correct the RFQ schema and remove superseded facts, review semantic arrival blocks, and retry rejected plans with tool feedback and a larger budget.
* Customer replies support versioned offers and explicit approval of revised terms. Execution remains rollout controlled, with shadow evaluation as the default.
* Multilingual requests, attachments and replies use checked translations while preserving shipment facts and consent evidence.
* Arrival constraints and cargo insurance are validated before all quote paths so incomplete or unsuitable offers are held for clarification.
* Each settled booking gets a separate tracking mail. Unanswered tracking requests recover durably, reference-only replies inherit the conversation language, and uncertain tracking delivery allows one final bounded send.

## 21 November 2026

* Blocked requests search shipment history first, then ask the customer and resume from their answers.
* Customer emails use shared personal wording and strict formatting rules. The additional LLM style reviewer is disabled.
