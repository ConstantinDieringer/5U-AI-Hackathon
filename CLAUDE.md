# 5U AI Hackathon: Team Austrian Power

A digital air freight forwarder for the 5U AI challenge (START HACK Munich 2026).

**Technical spec (binding):** [context/docs/TECH_SPEC.md](context/docs/TECH_SPEC.md). Read and follow it before writing any code.

## Before every task

Read all six files in `context/`:

1. `context/project-overview.md`: what we build, what 5U provides, rules, scoring
2. `context/code-standards.md`: implementation rules
3. `context/ui-context.md`: design principles and screens
4. `context/ai-workflow-rules.md`: workflow, scoping, verification
5. `context/progress-tracker.md`: current state, next steps, open questions
6. `context/architecture-context.md`: stack, boundaries, session model (links to `context/docs/TECH_SPEC.md` and `context/docs/design/`)

Follow `context/ai-workflow-rules.md`.

For customer email wording or composer changes, use `.agents/skills/customer-email-voice/SKILL.md` and its shared runtime profile `app/mail/customer_voice.md`.

Update `context/progress-tracker.md` after each meaningful change. If an implementation changes scope, standards or architecture, update the relevant context file before continuing.

Challenge sources: [context/docs/CHALLENGE.md](context/docs/CHALLENGE.md) and the PDFs in `context/docs/`.
