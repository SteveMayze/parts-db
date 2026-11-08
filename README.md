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

For production backups, the database and the `media/` folder are always backed up together (`manage.py backup_db`, daily on the server) so the PDF originals and the database remain in sync. In the Podman deployment `media/` is a mounted host folder, see [docs/deployment.md](docs/deployment.md).

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

## Deploying to your host (rootless Podman)

The app runs as a **rootless** Podman container of the ordinary Linux user `homeapps`, started by that user's own systemd. Deploying, updating, backing up and restoring need **no sudo password**: you log in by SSH key only. The Caddy reverse proxy lives in the separate [`cornil-gateway`](https://github.com/SteveMayze/cornil-gateway) repository; the app joins its Podman network `gateway` and is reached there as `parts-db-web:8000`.

Everything you import is stored in two folders on the host that are mounted into the container, so a restart, redeploy or image change never loses it:

- The database lives in the shared PostgreSQL container (database `partsdb`) since 2026-11-08; the old SQLite file is kept in `/u01/data/parts-db/legacy-sqlite/` until the owner removes it (see `docs/deployment.md`)
- `/u01/data/parts-db/media`: the uploaded PDF originals

A daily backup (database and PDFs together, verified) is taken by a systemd user timer.

```bash
cp infra/inventory.ini.example infra/inventory.ini    # then edit: host name and user (git-ignored)
make ansible-setup      # once: Ansible into .venv
make syntax             # checks the playbooks
make deploy             # builds the committed revision on the server and (re)starts the app
make createsuperuser    # first run only: create your login
make status | backup-now | fetch-backup | restore-drill | restore BACKUP=... | rollback
```

Plain-language background: [docs/podman-explained.md](docs/podman-explained.md). Design, first deployment, updating, backups, restore, rollback, reloading data after an empty start and troubleshooting: [docs/deployment.md](docs/deployment.md). Decisions: [docs/adr/](docs/adr/).

The old rootful playbooks (`make deploy-podman`, `make deploy-docker`) still exist as a rollback path and are removed in a later phase; do not use them for new deployments.

## Reverse proxy and URL prefix

The reverse proxy (Caddy, TLS, routing) is **not** part of this repository; see `cornil-gateway`. It forwards `https://<host>/parts-db/...` to this app and strips the `/parts-db` prefix. The deployment variables are in `infra/group_vars/partsdb_rootless.yml`:

| Variable | Environment variable | Purpose |
| --- | --- | --- |
| `url_prefix` | `DJANGO_URL_PREFIX` | e.g. `/parts-db`; generates prefixed links, redirects, static URLs; empty = site root |
| `behind_tls_proxy` | `DJANGO_BEHIND_TLS_PROXY` | trust `X-Forwarded-Proto` from the proxy |
| `allowed_hosts` | `DJANGO_ALLOWED_HOSTS` | accepted host names |
| `csrf_trusted_origins` | `DJANGO_CSRF_TRUSTED_ORIGINS` | accepted origins for form posts |
| `use_https` | `DJANGO_HTTPS` | Secure session/CSRF cookies (off until the plain port is closed) |
| `hsts_seconds` | `DJANGO_HSTS_SECONDS` | HSTS; stays 0 until the owner decides |

Paths are set by `DJANGO_DB_PATH` (database file), `DJANGO_MEDIA_ROOT` (PDF folder) and `DJANGO_BACKUP_DIR` (backups); without them the app uses `db.sqlite3` and `media/` in the project folder as before. `GET /health/` answers `ok` and is used by the container's health check.

### Database settings (SQLite by default, PostgreSQL optional)

Without `DJANGO_DB_HOST` the app uses SQLite exactly as before (development, tests, the current server). With it, the app uses PostgreSQL (driver `psycopg` 3). The password is only ever read from `DJANGO_DB_PASSWORD`; on the server that is a Podman secret, never a file or a log line.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DJANGO_DB_HOST` | unset = SQLite | host of the PostgreSQL server (`postgres` on the `db` network); setting it switches the engine |
| `DJANGO_DB_PORT` | `5432` | port |
| `DJANGO_DB_NAME` | `partsdb` | database |
| `DJANGO_DB_USER` | `partsdb` | role (not a superuser) |
| `DJANGO_DB_PASSWORD` | empty | password; from the Podman secret `partsdb-db-password` |
| `DJANGO_DB_PATH` | `db.sqlite3` | SQLite file; ignored when `DJANGO_DB_HOST` is set |
| `DJANGO_PG_BIN_DIR` | on `PATH` | folder with `pg_dump` / `pg_restore` (only needed outside the image) |

Connections are kept for 60 seconds and checked before reuse (`CONN_MAX_AGE`, `CONN_HEALTH_CHECKS`); connecting gives up after 5 seconds. In `infra/group_vars/partsdb_rootless.yml` the switch is `use_postgres` (default `false`: a deploy changes nothing until it is set). `make test-pg` runs the whole test suite against a throw-away PostgreSQL 17 container on your machine (needs Podman; install the PostgreSQL 17 client tools, for example `brew install libpq`, and put them on the `PATH` or in `DJANGO_PG_BIN_DIR`, otherwise the backup tests are skipped). `python manage.py data_fingerprint` prints a row count and a content hash per table (never a value), identical on SQLite and PostgreSQL for the same data.

Session and CSRF cookies are named `partsdb_sessionid` / `partsdb_csrftoken` and limited to the prefix path, so other apps on the same host do not log this one out. The app also accepts the prefix on its own port.

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
