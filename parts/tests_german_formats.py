from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import Part, Project
from .templatetags.german import de_date, de_datetime, de_number, de_plain, eur2, eur4
from .tests_conrad import LAYOUT_A_TEXT, import_batch, import_parsed
from .tests_conrad_layouts import discount_doc, parse
from .tests_ui_language import _extract_visible_text

EUR = " €"
PAGE_EUR = " €"  # im sichtbaren Seitentext sind geschützte Leerzeichen zusammengefasst
ENGLISH_DECIMAL = re.compile(r"(?<![\w.])\d+\.(?:\d{1,2}|\d{4})(?![\w.])")  # 1.000 ist deutsche Tausenderschreibweise
ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


class GermanFilterTests(SimpleTestCase):
    def test_prices_and_totals(self):
        self.assertEqual(eur4("4.5172"), f"4,5172{EUR}")
        self.assertEqual(eur4(Decimal("0.2672")), f"0,2672{EUR}")
        self.assertEqual(eur2("17.04"), f"17,04{EUR}")
        self.assertEqual(eur2(Decimal("1234.5")), f"1.234,50{EUR}")
        self.assertEqual(eur2("-7.77"), f"-7,77{EUR}")
        self.assertEqual(eur2("10", "CHF"), "10,00 CHF")
        self.assertEqual(eur2(""), "-")
        self.assertEqual(eur4(None), "-")

    def test_numbers_and_rates(self):
        self.assertEqual(de_number("1234.567", 2), "1.234,57")
        self.assertEqual(de_plain("16"), "16")
        self.assertEqual(de_plain("16.0"), "16")
        self.assertEqual(de_plain("5.5"), "5,5")

    def test_dates(self):
        self.assertEqual(de_date("2020-12-10"), "10.12.2020")
        self.assertEqual(de_date(date(2020, 12, 10)), "10.12.2020")
        self.assertEqual(de_date(None), "-")
        self.assertEqual(de_datetime("2020-12-10T14:30:00+00:00"), "10.12.2020 14:30")


class GermanFormatPagesTests(TestCase):
    def setUp(self):
        import tempfile

        from django.test import override_settings

        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        override = override_settings(MEDIA_ROOT=media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.project = Project.objects.create(name="Testprojekt", client="Kunde")
        import_batch(self.client, [LAYOUT_A_TEXT])
        self.client.post(reverse("parts:invoice_preview"), {"batch_project_id": str(self.project.pk)})
        import_parsed(self.client, [parse(discount_doc(), "rabatt.pdf")])

    def assert_german(self, response, label):
        self.assertEqual(response.status_code, 200, label)
        text = _extract_visible_text(response.content.decode("utf-8"))
        self.assertIsNone(ENGLISH_DECIMAL.search(text), f"{label}: englischer Dezimalpunkt: {ENGLISH_DECIMAL.search(text)}")
        self.assertIsNone(ISO_DATE.search(text), f"{label}: ISO-Datum: {ISO_DATE.search(text)}")
        return text

    def test_import_preview(self):
        text = self.assert_german(self.client.get(reverse("parts:invoice_preview")), "Vorschau")
        self.assertIn("26.05.2020", text)
        self.assertIn(f"91,21{PAGE_EUR}", text)
        self.assertIn(f"-7,77{PAGE_EUR}", text)
        self.assertIn(f"8,3950{PAGE_EUR}", text)  # Nettopreis mit vier Nachkommastellen
        self.assertIn(f"9,9900{PAGE_EUR}", text)
        self.assertIn("19 %", text)

    def test_related_pages(self):
        self.client.post(reverse("parts:invoice_preview"), {"batch_project_id": str(self.project.pk)})
        part = Part.objects.get(supplier_part_no="1258976")
        document = part.purchase_lines.get().document

        text = self.assert_german(self.client.get(reverse("parts:documents")), "Belegliste")
        self.assertIn("18.06.2024", text)

        text = self.assert_german(self.client.get(reverse("parts:document_detail", args=[document.pk])), "Belegseite")
        self.assertIn(f"11,40{PAGE_EUR}", text)
        self.assertIn(f"0,1100{PAGE_EUR}", text)  # Einzelpreis
        self.assertIn(f"5,50{PAGE_EUR}", text)  # Zeilensumme

        text = self.assert_german(self.client.get(reverse("parts:detail", args=[part.pk])), "Kaufhistorie")
        self.assertIn("18.06.2024", text)
        self.assertIn(f"0,1100{PAGE_EUR}", text)

        text = self.assert_german(
            self.client.get(reverse("parts:project_documents"), {"project": str(self.project.pk)}), "Projektansicht"
        )
        self.assertIn(f"11,40{PAGE_EUR}", text)  # Summe Brutto
        self.assertIn("18.06.2024", text)

        self.assert_german(self.client.get(reverse("parts:list")), "Bauteileliste")

    def test_real_conrad_and_reichelt_samples_render_german_previews(self):
        from pathlib import Path

        from django.core.files.uploadedfile import SimpleUploadedFile

        for folder in ("conrad", "reichelt"):
            files = sorted(Path("samples", folder).glob("*.pdf"))[:5]
            if not files:
                self.skipTest("Beispielbelege nicht vorhanden")
            upload = [SimpleUploadedFile(path.name, path.read_bytes(), "application/pdf") for path in files]
            response = self.client.post(reverse("parts:import"), {"invoice_files": upload})
            self.assertRedirects(response, reverse("parts:invoice_preview"), fetch_redirect_response=False)
            text = self.assert_german(self.client.get(reverse("parts:invoice_preview")), f"Vorschau {folder}")
            self.assertRegex(text, r"\d{2}\.\d{2}\.\d{4}")
            self.assertRegex(text, r"\d+,\d{4} €")

    def test_import_result_page_after_confirm(self):
        response = self.client.post(reverse("parts:invoice_preview"), {}, follow=True)
        self.assert_german(response, "Importergebnis")
        self.assertContains(response, "Import abgeschlossen.")

    def test_editable_part_form_shows_and_parses_german_formats(self):
        part = Part.objects.create(part_type="IC", last_price=Decimal("0.0924"), last_purchased=date(2020, 12, 10))
        response = self.client.get(reverse("parts:edit", args=[part.pk]))
        html = response.content.decode("utf-8")
        self.assertIn('value="0,0924"', html)
        self.assertIn('value="10.12.2020"', html)
        data = {
            "part_type": "IC", "value": "", "packaging": "", "description": "", "manufacturer": "", "mf_part_no": "",
            "supplier": "", "supplier_part_no": "", "last_price": "1,2345", "currency": "EUR", "last_purchased": "11.12.2020",
            "last_document_no": "", "quantity": "1", "location_type": "", "location_name": "", "position": "", "comment": "",
        }
        response = self.client.post(reverse("parts:edit", args=[part.pk]), data)
        self.assertEqual(response.status_code, 302)
        part.refresh_from_db()
        self.assertEqual((part.last_price, part.last_purchased), (Decimal("1.2345"), date(2020, 12, 11)))
