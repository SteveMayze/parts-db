# ADR 0001: Run behind the shared gateway under a URL prefix

Status: accepted

## Context
A separate repository (`cornil-gateway`) now owns the Caddy reverse proxy so that several Django apps can share one host. It forwards `/parts-db/...` to this app and strips the prefix. A Caddy playbook in this repo once overwrote the shared Caddyfile.

## Decision
- This repo contains no Caddy code; the old files live in git history only.
- The prefix is set by `DJANGO_URL_PREFIX` (becomes `FORCE_SCRIPT_NAME`); empty by default, so nothing changes without it.
- Cookies are named `partsdb_sessionid` / `partsdb_csrftoken` with the prefix as path so apps do not log each other out. Renaming logs admin users out once.
- A small middleware accepts the prefix on direct access, so the old published port keeps working with prefixed links.
- `listen_address` (default: all interfaces) controls the published port. Loopback-only is not possible until the app is rootless and joins the gateway network.
- The secret key moves from the world-readable unit file to a root-only environment file. The key itself is not rotated here; rotation logs everyone out and needs the owner's decision.
- Named URLs (`admin:login`, `parts:list`) are used for `LOGIN_URL` and redirects so they carry the prefix.

## Consequences
Hosts, origins and the prefix are deployment variables. Moving to rootless Podman is a separate later step.
