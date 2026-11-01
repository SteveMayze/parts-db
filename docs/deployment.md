# Deployment: rootless Podman under `homeapps`

For concepts in plain language read `docs/podman-explained.md` first. The design decision is `docs/adr/0002-rootless-podman-under-homeapps.md`. **Nothing described here has run on the real server yet** (phase R1 prepared and rehearsed it); see "What is and is not verified" at the end.

## Design in one page
- Server `cornil.fritz.box`, Linux user `homeapps` (key-only SSH, linger on). The Caddy gateway (`cornil-gateway` repository) serves `https://cornil.fritz.box/parts-db/` and strips the prefix.
- The app is one container `parts-db-web` from the image `localhost/parts-db`, started by `~/.config/containers/systemd/parts-db-web.container` (a Quadlet file) through `homeapps`' own systemd.
- It joins the Podman network `gateway` (so Caddy reaches it as `parts-db-web:8000`) and publishes `127.0.0.1:8891` for health probes and tunnels only.
- Three mounted folders hold all state: `~/parts-db/data` (database), `~/parts-db/media` (PDF originals incl. `tmp_imports`), `~/parts-db/backups`. Also `~/parts-db/config/env` (secret key, 0600) and `~/parts-db/build`.
- Variables are in `infra/group_vars/partsdb_rootless.yml` (no secrets). The host name and user come from the git-ignored `infra/inventory.ini` (copy of `infra/inventory.ini.example`; use the alias for `homeapps` in the `[partsdb_rootless]` group).

## Preconditions (checked, never changed, by `make deploy`)
Connected as `homeapps`; linger on; the user's systemd running; Podman installed; the file `~/.config/containers/systemd/gateway.network` exists (the gateway repository creates it). If one is missing the deploy stops with a plain message and changes nothing.

## First deployment
```
cp infra/inventory.ini.example infra/inventory.ini   # edit: the real host name; user homeapps (git-ignored file)
make ansible-setup                                   # once: Ansible into .venv
make syntax                                          # normal: "ok" for each playbook
git status                                           # commit your work first: the deploy ships the committed revision
make deploy                                          # builds the image on the server, creates folders and the secret key, starts the app
make createsuperuser                                 # asks for a user name and a password (typed, never stored in a file)
make status                                          # health "healthy", [http=200], backup timer scheduled
```
The container starts with an empty database. `make deploy` may take a few minutes (it pulls the base image and installs packages from PyPI on the server).

Looking at it before the gateway sends traffic: open a tunnel from your computer and browse locally.
```
ssh -L 8891:127.0.0.1:8891 homeapps@<server>         # keep this window open: forwards your local port 8891 to the app
# then open http://127.0.0.1:8891/parts-db/parts/    (the app also answers with the prefix on its own port)
```

## Updating
Commit, then `make deploy`. It takes a verified pre-deploy backup (when a database exists), builds the new image, runs `check --deploy` and migrations, swaps the image and restarts, waits for "healthy", keeps the newest 5 images (only `localhost/parts-db`). Data folders are never emptied or removed.

## Backups
- Daily 03:15 (plus up to 10 minutes random delay; missed runs are made up after a reboot): `manage.py backup_db --prune` as a systemd *user* timer. Check with `make status` or `systemctl --user list-timers`.
- Now: `make backup-now`. One archive `backup-YYYYMMDD-HHMMSS[-label].tar.gz` holds the database (SQLite online backup), every media file, and a manifest with SHA-256 checksums, the integrity-check result and row counts. It is re-opened and verified before success is reported. Labels used: `manual`, `pre-deploy`, `pre-rollback`, `pre-restore`.
- Retention: newest per day for 14 days, newest per month for 12 months, labelled ones for 30 days, and always the newest backup.
- Off-server copy: `make fetch-backup` (or `BACKUP=name`) puts a verified copy in `local/backups/` (git-ignored, mode 0600). Keep it out of synced or shared folders: it contains your data.

## Restore
1. `make restore-drill` (optionally `BACKUP=name`): restores into a throw-away place and checks the result. Expected ending `DRILL_OK`. Live data is not touched.
2. `make restore BACKUP=backup-....tar.gz`: verifies the archive, asks you to type `RESTORE`, stops the app, takes a `pre-restore` backup, moves the existing database and media aside (database as `db.sqlite3.before-restore-<time>`, media files into `media/.before-restore-<time>/`; nothing is deleted), restores, restarts and waits for "healthy". Refuses to run without the confirmation.

## Rollback
`make rollback` puts the previous image back (data untouched). If a failed release changed the database in a way the old code cannot read: `make rollback BACKUP=backup-<time>-pre-deploy.tar.gz`. Before the gateway switch, rolling back to the old rootful instance is simply not switching the gateway upstream: it is still running.

## Reloading data after an empty start
The new instance is empty. From the running old app (do not touch the old instance otherwise) export your parts as CSV (the link **Alle Bauteile exportieren** on the parts list), then in the new instance:
1. Log in (`make createsuperuser` first time).
2. **Bestand aus CSV laden**: upload the CSV. This creates parts only (no suppliers, documents or PDFs).
3. **Belege importieren**: upload the invoice PDFs again (up to 20 per batch). Each import stores the original in `media/` and the purchase history in the database.
4. `make backup-now`, then `make fetch-backup`.
The 38 documents whose PDF originals were lost in the old container cannot be recovered; re-import them from your own files if you still have them.

## Persistence proof (planned for phase R2)
Import a test PDF, then redeploy (same image, then a changed image) and `systemctl --user restart parts-db-web.service`; show that the file (name and size) and the database row are still there. A unit test already proves that the real upload view stores the PDF in the configured media folder.

## Switching the gateway (outline; the exact runbook is written in phase R2)
Order: rootless instance first (on `gateway`), gateway upstream second (`partsdb_upstream: parts-db-web:8000`, done in the gateway repository), rootful stopped third, the plain port closes last (phase R3). Stopping the rootful unit needs sudo and is run by the owner, who also decides about removing it.

## Troubleshooting
See the table in `docs/podman-explained.md`, section 7.

## What is and is not verified
Verified (phase R1): the complete test suite passes with the prefix empty and set, including a test that an uploaded PDF lands in the configured media folder and a database row points to it, backup/restore tests, and artifact tests (sudo-free, hardening, mounts, isolation from the Tax app, no Caddy code); `make syntax`; a `--check` dry run against the real server for connection and preconditions; the templates rendered with Ansible's engine; the real Quadlet generator in dry-run mode accepted the generated unit (as `homeapps`, in a throw-away folder, nothing installed).
Not verified (only the real server can prove): the image build on the server, starting the container, the health check, `keep-id` file ownership, gateway reaching `parts-db-web:8000`, the backup timer firing, reboot survival, the persistence proof.
