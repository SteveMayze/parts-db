# Podman, containers and the rootless setup, explained from scratch

This guide assumes you know nothing about containers, Podman, systemd or Quadlet. Every term is explained the first time it appears, and every command has a one-line explanation. The operating procedures are in `docs/deployment.md`; the design decision is in `docs/adr/0002-rootless-podman-under-homeapps.md`.

*Where this comes from:* the concepts, the before/after picture, the glossary and the troubleshooting table are adapted from `docs/podman-explained.md` in the public repository `SteveMayze/Tax-and-KK-Tool` (the sister app on the same server), rewritten for parts-db: no bootstrap, no data migration, and with the persistent PDF folder that this app was missing.

**Status:** the rootless setup is written and rehearsed (tests, Ansible syntax check, the real Quadlet generator in dry-run mode) but has **not been run on the server yet**. Where this guide says what you "will see", that is what the code is designed to produce, not something observed on the real server.

## 1. The ideas

- **Container.** A packaged copy of the app, with everything it needs inside, that runs isolated from the rest of the server. It cannot see the server's other files unless we explicitly share a folder with it. The packaged copy is called an **image**; a container is an image that is running.
- **The container's own storage is temporary.** Anything the app writes *inside* the container that is not in a shared folder is thrown away whenever the container is recreated (restart by the service manager, redeploy, new image). **This is exactly what went wrong before**: uploaded PDFs were stored inside the container, so every redeploy deleted them. Now the database and the PDFs are in **mounted folders** (see below), which live on the server's disk and survive everything.
- **Mounted folder (volume, bind mount).** A folder on the server that is shared into the container under a chosen name. The app writes to `/media`; the files really land in `/home/homeapps/parts-db/media` on the server.
- **Podman.** The program that builds images and runs containers. (Docker is a well-known alternative.)
- **Root and sudo.** `root` is the all-powerful administrator account. `sudo` means "do this one command as root"; it asks for your password.
- **Rootful Podman** means Podman runs as root; deploying needs `sudo`; an escape from the container would be root on the server. This is how the app runs **today**.
- **Rootless Podman** means Podman runs as an ordinary Linux user with no special powers. Deploying and restarting need no `sudo`. This is where the app is **going**.
- **User namespace / subordinate ids.** The trick that makes rootless work: Linux lends each user a private block of user numbers (`/etc/subuid`, `/etc/subgid`) that the container may use as its own. Our setting (`keep-id`) makes the service user `homeapps` appear inside the container as the app's user (number 10001), so the app can write to the folders that belong to `homeapps`.
- **systemd.** The server's service manager. It starts programs at boot and restarts them when they fail. There is one for the whole server (changing it needs root) and one **per user** (needs no root).
- **Quadlet.** A small text file (`*.container`) that describes a container; systemd turns it into a service. The new one lives in `homeapps`' own folder `~/.config/containers/systemd/` and is controlled with `systemctl --user`.
- **Linger.** A setting that lets a user's services keep running with nobody logged in and start at boot. It was switched on once for `homeapps` when the Tax app was set up.
- **SSH key.** A pair of files that replace a password when logging in. `homeapps` has **no password**; only your key opens it.
- **Health check.** A tiny test the container runs on itself every 30 seconds (it asks the app's `/health/` page whether it is alive). If it fails three times, the container is restarted.
- **Podman network.** A private virtual switch between containers. The `gateway` network (owned by the gateway repository) lets the Caddy proxy reach this app by its container name `parts-db-web`, so no port needs to be open to the LAN.

## 2. Before and after

```
BEFORE (rootful)                                  AFTER (rootless)

make deploy-podman ── asks for the sudo password   make deploy ── no password, SSH key only
        │                                                 │
        ▼                                                 ▼
  Ansible logs in as an admin login, uses sudo      Ansible logs in as "homeapps", runs every step as that user
        │                                                 │
        ▼                                                 ▼
  root ── Podman ── container                        homeapps ── Podman ── container
  only the database FILE is mounted;                 data/ (database folder) and media/ (PDFs) are mounted;
  PDFs live in the container and are lost            nothing you import is lost by a restart or redeploy
  port 8888 open to the whole LAN                    port closed to the LAN; Caddy reaches the container by name
  secret key in a root-only file                     NEW secret key in a file only homeapps can read
  backup: none                                       daily verified backup of database + PDFs, restore tested
```

## 3. What needs sudo, and what does not

| Step | Needs sudo? | Why |
|---|---|---|
| Stopping the old rootful container (once, owner runs it, `docs/deployment.md`) | yes | it is a root-owned service |
| Removing the old rootful setup (later, owner's decision) | yes | same |
| Everything else: `make deploy`, `status`, `backup-now`, `restore-drill`, `fetch-backup`, `restore`, `rollback`, `createsuperuser`, `changepassword` | **no** | they run as `homeapps` |

A test in the repository fails if `sudo`, `become` or `-K` (Ansible's "ask for the sudo password" flag) ever appears in the rootless Makefile targets, playbooks or templates. The legacy rootful targets (`deploy-podman`, `deploy-docker`) are the only ones left that use `-K`; they disappear in phase R3.

## 4. Daily commands

Run on your own computer, from the project folder. None asks for a sudo password.

```
make ansible-setup           # once: installs Ansible into .venv
make syntax                  # checks that all playbooks are well-formed; changes nothing
make deploy                  # builds the committed code on the server as homeapps and restarts the app
make status                  # read-only overview: who runs the app, its health, data owner, next backup
make backup-now              # takes a verified backup (database + PDFs) right now, on the server
make fetch-backup            # copies the newest backup to local/backups on your computer and verifies it there
make restore-drill           # proves the newest backup restores, in a throw-away place; live data untouched
make restore BACKUP=backup-YYYYMMDD-HHMMSS.tar.gz   # replaces live data with that backup (type RESTORE to confirm)
make rollback                # puts the previous version of the app back (add BACKUP=... to restore its data too)
make createsuperuser         # creates your login for the web page (asks for a name and password)
make changepassword USERNAME=name   # sets a new password for that login
```

Development helpers: `make setup` installs the app's Python packages; `make test` runs the test suite; `make check` runs Django's own configuration check.

What normal output looks like:

- `make deploy` ends with a summary: the deployed commit, the pre-deploy backup name (or "none (first deploy)"), whether migrations were applied. Waiting for "healthy" can take a minute or two.
- `make status` shows `rootless=true`, service `active`, container health `healthy`, the data folders owned by `homeapps`' number, a next backup time, and `[http=200]`.
- `make restore-drill` ends with `DRILL_OK`.
- `make rollback` ends with the health endpoint answering 200.

## 5. Checking that it really is rootless (commands for you)

```
ssh homeapps@<server>                                  # log in as the service user (your key opens it)
podman ps                                              # lists running containers; parts-db-web must be there
podman top parts-db-web huser user pid                 # HUSER = user on the server (homeapps, never root); USER = inside (10001)
ls -ln ~/parts-db/data ~/parts-db/media                # the files; owned by homeapps' number
systemctl --user list-timers                           # the daily backup timer and when it fires next
systemctl --user status parts-db-web.service           # the app's service: should say active (running)
loginctl show-user homeapps --property=Linger          # must say Linger=yes, so it runs with nobody logged in
curl -s http://127.0.0.1:8891/health/                  # the health page; answers ok
```

Survives a restart: `systemctl --user restart parts-db-web.service` (as homeapps), then `make status`. Survives a reboot (only you can check this): reboot the server, wait a minute or two with **nobody logged in**, then `make status`.

## 6. Glossary

| Term | Meaning |
|---|---|
| Ansible | the tool that runs the playbooks (step lists) on the server from your computer |
| Playbook | one Ansible step list, e.g. `infra/deploy.yaml` |
| Container / image | a running / packaged copy of the app |
| Mounted folder | a server folder shared into the container; the only place data survives |
| Health check | the container's self-test every 30 seconds |
| keep-id | the user-namespace setting that shows `homeapps` as the app user inside the container |
| Linger | lets a user's services run without a login and at boot |
| Podman network | a private virtual switch between containers (`gateway`) |
| Quadlet | a text file describing a container for systemd |
| Rootful / rootless | Podman run by root / by an ordinary user |
| Service user | the ordinary Linux user `homeapps` that runs the containers |
| Subordinate ids | the block of user numbers lent to a user for containers |
| systemd, `systemctl --user` | the service manager, and the way to talk to a user's own one |

## 7. Troubleshooting

| What you see | Likely cause and what to do |
|---|---|
| `Permission denied (publickey)` logging in as `homeapps` | the key your SSH really uses is not authorised for `homeapps`; `ssh -G <server> \| grep identityfile` shows which key is used |
| `Linger must be on` | linger was not switched on for `homeapps`; that is a one-time admin step done by the Tax repo's bootstrap |
| `The user's own systemd must be running` | log in once with `ssh homeapps@<server>`; check `loginctl show-user homeapps` |
| `... gateway.network does not exist` | the gateway repository has not been deployed; deploy it first. Nothing was changed |
| `Failed to connect to bus` or `XDG_RUNTIME_DIR` errors | the user has no session yet; log in once by SSH and retry |
| Build fails while pulling the base image or packages | the server cannot reach the image registry or PyPI; check with `curl -I https://pypi.org` on the server |
| `newuidmap` / `cannot set up namespace` / `no subuid ranges found` | `homeapps` has no sub-id range; it was created by the Tax bootstrap, so check `/etc/subuid` |
| Container starts but never says `healthy` | `ssh homeapps@<server> journalctl --user -u parts-db-web -n 80`, then `curl http://127.0.0.1:8891/health/` on the server; the second tells whether the app or only the check fails |
| `permission denied` writing the database or a PDF | the data folders are not owned by `homeapps` (`ls -ln ~/parts-db/data ~/parts-db/media`) or the `keep-id` mapping was changed |
| 403 when submitting a form | the page's address is not in `csrf_trusted_origins`; through an SSH tunnel use the `127.0.0.1:8891` address as in `docs/deployment.md` |
| `Tracked files have uncommitted changes` | the deploy ships exactly the committed revision; commit first (or `-e allow_dirty=true` to deploy HEAD anyway) |
| A memory or process limit "was not applied" | the user's systemd does not hand the `memory` or `pids` controller to the user; `make status` prints the applied limits |
