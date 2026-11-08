"""PostgreSQL support (ADR 0003): settings contract, deployment files, ordering, fingerprint, backup and restore.

Everything here runs on SQLite by default. The tests marked `postgres_only` need the suite to run against PostgreSQL
(`make test-pg`, or the five DJANGO_DB_* variables) and the PostgreSQL client tools; otherwise they are skipped.
"""
import datetime
import json
import re
import shutil
import tarfile
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from . import backup
from .management.commands import data_fingerprint
from .models import Part, Project, Supplier
from .ordering import sort_by_text, sort_texts
from .tests_deploy_artifacts import active_lines, code_only, group_vars, read, rendered
from .tests_storage import settings_value

PG_ENV = {"DJANGO_DB_HOST": "postgres", "DJANGO_DB_NAME": "partsdb", "DJANGO_DB_USER": "partsdb", "DJANGO_DB_PASSWORD": "not-a-real-password"}
on_postgres = settings.USING_POSTGRES and shutil.which("pg_dump", path=(settings.PG_BIN_DIR or None)) is not None
postgres_only = __import__("unittest").skipUnless(on_postgres, "needs the suite on PostgreSQL (make test-pg) and the client tools")


class SettingsContractTests(SimpleTestCase):
    def test_sqlite_exactly_as_before_when_no_host_is_set(self):
        self.assertEqual(settings_value("s.DATABASES['default']['ENGINE']"), "django.db.backends.sqlite3")
        self.assertEqual(settings_value("s.DATABASES['default']['NAME']", DJANGO_DB_PATH="/data/db.sqlite3"), "/data/db.sqlite3")
        self.assertEqual(settings_value("s.USING_POSTGRES"), "False")
        # the other four variables alone do not switch the engine; an empty host does not either
        self.assertEqual(settings_value("s.USING_POSTGRES", DJANGO_DB_NAME="x", DJANGO_DB_USER="y", DJANGO_DB_PASSWORD="z", DJANGO_DB_HOST=" "), "False")

    def test_postgresql_from_the_five_variables(self):
        env = {**PG_ENV, "DJANGO_DB_PORT": "6543"}
        self.assertEqual(settings_value("s.DATABASES['default']['ENGINE']", **env), "django.db.backends.postgresql")
        got = settings_value("[s.DATABASES['default'][k] for k in ('HOST', 'PORT', 'NAME', 'USER', 'PASSWORD')]", **env)
        self.assertEqual(got, "['postgres', '6543', 'partsdb', 'partsdb', 'not-a-real-password']")

    def test_port_defaults_to_5432_and_the_sqlite_path_is_ignored(self):
        self.assertEqual(settings_value("s.DATABASES['default']['PORT']", **PG_ENV), "5432")
        self.assertEqual(settings_value("'NAME' in s.DATABASES['default'] and s.DATABASES['default']['NAME']", DJANGO_DB_PATH="/data/db.sqlite3", **PG_ENV), "partsdb")

    def test_connections_are_modest_and_fail_fast(self):
        self.assertEqual(settings_value("(s.DATABASES['default']['CONN_MAX_AGE'], s.DATABASES['default']['CONN_HEALTH_CHECKS'], s.DATABASES['default']['OPTIONS'])", **PG_ENV),
                         "(60, True, {'connect_timeout': 5})")

    def test_the_password_is_never_logged(self):
        self.assertNotIn("DATABASES", settings_value("s.LOGGING", **PG_ENV))
        self.assertNotIn("not-a-real-password", settings_value("s.LOGGING", **PG_ENV))

    def test_the_driver_is_pinned_and_ships_its_own_libpq(self):
        lines = [l.strip() for l in read("requirements.txt").splitlines()]
        self.assertIn("psycopg[binary]==3.3.8", lines)       # a wheel exists for CPython 3.12 (manylinux), so no libpq5 in the image


class DockerfileClientToolsTests(SimpleTestCase):
    def test_postgresql_client_17_or_newer_is_installed_and_checked_at_build_time(self):
        text = read("Dockerfile")
        self.assertRegex(text, r"postgresql-client-(\d+)")
        major = int(re.search(r"postgresql-client-(\d+)", text).group(1))
        self.assertGreaterEqual(major, 17)                    # not older than the server (default major 17)
        self.assertIn("pg_dump --version", text)              # the build fails if the tools are missing or too old

    def test_the_image_keeps_its_other_properties(self):
        lines = active_lines("Dockerfile")
        self.assertEqual([l for l in lines if l.startswith("USER ")], ["USER 10001:10001"])
        self.assertNotIn("--privileged", read("Dockerfile"))
        # packages are installed without recommends and the apt lists are removed again
        self.assertIn("--no-install-recommends", read("Dockerfile"))
        self.assertIn("/var/lib/apt/lists", read("Dockerfile"))


class PostgresUnitTests(SimpleTestCase):
    def setUp(self):
        self.unit = rendered("parts-db-web.container.j2", use_postgres=True)

    def test_networks_dependency_and_secret(self):
        self.assertEqual(re.findall(r"^Network=(.+)$", self.unit, re.M), ["gateway.network", "db.network"])
        self.assertEqual(re.findall(r"^Wants=(.+)$", self.unit, re.M), ["postgres.service"])
        self.assertEqual(re.findall(r"^After=(.+)$", self.unit, re.M), ["postgres.service"])
        self.assertNotRegex(self.unit, r"(?m)^Requires=")      # restarting PostgreSQL must not restart the app
        self.assertEqual(re.findall(r"^Secret=(.+)$", self.unit, re.M), ["partsdb-db-password,type=env,target=DJANGO_DB_PASSWORD"])

    def test_four_non_secret_variables_and_no_password_text(self):
        env = re.findall(r"^Environment=(.+)$", self.unit, re.M)
        for expected in ("DJANGO_DB_HOST=postgres", "DJANGO_DB_PORT=5432", "DJANGO_DB_NAME=partsdb", "DJANGO_DB_USER=partsdb"):
            self.assertIn(expected, env)
        self.assertFalse([e for e in env if e.startswith(("DJANGO_DB_PASSWORD", "DJANGO_DB_PATH"))])
        self.assertNotIn("DJANGO_DB_PASSWORD=", self.unit)
        self.assertNotIn("DJANGO_DB_PASSWORD=", read("infra/templates/parts-db.env.j2"))
        self.assertNotIn("DJANGO_DB_PASSWORD=", read("infra/group_vars/partsdb_rootless.yml"))

    def test_no_database_port_is_published_and_the_guard_and_hardening_stay(self):
        self.assertEqual(re.findall(r"^PublishPort=(.+)$", self.unit, re.M), ["127.0.0.1:8891:8000"])
        self.assertNotIn("5432", re.sub(r"DJANGO_DB_PORT=5432", "", self.unit))
        for line in ("ExecStartPre=/usr/bin/mountpoint -q /u01", "ReadOnly=true", "NoNewPrivileges=true", "DropCapability=ALL"):
            self.assertIn(line, self.unit)

    def test_one_off_runs_see_the_same_database(self):
        for name in ("parts-db-backup.service.j2", "manage.sh.j2"):
            text = rendered(name, use_postgres=True)
            self.assertIn("--secret partsdb-db-password,type=env,target=DJANGO_DB_PASSWORD", text, name)
            self.assertIn("--network db ", text, name)
            self.assertIn("-e DJANGO_DB_HOST=postgres", text, name)
            self.assertNotIn("DJANGO_DB_PATH", text, name)
            self.assertNotIn("not-a-real-password", text)

    def test_switched_off_by_default_and_then_renders_the_unit_as_before(self):
        self.assertIs(group_vars()["use_postgres"], False)
        off = rendered("parts-db-web.container.j2")
        self.assertEqual(re.findall(r"^Network=(.+)$", off, re.M), ["gateway.network"])
        for needle in ("Wants=", "After=", "Secret=", "DJANGO_DB_HOST", "db.network", "postgres"):
            self.assertNotIn(needle, code_only(off), needle)
        self.assertIn("Environment=DJANGO_DB_PATH=/data/db.sqlite3", off)
        for name in ("parts-db-backup.service.j2", "manage.sh.j2"):
            self.assertNotIn("--secret", rendered(name))
            self.assertIn("-e DJANGO_DB_PATH=/data/db.sqlite3", rendered(name))

    def test_deploy_checks_the_network_file_and_the_secret_by_name_before_the_first_change(self):
        text = read("infra/deploy.yaml")
        first_change = text.index("Create the config and data directories")
        for needle in ("{{ quadlet_dir }}/{{ db_network }}", "podman secret exists {{ db_secret }}", "db_secret_check.rc == 0"):
            self.assertIn(needle, text)
            self.assertLess(text.index(needle), first_change, needle)
        for forbidden in ("secret inspect", "--showsecret", "secret ls"):
            self.assertNotIn(forbidden, text)                  # the value is never read, only the name looked up

    def test_the_pre_deploy_backup_and_restore_paths_know_both_engines(self):
        self.assertIn("--skip-if-empty", read("infra/deploy.yaml"))
        self.assertIn("--new-database auto --swap --confirm-swap SWAP", group_vars()["restore_db_args"])
        self.assertIn("--target-db /data/db.sqlite3", group_vars()["restore_db_args"])
        self.assertIn("--drill", read("infra/restore-drill.yaml"))


class OrderingTests(TestCase):
    """Umlauts, mixed case and numbers sort the same on SQLite and on PostgreSQL (explicit, not left to the database)."""
    NAMES = ["Zebra", "ärger", "Anton", "a10", "a2", "Ärmel", "äpfel", "Zoo", "zwei", "Über", "Ulm", "ab"]
    EXPECTED = ["a2", "a10", "ab", "Anton", "äpfel", "ärger", "Ärmel", "Über", "Ulm", "Zebra", "Zoo", "zwei"]

    def test_sort_texts_and_sort_by_text(self):
        self.assertEqual(sort_texts(["b", "B", "ä", "a", "a10", "a9", "Z", "ü", "u"]), ["a", "ä", "a9", "a10", "B", "b", "u", "ü", "Z"])

    def test_project_list_dropdowns_and_clients_are_in_explicit_order(self):
        for name in self.NAMES:
            Project.objects.create(name=name, client=name.swapcase())
        response = self.client.get(reverse("parts:projects"))
        got = [p.name for p in response.context["projects"]]
        self.assertEqual(got, self.EXPECTED)
        self.assertEqual(list(response.context["clients"]), sort_texts(c.swapcase() for c in self.NAMES))

    def test_part_list_default_order_and_filter_lists(self):
        for index, name in enumerate(self.NAMES):
            Part.objects.create(part_type=name, value=f"{index}", packaging=name)
        response = self.client.get(reverse("parts:list"))
        self.assertEqual([p.part_type for p in response.context["parts"]], self.EXPECTED)
        self.assertEqual(list(response.context["part_types"]), sort_texts(self.NAMES))
        self.assertEqual(list(response.context["packaging_options"]), sort_texts(self.NAMES))
        self.assertEqual(list(response.context["part_types"]), self.EXPECTED)

    def test_case_variants_of_the_same_word_keep_a_fixed_order(self):
        self.assertEqual(sort_texts(["ä", "Ä", "a", "A"]), sort_texts(["A", "a", "Ä", "ä"]))


def golden_data():
    """Deterministic rows (explicit ids and timestamps: PostgreSQL sequences are not reset between tests)."""
    stamp = datetime.datetime(2024, 3, 5, 12, 30, 15, 123456, tzinfo=datetime.timezone.utc)
    supplier = Supplier.objects.create(pk=101, name="Conrad Electronic", url="https://example.invalid")
    Supplier.objects.filter(pk=supplier.pk).update(created_at=stamp, updated_at=stamp)
    project = Project.objects.create(pk=102, name="Wetterstation", client="Müller", note="Größe 3,5 mm", active=True)
    Project.objects.filter(pk=project.pk).update(created_at=stamp, updated_at=stamp)
    part = Part.objects.create(pk=103, part_type="Widerstand", value="10 kΩ", packaging="0805", quantity=7,
                               last_price=Decimal("0.0500"), last_purchased=datetime.date(2024, 3, 1), comment="ä")
    Part.objects.filter(pk=part.pk).update(created_at=stamp, updated_at=stamp)


class FingerprintTests(TestCase):
    def run_fp(self, *args):
        out = StringIO()
        call_command("data_fingerprint", *args, stdout=out)
        return out.getvalue()

    def test_same_data_same_fingerprint_and_a_change_changes_it(self):
        golden_data()
        first = self.run_fp()
        self.assertEqual(first, self.run_fp())
        Part.objects.update(quantity=8)
        self.assertNotEqual(first, self.run_fp())

    def test_prints_counts_and_hashes_but_never_a_value(self):
        golden_data()
        out = self.run_fp()
        for value in ("Widerstand", "Conrad", "Müller", "0805", "example.invalid", "Wetterstation"):
            self.assertNotIn(value, out)
        self.assertRegex(out, r"(?m)^parts\.part rows=1 sha256=[0-9a-f]{64}$")
        self.assertRegex(out, r"(?m)^TOTAL rows=\d+ sha256=[0-9a-f]{64}$")

    def test_ids_of_content_types_and_permissions_do_not_matter(self):
        from django.contrib.auth.models import Permission, User
        from django.contrib.admin.models import LogEntry
        from django.contrib.contenttypes.models import ContentType
        golden_data()
        user = User.objects.create(username="u", password="x", date_joined=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc))
        ct = ContentType.objects.get_for_model(Part)
        LogEntry.objects.create(user=user, content_type=ct, object_id="1", object_repr="x", action_flag=1, change_message="")
        LogEntry.objects.update(action_time=datetime.datetime(2024, 1, 2, tzinfo=datetime.timezone.utc))
        user.user_permissions.add(Permission.objects.get(codename="view_part"))
        # a reference to a content type or permission is compared by its natural key, not by its (engine specific) id
        resolvers = data_fingerprint._natural_resolvers()
        self.assertEqual(resolvers[ContentType][ct.pk], "parts.part")
        self.assertEqual(resolvers[Permission][Permission.objects.get(codename="view_part").pk], "parts.part.view_part")
        out = self.run_fp()
        self.assertNotIn("contenttypes.contenttype", out)
        self.assertNotIn("auth.permission", out)
        self.assertRegex(out, r"(?m)^admin\.logentry rows=1 ")

    def test_golden_fingerprint_is_the_same_on_every_engine(self):
        golden_data()
        rows, _ = data_fingerprint.fingerprint()
        by_model = {label: (count, digest) for label, count, digest in rows}
        # These hashes were produced on SQLite and must be reproduced on PostgreSQL (make test-pg).
        self.assertEqual(by_model["parts.part"][0], 1)
        self.assertEqual(GOLDEN["parts.part"], by_model["parts.part"][1])
        self.assertEqual(GOLDEN["parts.project"], by_model["parts.project"][1])
        self.assertEqual(GOLDEN["parts.supplier"], by_model["parts.supplier"][1])


GOLDEN = {       # produced on SQLite; PostgreSQL must give the same
    "parts.part": "34ff9d6f203954556a8dd9b5e3007ff002dc4e1c990d56e82990904f3fc9d2af",
    "parts.project": "4feae4bc141c85a2a58266283b93c92502af339f4b07564042e7f0f2706e1597",
    "parts.supplier": "9e6e9c47eb546c6d6915245e3428098f4097d78ceda08bce47f7fd9ab860b68c",
}


def make_archive_dir():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    media = root / "media"
    (media / "purchase_documents" / "2024").mkdir(parents=True)
    (media / "purchase_documents" / "2024" / "a.pdf").write_bytes(b"%PDF-1.4 a")
    (media / "tmp_imports").mkdir()
    (media / "tmp_imports" / "t.pdf").write_bytes(b"%PDF-1.4 t")
    return tmp, root, media


class ArchiveEngineTests(SimpleTestCase):
    def test_an_unknown_engine_in_the_manifest_is_refused(self):
        tmp, root, _ = make_archive_dir()
        self.addCleanup(tmp.cleanup)
        stage = root / "stage"
        stage.mkdir()
        (stage / "MANIFEST.json").write_text(json.dumps({"engine": "mysql", "files": {}, "row_counts": {}}))
        archive = root / "backup-20240101-000000.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(stage / "MANIFEST.json", arcname="MANIFEST.json")
        with self.assertRaisesRegex(backup.BackupError, "unknown database engine"):
            backup.verify_archive(archive, root / "work")

    def test_a_sqlite_archive_cannot_be_restored_into_postgresql(self):
        with self.assertRaises(CommandError):
            call_command("restore_backup", "x.tar.gz", "--new-database", "x", "--target-media", "/tmp/nowhere")       # not on PostgreSQL

    def test_pg_environment_carries_the_password_outside_the_command_line(self):
        env = backup.pg_env({"HOST": "h", "PORT": 5432, "USER": "u", "PASSWORD": "secret-value", "NAME": "d"})
        self.assertEqual((env["PGHOST"], env["PGUSER"], env["PGPASSWORD"]), ("h", "u", "secret-value"))


class PostgresBackupTests(TransactionTestCase):
    """Real pg_dump / pg_restore against the test database. Skipped unless the suite runs on PostgreSQL."""

    def setUp(self):
        from django.db import connection
        self.db = dict(connection.settings_dict)
        self.tmp, self.root, self.media = make_archive_dir()
        self.addCleanup(self.tmp.cleanup)
        self.backups = self.root / "backups"
        golden_data()

    def make(self, label=""):
        return backup.create_pg_backup(self.db, self.media, self.backups, label=label, app_version="abc123")

    def drop(self, name):
        with backup._pg_connect(self.db, "postgres") as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')

    @postgres_only
    def test_archive_holds_dump_media_and_a_manifest_in_the_same_style(self):
        path = self.make("pre-deploy")
        with tempfile.TemporaryDirectory() as work:
            manifest, out = backup.verify_archive(path, work)
        self.assertEqual(manifest["engine"], "postgresql")
        self.assertEqual(manifest["app_version"], "abc123")
        self.assertEqual(set(manifest["files"]), {"db.dump", "media/purchase_documents/2024/a.pdf", "media/tmp_imports/t.pdf"})
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", h) for h in manifest["files"].values()))
        self.assertEqual(manifest["row_counts"]["parts_part"], 1)
        self.assertEqual(manifest["row_counts"]["parts_supplier"], 1)
        self.assertIn("auth_user", manifest["row_counts"])
        self.assertRegex(path.name, r"^backup-\d{8}-\d{6}-pre-deploy\.tar\.gz$")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    @postgres_only
    def test_a_changed_file_or_a_corrupt_dump_is_detected(self):
        path = self.make()
        bad = self.root / "bad.tar.gz"
        with tempfile.TemporaryDirectory() as work:
            backup.verify_archive(path, work)
            extracted = Path(work) / "extracted"
            (extracted / "media" / "tmp_imports" / "t.pdf").write_bytes(b"changed")
            with tarfile.open(bad, "w:gz") as tar:
                for f in extracted.rglob("*"):
                    if f.is_file():
                        tar.add(f, arcname=f.relative_to(extracted).as_posix())
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaisesRegex(backup.BackupError, "Checksum mismatch"):
                backup.verify_archive(bad, work)

    @postgres_only
    def test_backup_while_the_app_writes_is_consistent(self):
        path = self.make()
        Part.objects.create(part_type="Kondensator")          # a write after the backup does not change the archive
        with tempfile.TemporaryDirectory() as work:
            manifest, _ = backup.verify_archive(path, work)
        self.assertEqual(manifest["row_counts"]["parts_part"], 1)

    @postgres_only
    def test_restore_goes_into_a_new_empty_database_and_leaves_the_live_one_alone(self):
        path = self.make()
        name = "partsdb_restore_test"
        self.addCleanup(self.drop, name)
        target = self.root / "restored-media"
        result = backup.restore_pg_archive(path, self.db, name, target)
        self.assertEqual(result["new_db"], name)
        self.assertIsNone(result["swapped_aside_db"])
        self.assertEqual(backup.pg_user_tables(self.db, name)["parts_part"], 1)
        self.assertEqual((target / "purchase_documents" / "2024" / "a.pdf").read_bytes(), b"%PDF-1.4 a")
        self.assertEqual(Part.objects.count(), 1)             # live database untouched
        with self.assertRaisesRegex(backup.BackupError, "not empty"):
            backup.restore_pg_archive(path, self.db, name, self.root / "other-media")
        with self.assertRaisesRegex(backup.BackupError, "new database, not the live one"):
            backup.restore_pg_archive(path, self.db, self.db["NAME"], self.root / "other-media")
        with self.assertRaisesRegex(backup.BackupError, "already contains data"):
            backup.restore_pg_archive(path, self.db, "partsdb_restore_test2", target)

    @postgres_only
    def test_swap_renames_the_live_database_aside_and_never_drops_it(self):
        path = self.make()
        live, restored = "partsdb_swap_live", "partsdb_swap_new"
        with backup._pg_connect(self.db, "postgres") as admin:
            admin.execute(f'CREATE DATABASE "{live}"')
        stamp = datetime.datetime(2030, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
        aside = f"{live}_before_restore_20300102_030405"
        for name in (live, restored, aside):
            self.addCleanup(self.drop, name)
        db = {**self.db, "NAME": live}
        result = backup.restore_pg_archive(path, db, restored, self.root / "m", swap=True, now=stamp)
        self.assertEqual(result["swapped_aside_db"], aside)
        self.assertEqual(backup.pg_user_tables(db, live)["parts_part"], 1)       # the restored data is now the live name
        self.assertEqual(backup.pg_user_tables(db, aside), {})                   # the old (empty) live database still exists
        with backup._pg_connect(db, "postgres") as admin:
            self.assertIsNone(admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (restored,)).fetchone())

    @postgres_only
    def test_the_command_demands_the_swap_confirmation(self):
        path = self.make()
        with self.assertRaisesRegex(CommandError, "confirm-swap SWAP"):
            call_command("restore_backup", str(path), "--new-database", "partsdb_cmd_test", "--swap", "--target-media", str(self.root / "m"))

    @postgres_only
    def test_commands_backup_verify_and_drill(self):
        out = StringIO()
        call_command("backup_db", "--media", str(self.media), "--backup-dir", str(self.backups), "--label", "pre-deploy", stdout=out)
        self.assertRegex(out.getvalue(), r"BACKUP_CREATED backup-\d{8}-\d{6}-pre-deploy\.tar\.gz files=3 engine=postgresql")
        name = out.getvalue().split()[1]
        out = StringIO()
        call_command("restore_backup", name, "--backup-dir", str(self.backups), stdout=out)
        self.assertIn("VERIFIED integrity=ok checksums=ok files=3 media_files=2", out.getvalue())
        self.assertIn("engine=postgresql", out.getvalue())
        out = StringIO()
        call_command("restore_backup", name, "--backup-dir", str(self.backups), "--drill", stdout=out)
        self.assertIn("DRILL_RESTORED", out.getvalue())
        self.assertIn("scratch_database_dropped=yes", out.getvalue())
        with backup._pg_connect(self.db, "postgres") as admin:
            left = admin.execute("SELECT datname FROM pg_database WHERE datname LIKE 'partsdb_drill_%'").fetchall()
        self.assertEqual(left, [])

    @postgres_only
    def test_skip_if_empty_and_db_option(self):
        with self.assertRaisesRegex(CommandError, "only for SQLite"):
            call_command("backup_db", "--db", "x", "--backup-dir", str(self.backups))
        out = StringIO()
        call_command("backup_db", "--skip-if-empty", "--media", str(self.media), "--backup-dir", str(self.backups), stdout=out)
        self.assertIn("BACKUP_CREATED", out.getvalue())      # the test database has tables, so a backup is made

    @postgres_only
    def test_prune_applies_to_postgres_archives_too(self):
        path = self.make("pre-deploy")
        self.assertEqual(backup.prune(self.backups), [])
        self.assertTrue(path.exists())


class TestPgTargetTests(SimpleTestCase):
    def test_make_test_pg_runs_a_throw_away_container_that_is_always_removed(self):
        self.assertIn("test-pg:", read("Makefile"))
        script = read("infra/test-pg.sh")
        self.assertIn("podman run -d --rm", script)
        self.assertIn('trap cleanup EXIT INT TERM', script)
        self.assertIn('podman rm -f "$NAME"', script)
        self.assertIn('127.0.0.1:$PORT:5432', script)             # loopback only, never published on the LAN
        self.assertIn("/dev/urandom", script)                     # a random password for this run only
        for var in ("DJANGO_DB_HOST", "DJANGO_DB_PORT", "DJANGO_DB_NAME", "DJANGO_DB_USER", "DJANGO_DB_PASSWORD"):
            self.assertIn(var, script)
        self.assertNotRegex(script, r"POSTGRES_PASSWORD=[A-Za-z0-9]")    # no fixed password in the file
        self.assertIn("test-pg.sh", read("Makefile"))
