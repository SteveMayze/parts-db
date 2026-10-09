from __future__ import annotations

import re
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from . import conrad_fixtures as fx
from .models import ImportedDocument, Part, PurchaseDocument, PurchaseLine
from .tests_import_preview import invoice, prefix_of, store_preview
from .tests_ui_language import ENGLISH_UI_WORDS, _extract_visible_text


class PreviewLocationTests(TestCase):
    NAME = "Ordner 3"

    def setUp(self):
        import tempfile

        from django.test import override_settings

        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)

    def preview(self, lines=None, count=1, **extra):
        docs = [
            invoice(lines or [{"description": f"Teil {i}", "no": f"20000{i}"}], number=f"960000000{i}")
            for i in range(count)
        ]
        return store_preview(self.client, docs, **extra)

    def confirm(self, **post):
        return self.client.post(reverse("parts:invoice_preview"), post)

    def part(self, number):
        return Part.objects.get(supplier_part_no=number)

    def test_location_set_per_line_goes_to_the_new_part(self):
        docs = self.preview()
        prefix = prefix_of(docs)
        response = self.confirm(
            **{f"location_type::{prefix}": "folder", f"location_name::{prefix}": self.NAME, f"location_position::{prefix}": "Seite 2"}
        )
        self.assertRedirects(response, reverse("parts:list"))
        part = self.part("200000")
        self.assertEqual((part.location_type, part.location_name, part.position), ("folder", self.NAME, "Seite 2"))

    def test_batch_default_fills_only_empty_lines_and_line_value_wins(self):
        docs = self.preview(
            [{"description": "A", "no": "200000"}, {"description": "B", "no": "200001"}, {"description": "C", "no": "200002"}]
        )
        second = prefix_of(docs, line_index=1)
        self.confirm(
            batch_location_type="drawer",
            batch_location_name="Schublade 1",
            **{f"location_type::{second}": "folder", f"location_name::{second}": "Mappe 7"},
        )
        self.assertEqual((self.part("200000").location_type, self.part("200000").location_name), ("drawer", "Schublade 1"))
        self.assertEqual((self.part("200001").location_type, self.part("200001").location_name), ("folder", "Mappe 7"))
        self.assertEqual(self.part("200002").location_name, "Schublade 1")

    def test_document_default_beats_batch_default(self):
        docs = self.preview(count=2)
        doc_id = docs[0]["document_id"]
        self.confirm(
            batch_location_type="drawer",
            batch_location_name="Schublade 1",
            **{f"doc_location_type::{doc_id}": "folder", f"doc_location_name::{doc_id}": "Mappe 2"},
        )
        self.assertEqual(self.part("200000").location_name, "Mappe 2")
        self.assertEqual(self.part("200000").location_type, "folder")
        self.assertEqual(self.part("200000").location_name, "Mappe 2")
        self.assertEqual(self.part("200000").supplier_part_no, "200000")
        self.assertEqual(Part.objects.exclude(location_name="Mappe 2").get().location_name, "Schublade 1")

    def test_matched_part_without_location_gets_the_line_location(self):
        Part.objects.create(supplier="Conrad Electronic SE", supplier_part_no="200000")
        docs = self.preview()
        prefix = prefix_of(docs)
        self.confirm(**{f"location_type::{prefix}": "folder", f"location_name::{prefix}": self.NAME})
        self.assertEqual(Part.objects.count(), 1)
        self.assertEqual(self.part("200000").location_name, self.NAME)

    def test_matched_part_with_location_is_not_overwritten_without_confirmation(self):
        Part.objects.create(
            supplier="Conrad Electronic SE", supplier_part_no="200000", quantity=5, location_type="drawer", location_name="Schublade 1"
        )
        docs = self.preview()
        prefix = prefix_of(docs)
        response = self.confirm(**{f"location_type::{prefix}": "folder", f"location_name::{prefix}": self.NAME})
        self.assertEqual(response.status_code, 200)  # Vorschau bleibt mit Fehlermeldung stehen
        self.assertContains(response, "Lagerort ändern")
        self.assertContains(response, "Schublade 1")
        part = self.part("200000")
        self.assertEqual((part.location_name, part.quantity), ("Schublade 1", 5))  # nichts geschrieben
        self.assertEqual(PurchaseLine.objects.count(), 0)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("bereits in" in m for m in messages), messages)

        response = self.confirm(
            **{f"location_type::{prefix}": "folder", f"location_name::{prefix}": self.NAME, f"change_location::{prefix}": "on"}
        )
        self.assertRedirects(response, reverse("parts:list"))
        part.refresh_from_db()
        self.assertEqual((part.location_type, part.location_name), ("folder", self.NAME))

    def test_batch_default_never_moves_a_matched_part_that_has_a_location(self):
        Part.objects.create(supplier="Conrad Electronic SE", supplier_part_no="200000", location_type="drawer", location_name="Schublade 1")
        self.preview()
        response = self.confirm(batch_location_type="folder", batch_location_name=self.NAME)
        self.assertRedirects(response, reverse("parts:list"))
        self.assertEqual(self.part("200000").location_name, "Schublade 1")

    def test_same_part_on_two_documents_a_differing_location_is_rejected(self):
        docs = store_preview(
            self.client,
            [
                invoice([{"description": "A", "no": "300000"}], number="9600000011"),
                invoice([{"description": "A", "no": "300000"}], number="9600000012"),
            ],
        )
        first, second = prefix_of(docs, 0), prefix_of(docs, 1)
        response = self.confirm(
            **{
                f"location_type::{first}": "folder",
                f"location_name::{first}": "Mappe 1",
                f"location_type::{second}": "drawer",
                f"location_name::{second}": "Schublade 9",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Part.objects.count(), 0)
        self.assertEqual(PurchaseLine.objects.count(), 0)

    def test_skipped_line_ignores_location(self):
        docs = self.preview()
        prefix = prefix_of(docs)
        response = self.confirm(
            **{f"skip::{prefix}": "on", f"location_name::{prefix}": self.NAME, f"location_type::{prefix}": "ungueltig"}
        )
        self.assertRedirects(response, reverse("parts:list"))
        self.assertEqual(Part.objects.count(), 0)

    def test_unknown_location_type_is_an_error_listing_valid_values(self):
        docs = self.preview()
        prefix = prefix_of(docs)
        response = self.confirm(**{f"location_type::{prefix}": "regal", f"location_name::{prefix}": self.NAME})
        self.assertEqual(response.status_code, 200)
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("Unbekannter Lagerort-Typ 'regal'", text)
        for valid in ("folder (Mappe)", "drawer (Schublade)", "storage_box (Sortierbox)"):
            self.assertIn(valid, text)
        self.assertEqual(Part.objects.count(), 0)

    def test_german_label_is_accepted_as_location_type(self):
        docs = self.preview()
        prefix = prefix_of(docs)
        self.confirm(**{f"location_type::{prefix}": "mappe", f"location_name::{prefix}": self.NAME})
        self.assertEqual(self.part("200000").location_type, "folder")

    def test_case_spacing_and_umlaut_variants_reuse_the_existing_spelling(self):
        Part.objects.create(part_type="X", location_type="drawer", location_name="Schublade Größe")
        docs = self.preview([{"description": "A", "no": "200000"}, {"description": "B", "no": "200001"}])
        first, second = prefix_of(docs, line_index=0), prefix_of(docs, line_index=1)
        decomposed = "schublade  größe"  # kombinierendes Umlautzeichen, Kleinschreibung, doppeltes Leerzeichen
        response = self.confirm(
            **{
                f"location_type::{first}": "drawer",
                f"location_name::{first}": decomposed,
                f"location_type::{second}": "drawer",
                f"location_name::{second}": "  SCHUBLADE GRÖSSE ",
            }
        )
        self.assertRedirects(response, reverse("parts:list"), fetch_redirect_response=False)
        names = set(Part.objects.exclude(location_name="").values_list("location_name", flat=True))
        self.assertEqual(names, {"Schublade Größe"})
        page = self.client.get(reverse("parts:list"))
        self.assertContains(page, "vorhandene Schreibweise &#x27;Schublade Größe&#x27;")

    def test_new_names_in_one_batch_share_one_spelling(self):
        docs = self.preview([{"description": "A", "no": "200000"}, {"description": "B", "no": "200001"}])
        first, second = prefix_of(docs, line_index=0), prefix_of(docs, line_index=1)
        self.confirm(**{f"location_name::{first}": "Ordner 3", f"location_name::{second}": "ordner 3"})
        self.assertEqual(set(Part.objects.values_list("location_name", flat=True)), {"Ordner 3"})

    def test_no_stock_change_mode_still_stores_location_of_new_part(self):
        docs = self.preview()
        prefix = prefix_of(docs)
        self.confirm(
            quantity_mode="no_stock_change", **{f"location_type::{prefix}": "folder", f"location_name::{prefix}": self.NAME}
        )
        part = self.part("200000")
        self.assertEqual((part.quantity, part.location_name), (0, self.NAME))

    def test_last_used_location_is_remembered_in_the_session_only(self):
        docs = self.preview()
        prefix = prefix_of(docs)
        self.confirm(**{f"location_type::{prefix}": "folder", f"location_name::{prefix}": self.NAME})
        self.assertEqual(self.client.session["last_import_location"], {"type": "folder", "name": self.NAME, "position": ""})

        rows = [fx.item("1000001", "Testartikel", "19", "1", "1,19", "1,19"), fx.netto("1,00", "19,0", "19", "0,19"), fx.legend("19", "19")]
        pdf = fx.make_layout_b_pdf("9700000077", "01.01.2020", [rows], "1,19")
        response = self.client.post(reverse("parts:import"), {"invoice_files": SimpleUploadedFile("neu.pdf", pdf, "application/pdf")})
        self.assertRedirects(response, reverse("parts:invoice_preview"))
        page = self.client.get(reverse("parts:invoice_preview"))
        self.assertContains(page, f'name="batch_location_name" value="{self.NAME}"')

    def test_preview_shows_location_inputs_current_location_and_change_checkbox(self):
        Part.objects.create(supplier="Conrad Electronic SE", supplier_part_no="200000", location_type="folder", location_name="Mappe 5")
        self.preview()
        page = self.client.get(reverse("parts:invoice_preview"))
        html = page.content.decode("utf-8")
        for text in (
            "Lagerort für alle Zeilen ohne Lagerort",
            "Lagerort-Typ",
            "Position (optional)",
            "Aktueller Lagerort",
            "Mappe 5",
            "Lagerort ändern",
        ):
            self.assertContains(page, text)
        self.assertIn("Sortierbox</option>", html)
        self.assertIn('<datalist id="known-location-names">', html)
        text = _extract_visible_text(html)
        for word in ENGLISH_UI_WORDS:
            self.assertNotRegex(text, rf"\b{re.escape(word)}\b")

    def test_failure_during_confirm_leaves_nothing_behind(self):
        existing = Part.objects.create(
            supplier="Conrad Electronic SE", supplier_part_no="200001", quantity=1, location_name="Alt", location_type="drawer"
        )
        docs = self.preview([{"description": "A", "no": "200000"}, {"description": "B", "no": "200001"}])
        first, second = prefix_of(docs, line_index=0), prefix_of(docs, line_index=1)
        calls = {"count": 0}
        real_create = PurchaseLine.objects.create

        def failing_create(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("Testfehler")
            return real_create(*args, **kwargs)

        with patch.object(PurchaseLine.objects, "create", side_effect=failing_create):
            with self.assertRaises(RuntimeError):
                self.confirm(**{f"location_type::{first}": "folder", f"location_name::{first}": "Neu", f"location_name::{second}": "Alt"})
        self.assertEqual(Part.objects.count(), 1)
        self.assertEqual(PurchaseLine.objects.count(), 0)
        self.assertEqual(PurchaseDocument.objects.count(), 0)
        self.assertEqual(ImportedDocument.objects.count(), 0)
        existing.refresh_from_db()
        self.assertEqual((existing.quantity, existing.location_name), (1, "Alt"))

    def test_temp_pdf_survives_a_rejected_confirm_and_is_removed_after_success(self):
        import tempfile
        from pathlib import Path

        from django.test import override_settings

        with tempfile.TemporaryDirectory() as media:
            temp = Path(media) / "beleg.pdf"
            temp.write_bytes(b"%PDF-1.4 test")
            Part.objects.create(supplier="Conrad Electronic SE", supplier_part_no="200000", location_type="drawer", location_name="Schublade 1")
            docs = store_preview(
                self.client, [invoice([{"description": "A", "no": "200000"}], number="9600000090", pdf_temp_path=str(temp), pdf_original_name="beleg.pdf")]
            )
            prefix = prefix_of(docs)
            with override_settings(MEDIA_ROOT=media):
                self.confirm(**{f"location_type::{prefix}": "folder", f"location_name::{prefix}": self.NAME})
                self.assertTrue(temp.exists())
                self.assertEqual(PurchaseDocument.objects.count(), 0)
                self.assertEqual([p.name for p in Path(media).rglob("*.pdf")], ["beleg.pdf"])  # nur die Temp-Datei

                self.confirm(
                    **{f"location_type::{prefix}": "folder", f"location_name::{prefix}": self.NAME, f"change_location::{prefix}": "on"}
                )
                self.assertFalse(temp.exists())
                self.assertTrue(PurchaseDocument.objects.get().pdf_file)

    def test_rejected_confirm_keeps_typed_values_and_can_be_repeated(self):
        Part.objects.create(supplier="Conrad Electronic SE", supplier_part_no="200000", location_type="drawer", location_name="Schublade 1")
        docs = self.preview()
        prefix = prefix_of(docs)
        response = self.confirm(
            **{
                f"location_type::{prefix}": "folder",
                f"location_name::{prefix}": self.NAME,
                f"description::{prefix}": "Von Hand geändert",
                f"quantity::{prefix}": "7",
            }
        )
        self.assertContains(response, "Von Hand geändert")
        self.assertContains(response, 'value="7"')
        self.assertContains(response, f'value="{self.NAME}"')
        response = self.confirm(
            **{
                f"location_type::{prefix}": "folder",
                f"location_name::{prefix}": self.NAME,
                f"change_location::{prefix}": "on",
                f"quantity::{prefix}": "7",
            }
        )
        self.assertRedirects(response, reverse("parts:list"))
        self.assertEqual(self.part("200000").quantity, 7)
