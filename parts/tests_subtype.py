from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from .models import Part


class PartSubtypeModelTests(TestCase):
    def test_subtype_is_optional_and_defaults_to_empty(self):
        part = Part.objects.create(part_type="Widerstand", value="10k")
        part.refresh_from_db()
        self.assertEqual(part.part_subtype, "")

    def test_type_and_subtype_are_stripped_on_save_but_case_is_kept(self):
        part = Part.objects.create(part_type="  Diode ", part_subtype="  Zener\t")
        part.refresh_from_db()
        self.assertEqual(part.part_type, "Diode")
        self.assertEqual(part.part_subtype, "Zener")

    def test_str_includes_subtype_when_set(self):
        part = Part(part_type="Diode", part_subtype="Zener", value="5V1", packaging="SOD-123")
        self.assertEqual(str(part), "Diode/Zener 5V1 (SOD-123)")
        self.assertEqual(str(Part(part_type="Diode", value="1N4148", packaging="DO-35")), "Diode 1N4148 (DO-35)")

    def test_default_ordering_is_type_then_subtype(self):
        Part.objects.create(part_type="Diode", part_subtype="Zener", value="a")
        Part.objects.create(part_type="Diode", part_subtype="TVS", value="b")
        Part.objects.create(part_type="Diode", value="c")
        Part.objects.create(part_type="Diode", part_subtype="Photodiode", value="d")
        self.assertEqual(
            list(Part.objects.values_list("part_subtype", flat=True)),
            ["", "Photodiode", "TVS", "Zener"],
        )


class PartSubtypeMigrationTests(TransactionTestCase):
    migrate_from = [("parts", "0007_purchasedocument_discount_total")]
    migrate_to = [("parts", "0008_part_part_subtype")]

    def test_migration_is_reversible_and_keeps_existing_parts(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldPart = old_apps.get_model("parts", "Part")
        self.assertFalse(any(f.name == "part_subtype" for f in OldPart._meta.get_fields()))
        OldPart.objects.create(part_type="Zener", value="5V1")

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        NewPart = new_apps.get_model("parts", "Part")
        part = NewPart.objects.get(part_type="Zener")
        self.assertEqual(part.part_subtype, "")

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
