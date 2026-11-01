# Project Tracking (Testing Goat style)

This file tracks small, test-first increments inspired by *Obey the Testing Goat*:
- functional behavior first (outside-in)
- then minimal code changes
- then refactor with tests green

## Current testing baseline

- [x] Basic model behavior tests (`Part.full_location`)
- [x] Search/list behavior tests (free-text + filters)
- [x] CRUD view tests (create/edit/delete)
- [x] Sortable parts-list headers with safe query-param sorting, active-column indicator, and natural/Unicode-aware ordering, including the `Wert` column
- [x] Deployment hardening for the stale Caddy Cloudsmith apt source issue
- [x] Django test command wired and passing

## Recently completed

- [x] Added a reusable, whitelist-based sort helper to the parts list view so sorting is safe and bookmarkable, including the `Wert` column
- [x] Kept search text, filters and pagination state while changing sort direction or column
- [x] Added regression tests for umlauts, natural numbers, empty values, invalid sort inputs, and active-header behavior

## Gateway prefix phase (completed 2026-10-19)

- [x] Proxy moved to the separate `cornil-gateway` repo; all Caddy code removed from this repo (see `docs/adr/0001-run-behind-gateway-under-url-prefix.md`)
- [x] App runs under a URL prefix (`DJANGO_URL_PREFIX`), with TLS-proxy header, configurable hosts and CSRF origins, own cookie names scoped to the prefix, prefix-aware static URL
- [x] Prefix tests (real test client) added; all tests pass with the prefix empty and set
- [x] Quadlet deploy: `listen_address` variable (default all interfaces), secret key moved to a root-only environment file
- [x] Container error logging; `feature/part-subtype` (Untertyp) merged into `main`
- [x] Verified through the gateway and on the direct port; gateway `make smoke` passes
- [ ] Not done: rotate the secret key (owner decision, deferred)
- [~] Rootless / join the gateway network / close the port: see the phase below

## Rootless Podman phase (version 2 plan, `docs/prompts/PARTSDB-ROOTLESS-2.md`)

Decision: no data migration. The PDF folder was never mounted in the rootful container, so PDF originals (all 38 documents) are already lost; the owner reloads data through the CSV and PDF imports. See `docs/adr/0002-rootless-podman-under-homeapps.md`.

### R1 prepared and rehearsed (2026-11-01; nothing deployed)
- [x] App: `DJANGO_DB_PATH`, `DJANGO_MEDIA_ROOT`, `DJANGO_BACKUP_DIR`, `DJANGO_HTTPS`, `DJANGO_HSTS_SECONDS`; `/health/`; `DJANGO_BEHIND_TLS_PROXY=0` now means off
- [x] Dockerfile: non-root user 10001, `collectstatic` at build, `healthcheck.py`; `.containerignore`
- [x] `parts/backup.py` + `backup_db` / `restore_backup` commands (database via SQLite online backup + whole media tree, manifest, verification, retention)
- [x] `infra/`: deploy, status, backup, fetch-backup, restore-drill, restore, rollback playbooks, Quadlet and backup timer templates, `partsdb_rootless.yml`; no sudo/become; Makefile targets
- [x] Tests: upload lands in media, backups, health, settings, sudo-free, hardening, mounts, isolation from Tax names, no Caddy code
- [x] Docs: ADR 0002, `docs/deployment.md`, `docs/podman-explained.md`, README
- [x] R2: first rootless deploy, persistence proof (redeploy same/changed image, restart), backup / fetch / restore-drill, gateway serves the rootless container
- [x] R2: owner stopped the rootful unit and renamed its unit file; reboot test with nobody logged in passed (container came back healthy, data intact, gateway 200)
- [ ] R3: loopback-only already default; close port 8888, `DJANGO_HTTPS=1`, drop the CSRF origin for `:8888`, remove the rootful and Docker playbooks and Makefile targets

## Next increments

### Phase 1 — Core user behavior (now)
- [ ] Add one end-to-end functional test for "find part by text and location"
- [ ] Add one end-to-end functional test for "add part then immediately find it"

### Phase 2 — Safer forms and validation
- [ ] Add form validation tests (required fields, quantity bounds)
- [ ] Add regression tests for invalid form submissions keeping user input visible

### Phase 3 — Search quality improvements
- [ ] Add tests for combined query + filters precedence
- [ ] Add tests for case-insensitive matching expectations
- [ ] Decide and test ordering preferences for results

### Phase 4 — Refactoring with confidence
- [ ] Split tests into focused files/packages as suite grows
- [ ] Add helper/factory functions to reduce duplication in test fixtures

### Phase 5 — Optional advanced topics
- [ ] Add browser-driven functional tests (Selenium/Playwright) if desired
- [ ] Add CI test run

## Mapping to the Testing Goat TOC

Initial focus aligns with early chapters: functional tests + unit tests + refactoring cycle, then gradual expansion toward form handling and test organization.
