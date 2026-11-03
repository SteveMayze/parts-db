# Parts-DB working rules
- Never print or commit secrets, real host names, addresses or usernames. Deployment values live in ignored files (`infra/group_vars/all.yml`, `infra/inventory*.ini`, `docs/prompts/`).
- This repo has no Caddy code; the proxy belongs to `cornil-gateway`. Do not touch that repo or the tax app.
- Work one phase at a time, stop with an end-of-phase report, explain concepts in plain language.
- Add an ADR in `docs/adr/` after each decision.
- Tests: `.venv/bin/python manage.py test` (dev deps in `requirements-dev.txt`, needs reportlab).
- Prefix support: `DJANGO_URL_PREFIX`, `DJANGO_BEHIND_TLS_PROXY`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_ALLOWED_HOSTS` (see README).
