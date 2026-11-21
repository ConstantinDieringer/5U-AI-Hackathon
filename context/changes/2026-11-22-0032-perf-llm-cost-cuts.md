LLM cost cuts (PR #69): extraction retry and thread rescue run on `gpt-5-mini` (were `gpt-5`), rescue budget 2 calls with a bounded prompt (`THREAD_RESCUE_MAX_MAILS` 8, `THREAD_RESCUE_BODY_CHARS` 2500), composer repair off by default (`COMPOSE_REPAIR`).
Pending decision: Railway must not still set `LLM_RETRY_MODEL`/`LLM_RESCUE_MODEL=gpt-5` or `THREAD_RESCUE_MAX_CALLS=6`.
Not verified: live.
