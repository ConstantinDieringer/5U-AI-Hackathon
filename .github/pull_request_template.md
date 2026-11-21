## Was und warum

<!-- Runde/Fehler-Log-Eintrag, Ursache, Fix in 2–3 Sätzen -->

## Checkliste

- [ ] Eigener Branch, kein direkter Push auf `main`
- [ ] Niemand sonst arbeitet am selben Fall (in #update angekündigt)
- [ ] `python -m pytest -q` lokal grün, CI grün
- [ ] Keine Secrets im Diff (Keys, Passwörter, Webhook-URLs)
- [ ] Neue/geänderte Flags mit Default in der PR genannt

## Merge (nur Push-Verantwortliche/r)

- [ ] Deploy-Ampel grün (`GET /api/deploy-gate`)
- [ ] Nach dem Merge: `python -m app.deploy_check` meldet `LIVE`
- [ ] Erst dann in #update "live" schreiben, mit Commit-Hash
