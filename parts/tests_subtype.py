from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

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


class PartSubtypeFormTests(TestCase):
    def setUp(self):
        Part.objects.create(part_type="Diode", part_subtype="Zener", value="5V1")
        Part.objects.create(part_type="Diode", part_subtype="TVS", value="SMBJ5.0")
        Part.objects.create(part_type="LED", value="rot")

    def _payload(self, **extra):
        data = {
            "part_type": "Diode",
            "part_subtype": "Photodiode",
            "value": "BPW34",
            "quantity": "3",
            "currency": "EUR",
            "last_price": "0,00",
        }
        data.update(extra)
        return data

    def test_form_accepts_missing_subtype(self):
        from .forms import PartForm

        data = self._payload()
        del data["part_subtype"]
        form = PartForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["part_subtype"], "")

    def test_create_with_subtype_strips_whitespace(self):
        response = self.client.post(reverse("parts:add"), self._payload(part_subtype="  Photodiode "))
        self.assertRedirects(response, reverse("parts:list"))
        self.assertTrue(Part.objects.filter(part_type="Diode", part_subtype="Photodiode", value="BPW34").exists())

    def test_update_can_set_subtype_on_existing_part(self):
        part = Part.objects.get(part_type="LED")
        response = self.client.post(reverse("parts:edit", args=[part.pk]), self._payload(part_type="LED", part_subtype="IR", value="rot"))
        self.assertRedirects(response, reverse("parts:list"))
        part.refresh_from_db()
        self.assertEqual(part.part_subtype, "IR")

    def test_form_page_offers_existing_types_and_subtypes_as_datalists(self):
        response = self.client.get(reverse("parts:add"))
        self.assertContains(response, 'id="known-part-types"')
        self.assertContains(response, 'id="known-part-subtypes"')
        self.assertContains(response, '<option value="Zener">')
        self.assertContains(response, '<option value="TVS">')
        self.assertEqual(response.context["subtypes_by_type"], {"diode": ["TVS", "Zener"]})
        self.assertContains(response, 'list="known-part-subtypes"')


class PartSubtypeListTests(TestCase):
    def setUp(self):
        self.zener = Part.objects.create(part_type="Diode", part_subtype="Zener", value="BZX84-5V1")
        self.tvs = Part.objects.create(part_type="Diode", part_subtype="TVS", value="SMBJ5.0A")
        self.plain = Part.objects.create(part_type="Diode", value="1N4148")
        self.led = Part.objects.create(part_type="LED", part_subtype="Zener-ähnlich", value="LED-rot")

    def _list(self, **params):
        response = self.client.get(reverse("parts:list"), params)
        self.assertEqual(response.status_code, 200)
        return response, list(response.context["parts"])

    def test_list_shows_subtype_column_and_keeps_parts_without_subtype(self):
        response, parts = self._list()
        self.assertContains(response, "Untertyp")
        self.assertIn(self.plain, parts)
        self.assertEqual(len(parts), 4)

    def test_filter_by_type_then_subtype(self):
        _, parts = self._list(part_type="Diode", part_subtype="TVS")
        self.assertEqual(parts, [self.tvs])

    def test_subtype_filter_is_case_insensitive(self):
        _, parts = self._list(part_type="diode", part_subtype="zener")
        self.assertEqual(parts, [self.zener])

    def test_search_matches_subtype_case_insensitively(self):
        _, parts = self._list(q="zener")
        self.assertCountEqual(parts, [self.zener, self.led])
        _, parts = self._list(q="TVS")
        self.assertEqual(parts, [self.tvs])

    def test_subtype_options_follow_selected_type(self):
        response, _ = self._list(part_type="Diode")
        self.assertCountEqual(response.context["part_subtypes"], ["Zener", "TVS"])
        response, _ = self._list()
        self.assertCountEqual(response.context["part_subtypes"], ["Zener", "TVS", "Zener-ähnlich"])

    def test_sort_by_type_uses_subtype_as_tie_breaker(self):
        _, parts = self._list(sort="type", dir="asc")
        self.assertEqual([p.pk for p in parts], [self.plain.pk, self.tvs.pk, self.zener.pk, self.led.pk])

    def test_detail_page_shows_subtype(self):
        response = self.client.get(reverse("parts:detail", args=[self.zener.pk]))
        self.assertContains(response, "Untertyp:")
        self.assertContains(response, "Zener")


class PartAdminActionTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(user)
        self.zener = Part.objects.create(part_type="Zener", value="5V1")
        self.tvs = Part.objects.create(part_type="TVS", part_subtype="alt", value="SMBJ")
        self.url = reverse("admin:parts_part_changelist")

    def _post(self, parts, **fields):
        data = {"action": "set_classification", "_selected_action": [p.pk for p in parts], "apply": "1"}
        data.update(fields)
        return self.client.post(self.url, data)

    def test_action_shows_confirmation_form_first(self):
        response = self.client.post(self.url, {"action": "set_classification", "_selected_action": [self.zener.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Untertyp")
        self.zener.refresh_from_db()
        self.assertEqual(self.zener.part_type, "Zener")

    def test_action_sets_type_and_subtype_keeping_case_and_stripping(self):
        response = self._post([self.zener], part_type=" Diode ", part_subtype=" Zener ")
        self.assertEqual(response.status_code, 302)
        self.zener.refresh_from_db()
        self.assertEqual((self.zener.part_type, self.zener.part_subtype), ("Diode", "Zener"))

    def test_blank_fields_leave_values_unchanged_and_clear_option_clears(self):
        self._post([self.tvs], part_type="Diode", part_subtype="")
        self.tvs.refresh_from_db()
        self.assertEqual((self.tvs.part_type, self.tvs.part_subtype), ("Diode", "alt"))
        self._post([self.tvs], clear_subtype="on")
        self.tvs.refresh_from_db()
        self.assertEqual((self.tvs.part_type, self.tvs.part_subtype), ("Diode", ""))

    def test_admin_changelist_and_filter_load(self):
        response = self.client.get(self.url, {"part_subtype": "alt", "q": "tvs"})
        self.assertEqual(response.status_code, 200)


class PartSubtypeCsvTests(TestCase):
    HEADER_WITH = "part_type;part_subtype;value;quantity\n"
    HEADER_WITHOUT = "part_type;value;quantity\n"

    def _apply(self, csv_text, mode=None):
        from .stock_csv import IMPORT_MODE_UPDATE, apply_stock_preview, parse_csv_bytes, prepare_stock_preview

        parsed = parse_csv_bytes(csv_text.encode("utf-8"))
        preview = prepare_stock_preview(parsed, mode=mode or IMPORT_MODE_UPDATE)
        return apply_stock_preview(preview)

    def test_csv_without_subtype_column_still_imports(self):
        self.assertEqual(self._apply(self.HEADER_WITHOUT + "Diode;1N4148;5\n"), (1, 0))
        self.assertEqual(Part.objects.get(value="1N4148").part_subtype, "")

    def test_csv_with_subtype_column_and_german_alias(self):
        self.assertEqual(self._apply(self.HEADER_WITH + "Diode;Zener;5V1;3\nDiode;;1N4148;4\n"), (2, 0))
        self.assertEqual(Part.objects.get(value="5V1").part_subtype, "Zener")
        self.assertEqual(Part.objects.get(value="1N4148").part_subtype, "")
        self._apply("Typ;Untertyp;Wert;Menge\nLED;IR;940nm;2\n")
        self.assertEqual(Part.objects.get(value="940nm").part_subtype, "IR")

    def test_blank_subtype_does_not_clear_existing_value_on_update(self):
        Part.objects.create(part_type="Diode", part_subtype="Zener", value="5V1", quantity=1)
        self.assertEqual(self._apply(self.HEADER_WITH + "Diode;;5V1;9\n"), (0, 1))
        part = Part.objects.get(value="5V1")
        self.assertEqual((part.part_subtype, part.quantity), ("Zener", 9))

    def test_update_can_set_subtype_on_existing_part(self):
        Part.objects.create(part_type="Diode", value="5V1", quantity=1)
        self._apply(self.HEADER_WITH + "Diode;Zener;5V1;1\n")
        self.assertEqual(Part.objects.get(value="5V1").part_subtype, "Zener")

    def test_export_includes_subtype_column(self):
        Part.objects.create(part_type="Diode", part_subtype="Zener", value="5V1")
        response = self.client.get(reverse("parts:stock_csv_export"))
        lines = response.content.decode("utf-8").lstrip("\ufeff").splitlines()
        self.assertEqual(lines[0].split(";")[:3], ["id", "part_type", "part_subtype"])
        self.assertEqual(lines[1].split(";")[1:4], ["Diode", "Zener", "5V1"])

    def test_template_download_has_subtype_column(self):
        response = self.client.get(reverse("parts:stock_csv_template"))
        self.assertIn("part_subtype", response.content.decode("utf-8").lstrip("\ufeff").splitlines()[0])
