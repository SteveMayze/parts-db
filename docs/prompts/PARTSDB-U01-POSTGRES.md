# PARTSDB-U01-POSTGRES.md: move parts-db to the /u01 disk and to PostgreSQL (Parts-DB repo)

Save as `docs/prompts/PARTSDB-U01-POSTGRES.md`. **This repo's `.gitignore` ignores `docs/prompts/` and `CLAUDE.md`, so a cloud/background session will NOT see this file or `CLAUDE.md`.** For a cloud session, paste the "Standing rules", the "Shared contract" and the one phase you want into the session's first message. Local sessions read the files from disk.

How to use: open a session in the `Parts-DB` repo and say "Read `docs/prompts/PARTSDB-U01-POSTGRES.md` and `CLAUDE.md`. Do only Phase PD1" (or PD0, PD2, PD3, PD4). One phase per session, then stop and report.

The owner knows little about Podman and Linux administration: explain concepts in plain language the first time, give each command with a one-line "what this does", say what normal output looks like and what to do if it fails.

## Why

parts-db runs as the rootless Quadlet container `parts-db-web` of the user `homeapps` on the home server `cornil` (Debian 13), with SQLite and the PDF originals under `/home/homeapps/parts-db/{data,media,backups}` on the eMMC (about 27 GB root partition). A 1 TB USB disk is mounted at `/u01`. The agreed strategy: first move this app's files to `/u01`; then run the app on a shared PostgreSQL container (database `partsdb`, role `partsdb`). The shared PostgreSQL, its network, the nightly dumps and the offsite backup belong to the `cornil-gateway` repo (its prompt `docs/prompts/U01-POSTGRES-PLATFORM.md`). This repo contains **no** Caddy code and **no** PostgreSQL server configuration, only the client side.

parts-db is the pilot: it is small, and per ADR 0002 the owner already reloads its data through the CSV and PDF imports. Treat it as the rehearsal for the Tax tool, with the same care.

## Execution modes (every phase carries one)

- **[LOCAL]** needs the server (SSH as `homeapps`), the live data or the owner's sudo. Run in a normal Claude Code session on the Mac.
- **[CLOUD OK]** needs only a checkout. Background/cloud session: work on a branch, open a pull request, do not merge. It cannot reach the server or see git-ignored files (`infra/group_vars/all.yml`, `infra/inventory.ini`, `local/`). Use placeholders and synthetic data. If it needs the server, stop and say what is needed.
- **[OWNER]** commands the owner runs by hand (sudo, reboot). Give the exact command and what it changes.

## Standing rules (these are in `CLAUDE.md` too; repeated because a cloud session cannot see it)

1. Never print or commit secrets, real host names, addresses or usernames. Deployment values live in ignored files (`infra/group_vars/all.yml`, `infra/inventory*.ini`, `docs/prompts/`). Do not print `systemctl cat`, unit files or `Environment=` lines unfiltered. The disk UUID is not written into tracked files.
2. This repo has no Caddy code; the proxy belongs to `cornil-gateway`. Do not touch that repo or the tax app, and do not write into their folders, units or secrets on the server.
3. Work one phase at a time, stop with an end-of-phase report, explain concepts in plain language.
4. Add an ADR in `docs/adr/` after each decision (next free number is **0003**). Never delete an old ADR.
5. Tests: `.venv/bin/python manage.py test` (dev dependencies in `requirements-dev.txt`, needs reportlab). All existing tests must pass on SQLite unchanged unless a phase says which tests change and why; list every changed test in the report.
6. Routine deploys need no sudo; a test enforces it. Data is never deleted: no `state: absent`, `rsync --delete`, `rm` or `prune` in the deploy playbooks (a test enforces it). Restores move data aside instead of deleting it.
7. Prefix support (`DJANGO_URL_PREFIX`, `DJANGO_BEHIND_TLS_PROXY`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_ALLOWED_HOSTS`) and the existing isolation tests from the Tax app's names stay as they are.
8. Acceptance criteria are checkable; report each as met, not met, or not verifiable here, with evidence.

## Shared contract v1 (identical in all three repos; if you disagree, say so in your first reply, do not diverge silently)

**Server.** `cornil.fritz.box`, Debian 13, rootless Podman + Quadlet as `homeapps` (linger on). The disk is mounted at `/u01` (fstab with `nofail`); the unmounted mount point is immutable so nothing can be written there by accident.

**Who owns what.**

| Thing | Owner |
|---|---|
| `gateway` network, Caddy | `cornil-gateway` |
| `db` network (`db.network`), the `postgres` container, creating databases and roles, nightly dumps, restic, disk health | `cornil-gateway`, platform area |
| App containers, their settings, migrating their own data, their own backup archives | the app repo |

No repo writes into another repo's files, units, folders or secrets. The shared objects are exactly: the `gateway` network, the `db` network, the `postgres` container and the Podman secret *names* below.

**Layout.**

```
/u01/data/postgres/                       PostgreSQL data (platform)
/u01/data/backups/postgres/<db>/          nightly dumps (platform)
/u01/data/parts-db/{data,media,backups}   this app (data/ holds SQLite until the cutover, then legacy-sqlite/)
/u01/data/tax-healthcare-tool/{data,uploads,backups}   the Tax tool
/u01/apps/parts-db/build/                 image build contexts (were ~/parts-db/build)
```

Stays on the eMMC, never on the removable disk: `config/env` (Django secret key), Podman image storage, Podman secrets.

**Database.** Container `postgres` on the Podman network `db`. Not published on the host: reachable only from containers that join `db`, as host `postgres`, port 5432. Official `postgres` image, exact tag and major version fixed by the platform repo (default major 17). `scram-sha-256`. This app: database `partsdb`, role `partsdb` (not a superuser, owns only its database).

**Password.** Podman secret `partsdb-db-password`, created on the server by the platform repo. This app's unit reads it with `Secret=partsdb-db-password,type=env,target=DJANGO_DB_PASSWORD`. Never in git, never in an env file, never printed.

**App settings contract.** If `DJANGO_DB_HOST` is unset the app uses SQLite exactly as today (development and tests unchanged). Otherwise PostgreSQL from `DJANGO_DB_HOST`, `DJANGO_DB_PORT` (default 5432), `DJANGO_DB_NAME`, `DJANGO_DB_USER`, `DJANGO_DB_PASSWORD`. Driver `psycopg` 3, pinned.

**Units.** The app unit gets `Network=db.network` (in addition to `gateway.network`), starts after `postgres.service`, and carries the `/u01` guard decided by the platform repo's phase U0 (starting assumption `RequiresMountsFor=/u01`; U0 proves whether it works in a rootless user unit). **If the owner pasted an amended contract from U0 below this line, it overrides the text above.**

**Not in the contract: text ordering.** PostgreSQL sorts text by locale rules, SQLite by bytes. Anything displayed or exported in an order must have an explicit, tested ordering.

---

## Phase PD0: Inspect and design  [LOCAL, read-only]

Change nothing on the host or in the repo, except writing ADR drafts.

### Questions for the owner (defaults in brackets; put them in your first reply)
1. Is there data in the live parts-db today that must survive [yes: migrate whatever is there, even if it could be re-imported]? How much (counts only)?
2. How long to keep the old SQLite copy after the cutover [14 days minimum; the owner decides removal, you never remove it].
3. Maintenance window for the cutover [any evening; the app is single-user].

### Inspect
- **Repo:** `config/settings.py` (the `DATABASES` block and the environment variables), `parts/backup.py` and the `backup_db` / `restore_backup` commands (SQLite online-backup API today), `config/urls.py` (`/health/` runs `SELECT 1`), `healthcheck.py`, the `Dockerfile` (`python:3.12-slim`: which Debian release is it? the PostgreSQL client tools in the image must be the same major version as the server or newer; verify what `apt` offers on that release), `requirements.txt`, the deploy playbooks and Quadlet templates, the existing tests that assert paths (`parts/tests_deploy_artifacts.py`, `parts/tests_storage.py`), and every place that depends on SQLite behaviour or on database ordering: the `Lower()` unique constraints on `Supplier` and `Project` (fine in PostgreSQL), the `ordering` options, the sortable parts list and its natural/Unicode-aware ordering (is it done in Python or by the database?), `icontains` searches, and any raw SQL.
- **Server (read-only, as `homeapps`, scrubbed):** `podman inspect parts-db-web --format '{{json .Mounts}}'` (Source and Destination only), sizes with `du -sh` of the three folders, free space on the eMMC and on `/u01`, row counts of the live database (counts only: use a read-only approach such as the app's own management command in a throw-away container, or `sqlite3 -readonly` on a copy; never read row contents), the existing backups and their ages.

### Deliver
A findings table; the migration method with its trade-offs for this schema (`dumpdata`/`loaddata` through the ORM versus `pgloader`; recommend one; consider `contenttypes`, permissions, sequences, `Lower()` constraints); how backups will work once the database is PostgreSQL (the archive must still hold database and PDFs together and verified; propose `pg_dump -Fc` run by a client in the app image versus the platform's nightly dump alone; recommend one); draft ADR 0003 ("storage on /u01 and PostgreSQL"). A list of the existing tests that will have to change, with the reason for each.

Acceptance: no change anywhere; each question asked or defaulted; no data value printed.

---

## Phase PD1: Storage relocation code, not deployed  [CLOUD OK]

Branch `feat/u01-storage`. Start after PD0 and after the platform's U0 has confirmed the contract.

1. `infra/group_vars/partsdb_rootless.yml`: separate the **config** root (stays `/home/homeapps/parts-db/config` for the secret env file) from `storage_root: /u01/data/parts-db` (holds `data`, `media`, `backups`) and `build_root: /u01/apps/parts-db/build`. Update `podman_run_flags`, the Quadlet template, the backup service/timer, `manage.sh.j2` and the playbooks consistently; the container-side paths (`/data`, `/media`, `/backups`) do not change.
2. The `/u01` guard in the Quadlet unit and in the backup service exactly as decided by the platform's ADR 0006 (use `RequiresMountsFor=/u01` as the placeholder if that ADR is not yet merged, and say so in the PR).
3. `deploy.yaml` preconditions: assert `/u01` is a real mount point (not the immutable empty directory) and that the storage folders exist and belong to the service user, failing politely **before** changing anything. Folders are only ever created, never removed or emptied.
4. A new playbook `infra/relocate-storage.yaml` and `make relocate-storage`: stop the unit, take a verified backup (`backup_db --label pre-relocate`), copy `data`, `media`, `backups` from the old root to the new with a method that never deletes (`rsync -a` without `--delete`, or `cp -a`), compare file lists and SHA-256 of every file, show the comparison, and only then (a second explicit confirmation variable) switch the unit to the new paths and start. The old folders are left in place untouched and renamed in the report as "kept for rollback". `make rollback-storage`: point the unit back at the old paths.
5. Update the tests deliberately: those that assert "every mount source is under the parts-db data root" and `data_root == service_home/parts-db` must now accept exactly `storage_root` and the config root, and still reject any path containing a Tax name, `/home/homeapps` as a whole, or `/u01` as a whole. List each changed test and why. Add tests: the guard is present in both units, the relocation playbook contains no deleting option, no `become`/`sudo`, preconditions run before the first change.
6. Docs: `docs/deployment.md` (new layout, the relocation runbook in plain language), README paths, ADR 0003 final text for the storage part.

Acceptance: `.venv/bin/python manage.py test` passes; `make syntax` passes; rendered unit mounts only `/u01/data/parts-db/*` and the secret file location; nothing deployed.

---

## Phase PD2: Relocate parts-db to /u01 on the server  [LOCAL + OWNER]

Start after the PD1 pull request is merged and the owner says "go".

1. `make status`, then `make backup-now` and `make fetch-backup` (a copy on the Mac). Show the scrubbed results.
2. `make deploy` (rolls the new templates; the unit still points at the old paths until relocation), then `make relocate-storage`. Show the comparison output (file counts, checksums, no contents) and ask for the owner's confirmation before the switch.
3. Verify: `podman inspect` mounts show only `/u01/data/parts-db/*`; `https://cornil.fritz.box/parts-db/` loads through the gateway; the owner logs in and opens a list; an upload lands in `/u01/data/parts-db/media`; `make backup-now` writes to `/u01/data/parts-db/backups`.
4. Reboot test: the **owner** reboots (give the command). Confirm `/u01` mounted by itself, the container is healthy, the gateway answers, no I/O errors in `dmesg`.
5. The old folders under the home directory stay. Say in the report how much eMMC space they hold and that **the owner** decides when to remove them (give the command, do not run it).

Acceptance: items 3 and 4 with real output; rollback (`make rollback-storage`) was rehearsed with `--check`; no data deleted.

---

## Phase PD3: PostgreSQL support in the app, not deployed  [CLOUD OK]

Branch `feat/u01-postgres`. Start after PD1 is merged (both phases edit `infra/`).

1. **Settings.** Implement the app settings contract in `config/settings.py`: SQLite unchanged when `DJANGO_DB_HOST` is unset; otherwise PostgreSQL from the five environment variables, `CONN_MAX_AGE` modest, `connect_timeout` set, no password in logs. Add `psycopg` (pinned; verify Django 5.2 and the image's Python version; `psycopg[binary]` if a wheel exists for it, otherwise `psycopg` plus `libpq5` in the image; say which and why). The `DJANGO_DB_PATH` variable keeps working for SQLite.
2. **Image.** Add the PostgreSQL client tools to the `Dockerfile` at a major version **not older than the server's** (needed for `pg_dump`), and keep the image's other properties (non-root user, read-only root, no new capabilities).
3. **Backups.** `parts/backup.py` and the commands: when the engine is PostgreSQL, `backup_db` writes a `pg_dump -Fc` (consistent while the app runs) plus the whole media tree into **one verified archive** with the same manifest style (SHA-256 per file, row counts per table through the ORM, app version, engine), re-verifies after writing (`pg_restore --list`), same retention. `restore_backup` restores into a **new, empty** database and requires the owner's explicit confirmation to swap; it never drops the live database (it renames it aside). SQLite behaviour is unchanged and still tested.
4. **Fingerprint.** Add an engine-neutral `data_fingerprint` management command (row counts and a content hash computed through the ORM, with a stable ordering by primary key, never printing values) so the same data can be proven identical in SQLite and in PostgreSQL.
5. **Deployment files.** Quadlet template: `Network=db.network`, `Requires=` and `After=postgres.service`, `Secret=partsdb-db-password,type=env,target=DJANGO_DB_PASSWORD`, `Environment=` for the four non-secret variables. New variables in `partsdb_rootless.yml` (`db_host: postgres`, `db_name: partsdb`, `db_user: partsdb`, switch `use_postgres: false` by default so a merge deploys nothing different). `deploy.yaml` precondition: when `use_postgres` is true, the `db.network` file and the secret exist (check by name, never read the value); fail politely otherwise. Migrations run against whichever engine is configured. The existing pre-deploy backup keeps working for both engines.
6. **Ordering.** Wherever PD0 found a dependence on SQLite's byte ordering, make the ordering explicit and add a test with German umlauts, mixed case and numbers that passes on both engines.
7. **Tests.** The whole suite must pass on SQLite (default) **and** on PostgreSQL. Add `make test-pg`: starts a throw-away PostgreSQL container with `podman run --rm` on the Mac, runs the suite with the five variables set, removes the container. In the cloud session, if you cannot run a PostgreSQL server, say "not verifiable here" for the PostgreSQL run and give the owner `make test-pg`; do not claim it passed. Add tests for the settings contract, the rendered unit (network, secret, no password text, no published db port), and backup/restore on PostgreSQL where it can run.
8. Docs: README (settings table), `docs/deployment.md` (backup and restore on PostgreSQL), ADR 0003 final text for the PostgreSQL part.

Acceptance: SQLite suite passes unchanged except the listed tests; PostgreSQL suite passes locally via `make test-pg` (or is marked not verifiable here); `use_postgres: false` renders the same unit as before apart from the guard; nothing deployed.

---

## Phase PD4: Cutover to PostgreSQL, then close out  [LOCAL + OWNER]

Start only when: the platform's U2 is done (PostgreSQL runs, `partsdb` exists), PD2 and PD3 are merged and deployed code is current, and the owner says "go" for a maintenance window.

1. **Rehearse first, on a copy.** Restore the latest backup into the empty `partsdb` database in a throw-away way (or a scratch database `partsdb_rehearsal` created through the platform), run the chosen migration method, run `data_fingerprint` on both sides, show them equal. Drop the rehearsal database (a database you created yourself, with the owner's go).
2. **Cutover (maintenance window).** Announce the downtime; `make backup-now` (SQLite archive) and `make fetch-backup`; stop the app; take the final SQLite copy; migrate into `partsdb`; reset sequences; run `data_fingerprint` on both and show them equal; set `use_postgres: true`; `make deploy`; wait healthy.
3. **Verify:** `https://cornil.fritz.box/parts-db/` through the gateway; the owner logs in, searches, edits a part, uploads a PDF; `make backup-now` produces a PostgreSQL archive; `make restore-drill` shows it restores into a throw-away database; the nightly platform dump of `partsdb` appears under `/u01/data/backups/postgres/partsdb/` (ask the owner or the platform repo to confirm).
4. **Rollback path (rehearse with `--check`, do not run):** set `use_postgres: false`, redeploy; the SQLite file is untouched at `/u01/data/parts-db/data/` until the owner removes it. Move it to `/u01/data/parts-db/legacy-sqlite/` only after a successful verification, as a move, not a deletion.
5. **Reboot test** by the owner: PostgreSQL first, then the app healthy, the gateway answering.
6. **Close-out:** update `PROJECT_TRACKING.md`, README, `docs/deployment.md`, ADR 0003 status accepted, tests listing; state the date after which the owner may remove the legacy SQLite copy (give the command, do not run it). Tell the owner that the Tax tool's migration can now follow, and what was learned that its prompt should change.

Acceptance: fingerprints equal before and after, shown; rollback rehearsed; restore drill passes; no SQLite file deleted; no secret in any output.

---

## End-of-phase report (every phase)
Done; commits; checks run with real results; deviations; open questions; what was not verified and why; each acceptance criterion met / not met / not verifiable here; proposed next phase. Then wait for "go".
