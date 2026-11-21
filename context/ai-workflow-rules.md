# AI Workflow Rules

## Approach

Build incrementally and spec-driven. Implement against the context files; do not infer or invent behavior from scratch.

## Context Loading

- Read only what the task needs: the always-read core plus the matching rows of the routing table in `CLAUDE.md`.
- Prefer `grep` over full reads for TECH_SPEC, design docs, research notes and `context/history/`.
- When code and docs disagree, the code plus `app/config.py` describe the actual state; fix the doc (or add an open question) in the same change.

## Context Hygiene (keeps every task fast)

- Always-read files have byte budgets in `tests/test_context_budget.py`; a change that exceeds them must move content out, not raise the budget.
- Progress tracker: current state only, edited by the context owner alone. Every PR records its change as one new file in `context/changes/` (`context/changes/README.md`) and never edits another PR's file.
- Area files describe current behavior, not how we got there. Replace outdated lines instead of appending; history goes to `context/history/`.
- Never paste test logs, replay tables or round-by-round narratives into context files; put them in the commit or PR.

## Scoping Rules

- Work on one feature unit or subsystem at a time; prefer small, verifiable increments.
- Split a step if it combines: mail ingestion and pricing logic; the pricing engine and LLM prompts; a background worker and the UI; more than one external integration; behavior the context files do not clearly define.
- If a change cannot be verified end to end quickly, the scope is too broad.

## Handling Missing Requirements

- Do not invent product behavior that the context files do not define.
- Ambiguous requirement: resolve it in the relevant area file before implementing.
- Missing requirement: add an `Open question:` line to your `context/changes/` file before continuing.
- Live mail workflows follow the history-first clarification policy in `context/areas/rfq.md`.

## Verification

- Verify each unit end to end against the real mailbox or portal when possible; behavior changes run the replay (`python -m app.agents.rfq_replay`).
- Note in one line what was not verified.

## Before Moving To The Next Unit

1. The current unit works end to end within its scope.
2. No rule in `code-standards.md` or the touched area file was violated.
3. The PR's `context/changes/` file and the touched area file reflect the completed work.
