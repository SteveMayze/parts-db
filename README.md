# Parts DB (minimal Django app)

Local single-user Django app for tracking electronic parts in one flat table so you can quickly answer:

> Do I already have this part, and where is it?

## Requirements

- Python 3.x
- pip

## Setup

1. Create and activate a virtual environment:
   - macOS/Linux:
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run migrations:
   ```bash
   python manage.py migrate
   ```
4. Load sample parts:
   ```bash
   python manage.py loaddata sample_parts
   ```
5. (Optional) Create an admin user:
   ```bash
   python manage.py createsuperuser
   ```
6. Start the app:
   ```bash
   python manage.py runserver
   ```

## Troubleshooting

If you see:

`ImportError: Couldn't import Django... Did you forget to activate a virtual environment?`

then Django is not installed in the currently active interpreter. Run:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py check
```

## URLs

- Search/list page: <http://127.0.0.1:8000/parts/>
- Admin: <http://127.0.0.1:8000/admin/>

## Purchase history and document storage

The app keeps purchase history in dedicated `Supplier`, `PurchaseDocument`, and `PurchaseLine` records, while the stock table remains the flat `Part` model. Documents can store an optional PDF, and the app is configured to keep uploaded files in the local `media/` directory outside the repository.

UI entry points:
- `Belege importieren` in the parts page imports PDFs and writes supplier/document/line history.
- `Kaufbelege` shows searchable/filterable purchase documents.
- `Projektbelege` lists documents grouped by selected project.
- `Projekte` provides project management (create/edit/activate/deactivate/delete).
- `Historie` on a part row shows its purchase history.

For production backups, back up the SQLite file (or database host) and the `media/` folder together so the PDF originals and the database remain in sync. In the Docker/Ansible deployment this folder should live on the same persistent volume as the app data.

## Loading stock from CSV (parts already on hand)

Use this when you have inventory without invoices/documents.

- Open **Bestand aus CSV laden** from the parts list.
- Download **Vorlage herunterladen** for the exact import format.
- Supported headers (case-insensitive, any order):  
  `id, part_type, value, packaging, description, quantity, location_type, location_name, position, manufacturer, mf_part_no, comment`  
  German aliases from the UI are also accepted (for example `Typ`, `Wert`, `Verpackung`, `Menge`, `Lagerort`, `Herstellerteilnummer`, `Kommentar`).
- Save CSV as **CSV UTF-8** (or CP1252 is accepted), delimiter `;`, `,` or tab.
- Keep `quantity` as a whole number (`>= 0`).
- For spreadsheet safety, format the **packaging** column as text before entering values like `0805`.

The CSV stock load updates only `Part` records. It does **not** create or modify Supplier, PurchaseDocument, PurchaseLine, or Project data.

## Running tests

Run all tests:

```bash
python manage.py test
```

The project includes a first TDD-style baseline for:
- model behavior
- search/filter behavior
- CRUD view behavior
- invoice parsing and matching

Progressive test milestones are tracked in [PROJECT_TRACKING.md](./PROJECT_TRACKING.md).

## Importing supplier documents (PDF)

The app supports:
- Conrad invoices
- Reichelt order confirmations (`Eingangsbestätigung der Bestellung`)

### Batch upload

1. Open the import page from the parts list.
2. Upload one or more PDFs (up to 20 files, max 10 MB per file).
3. Optional: select a supplier override for the whole batch.
4. Review grouped per-document preview sections.
5. Confirm import in one transaction.

Rules:
- One supplier per batch.
- Failed files are listed and excluded; continuing requires explicit confirmation.
- Documents are processed by date (then document number), not upload order.
- You can choose quantity mode:
  - **Add quantities to stock** (default)
  - **Do not change stock** (new parts start with quantity 0)

### Duplicate protection

Imported documents are tracked in `ImportedDocument` (unique supplier + document number).
- Already imported documents are detected and excluded by default.
- You can explicitly include known duplicates if needed.
- If the same document number appears twice in one batch, only the first is kept.

> Note: import either the order confirmation or the invoice for an order, not both.

### Reichelt specifics

- Only the order-confirmation layout is supported.
- The stored document number is the `I-...` reference.
- Net unit price is derived from gross totals using the VAT factor from the document totals.
- `WHG` is parsed as a raw column value and used only for preview notes.

## Deploying to your host (Docker + Ansible)

This follows the Testing Goat style from chapters 9–12: containerize, production settings via env vars, and automate deployment.

### One-time local setup

1. Install Ansible into your local venv:
   ```bash
   pip install ansible
   ```
2. Install required Ansible collections:
   ```bash
   ansible-galaxy collection install -r infra/requirements.yml
   ```
3. Create deployment vars:
   ```bash
   cp infra/group_vars/all.yml.example infra/group_vars/all.yml
   ```
4. Create a local inventory (kept out of git):
   ```bash
   cp infra/inventory.ini.example infra/inventory.local.ini
   ```
5. Edit `infra/group_vars/all.yml` and set real values for:
   - `django_secret_key`
   - `django_allowed_host`
   - `app_port` (optional)
6. Edit `infra/inventory.local.ini` and set your deployment target host and user.

Optional helper commands (from project root):

```bash
make ansible-setup
make deploy-docker
make deploy-podman
```

### Deploy

From project root:

```bash
ansible-playbook -i infra/inventory.local.ini infra/deploy-playbook.yaml -vv
```

If your host requires a sudo password (typical on first run), use:

```bash
ansible-playbook -i infra/inventory.local.ini infra/deploy-playbook.yaml -K -vv
```

What it does:
- SSHes to your configured `ansible_user@inventory_hostname`
- installs Docker
- syncs project source to the host
- builds the Docker image remotely
- runs migrations + `collectstatic`
- starts/restarts the app container
- smoke-checks `http://<your-host>:<app_port>/parts/`

### APT issue: stale Caddy Cloudsmith repo

Some hosts still carry a broken Caddy APT source from `dl.cloudsmith.io`. It can return `402 Payment Required` and abort `apt update` before any required package install succeeds.

If you see errors like `Failed to fetch ... dl.cloudsmith.io ... 402` or `repository ... InRelease is no longer signed`, remove the stale repo first:

```bash
make caddy-cleanup
```

or directly:

```bash
ansible-playbook -i infra/inventory.local.ini infra/cleanup-caddy-apt-sources.yaml -K -vv
```

The cleanup removes stale Caddy source files from `/etc/apt/sources.list.d/` and refreshes the apt cache before the main deployment continues.

### Secrets and safety

- `infra/group_vars/all.yml` is ignored by git and must stay local.
- Do not commit real secret keys.
- You can rotate `django_secret_key` by updating that file and re-running deploy.

### Optional: passwordless sudo for unattended deploys

If you want deploys to run without `-K`, configure passwordless sudo on the host.

1. On your deployment host, as root:
   ```bash
   visudo -f /etc/sudoers.d/<deploy-user>-ansible
   ```
2. Add:
   ```text
   <deploy-user> ALL=(ALL) NOPASSWD:ALL
   ```
3. Set secure permissions:
   ```bash
   chmod 440 /etc/sudoers.d/<deploy-user>-ansible
   ```
4. Validate from your local machine:
   ```bash
   ssh <deploy-user>@<your-host> 'sudo -n true && echo ok'
   ```

Use a dedicated deployment user with scoped sudo rules for stricter security.

## Deploying with Podman + Quadlet (alternative to Docker)

This path keeps the current Docker workflow intact and adds a Podman-based deployment using a system Quadlet unit.

Run:

```bash
ansible-playbook -i infra/inventory.local.ini infra/deploy-playbook-podman-quadlet.yaml -K -vv
```

What it does:
- installs Podman
- syncs project source to the host
- builds the image with Podman (rootful, same storage as system service)
- runs migrations + `collectstatic` with Podman (rootful)
- writes a Quadlet file at `/etc/containers/systemd/parts-db.container`
- enables/restarts `parts-db.service`
- smoke-checks `http://<your-host>:<app_port>/parts/`

## Caddy options (proxy or disable)

If Caddy is already running and owning your host on port 80, you have two choices:

### Option A: Reconfigure Caddy to proxy this app (recommended)

```bash
make caddy-proxy
```

This writes `/etc/caddy/Caddyfile` to proxy:
- `/parts*` -> `127.0.0.1:8888`
- `/admin*` -> `127.0.0.1:8888`
- `/static*` -> `127.0.0.1:8888`

Then you can use:
- `http://<your-host>/parts/`

### Option B: Disable Caddy entirely

```bash
make caddy-disable
```

Then access directly via app port:
- `http://<your-host>:<app_port>/parts/`

## Why "server not found" can happen

For this host, deployment still requires one successful privileged setup run because base packages are missing:
- no Docker/Podman installed
- Python lacks `pip` and `venv` modules

So either deployment path needs sudo once to install prerequisites. Use `-K` on the first run:

```bash
ansible-playbook -i infra/inventory.local.ini infra/deploy-playbook-podman-quadlet.yaml -K -vv
```

Then verify from the host:

```bash
ssh <deploy-user>@<your-host> 'sudo systemctl status parts-db.service --no-pager -l'
ssh <deploy-user>@<your-host> 'sudo ss -ltn | grep <app_port>'
```

And from your client:
- `http://<your-host>:<app_port>/parts/`

## Notes

- Data model is intentionally one table: `Part`.
- No uniqueness constraints are enforced yet.
- Quantity is approximate by design.

## German UI glossary

- Part -> **Bauteil**
- Location -> **Lagerort**
- Folder -> **Mappe**
- Drawer -> **Schublade**
- Storage Box -> **Sortierbox**
