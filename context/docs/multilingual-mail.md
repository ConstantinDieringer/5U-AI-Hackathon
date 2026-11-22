# Multilingual customer mail

Customer languages are no longer reduced to German or English. `mail/language.py` combines
the established function-word checks with a local Lingua detector. Ambiguous acknowledgements
inherit the conversation language; the latest meaningful customer reply can change it.
Model language tags accept BCP 47 codes without a fixed language allowlist. An unknown local
language can be identified by the gateway. Names, companies and quoted history do not select
the reply language.

`mail/multilingual.py` provides the shared language boundary for RFQs, clarification replies,
booking, thread rescue, tracking, follow-ups and emergency drafts. English and German retain
their existing rule paths. Other messages get an English working copy before extraction and
deterministic checks. Original subjects, bodies and raw messages remain in the CRM. Readable
foreign attachment texts are translated together in one call; page images still reach the
existing extraction model. Local-only replay reads cached translations and never makes a
gateway call.

The current audio recovery chain (Groq, allowed gateway audio, offline Whisper) is retained.
The offline fallback uses multilingual Whisper `base` with automatic language detection.
Docker bakes that model into the image. Legacy `.en` speech/local model settings are normalized
to multilingual variants; deployment must rebuild the image for the matching model.

Incoming translations retain negation, conditions, revisions, deferrals, units and dangerous
goods declarations. A translated booking approval also needs explicit unconditional consent
and an exact excerpt from the current original message. It still passes the existing booking
rules. A model's translation of a conditional statement to "book" cannot authorize booking.
Known carrier names, numeric values, references, airport codes and currencies are protected
with markers. Calculations and conversions stay in the existing deterministic pricing code.

`hackloop.send_customer` selects the recipient's language from the incoming thread, or accepts
an explicit language from an agent. Its shared translation boundary covers subject and body,
including fixed templates and deterministic fallbacks. The common customer writing profile
also governs these translations. Calendar dates use Babel locale formats; prices keep the
existing EUR and number presentation. Names, references, dates and the team signature remain
protected. `review=False` skips the style reviewer, but does not skip required translation.
Existing verified French, Spanish, Italian, Dutch, Polish and Turkish quote templates are retained; already native
output needs no additional translation call. The mandatory tracking invitation uses its native
template or a cached checked translation, which the final gate and recovery worker recognize.
The reviewer itself and its enable setting are unchanged. Human-authored manual replies use
the language the operator wrote; emergency draft previews are localized.

Translations use `llm.complete`, the configured compose model and the existing durable call
budget. Cache keys bind source content, protected values, language, session and version.
Typical foreign RFQs need one incoming normalization and one outgoing translation in addition
to extraction/composition; foreign readable documents add one batch call. This costs tokens
and time. The first tracking invitation for a language without a fixed template can add one
translation, cached across sessions separately for conditional and booked wording. No
additional production instance, subscription or credential is required. The older standalone
`reply_language` helper remains for compatibility; transport uses only the common boundary.

There is no promise of perfect translation for every human language. Actual coverage depends
on the gateway model and local detection confidence. Unsupported or unverified translations,
budget exhaustion and malformed output create `language_error`/attachment validation events;
they never silently send an English fallback. Existing pause, manual takeover, quarantine,
deadline and send-deduplication checks still apply after translation. Input bounds are 32,000
characters of current message content and 64,000 per attachment-text batch. Native units are translated as
units; they are never converted by the translator.

The essential wire tests in `tests/test_quote_languages.py` exercise language preservation with a stubbed gateway and transport. The cases cover French and Polish quote messages.

Library references: [Lingua](https://github.com/pemistahl/lingua-py),
[Babel date formatting](https://babel.pocoo.org/en/latest/api/dates.html).
