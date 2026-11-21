# context/changes/

One small file per PR instead of editing `context/progress-tracker.md`. Every PR adds its own
new file, so two PRs never touch the same line and context merges stop conflicting.

## Rules

- **Name:** `YYYY-MM-DD-HHMM-<branch-slug>.md` (local time when you open the PR), e.g.
  `2026-11-22-0130-fix-thread-orphans.md`. Sorting by name is sorting by time.
- **Content:** one to three lines, under 600 bytes (tested). What changed for the system, flags
  with their default, what was not verified. No test logs or replay tables; those go in the PR.
- **Optional lines:** `Open question: ...` or `Pending decision: ...`. The context owner moves
  them into the tracker.
- **Never edit or delete another PR's file.** If something is superseded, say so in your own file.
- **Reading:** an AI session reads the newest eight files (`ls context/changes | sort | tail -8`).
- **Archiving:** only the context owner moves older files to `context/history/` in one PR.

## Template

```
<what changed, one sentence>. Flag `X` default off.
Not verified: <what>.
```
