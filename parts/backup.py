"""Backup and restore of the database (SQLite or PostgreSQL) and the media folder (PDF originals), see docs/deployment.md.

A backup is one tar.gz: db.sqlite3 (a consistent copy made with SQLite's online backup API, so it is safe while
the app runs), media/... (every file of the media folder, including tmp_imports) and MANIFEST.json (SHA-256 of
every file, integrity-check result, row counts per table). Database and media are always taken together, so a
row never points to a file that is missing from the same backup. Restores verify everything first and never
overwrite silently. Only counts and file names are reported, never database contents.

On PostgreSQL the database file is a `pg_dump -Fc` (db.dump) taken from one exported snapshot, so the dump and the
row counts in the manifest describe exactly the same moment, while the app keeps running. Restores go into a NEW,
EMPTY database; swapping it in for the live one renames the live database aside, it never drops it.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

MANIFEST = 'MANIFEST.json'
DB_NAME = 'db.sqlite3'
PG_DUMP = 'db.dump'
SQLITE, POSTGRESQL = 'sqlite', 'postgresql'
DB_FILES = {SQLITE: DB_NAME, POSTGRESQL: PG_DUMP}
DB_NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,62}$')
MEDIA = 'media'
NAME_RE = re.compile(r'^backup-(\d{8})-(\d{6})(?:-([a-z0-9-]+))?\.tar\.gz$')
LABEL_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,30}$')


class BackupError(Exception):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _media_files(root: Path):
    return sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink()) if root.is_dir() else []


def inspect_db(path: Path) -> dict:
    """Integrity check result and row counts per table (opened read-only)."""
    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            check = conn.execute('PRAGMA integrity_check').fetchall()
            result = 'ok' if check == [('ok',)] else '; '.join(r[0] for r in check)[:300]
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise BackupError(f'Database cannot be read: {e}') from e
    return {'integrity_check': result, 'row_counts': counts}


def _copy_db(source: Path, dest: Path):
    """Consistent online copy (SQLite backup API)."""
    try:
        src = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except sqlite3.Error as e:
        raise BackupError(f'Database cannot be copied: {e}') from e


def create_backup(db_path, media_dir, backup_dir, label='', now=None, app_version='') -> Path:
    """Write backup-YYYYmmdd-HHMMSS[-label].tar.gz into backup_dir and return its path."""
    db_path, media_dir, backup_dir = Path(db_path), Path(media_dir), Path(backup_dir)
    if not db_path.is_file():
        raise BackupError('Database file not found.')
    if label and not LABEL_RE.match(label):
        raise BackupError('Label may only contain lower-case letters, digits and hyphens.')
    now = now or datetime.datetime.now(datetime.timezone.utc)
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    name = f'backup-{now:%Y%m%d-%H%M%S}{"-" + label if label else ""}.tar.gz'
    final = backup_dir / name
    if final.exists():
        raise BackupError(f'{name} already exists.')

    with tempfile.TemporaryDirectory(dir=backup_dir, prefix='.staging-') as tmp:
        stage = Path(tmp)
        _copy_db(db_path, stage / DB_NAME)
        info = inspect_db(stage / DB_NAME)
        if info['integrity_check'] != 'ok':
            raise BackupError(f'Database integrity check failed: {info["integrity_check"]}')
        files = {DB_NAME: _sha256(stage / DB_NAME)}
        for src in _media_files(media_dir):
            rel = Path(MEDIA) / src.relative_to(media_dir)
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, stage / rel)
            files[rel.as_posix()] = _sha256(stage / rel)
        manifest = {'format': 1, 'engine': SQLITE, 'app_version': app_version,
                    'created_utc': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'label': label, 'files': files, **info}
        (stage / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))
        partial = backup_dir / f'.{name}.partial'
        with tarfile.open(partial, 'w:gz') as tar:
            tar.add(stage / MANIFEST, arcname=MANIFEST)
            for rel in files:
                tar.add(stage / rel, arcname=rel)
        os.chmod(partial, 0o600)
        os.replace(partial, final)           # atomic: a half-written file never carries the final name
    return final


def _safe_members(tar):
    for m in tar.getmembers():
        parts = Path(m.name).parts
        ok = m.isfile() and not Path(m.name).is_absolute() and '..' not in parts and (
            m.name in {DB_NAME, PG_DUMP, MANIFEST} or (len(parts) > 1 and parts[0] == MEDIA))
        if not ok:
            raise BackupError(f'Unexpected entry in archive: {m.name[:80]!r}')
        yield m


def verify_archive(archive, workdir) -> tuple[dict, Path]:
    """Extract into workdir and check manifest, hashes and the database. Returns (manifest, extracted dir)."""
    archive, workdir = Path(archive), Path(workdir)
    if not archive.is_file():
        raise BackupError('Backup archive not found.')
    out = workdir / 'extracted'
    out.mkdir(parents=True)
    try:
        with tarfile.open(archive, 'r:gz') as tar:
            members = list(_safe_members(tar))
            tar.extractall(out, members=members, filter='data')
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BackupError(f'Archive cannot be read: {e}') from e
    try:
        manifest = json.loads((out / MANIFEST).read_text())
    except (OSError, ValueError) as e:
        raise BackupError('Manifest missing or unreadable.') from e
    files = manifest.get('files')
    engine = manifest.get('engine', SQLITE)
    if engine not in DB_FILES:
        raise BackupError('Manifest names an unknown database engine.')
    db_file = DB_FILES[engine]
    if not isinstance(files, dict) or db_file not in files:
        raise BackupError('Manifest does not list the database.')
    on_disk = {p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file()} - {MANIFEST}
    if on_disk != set(files):
        raise BackupError('Archive contents do not match the manifest.')
    for rel, expected in files.items():
        if _sha256(out / rel) != expected:
            raise BackupError(f'Checksum mismatch for {rel}.')
    if engine == POSTGRESQL:
        # Row counts are compared when the dump is restored into a database (they come from the ORM at backup time).
        pg_check_dump(out / PG_DUMP)
    else:
        info = inspect_db(out / DB_NAME)
        if info['integrity_check'] != 'ok':
            raise BackupError(f'Database integrity check failed: {info["integrity_check"]}')
        if info['row_counts'] != manifest.get('row_counts'):
            raise BackupError('Row counts differ from the manifest.')
    return manifest, out


def _move_aside(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    aside = path.with_name(f'{path.name}.before-restore-{stamp}')
    os.replace(path, aside)
    return aside


def restore_archive(archive, target_db, target_media, force=False, now=None) -> dict:
    """Verify, then place the database and media. Existing targets are refused unless force=True, in
    which case they are moved aside (never deleted)."""
    target_db, target_media = Path(target_db), Path(target_media)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = f'{now:%Y%m%d-%H%M%S}'
    media_present = target_media.exists() and any(target_media.iterdir())
    if (target_db.exists() or media_present) and not force:
        raise BackupError('Target already contains data; refusing to overwrite (use force to move it aside).')
    target_db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target_db.parent, prefix='.restore-') as tmp:
        manifest, out = verify_archive(archive, tmp)
        aside = []
        for p in (target_db, Path(f'{target_db}-wal'), Path(f'{target_db}-shm')):
            aside.append(_move_aside(p, stamp))
        if media_present:
            # A mounted folder itself cannot be renamed; move its contents into a sibling-named folder inside it.
            holder = target_media / f'.before-restore-{stamp}'
            holder.mkdir()
            for child in list(target_media.iterdir()):
                if child != holder:
                    os.replace(child, holder / child.name)
            aside.append(holder)
        shutil.copy2(out / DB_NAME, target_db)
        os.chmod(target_db, 0o600)
        target_media.mkdir(parents=True, exist_ok=True)
        if (out / MEDIA).is_dir():
            shutil.copytree(out / MEDIA, target_media, dirs_exist_ok=True)
    return {'manifest': manifest, 'moved_aside': [str(a) for a in aside if a]}


# --- PostgreSQL ---------------------------------------------------------------------------------------------------

def _pg_tool(name):
    from django.conf import settings
    base = getattr(settings, 'PG_BIN_DIR', '')
    return str(Path(base) / name) if base else name


def pg_env(db):
    """Environment for the PostgreSQL client tools. The password travels in the environment, never on a command line."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
    env.update({'PGHOST': str(db['HOST']), 'PGPORT': str(db['PORT']), 'PGUSER': str(db['USER']),
                'PGPASSWORD': str(db.get('PASSWORD') or ''), 'PGCONNECT_TIMEOUT': '10'})
    return env


def _run(cmd, env=None, timeout=600):
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BackupError(f'{Path(cmd[0]).name} could not run: {type(e).__name__}') from e
    if result.returncode != 0:
        # The tools do not echo the password; still only the first lines of their message are shown.
        message = ' '.join(result.stderr.strip().splitlines()[:3])[:300]
        raise BackupError(f'{Path(cmd[0]).name} failed: {message}')
    return result


def pg_check_dump(path: Path):
    """The dump must be readable and contain a table of contents (pg_restore --list)."""
    result = _run([_pg_tool('pg_restore'), '--list', str(path)])
    if 'TABLE DATA' not in result.stdout:
        raise BackupError('The database dump contains no table data.')


def orm_row_counts() -> dict:
    """Row count per table through the ORM, for every model of the project (including many-to-many tables)."""
    from django.apps import apps
    return {m._meta.db_table: m._base_manager.count() for m in sorted(
        apps.get_models(include_auto_created=True), key=lambda m: m._meta.db_table)}


def create_pg_backup(db, media_dir, backup_dir, label='', now=None, app_version='') -> Path:
    """backup-YYYYmmdd-HHMMSS[-label].tar.gz holding db.dump (pg_dump -Fc), media/... and MANIFEST.json."""
    from django.db import connection, transaction
    media_dir, backup_dir = Path(media_dir), Path(backup_dir)
    if label and not LABEL_RE.match(label):
        raise BackupError('Label may only contain lower-case letters, digits and hyphens.')
    now = now or datetime.datetime.now(datetime.timezone.utc)
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    name = f'backup-{now:%Y%m%d-%H%M%S}{"-" + label if label else ""}.tar.gz'
    final = backup_dir / name
    if final.exists():
        raise BackupError(f'{name} already exists.')

    with tempfile.TemporaryDirectory(dir=backup_dir, prefix='.staging-') as tmp:
        stage = Path(tmp)
        # One REPEATABLE READ snapshot, exported: pg_dump reads exactly this state, and so do the row counts.
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                    cursor.execute('SELECT pg_export_snapshot()')
                    snapshot = cursor.fetchone()[0]
                _run([_pg_tool('pg_dump'), '-Fc', '--no-owner', '--no-acl', f'--snapshot={snapshot}',
                      f'--dbname={db["NAME"]}', '-f', str(stage / PG_DUMP)], env=pg_env(db))
                counts = orm_row_counts()
        except BackupError:
            raise
        except Exception as e:                      # driver errors: type only, a message could carry connection details
            raise BackupError(f'Database cannot be read: {type(e).__name__}') from e
        pg_check_dump(stage / PG_DUMP)
        files = {PG_DUMP: _sha256(stage / PG_DUMP)}
        for src in _media_files(media_dir):
            rel = Path(MEDIA) / src.relative_to(media_dir)
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, stage / rel)
            files[rel.as_posix()] = _sha256(stage / rel)
        manifest = {'format': 1, 'engine': POSTGRESQL, 'app_version': app_version,
                    'created_utc': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'label': label, 'files': files,
                    'integrity_check': 'ok', 'row_counts': counts}
        (stage / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))
        partial = backup_dir / f'.{name}.partial'
        with tarfile.open(partial, 'w:gz') as tar:
            tar.add(stage / MANIFEST, arcname=MANIFEST)
            for rel in files:
                tar.add(stage / rel, arcname=rel)
        os.chmod(partial, 0o600)
        os.replace(partial, final)
    return final


def _pg_connect(db, dbname):
    import psycopg
    try:
        return psycopg.connect(host=db['HOST'], port=db['PORT'], user=db['USER'], password=db.get('PASSWORD') or '',
                               dbname=dbname, connect_timeout=10, autocommit=True)
    except psycopg.Error as e:
        raise BackupError(f'Cannot connect to database {dbname!r}: {type(e).__name__}') from e


def pg_user_tables(db, dbname) -> dict:
    """Row count of every ordinary table in a database (public schema), by exact count."""
    import psycopg
    with _pg_connect(db, dbname) as conn:
        try:
            # Django registers its own loaders on psycopg's global adapters, so text can arrive as bytes: decode.
            names = [r[0].decode() if isinstance(r[0], (bytes, memoryview)) else r[0] for r in conn.execute(
                "SELECT tablename::text FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")]
            return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
        except psycopg.Error as e:
            raise BackupError(f'Cannot read database {dbname!r}: {type(e).__name__}') from e


def restore_pg_archive(archive, db, new_db, target_media, force=False, swap=False, now=None) -> dict:
    """Restore into a NEW, EMPTY database `new_db` and the media into `target_media`.

    The live database is never touched unless swap=True, and then it is renamed aside, not dropped. A database that
    already exists is accepted only if it has no tables (for instance one the platform created for this purpose)."""
    import psycopg
    target_media = Path(target_media)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = f'{now:%Y%m%d_%H%M%S}'
    live = db['NAME']
    if not DB_NAME_RE.match(new_db):
        raise BackupError('Database name may only contain lower-case letters, digits and underscores.')
    if new_db == live:
        raise BackupError('The restore target must be a new database, not the live one (use swap to replace it).')
    media_present = target_media.exists() and any(target_media.iterdir())
    if media_present and not force:
        raise BackupError('Media folder already contains data; refusing to overwrite (use force to move it aside).')

    with tempfile.TemporaryDirectory() as tmp:
        manifest, out = verify_archive(archive, tmp)
        if manifest.get('engine') != POSTGRESQL:
            raise BackupError('This archive holds a SQLite database; it cannot be restored into PostgreSQL.')
        with _pg_connect(db, 'postgres') as admin:
            exists = admin.execute('SELECT 1 FROM pg_database WHERE datname = %s', (new_db,)).fetchone()
            if not exists:
                try:
                    admin.execute(f'CREATE DATABASE "{new_db}"')
                except psycopg.Error as e:
                    raise BackupError(
                        f'Cannot create database {new_db!r} ({type(e).__name__}): the database role needs CREATEDB, '
                        'or the platform must create the empty database first.') from e
        if pg_user_tables(db, new_db):
            raise BackupError(f'Database {new_db!r} is not empty; refusing to restore into it.')
        _run([_pg_tool('pg_restore'), '--no-owner', '--no-acl', '--exit-on-error', f'--dbname={new_db}',
              str(out / PG_DUMP)], env=pg_env(db))
        restored = pg_user_tables(db, new_db)
        expected = manifest['row_counts']
        if {t: restored.get(t) for t in expected} != expected:
            raise BackupError('Row counts of the restored database differ from the manifest.')
        aside_media = None
        if media_present:
            aside_media = target_media / f'.before-restore-{stamp}'
            aside_media.mkdir()
            for child in list(target_media.iterdir()):
                if child != aside_media:
                    os.replace(child, aside_media / child.name)
        target_media.mkdir(parents=True, exist_ok=True)
        if (out / MEDIA).is_dir():
            shutil.copytree(out / MEDIA, target_media, dirs_exist_ok=True)

    result = {'manifest': manifest, 'new_db': new_db, 'moved_aside': [str(aside_media)] if aside_media else [],
              'swapped_aside_db': None}
    if swap:
        aside_db = f'{live}_before_restore_{stamp}'
        with _pg_connect(db, 'postgres') as admin:
            try:
                admin.execute(f'ALTER DATABASE "{live}" RENAME TO "{aside_db}"')
            except psycopg.Error as e:
                raise BackupError(
                    f'Cannot rename the live database ({type(e).__name__}): stop the app first, and the role needs '
                    f'CREATEDB. Nothing was swapped; the restored copy is database {new_db!r}.') from e
            try:
                admin.execute(f'ALTER DATABASE "{new_db}" RENAME TO "{live}"')
            except psycopg.Error as e:
                admin.execute(f'ALTER DATABASE "{aside_db}" RENAME TO "{live}"')       # put the live one back
                raise BackupError(f'Swap failed and was undone ({type(e).__name__}).') from e
        result['swapped_aside_db'] = aside_db
    return result


def drill_pg_archive(archive, db, now=None) -> dict:
    """Prove an archive restores: verify it, restore it into a scratch database that THIS call creates, compare the
    row counts, and drop that scratch database again. Only a database created here (and named partsdb_drill_*) is
    ever dropped. The live database and the media folder are not touched."""
    import psycopg
    now = now or datetime.datetime.now(datetime.timezone.utc)
    scratch = f'partsdb_drill_{now:%Y%m%d_%H%M%S}'
    with tempfile.TemporaryDirectory() as tmp:
        manifest, out = verify_archive(archive, tmp)
        if manifest.get('engine') != POSTGRESQL:
            raise BackupError('This archive holds a SQLite database; the PostgreSQL drill cannot use it.')
        with _pg_connect(db, 'postgres') as admin:
            try:
                admin.execute(f'CREATE DATABASE "{scratch}"')
            except psycopg.Error as e:
                raise BackupError(f'Cannot create a scratch database ({type(e).__name__}): the role needs CREATEDB.') from e
            try:
                _run([_pg_tool('pg_restore'), '--no-owner', '--no-acl', '--exit-on-error', f'--dbname={scratch}',
                      str(out / PG_DUMP)], env=pg_env(db))
                restored = pg_user_tables(db, scratch)
                expected = manifest['row_counts']
                if {t: restored.get(t) for t in expected} != expected:
                    raise BackupError('Row counts of the restored scratch database differ from the manifest.')
            finally:
                assert scratch.startswith('partsdb_drill_')
                admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
    return {'manifest': manifest, 'scratch_db': scratch}


def parse_name(name: str):
    """(datetime, label) for a backup file name, or None if it is not one of ours."""
    m = NAME_RE.match(name)
    if not m:
        return None
    when = datetime.datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S').replace(tzinfo=datetime.timezone.utc)
    return when, m.group(3) or ''


def prune(backup_dir, now=None, keep_days=14, keep_months=12, keep_labelled_days=30) -> list[Path]:
    """Delete old backups. Kept: the newest backup of each calendar day within the last `keep_days` days, the
    newest of each calendar month within the last `keep_months` months, every labelled backup (pre-deploy, ...)
    younger than `keep_labelled_days`, and always the newest backup of all, so the only copy is never removed.
    Only files that match the backup naming pattern are ever touched."""
    backup_dir = Path(backup_dir)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    found = [(parse_name(p.name), p) for p in backup_dir.iterdir() if p.is_file()] if backup_dir.is_dir() else []
    found = sorted(((m, p) for m, p in found if m), key=lambda t: t[0][0], reverse=True)
    keep, days, months = set(), set(), set()
    this_month = now.year * 12 + now.month
    for (when, label), p in found:                       # newest first: the first one seen per day/month wins
        age_days = (now.date() - when.date()).days
        month_age = this_month - (when.year * 12 + when.month)
        if label and age_days < keep_labelled_days:
            keep.add(p)
        if age_days < keep_days and when.date() not in days:
            days.add(when.date())
            keep.add(p)
        if month_age < keep_months and (when.year, when.month) not in months:
            months.add((when.year, when.month))
            keep.add(p)
    if found:
        keep.add(found[0][1])
    removed = []
    for _, p in found:
        if p not in keep:
            p.unlink()
            removed.append(p)
    return removed
