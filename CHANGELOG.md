# Significant changes

Only major capabilities or behavior changes belong here. Combine related work, newest first, at most eight short entries. Small fixes, test results and detailed history belong in Git and PRs.

## 22 November 2026

* GPT-5.6 Sol handles text and vision, with source-unit and currency checks. Exact conversions prevent cubic feet being priced as cubic metres. Recovery interprets unfamiliar requests, repairs evidenced facts and retries rejected plans with tool feedback.
* Fixed simulation identities: mailbox changes, lost mail access and communication rerouting claims are quarantined before workflow actions, including from known senders. Automatic contact changes and historical redirects are disabled by default.
* Customer replies support versioned offers and explicit approval of revised terms. Execution remains rollout controlled, with shadow evaluation as the default.
* Multilingual requests, attachments and replies use checked translations while preserving shipment facts and consent evidence.
* Arrival constraints and cargo insurance are validated before all quote paths so incomplete or unsuitable offers are held for clarification.
* Each settled booking gets a separate tracking mail. Unanswered tracking requests recover durably, reference-only replies inherit the conversation language, and uncertain tracking delivery allows one final bounded send.

## 21 November 2026

* Blocked requests search shipment history first, then ask the customer and resume from their answers.
* Customer emails use shared personal wording and strict formatting rules. The additional LLM style reviewer is disabled.
