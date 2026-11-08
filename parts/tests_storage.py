"""Persistent storage and operations: configurable paths, health endpoint, PDF upload location, backup and restore."""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from . import backup
from . import conrad_fixtures as fx
from .models import PurchaseDocument


def settings_value(expression, **env):
    """Evaluate an expression against a freshly imported config.settings with the given environment."""
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
    full_env.update(env)
    result = subprocess.run(
        [sys.executable, "-c", f"from config import settings as s; print({expression})"],
        cwd=settings.BASE_DIR, env=full_env, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


class EnvironmentSettingsTests(SimpleTestCase):
    def test_defaults_are_unchanged(self):
        self.assertEqual(settings_value("s.DATABASES['default']['NAME']"), str(Path(settings.BASE_DIR) / "db.sqlite3"))
        self.assertEqual(settings_value("s.MEDIA_ROOT"), str(Path(settings.BASE_DIR) / "media"))
        self.assertEqual(settings_value("(getattr(s, 'SESSION_COOKIE_SECURE', False), getattr(s, 'CSRF_COOKIE_SECURE', False), s.SECURE_HSTS_SECONDS)"), "(False, False, 0)")

    def test_database_media_and_backup_paths_come_from_the_environment(self):
        env = {"DJANGO_DB_PATH": "/data/db.sqlite3", "DJANGO_MEDIA_ROOT": "/media", "DJANGO_BACKUP_DIR": "/backups"}
        self.assertEqual(settings_value("s.DATABASES['default']['NAME']", **env), "/data/db.sqlite3")
        self.assertEqual(settings_value("s.MEDIA_ROOT", **env), "/media")
        self.assertEqual(settings_value("s.BACKUP_DIR", **env), "/backups")

    def test_https_switches_secure_cookies_and_zero_means_off(self):
        self.assertEqual(settings_value("(s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE)", DJANGO_HTTPS="1"), "(True, True)")
        self.assertEqual(settings_value("hasattr(s, 'SESSION_COOKIE_SECURE') and s.SESSION_COOKIE_SECURE", DJANGO_HTTPS="0"), "False")

    def test_hsts_seconds_come_from_the_environment(self):
        self.assertEqual(settings_value("s.SECURE_HSTS_SECONDS", DJANGO_HSTS_SECONDS="60"), "60")

    def test_tls_proxy_flag_treats_zero_as_off(self):
        self.assertEqual(settings_value("hasattr(s, 'SECURE_PROXY_SSL_HEADER')", DJANGO_BEHIND_TLS_PROXY="0"), "False")
        self.assertEqual(settings_value("hasattr(s, 'SECURE_PROXY_SSL_HEADER')", DJANGO_BEHIND_TLS_PROXY="1"), "True")


class HealthTests(TestCase):
    def test_health_answers_ok_without_login(self):
        response = self.client.get("/health/")
        self.assertEqual((response.status_code, response.content), (200, b"ok"))

    @override_settings(ALLOWED_HOSTS=["127.0.0.1"])
    def test_health_answers_for_the_container_script_host(self):
        self.assertEqual(self.client.get("/health/", HTTP_HOST="127.0.0.1:8000").status_code, 200)


class UploadLandsInMediaTests(TestCase):
    def test_uploaded_pdf_is_stored_in_the_configured_media_directory(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media):
                rows = [fx.item("1000001", "Testartikel", "19", "1", "1,19", "1,19"), fx.netto("1,00", "19,0", "19", "0,19"), fx.legend("19", "19")]
                pdf = fx.make_layout_b_pdf("9700000088", "01.01.2020", [rows], "1,19")
                response = self.client.post(reverse("parts:import"), {"invoice_files": SimpleUploadedFile("beleg.pdf", pdf, "application/pdf")})
                self.assertRedirects(response, reverse("parts:invoice_preview"))
                self.assertEqual(len(list((Path(media) / "tmp_imports").glob("*.pdf"))), 1)   # the temporary copy is in the media folder too

                docs = self.client.session["invoice_preview_batch"]["documents"]
                prefix = docs[0]["lines"][0]["field_prefix"]
                self.client.post(reverse("parts:invoice_preview"), {f"location_type::{prefix}": "folder", f"location_name::{prefix}": "Ordner 1"})

                document = PurchaseDocument.objects.get()
                self.assertTrue(document.pdf_file)
                stored = Path(document.pdf_file.path)
                self.assertTrue(stored.is_file())
                self.assertEqual(stored.read_bytes(), pdf)
                self.assertTrue(stored.is_relative_to(Path(media)))        # a mounted folder, not a path inside the image


def make_db(path, rows=2):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE thing (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO thing (name) VALUES (?)", [(f"n{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


class BackupTests(SimpleTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "data" / "db.sqlite3"
        self.db.parent.mkdir()
        make_db(self.db)
        self.media = self.root / "media"
        (self.media / "purchase_documents" / "2024" / "03").mkdir(parents=True)
        (self.media / "purchase_documents" / "2024" / "03" / "a.pdf").write_bytes(b"%PDF-1 a")
        (self.media / "tmp_imports").mkdir()
        (self.media / "tmp_imports" / "t.pdf").write_bytes(b"%PDF-1 t")
        self.backups = self.root / "backups"

    def create(self, **kw):
        return backup.create_backup(self.db, self.media, self.backups, **kw)

    def test_backup_holds_database_and_every_media_file_with_checksums(self):
        path = self.create(label="manual")
        self.assertRegex(path.name, r"^backup-\d{8}-\d{6}-manual\.tar\.gz$")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with tempfile.TemporaryDirectory() as tmp:
            manifest, out = backup.verify_archive(path, tmp)
            self.assertEqual(set(manifest["files"]), {"db.sqlite3", "media/purchase_documents/2024/03/a.pdf", "media/tmp_imports/t.pdf"})
            self.assertEqual(manifest["row_counts"], {"thing": 2})
            self.assertEqual((out / "media/tmp_imports/t.pdf").read_bytes(), b"%PDF-1 t")

    def test_backup_works_while_the_database_is_open_for_writing(self):
        live = sqlite3.connect(self.db)
        live.execute("INSERT INTO thing (name) VALUES ('x')")      # uncommitted write in progress
        path = self.create()
        live.commit()
        live.close()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(backup.verify_archive(path, tmp)[0]["row_counts"], {"thing": 2})

    def test_tampered_archive_is_rejected(self):
        path = self.create()
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(path) as tar:
                tar.extractall(tmp, filter="data")
            (Path(tmp) / "media/tmp_imports/t.pdf").write_bytes(b"changed")
            bad = self.root / "bad.tar.gz"
            with tarfile.open(bad, "w:gz") as tar:
                for name in ("MANIFEST.json", "db.sqlite3", "media/purchase_documents/2024/03/a.pdf", "media/tmp_imports/t.pdf"):
                    tar.add(Path(tmp) / name, arcname=name)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesMessage(backup.BackupError, "Checksum mismatch"):
            backup.verify_archive(bad, tmp)

    def test_unexpected_archive_entries_are_rejected(self):
        evil = self.root / "evil.tar.gz"
        (self.root / "x").write_text("x")
        with tarfile.open(evil, "w:gz") as tar:
            tar.add(self.root / "x", arcname="../x")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesMessage(backup.BackupError, "Unexpected entry"):
            backup.verify_archive(evil, tmp)

    def test_restore_refuses_non_empty_targets_without_force(self):
        path = self.create()
        with self.assertRaisesMessage(backup.BackupError, "refusing to overwrite"):
            backup.restore_archive(path, self.db, self.media)

    def test_restore_into_empty_targets_and_with_force_moves_old_data_aside(self):
        path = self.create()
        fresh_db, fresh_media = self.root / "new" / "db.sqlite3", self.root / "new" / "media"
        backup.restore_archive(path, fresh_db, fresh_media)
        self.assertEqual(sqlite3.connect(fresh_db).execute("SELECT COUNT(*) FROM thing").fetchone()[0], 2)
        self.assertEqual((fresh_media / "tmp_imports/t.pdf").read_bytes(), b"%PDF-1 t")

        sqlite3.connect(self.db).execute("PRAGMA user_version")        # live data exists; now restore over it
        (self.media / "tmp_imports" / "newer.pdf").write_bytes(b"newer")
        result = backup.restore_archive(path, self.db, self.media, force=True)
        self.assertTrue(result["moved_aside"])
        kept = [p for p in self.media.rglob("newer.pdf")]
        self.assertEqual(len(kept), 1)                                  # the newer file was moved aside, not deleted
        self.assertIn("before-restore", str(kept[0]))
        self.assertFalse((self.media / "tmp_imports" / "newer.pdf").exists())

    def test_prune_keeps_recent_days_months_labelled_and_always_the_newest(self):
        now = datetime.datetime(2026, 6, 15, 12, tzinfo=datetime.timezone.utc)
        self.backups.mkdir()
        names = ["backup-20260615-031500.tar.gz", "backup-20260615-010000.tar.gz",       # same day: older one goes
                 "backup-20260610-031500.tar.gz", "backup-20260301-031500.tar.gz",        # 1 March goes: 2 March is the newest of that month
                 "backup-20260302-031500.tar.gz", "backup-20240101-031500.tar.gz"]        # kept as the monthly one; 2024 is too old
        for n in names:
            (self.backups / n).write_bytes(b"x")
        (self.backups / "notes.txt").write_text("not a backup")
        removed = {p.name for p in backup.prune(self.backups, now=now)}
        self.assertEqual(removed, {"backup-20260615-010000.tar.gz", "backup-20260301-031500.tar.gz", "backup-20240101-031500.tar.gz"})
        self.assertTrue((self.backups / "notes.txt").exists())

    def test_prune_never_removes_the_only_backup(self):
        self.backups.mkdir()
        (self.backups / "backup-20200101-031500.tar.gz").write_bytes(b"x")
        self.assertEqual(backup.prune(self.backups, now=datetime.datetime(2026, 6, 15, tzinfo=datetime.timezone.utc)), [])

    @override_settings(USING_POSTGRES=False)          # this test is about the SQLite file path (also when the suite runs on PostgreSQL)
    def test_commands_create_verify_and_restore(self):
        out = StringIO()
        call_command("backup_db", "--db", str(self.db), "--media", str(self.media), "--backup-dir", str(self.backups), "--label", "pre-deploy", "--prune", stdout=out)
        self.assertIn("BACKUP_CREATED", out.getvalue())
        name = out.getvalue().split()[1]
        out = StringIO()
        call_command("restore_backup", name, "--backup-dir", str(self.backups), stdout=out)
        self.assertIn("VERIFIED integrity=ok checksums=ok files=3 media_files=2", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("restore_backup", name, "--backup-dir", str(self.backups), "--target-db", str(self.db))   # needs --target-media
        with self.assertRaises(CommandError):
            call_command("restore_backup", name, "--backup-dir", str(self.backups), "--target-db", str(self.db), "--target-media", str(self.media))   # refuses
