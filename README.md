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

## Sortable parts list

The main parts list supports clickable column headers for `Typ`, `Wert`, `Beschreibung`, `Teilenummer` and `Lagerort`. The sort state is encoded in the query string (for example `?sort=type&dir=asc`), so a bookmark or reload keeps the selected order.

The active column shows the sort direction with an arrow and `aria-sort`, while search text, filters and other query parameters remain preserved when switching sort order. Sorting uses a safe whitelist, resets the page to 1, and falls back to the default ordering (`Typ`, then `Wert`, then `Verpackung`) if an invalid sort key or direction is supplied.

The comparison logic is intentionally robust:
- case-insensitive and umlaut-aware comparison using normalized text (`casefold`, NFC)
- natural numeric ordering inside text values (for example `Ordner 2` before `Ordner 10`)
- empty values last in both sort directions
- stable tie-breaking via the default model ordering

## Purchase history and document storage

The app keeps purchase history in dedicated `Supplier`, `PurchaseDocument`, and `PurchaseLine` records, while the stock table remains the flat `Part` model. Documents can store an optional PDF, and the app is configured to keep uploaded files in the local `media/` directory outside the repository.

UI entry points:
- `Belege importieren` in the parts page imports PDFs and writes supplier/document/line history.
- `Kaufbelege` shows searchable/filterable purchase documents.
- `Projektbelege` lists documents grouped by selected project.
- `Projekte` provides project management (create/edit/activate/deactivate/delete).
- `Historie` on a part row shows its purchase history.

For production backups, back up the SQLite file (or database host) and the `media/` folder together so the PDF originals and the database remain in sync. In the Docker/Ansible deployment this folder should live on the same persistent volume as the app data.

## Type and subtype (Typ / Untertyp)

Each part has a free-text **Typ** (broad category, e.g. `Diode`, `LED`) and an optional free-text **Untertyp** (e.g. `Zener`, `TVS`, `Photodiode`).

- Both fields autocomplete from values already in the database (the Untertyp suggestions narrow to the chosen Typ), but any text is accepted.
- Whitespace is trimmed on save; capitalisation is kept as entered. Filtering and search ignore case.
- The parts list can be filtered by Typ and then by Untertyp, and search also matches the Untertyp.
- To re-classify existing parts in bulk: open **Verwaltung → Bauteile**, tick the parts, choose the action **Typ/Untertyp der ausgewählten Bauteile setzen**, fill in only the fields you want to change.

## Loading stock from CSV (parts already on hand)

Use this when you have inventory without invoices/documents.

- Open **Bestand aus CSV laden** from the parts list.
- Download **Vorlage herunterladen** for the exact import format.
- Supported headers (case-insensitive, any order):  
  `id, part_type, part_subtype (optional), value, packaging, description, quantity, location_type, location_name, position, manufacturer, mf_part_no, comment`  
  German aliases from the UI are also accepted (for example `Typ`, `Untertyp`, `Wert`, `Verpackung`, `Menge`, `Lagerort`, `Herstellerteilnummer`, `Kommentar`).
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

### Conrad specifics

- Only documents titled `Rechnung` are accepted (credit notes, delivery notes, ... are rejected).
- The layout is detected from markers and shown in the preview:
  - **A** (Pos. / Art.-Nr. table, `Summe Nettowert`): newer invoices.
  - **B** (Bestell-Nr. table, `NETTO` totals row): 2020 invoices, read by word positions.
  - Unknown layout: the import stops and lists which markers were found and which are missing.
  - A new layout needs one reader function and one entry in `_LAYOUTS` in `parsers/conrad.py`, plus a sample.
- Conrad prints **gross** prices. The unit price is derived from `Gesamtpreis ÷ Menge` (4 decimals; handles
  rounded prices and `Preis / 1.000 ST`). Purchase lines are stored as gross; `Part.last_price` is the net price,
  derived with the VAT rate of the line's tax class (from the `StK. n: x% Umsatzsteuer` legend).
- Discount rows (`Sonderrabatt`) are not parts and never change stock. The signed gross sum is stored in
  `PurchaseDocument.discount_total`; it is not distributed over the lines or projects.
- `Teillieferung n/m`, note rows and the blocks `Folgende Artikel werden separat geliefert / nachgeliefert`
  are stored in the document notes. Rows in those blocks are never imported (that quantity is on another invoice).
- Self-checks (shown as warnings in the preview, naming the file and the invoice number):
  a) lines + discounts = Endbetrag, b) NETTO + USt = Endbetrag, c) NETTO × rate ≈ USt (0,01),
  d) |Gesamtpreis ÷ Menge − printed Einzelpreis| ≤ 0,005, e) every tax class exists in the legend.
- Real invoices go to `samples/conrad/` (gitignored). Tests using them are skipped when the folder is missing;
  the other Conrad tests use synthetic PDFs generated with `reportlab` (`pip install -r requirements-dev.txt`).

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
   - `django_allowed_host` (or `django_allowed_hosts`, plus the proxy variables below)
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

## Reverse proxy and URL prefix

The reverse proxy (Caddy, TLS, routing) is **not** part of this repository. It is owned by the separate `cornil-gateway` repository, which serves several apps on one host. Older Caddy playbooks and Makefile targets from this repo are available in git history only.

The gateway forwards `https://<host>/parts-db/...` to this app and strips the `/parts-db` prefix. Configure the app with these deployment variables (see `infra/group_vars/all.yml.example`), which the Quadlet playbook writes to a root-only environment file on the host:

| Variable | Environment variable | Purpose |
| --- | --- | --- |
| `url_prefix` | `DJANGO_URL_PREFIX` | e.g. `/parts-db`; generates prefixed links, redirects, static URLs; empty = site root |
| `behind_tls_proxy` | `DJANGO_BEHIND_TLS_PROXY` | trust `X-Forwarded-Proto` from the proxy |
| `django_allowed_hosts` | `DJANGO_ALLOWED_HOSTS` | accepted host names |
| `csrf_trusted_origins` | `DJANGO_CSRF_TRUSTED_ORIGINS` | accepted origins for form posts |
| `listen_address` | (Quadlet `PublishPort`) | address of the published port; empty = all interfaces |

Session and CSRF cookies are named `partsdb_sessionid` / `partsdb_csrftoken` and limited to the prefix path, so other apps on the same host do not log this one out. The app also accepts the prefix on its own port, so `http://<host>:<app_port>/parts/` and `.../parts-db/parts/` both work.

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
