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
