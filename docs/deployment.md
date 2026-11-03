# Deployment: rootless Podman under `homeapps`

For concepts in plain language read `docs/podman-explained.md` first. The design decision is `docs/adr/0002-rootless-podman-under-homeapps.md`. **Nothing described here has run on the real server yet** (phase R1 prepared and rehearsed it); see "What is and is not verified" at the end.

## Design in one page
- Server `cornil.fritz.box`, Linux user `homeapps` (key-only SSH, linger on). The Caddy gateway (`cornil-gateway` repository) serves `https://cornil.fritz.box/parts-db/` and strips the prefix.
- The app is one container `parts-db-web` from the image `localhost/parts-db`, started by `~/.config/containers/systemd/parts-db-web.container` (a Quadlet file) through `homeapps`' own systemd.
- It joins the Podman network `gateway` (so Caddy reaches it as `parts-db-web:8000`) and publishes `127.0.0.1:8891` for health probes and tunnels only.
- Three mounted folders hold all state, on the removable disk `/u01` (ADR 0003): `/u01/data/parts-db/data` (database), `/u01/data/parts-db/media` (PDF originals incl. `tmp_imports`), `/u01/data/parts-db/backups`. Image build files are in `/u01/apps/parts-db/build`. The secret key stays on the internal disk: `~/parts-db/config/env` (0600), together with the deploy record `~/parts-db/DEPLOYED`.
- Before the move (and after a storage rollback) the same three folders are under `~/parts-db/`, the "legacy" layout. A one-word file `~/parts-db/config/storage_location` (`u01` or `legacy`) records which one is in use; `make deploy` refuses when it disagrees with `use_u01` in the variables.
- Variables are in `infra/group_vars/partsdb_rootless.yml` (no secrets). The host name and user come from the git-ignored `infra/inventory.ini` (copy of `infra/inventory.ini.example`; use the alias for `homeapps` in the `[partsdb_rootless]` group).

## Preconditions (checked, never changed, by `make deploy`)
Connected as `homeapps`; linger on; the user's systemd running; Podman installed; `/u01` is a real mount point (not the empty directory behind it); the three storage folders exist and belong to `homeapps`; the storage layout on the server matches `use_u01`; the file `~/.config/containers/systemd/gateway.network` exists (the gateway repository creates it). If one is missing the deploy stops with a plain message and changes nothing.

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

## Moving the data to /u01 (runbook, plain language)
Why: the internal disk is small. The big USB disk is mounted at `/u01`. The app's data moves there; the secret key stays where it is.

What protects you: the app's unit and the backup service refuse to start while `/u01` is not really mounted (`ExecStartPre=/usr/bin/mountpoint -q /u01`), so nothing can be written into the empty folder behind a missing disk. A failed start is retried every 10 seconds until the disk is back.

Steps (phase PD2, run by the owner and Claude together):
1. Before the move, the playbooks still need the old layout: add `EXTRA="-e use_u01=false"` to `make status`, `make backup-now`, `make fetch-backup` (and to `make deploy` if you deploy before moving).
2. `make relocate-storage`. It checks everything first (disk mounted, old folders present, new folders empty) and changes nothing if a check fails. Then it stops the app, takes a verified backup (`pre-relocate`), **copies** data, media and backups with `cp -a` (never deleting), compares the file lists and the SHA-256 of every file, and shows you the result (file counts and sizes, never contents). Only when you type `SWITCH` does it point the app at the new folders, write the marker and start. If you do not confirm, or anything fails, the app is started again on the old folders.
3. Afterwards the old folders stay in `~/parts-db/{data,media,backups}` untouched ("kept for rollback"). The report shows how much space they hold. You decide when to remove them; this repository never does.
4. Undo: `make rollback-storage` (asks for `ROLLBACK`, takes a safety backup first, points the app at the old folders). Changes made after the move exist only on `/u01`; the safety backup holds them.
5. After a rollback, `make deploy` needs `EXTRA="-e use_u01=false"` until you decide again.

Why a marker file and two switches: `use_u01` in the variables says where the files should be, the marker says where they are. Deploys stop if they disagree, so a routine deploy can never silently point the app at an empty folder.

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

## Persistence proof (done in phase R2)
With 4 imported invoices (4 PDFs, 4 database rows) the container was recreated by a redeploy of the same image, by a redeploy of a changed image, and by `systemctl --user restart parts-db-web.service`. After each, the 4 PDF files (same names and sizes) and the 4 rows were still there. A unit test also proves that the real upload view stores the PDF in the configured media folder.

## Switching the gateway, and stopping the old instance (runbook)
Order, and why: (1) rootless instance running on `gateway` (done), (2) gateway upstream switched to it, (3) you test, (4) only when you say so, the old rootful unit is stopped, (5) much later, the plain port 8888 is closed (phase R3). Stopping the old instance before step 2 would take the site down, because the gateway still points at it until then.

### 1. Before the switch
- Export your old parts as CSV from the old app (link **Alle Bauteile exportieren**) if you still want them. Nothing else is read or copied from the old instance.
- `make status` shows `healthy` and `[http=200]`; `make backup-now` has been run.

### 2. Switch the gateway (separate session, in the `cornil-gateway` repository)
It sets `partsdb_upstream: parts-db-web:8000` (the container name and the port **inside** the container, not 8891) and runs `make smoke`. Nothing in this repository changes for this step.

### 3. Test through the gateway
Open `https://cornil.fritz.box/parts-db/parts/` in a browser: log in, add a part, import a CSV, import an invoice PDF, open the admin (`/parts-db/admin/`). To confirm which instance answers, ask the new container's log: `ssh homeapps@<server> podman logs --since 5m parts-db-web` shows your requests; the old one would show nothing there.

### Rollback before step 4
Set `partsdb_upstream` back to the old value in the gateway repository and redeploy the gateway. The old instance is still running and untouched, so nothing else is needed. Anything entered in the new instance in the meantime stays there (and in its backups).

### 4. Stop the old rootful unit (you run these; they need your sudo password)
Do this only after step 3 passed and you have said so. It stops the old container and keeps it from starting again at boot. **It does not delete the old data folder, image or the unit file's contents.** Note that stopping recreates nothing, but any files that exist only inside the old container (never mounted) are lost when that container is removed later; they are not needed, see ADR 0002.
```
sudo systemctl stop parts-db.service    # stops the old rootful container; frees port 8888
sudo mv /etc/containers/systemd/parts-db.container /etc/containers/systemd/parts-db.container.rootful-disabled   # so it cannot start again after a reboot
sudo systemctl daemon-reload            # tells the server's systemd about the change
```
Expected: no output. Check from your computer: `curl -m 5 http://cornil.fritz.box:8888/` is refused, and `https://cornil.fritz.box/parts-db/parts/` still works.

### Going back to the old instance (only if step 4 was done and something is wrong)
```
sudo mv /etc/containers/systemd/parts-db.container.rootful-disabled /etc/containers/systemd/parts-db.container
sudo systemctl daemon-reload
sudo systemctl start parts-db.service   # starts the old container again on 8888
```
then point `partsdb_upstream` back at the old value in the gateway repository. The old database file was not touched.

### Later, your decision (documented, not run by anything): removing the old setup
Only when you are sure you will not go back. These delete permanently:
```
sudo rm /etc/containers/systemd/parts-db.container.rootful-disabled   # the old unit file
sudo podman rmi localhost/parts-db:latest                              # the old rootful image (the rootless images belong to homeapps and are separate)
sudo rm -r /etc/parts-db                                               # the old secret file
sudo rm -r <old data folder of the rootful setup>                      # the old database and static files: PERMANENT
sudo systemctl daemon-reload
```

### 5. After the switch
Reload anything you still want through the imports (see "Reloading data after an empty start"), run `make backup-now` and `make fetch-backup`. Closing port 8888 and turning on Secure cookies is phase R3 (after a few days of normal use).

## Troubleshooting
See the table in `docs/podman-explained.md`, section 7.

## What is and is not verified
Verified (phase R1): the complete test suite passes with the prefix empty and set, including a test that an uploaded PDF lands in the configured media folder and a database row points to it, backup/restore tests, and artifact tests (sudo-free, hardening, mounts, isolation from the Tax app, no Caddy code); `make syntax`; a `--check` dry run against the real server for connection and preconditions; the templates rendered with Ansible's engine; the real Quadlet generator in dry-run mode accepted the generated unit (as `homeapps`, in a throw-away folder, nothing installed).
Not verified (only the real server can prove): the image build on the server, starting the container, the health check, `keep-id` file ownership, gateway reaching `parts-db-web:8000`, the backup timer firing, reboot survival, the persistence proof.
