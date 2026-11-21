# AI Workflow Rules

## Approach

Build incrementally and spec-driven. The context files say what to build, how to build it, and where progress stands. Implement against them, and do not infer or invent behavior from scratch.

## Context Files

- These files are read on every prompt, so keep each one short. Record an outcome in one or two lines, never as a test log.
- Until `architecture-context.md` exists, do not pick a stack, a framework, a library or a folder structure on your own: ask.

## Scoping Rules

- Work on one feature unit or subsystem at a time.
- Prefer small, verifiable increments over large speculative changes.
- Do not combine unrelated system boundaries in one step.

## When To Split Work

Split a step if it combines:

- mail ingestion and pricing logic
- the pricing engine and LLM prompts
- a background worker and the UI
- more than one external integration (mailbox, portal, gateway)
- behavior that the context files do not clearly define

If a change cannot be verified end to end quickly, the scope is too broad, so split it.

## Handling Missing Requirements

- Do not invent product behavior that the context files do not define.
- If a requirement is ambiguous, resolve it in the relevant context file before implementing.
- If a requirement is missing, add it as an open question in `progress-tracker.md` before continuing.

## Verification

- Verify each unit end to end against the real mailbox or portal when possible.
- Note in one line what was not verified.

## When Quotes Start Failing

The setup changes about once an hour, without notice. When quotes fail:

1. Check our logs and the leaderboard first (`GET /v1/board/me` gives the verdict and reason code for each request).
2. Find what changed.
3. Fix the root cause.
4. Note the change in the Session Notes of `progress-tracker.md`.

## Keeping Docs In Sync

Update the relevant context file whenever the implementation changes any of these:

- system architecture or boundaries
- storage decisions
- code conventions or standards
- feature scope

The progress tracker must show the actual state of the implementation, not the intended state.

## Before Moving To The Next Unit

1. The current unit works end to end within its scope.
2. No rule in `code-standards.md` or `project-overview.md` was violated.
3. `progress-tracker.md` reflects the completed work.
