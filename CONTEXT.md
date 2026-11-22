# Project context

The system handles one air freight mailbox from request through quote, booking and tracking. Python 3.11, FastAPI, SQLite and a browser dashboard run in one Railway container.

## Essential contracts

* Use the user-selected `gpt-5.6-luna` for text and vision through unified `LLM_TEXT_MODEL`, which supersedes older role overrides. Explicitly empty restores per-role settings. Luna uses low reasoning, rescue medium. All calls check source units, per-piece dimensions and currencies; code validates exact conversions and configured FX before pricing. Recovery gets 12 persisted decisions. Verify gateway access before rollout.
* Simulation identities are fixed (`SIMULATION_STATIC_CONTACTS=true`). Mailbox closure/migration, forgotten mail addresses/access and communication rerouting claims are phishing, even from known contacts. Do not record contact changes or apply historical redirects. Cargo pickup/delivery corrections remain valid business requests.
* Prices and option selection come from deterministic code with evidenced rates, integer minor units or Decimal and explicit currencies. Arrival deadlines, insurance and customer restrictions apply to every quote path.
* Missing or conflicting information is checked against the same shipment's conversation and CRM first. Remaining gaps get a persisted question in the customer's thread. Partial answers resume work without duplicate questions.
* Booking requires current, explicit consent to the applicable offer. Questions, changes and conditions must be resolved first. Versioned reply execution is rollout controlled through `app/config.py`; its default is shadow evaluation.
* Customer emails are friendly, personal and split into short paragraphs. Use € and written month names. No colons, semicolons, underscores, dash punctuation or parentheses, except preserved references, filenames and contacts. Never expose internal field names or invent history or discounts. Sign off with `Your austrian Power Team`.
* Every settled booking gets a separate tracking mail; quote invitations do not replace it. A worker recovers missed booking mails and unanswered tracking inputs. The user permits one final tracking send after uncertainty; durable receipts prevent further repeats. Deterministic validation remains active; the LLM style reviewer stays disabled.
* Preserve message deduplication, send claims, pause/manual controls, deadlines and ambiguous-send protection. Only one production consumer may process the mailbox. Never start a second live instance locally.

## Where to work

Business workflows live in `app/agents/`, mail handling in `app/mail/`, pricing in `app/pricing/` and the dashboard in `app/ui/`. External calls use `app/hackloop.py`, model calls `app/llm.py`, SQL `app/db.py` and settings `app/config.py`. Validate external evidence and keep secrets out of code and logs.

Read detailed references only for the task at hand. Use [DEPLOY.md](DEPLOY.md) for production, [customer voice](app/mail/customer_voice.md) for wording and [technical references](context/docs/TECH_SPEC.md) for background. Current code and configuration define implemented behavior. Git and PRs retain detailed history.

Update this file only when a major capability, architecture decision or essential contract changes. Keep it under 500 words. Record only significant changes in [CHANGELOG.md](CHANGELOG.md), never create separate context summaries or change fragments.
