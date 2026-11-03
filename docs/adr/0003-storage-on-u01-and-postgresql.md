# ADR 0003: Storage on /u01 and PostgreSQL

Status: storage part (decisions 1, 2 and 7) final text written in phase PD1, not deployed yet; PostgreSQL part (decisions 3 to 6) still a draft until PD3; accepted after PD4.

## Context
parts-db runs as the rootless Quadlet container `parts-db-web` of `homeapps`, with SQLite, the PDF originals and the backups under `/home/homeapps/parts-db/{data,media,backups}` on the eMMC (27 GB root partition, 17 GB free). A 1 TB disk is mounted at `/u01`. The platform (`cornil-gateway`) runs a shared PostgreSQL 17 container with the `db` network and the secret `partsdb-db-password`. The shared contract v1 (`docs/prompts/PARTSDB-U01-POSTGRES.md`) fixes the layout, the settings and the unit lines.

Measured on 2026-11-03 (counts and sizes only): data 256 KB, media 1 MB (4 PDFs), backups 4.7 MB; live rows: 1 supplier, 1 user, 0 parts, 8 admin-log entries. The data is tiny, so the migration can be rehearsed cheaply and repeated.

## Decisions (proposed)
1. **Storage.** `data`, `media`, `backups` move to `/u01/data/parts-db`; builds to `/u01/apps/parts-db/build`. `config/env` (Django secret key) stays on the eMMC. Container-side paths do not change. Move by copy and checksum comparison, never by deletion; the old folders stay as rollback until the owner removes them.
2. **Guard.** The unit and the backup service carry `ExecStartPre=/usr/bin/mountpoint -q /u01`, `Restart=on-failure`, `RestartSec=10`, `StartLimitIntervalSec=0`. `RequiresMountsFor=` is not a guard in a user unit.
3. **Database.** PostgreSQL via `DJANGO_DB_HOST` and four more variables; SQLite stays the default when the host is unset (development, tests). Driver `psycopg` 3, pinned. The password arrives only as the Podman secret `partsdb-db-password`.
4. **Migration method.** ORM `dumpdata` / `loaddata` with natural keys, excluding `contenttypes`, `auth.permission` and `sessions` (migrate recreates them), then `sqlsequencereset`. Not `pgloader`: with this schema it adds a tool, type-cast rules and a second code path for no gain. Proof: an engine-neutral `data_fingerprint` command run on both sides.
5. **Backups on PostgreSQL.** The app's own `backup_db` keeps producing one archive holding database and PDFs together: `pg_dump -Fc` plus the media tree, SHA-256 manifest, `pg_restore --list` check, same retention. The platform's nightly dump stays as an independent second layer; it does not hold the PDFs, so it cannot replace the archive.
6. **Ordering.** PostgreSQL sorts text by locale, SQLite by bytes. The parts list sort is done in Python (`_sort_text_key`) and is engine independent; the database orderings (Meta `ordering`, dropdown `order_by`) get an explicit, tested ordering.

7. **How the storage switch is implemented (PD1).** `data_root` (where data, media and backups are *now*) is derived: `storage_root` when `use_u01` is true (the committed default), `legacy_root` (`~/parts-db`) otherwise. `config_root` (secret env file, `DEPLOYED`) never moves. A marker file `config/storage_location` on the server (`u01` or `legacy`) records the real state; `make deploy` refuses before changing anything when it disagrees with `use_u01`, when `/u01` is not a mount point, or when the storage folders are missing or not owned by `homeapps`. `make relocate-storage` stops the app, takes a `pre-relocate` backup, copies with `cp -a` into folders that must be empty, compares file lists and the SHA-256 of every file, shows the result, and switches only after the owner types `SWITCH`; on any failure the app is restarted on the old folders. `make rollback-storage` (confirmation `ROLLBACK`, safety backup first) points the unit back. Nothing is deleted: the old folders remain until the owner removes them. Builds use `/u01/apps/parts-db/build` and `TMPDIR` on `/u01`. The guard lines appear only in the `/u01` layout; the legacy layout renders the old unit.

## Consequences
- Tests asserting the old mount paths and `data_root == service_home/parts-db` change deliberately in PD1.
- The image needs PostgreSQL client tools of major version 17 or newer.
- `icontains`/`iexact` become locale-aware on PostgreSQL (umlauts match case-insensitively); SQLite only folds ASCII. This is an improvement, but is covered by a test.
