from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pdfplumber
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from parsers.conrad import ConradInvoiceParser
from parsers.formats import canonicalise_match_value, parse_date, parse_decimal
from parsers.reichelt import ReicheltInvoiceParser
from .forms import InvoiceUploadForm
from .models import ImportedDocument, Part, Project, PurchaseDocument, PurchaseLine, Supplier
from .stock_csv import IMPORT_MODE_ADD, IMPORT_MODE_SKIP, IMPORT_MODE_UPDATE, parse_csv_bytes, prepare_stock_preview
from .views import QUANTITY_MODE_KEEP, _build_batch_preview, _validate_single_supplier, build_invoice_preview


class PartModelTests(TestCase):
    def test_full_location_includes_position_when_present(self):
        part = Part.objects.create(
            part_type="Resistor",
            value="10k",
            packaging="0805",
            quantity=10,
            location_type=Part.LocationType.FOLDER,
            location_name="Folder 2",
            position="page 3, pocket 1",
        )
        self.assertEqual(part.full_location, "Mappe / Folder 2 / page 3, pocket 1")

    def test_full_location_omits_position_when_blank(self):
        part = Part.objects.create(
            part_type="Capacitor",
            value="0.1uF",
            packaging="0805",
            quantity=10,
            location_type=Part.LocationType.DRAWER,
            location_name="Drawer unit A",
            position="",
        )
        self.assertEqual(part.full_location, "Schublade / Drawer unit A")

    def test_casefold_and_nfc_normalisation_for_search_and_values(self):
        part = Part.objects.create(
            part_type="Gehäuse",
            value="0.1µF",
            packaging="0805",
            quantity=2,
            location_type=Part.LocationType.FOLDER,
            location_name="Folder 2",
        )
        self.assertEqual(canonicalise_match_value("GEHÄUSE"), canonicalise_match_value(part.part_type))
        self.assertIn("µ", part.value)
        self.assertEqual(parse_decimal("1.234,56", locale="de"), Decimal("1234.56"))
        self.assertEqual(parse_date("09.12.2021", locale="de").isoformat(), "2021-12-09")


class PartListViewTests(TestCase):
    def setUp(self):
        self.matching = Part.objects.create(
            part_type="IC",
            value="STM32F103C8T6",
            packaging="LQFP-48",
            description="ARM microcontroller",
            manufacturer="STMicroelectronics",
            mf_part_no="STM32F103C8T6",
            quantity=8,
            location_type=Part.LocationType.STORAGE_BOX,
            location_name="Blue storage box",
            position="slot B4",
        )
        self.other = Part.objects.create(
            part_type="Resistor",
            value="10k",
            packaging="0805",
            description="General resistor",
            manufacturer="Yageo",
            mf_part_no="RC0805JR-0710KL",
            quantity=100,
            location_type=Part.LocationType.FOLDER,
            location_name="Folder 1",
            position="page 1, pocket 2",
        )

    def test_list_page_loads(self):
        response = self.client.get(reverse("parts:list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "parts/part_list.html")

    def test_text_search_matches_multiple_fields(self):
        response = self.client.get(reverse("parts:list"), {"q": "STM32"})
        self.assertContains(response, self.matching.value)
        self.assertNotContains(response, self.other.value)

        response = self.client.get(reverse("parts:list"), {"q": "STMicroelectronics"})
        self.assertContains(response, self.matching.mf_part_no)
        self.assertNotContains(response, self.other.mf_part_no)

    def test_filter_by_part_type_and_packaging(self):
        Part.objects.create(
            part_type="IC",
            value="ATmega328P",
            packaging="TQFP-32",
            quantity=5,
            location_type=Part.LocationType.DRAWER,
            location_name="Drawer unit A",
            position="drawer 2",
        )
        response = self.client.get(reverse("parts:list"), {"part_type": "IC", "packaging": "LQFP-48"})
        self.assertContains(response, "STM32F103C8T6")
        self.assertNotContains(response, "ATmega328P")
        self.assertNotContains(response, "10k")

    def test_sort_by_type_ascending_and_descending(self):
        connector = Part.objects.create(
            part_type="Connector",
            value="HDR-20",
            packaging="Pinheader",
            quantity=3,
            location_type=Part.LocationType.FOLDER,
            location_name="Folder 3",
            position="pocket 1",
        )
        response = self.client.get(reverse("parts:list"), {"sort": "type", "dir": "asc"})
        ordering = [part.pk for part in response.context["parts"]]
        self.assertEqual(ordering[:3], [connector.pk, self.matching.pk, self.other.pk])

        response = self.client.get(reverse("parts:list"), {"sort": "type", "dir": "desc"})
        ordering = [part.pk for part in response.context["parts"]]
        self.assertEqual(ordering[:3], [self.other.pk, self.matching.pk, connector.pk])

    def test_sort_by_value_ascending_and_descending(self):
        resistor = Part.objects.create(
            part_type="Widerstand",
            value="10k",
            packaging="0603",
            description="Test resistor",
            quantity=2,
            location_type=Part.LocationType.DRAWER,
            location_name="Drawer unit A",
            position="drawer 2",
        )
        capacitor = Part.objects.create(
            part_type="Kondensator",
            value="100nF",
            packaging="0603",
            description="Test capacitor",
            quantity=3,
            location_type=Part.LocationType.DRAWER,
            location_name="Drawer unit A",
            position="drawer 3",
        )
        inductor = Part.objects.create(
            part_type="Spule",
            value="2,2µH",
            packaging="0603",
            description="Test inductor",
            quantity=4,
            location_type=Part.LocationType.DRAWER,
            location_name="Drawer unit A",
            position="drawer 4",
        )

        response = self.client.get(reverse("parts:list"), {"sort": "value", "dir": "asc"})
        ordering = [part.pk for part in response.context["parts"] if part.description in {"Test resistor", "Test capacitor", "Test inductor"}]
        self.assertEqual(ordering, [inductor.pk, resistor.pk, capacitor.pk])

        response = self.client.get(reverse("parts:list"), {"sort": "value", "dir": "desc"})
        ordering = [part.pk for part in response.context["parts"] if part.description in {"Test resistor", "Test capacitor", "Test inductor"}]
        self.assertEqual(ordering, [capacitor.pk, resistor.pk, inductor.pk])

    def test_sort_by_description_uses_casefold_and_umlauts(self):
        zener = Part.objects.create(
            part_type="Widerstand",
            value="1k",
            packaging="0603",
            description="Zener",
            quantity=2,
            location_type=Part.LocationType.DRAWER,
            location_name="Drawer unit A",
            position="drawer 2",
        )
        aermel = Part.objects.create(
            part_type="Widerstand",
            value="2k",
            packaging="0603",
            description="ärmel",
            quantity=3,
            location_type=Part.LocationType.DRAWER,
            location_name="Drawer unit A",
            position="drawer 3",
        )
        widerstand = Part.objects.create(
            part_type="Widerstand",
            value="3k",
            packaging="0603",
            description="Widerstand",
            quantity=4,
            location_type=Part.LocationType.DRAWER,
            location_name="Drawer unit A",
            position="drawer 4",
        )
        response = self.client.get(reverse("parts:list"), {"sort": "description", "dir": "asc"})
        pks = [part.pk for part in response.context["parts"] if part.description in {"ärmel", "Widerstand", "Zener"}]
        self.assertEqual(pks, [aermel.pk, widerstand.pk, zener.pk])

        response = self.client.get(reverse("parts:list"), {"sort": "description", "dir": "desc"})
        pks = [part.pk for part in response.context["parts"] if part.description in {"ärmel", "Widerstand", "Zener"}]
        self.assertEqual(pks, [zener.pk, widerstand.pk, aermel.pk])

    def test_sort_by_location_uses_name_position_then_type(self):
        Part.objects.create(
            part_type="Resistor",
            value="4.7k",
            packaging="0805",
            description="Test part",
            quantity=1,
            location_type=Part.LocationType.FOLDER,
            location_name="Ordner 10",
            position="Seite 12",
        )
        Part.objects.create(
            part_type="Resistor",
            value="4.7k",
            packaging="0805",
            description="Test part",
            quantity=1,
            location_type=Part.LocationType.FOLDER,
            location_name="Ordner 2",
            position="Seite 3",
        )
        Part.objects.create(
            part_type="Resistor",
            value="4.7k",
            packaging="0805",
            description="Test part",
            quantity=1,
            location_type=Part.LocationType.DRAWER,
            location_name="Ordner 2",
            position="Seite 3",
        )
        response = self.client.get(reverse("parts:list"), {"sort": "location", "dir": "asc"})
        locations = [part.full_location for part in response.context["parts"]]
        self.assertTrue(locations.index("Mappe / Ordner 2 / Seite 3") < locations.index("Mappe / Ordner 10 / Seite 12"))
        self.assertTrue(locations.index("Mappe / Ordner 2 / Seite 3") < locations.index("Schublade / Ordner 2 / Seite 3"))

    def test_empty_values_are_last_in_both_directions(self):
        empty = Part.objects.create(
            part_type="Resistor",
            value="4.7k",
            packaging="0805",
            description="",
            quantity=1,
            location_type=Part.LocationType.FOLDER,
            location_name="Folder 9",
            position="pocket 1",
        )
        response = self.client.get(reverse("parts:list"), {"sort": "description", "dir": "asc"})
        self.assertEqual(response.context["parts"][-1].pk, empty.pk)

        response = self.client.get(reverse("parts:list"), {"sort": "description", "dir": "desc"})
        self.assertEqual(response.context["parts"][-1].pk, empty.pk)

    def test_header_links_preserve_search_filters_and_reset_page_number(self):
        response = self.client.get(
            reverse("parts:list"),
            {"q": "stm32", "part_type": "IC", "packaging": "LQFP-48", "page": "4", "sort": "type", "dir": "asc"},
        )
        html = response.content.decode()
        self.assertIn("q=stm32", html)
        self.assertIn("part_type=IC", html)
        self.assertIn("packaging=LQFP-48", html)
        self.assertIn("page=1", html)
        self.assertEqual(html.count('aria-sort="ascending"'), 1)
        self.assertEqual(html.count("▲"), 1)
        self.assertFalse("aria-sort=\"descending\"" in html)

    def test_invalid_sort_and_dir_fall_back_safely(self):
        response = self.client.get(reverse("parts:list"), {"sort": "unknown", "dir": "invalid"})
        ordering = [part.pk for part in response.context["parts"]]
        expected = [part.pk for part in Part.objects.order_by("part_type", "value", "packaging", "location_name")]
        self.assertEqual(ordering, expected)


class PartCrudViewTests(TestCase):
    def setUp(self):
        self.part = Part.objects.create(
            part_type="Transistor",
            value="2N3904",
            packaging="THT",
            quantity=60,
            location_type=Part.LocationType.DRAWER,
            location_name="Drawer unit A",
            position="small drawer 7",
        )

    def test_create_part(self):
        response = self.client.post(
            reverse("parts:add"),
            {
                "part_type": "Capacitor",
                "value": "1uF",
                "packaging": "0805",
                "description": "Ceramic capacitor",
                "manufacturer": "Murata",
                "mf_part_no": "GRM21",
                "quantity": 50,
                "location_type": Part.LocationType.FOLDER,
                "location_name": "Folder 3",
                "position": "page 2, pocket 4",
                "comment": "From mixed lot",
            },
        )
        self.assertRedirects(response, reverse("parts:list"))
        self.assertTrue(Part.objects.filter(part_type="Capacitor", value="1uF", location_name="Folder 3").exists())

    def test_update_part(self):
        response = self.client.post(
            reverse("parts:edit", args=[self.part.pk]),
            {
                "part_type": self.part.part_type,
                "value": self.part.value,
                "packaging": self.part.packaging,
                "description": self.part.description,
                "manufacturer": self.part.manufacturer,
                "mf_part_no": self.part.mf_part_no,
                "quantity": 42,
                "location_type": self.part.location_type,
                "location_name": "Drawer unit B",
                "position": self.part.position,
                "comment": self.part.comment,
            },
        )
        self.assertRedirects(response, reverse("parts:list"))
        self.part.refresh_from_db()
        self.assertEqual(self.part.quantity, 42)
        self.assertEqual(self.part.location_name, "Drawer unit B")

    def test_delete_part(self):
        response = self.client.post(reverse("parts:delete", args=[self.part.pk]))
        self.assertRedirects(response, reverse("parts:list"))
        self.assertFalse(Part.objects.filter(pk=self.part.pk).exists())


class InvoiceUploadFormTests(TestCase):
    def test_form_accepts_multiple_pdf_files(self):
        file_a = SimpleUploadedFile("a.pdf", b"%PDF-1.7", content_type="application/pdf")
        file_b = SimpleUploadedFile("b.pdf", b"%PDF-1.7", content_type="application/pdf")
        form = InvoiceUploadForm(data={"supplier": ""}, files={"invoice_files": [file_a, file_b]})
        self.assertTrue(form.is_valid(), form.errors)


class ReicheltParserTests(TestCase):
    def setUp(self):
        self.parser = ReicheltInvoiceParser()

    def _word(self, text, x0, top):
        return {"text": text, "x0": float(x0), "top": float(top)}

    def _synthetic_pages(self):
        words = [
            self._word("Artikel-Nr.", 10, 100),
            self._word("Bezeichnung", 120, 100),
            self._word("WHG", 320, 100),
            self._word("Menge", 360, 100),
            self._word("Einzelpreis", 420, 100),
            self._word("Gesamtpreis", 500, 100),
            self._word("RND", 10, 120),
            self._word("0805", 40, 120),
            self._word("1", 70, 120),
            self._word("2,4K", 90, 120),
            self._word("SMD", 120, 120),
            self._word("Widerstand", 150, 120),
            self._word("1", 320, 120),
            self._word("50", 360, 120),
            self._word("0,01", 420, 120),
            self._word("€", 455, 120),
            self._word("0,55", 500, 120),
            self._word("€", 535, 120),
            self._word("Fortsetzung", 120, 136),
            self._word("Text", 180, 136),
            self._word("ABC-123", 10, 154),
            self._word("Drahtbrücken", 120, 154),
            self._word("0", 320, 154),
            self._word("1", 360, 154),
            self._word("12,94", 420, 154),
            self._word("€", 460, 154),
            self._word("12,94", 500, 154),
            self._word("€", 540, 154),
            self._word("Warenwert", 10, 190),
        ]
        text = (
            "reichelt elektronik GmbH & Co. KG\n"
            "Eingangsbestätigung der Bestellung (I-306393, P1009726)\n"
            "Datum: 26.01.2017\n"
            "Warenwert : 13,49 €\n"
            "(11,34 € netto)\n"
        )
        return [{"text": text, "words": words}]

    def test_parses_article_number_with_spaces_and_decimal_comma(self):
        parsed = self.parser.parse_pages(self._synthetic_pages(), filename="synth.pdf")
        self.assertEqual(parsed["invoice_no"], "I-306393")
        self.assertEqual(parsed["reference_note"], "P1009726")
        self.assertEqual(parsed["lines"][0]["supplier_part_no"], "RND 0805 1 2,4K")
        self.assertEqual(parsed["lines"][0]["description"], "SMD Widerstand Fortsetzung Text")
        self.assertEqual(parsed["lines"][0]["quantity"], 50)
        self.assertEqual(parsed["lines"][0]["unit_price"], Decimal("0.0092"))
        self.assertEqual(parsed["gross_total"], Decimal("13.49"))
        self.assertEqual(parsed["net_total"], Decimal("11.34"))
        self.assertIn("WHG-Werte unterscheiden sich", parsed["whg_note"])

    def test_unsupported_document_type_raises(self):
        pages = [{"text": "reichelt elektronik GmbH & Co. KG\nRechnung (I-123)\nDatum: 01.01.2020", "words": []}]
        with self.assertRaisesMessage(ValueError, "Nicht unterstützter Reichelt-Belegtyp"):
            self.parser.parse_pages(pages)

    def test_missing_i_number_raises(self):
        pages = [{"text": "reichelt elektronik GmbH & Co. KG\nEingangsbestätigung der Bestellung (P1009726)\nDatum: 01.01.2020", "words": []}]
        with self.assertRaisesMessage(ValueError, "Präfix 'I-'"):
            self.parser.parse_pages(pages)

    def test_invalid_vat_factor_raises(self):
        pages = self._synthetic_pages()
        pages[0]["text"] = pages[0]["text"].replace("(11,34 € netto)", "(10,00 € netto)")
        with self.assertRaisesMessage(ValueError, "MwSt-Faktor"):
            self.parser.parse_pages(pages)

    def test_non_eur_currency_raises(self):
        pages = self._synthetic_pages()
        pages[0]["words"] = [w.copy() for w in pages[0]["words"]]
        for word in pages[0]["words"]:
            if word["text"] == "€":
                word["text"] = "CHF"
                break
        with self.assertRaisesMessage(ValueError, "Nicht-EUR-Währung"):
            self.parser.parse_pages(pages)

    def test_round_trip_umlauts_in_parsed_text(self):
        pages = self._synthetic_pages()
        pages[0]["words"].insert(-1, self._word("lötbar", 180, 154))
        parsed = self.parser.parse_pages(pages)
        rendered = json.dumps(parsed, default=str, ensure_ascii=False)
        self.assertIn("lötbar", rendered)
        self.assertIn("Drahtbrücken", rendered)
        self.assertIn("Eingangsbestätigung", rendered)

    def test_reichelt_real_samples_if_available(self):
        samples_dir = Path("samples")
        if not samples_dir.exists():
            self.skipTest("samples directory not available")
        sample1 = samples_dir / "Reichelt-215124.pdf"
        sample2 = samples_dir / "Reichelt-306393.pdf"
        if not sample1.exists() or not sample2.exists():
            self.skipTest("Reichelt sample files not available")

        with sample1.open("rb") as handle:
            parsed1 = self.parser.parse_pdf(handle.read(), filename=sample1.name)
        self.assertEqual(parsed1["invoice_date"].isoformat(), "2016-12-23")
        self.assertEqual(parsed1["invoice_no"], "I-215124")
        self.assertEqual(len(parsed1["lines"]), 4)
        self.assertEqual([line["quantity"] for line in parsed1["lines"]], [5, 10, 20, 10])
        self.assertEqual(parsed1["gross_total"], Decimal("27.90"))
        self.assertEqual(parsed1["net_total"], Decimal("23.45"))

        with sample2.open("rb") as handle:
            parsed2 = self.parser.parse_pdf(handle.read(), filename=sample2.name)
        self.assertEqual(parsed2["invoice_date"].isoformat(), "2017-01-26")
        self.assertEqual(parsed2["invoice_no"], "I-306393")
        self.assertEqual(len(parsed2["lines"]), 5)
        self.assertEqual([line["quantity"] for line in parsed2["lines"]], [50, 10, 1, 1, 1])
        self.assertEqual(parsed2["gross_total"], Decimal("13.49"))
        self.assertEqual(parsed2["net_total"], Decimal("11.34"))
        self.assertAlmostEqual(float(parsed2["lines"][0]["printed_unit_price_gross"]), 0.01, places=2)


class InvoiceImportTests(TestCase):
    def test_conrad_parser_parses_sample_invoice_lines(self):
        sample = Path("samples/conrad/9732893499.pdf")
        if not sample.exists():
            self.skipTest("Conrad sample file not available")
        parsed = ConradInvoiceParser().parse_pdf(sample.read_bytes(), filename=sample.name)
        self.assertEqual(parsed["supplier"], "Conrad Electronic SE")
        self.assertEqual(parsed["invoice_no"], "9732893499")
        self.assertEqual(parsed["invoice_date"].isoformat(), "2021-12-09")
        self.assertEqual(len(parsed["lines"]), 6)
        self.assertEqual(parsed["layout"], "A")
        self.assertEqual(parsed["gross_total"], Decimal("51.05"))
        self.assertEqual(parsed["net_total"], Decimal("42.90"))
        self.assertEqual(parsed["warnings"], [])

    def test_invoice_preview_matches_existing_part_and_warns_for_duplicate_invoice(self):
        existing = Part.objects.create(
            part_type="Reihenklemme",
            value="2,5 mm²",
            packaging="THT",
            description="Reihenklemme 2,5 mm²",
            supplier="Conrad Electronic SE",
            supplier_part_no="1327363",
            mf_part_no="",
            quantity=5,
            location_type=Part.LocationType.DRAWER,
            location_name="Drawer 1",
            last_price=Decimal("2.99"),
            currency="EUR",
            last_document_no="9732893499",
        )
        ImportedDocument.objects.create(
            supplier="Conrad Electronic SE",
            document_no="9732893499",
            document_date=date(2021, 12, 9),
            document_type="invoice",
            original_filename="x.pdf",
            line_count=1,
        )
        preview = build_invoice_preview(
            {
                "supplier": "Conrad Electronic SE",
                "invoice_no": "9732893499",
                "invoice_date": "2021-12-09",
                "warning": "",
                "lines": [
                    {
                        "supplier": "Conrad Electronic SE",
                        "supplier_part_no": "1327363",
                        "mf_part_no": "",
                        "description": "Reihenklemme 2,5 mm²",
                        "quantity": 5,
                        "unit_price": Decimal("2.99"),
                        "currency": "EUR",
                    }
                ],
            }
        )
        self.assertTrue(preview["duplicate_invoice"])
        self.assertEqual(preview["lines"][0]["match_type"], "supplier_part_no")
        self.assertEqual(preview["lines"][0]["part"]["pk"], existing.pk)

    def test_invoice_preview_is_json_serializable(self):
        preview = build_invoice_preview(
            {
                "supplier": "Conrad Electronic SE",
                "invoice_no": "12345",
                "invoice_date": date(2026, 10, 7),
                "warning": "",
                "lines": [
                    {
                        "supplier": "Conrad Electronic SE",
                        "supplier_part_no": "ABC-1",
                        "mf_part_no": "",
                        "description": "Test part",
                        "quantity": 2,
                        "unit_price": Decimal("1.23"),
                        "currency": "EUR",
                    }
                ],
            }
        )
        json.dumps(preview)
        self.assertEqual(preview["invoice_date"], "2026-10-07")


class BatchImportTests(TestCase):
    def _preview_payload(self):
        return {
            "created_at": "2030-01-01T10:00:00+00:00",
            "quantity_mode": "add_stock",
            "supplier": "Reichelt",
            "failed_files": [],
            "skipped_duplicates": [],
            "summary": {"documents": 2, "lines": 2, "matched": 0, "new": 2, "needs_review": 0},
            "documents": [
                {
                    "supplier": "Reichelt",
                    "file_name": "newer.pdf",
                    "document_id": "Reichelt::I-2::newer.pdf",
                    "invoice_no": "I-2",
                    "invoice_date": "2024-01-02",
                    "document_type": "order_confirmation",
                    "warning": "",
                    "reference_note": "",
                    "whg_note": "",
                    "duplicate_invoice": False,
                    "lines": [
                        {
                            "index": 0,
                            "field_prefix": "Reichelt::I-2::newer.pdf::0",
                            "description": "Part A",
                            "supplier": "Reichelt",
                            "supplier_part_no": "A-1",
                            "mf_part_no": "",
                            "quantity": 5,
                            "unit_price": "1.0000",
                            "currency": "EUR",
                            "status": "new",
                            "candidates": [],
                            "part": None,
                        }
                    ],
                },
                {
                    "supplier": "Reichelt",
                    "file_name": "older.pdf",
                    "document_id": "Reichelt::I-1::older.pdf",
                    "invoice_no": "I-1",
                    "invoice_date": "2024-01-01",
                    "document_type": "order_confirmation",
                    "warning": "",
                    "reference_note": "",
                    "whg_note": "",
                    "duplicate_invoice": False,
                    "lines": [
                        {
                            "index": 0,
                            "field_prefix": "Reichelt::I-1::older.pdf::0",
                            "description": "Part A",
                            "supplier": "Reichelt",
                            "supplier_part_no": "A-1",
                            "mf_part_no": "",
                            "quantity": 2,
                            "unit_price": "0.5000",
                            "currency": "EUR",
                            "status": "new",
                            "candidates": [],
                            "part": None,
                        }
                    ],
                },
            ],
        }

    def test_batch_confirm_orders_by_date_and_merges_same_new_part(self):
        session = self.client.session
        session["invoice_preview_batch"] = self._preview_payload()
        session.save()

        response = self.client.post(reverse("parts:invoice_preview"), {})
        self.assertRedirects(response, reverse("parts:list"))
        self.assertEqual(Part.objects.count(), 1)
        part = Part.objects.get()
        self.assertEqual(part.quantity, 7)
        self.assertEqual(part.last_document_no, "I-2")
        self.assertEqual(part.last_price, Decimal("1.0000"))
        self.assertEqual(ImportedDocument.objects.count(), 2)
        self.assertEqual(Supplier.objects.count(), 1)
        self.assertEqual(PurchaseDocument.objects.count(), 2)
        self.assertEqual(PurchaseLine.objects.count(), 2)

    def test_document_list_displays_imported_purchase_documents(self):
        session = self.client.session
        session["invoice_preview_batch"] = self._preview_payload()
        session.save()

        self.client.post(reverse("parts:invoice_preview"), {})

        response = self.client.get(reverse("parts:documents"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "I-1")
        self.assertContains(response, "I-2")

    def test_batch_project_is_applied_to_all_lines(self):
        project = Project.objects.create(name="Projekt A", client="ZAM")
        session = self.client.session
        payload = self._preview_payload()
        payload["project_id"] = str(project.pk)
        session["invoice_preview_batch"] = payload
        session.save()

        response = self.client.post(
            reverse("parts:invoice_preview"),
            {"batch_project_id": str(project.pk)},
        )
        self.assertRedirects(response, reverse("parts:list"))
        self.assertEqual(PurchaseLine.objects.filter(project=project).count(), 2)

    def test_line_project_override_wins_over_batch_default(self):
        project_a = Project.objects.create(name="Projekt A")
        project_b = Project.objects.create(name="Projekt B")
        session = self.client.session
        payload = self._preview_payload()
        payload["project_id"] = str(project_a.pk)
        session["invoice_preview_batch"] = payload
        session.save()

        response = self.client.post(
            reverse("parts:invoice_preview"),
            {
                "batch_project_id": str(project_a.pk),
                "project::Reichelt::I-2::newer.pdf::0": str(project_b.pk),
            },
        )
        self.assertRedirects(response, reverse("parts:list"))
        self.assertEqual(PurchaseLine.objects.filter(project=project_a).count(), 1)
        self.assertEqual(PurchaseLine.objects.filter(project=project_b).count(), 1)


class ProjectManagementTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Alpha", client="ZAM", note="Test")
        supplier = Supplier.objects.create(name="Reichelt")
        document = PurchaseDocument.objects.create(
            supplier=supplier,
            document_type=PurchaseDocument.DocumentType.RECHNUNG,
            document_number="R-1",
            currency="EUR",
            shipping_cost=Decimal("1.00"),
            document_total=Decimal("11.00"),
        )
        self.line = PurchaseLine.objects.create(
            document=document,
            position="1",
            description="Testzeile",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("10.0000"),
            line_total=Decimal("10.0000"),
            currency="EUR",
            price_is_net=True,
            project=self.project,
        )

    def _preview_payload(self):
        return {
            "created_at": "2030-01-01T10:00:00+00:00",
            "quantity_mode": "add_stock",
            "supplier": "Reichelt",
            "failed_files": [],
            "skipped_duplicates": [],
            "summary": {"documents": 2, "lines": 2, "matched": 0, "new": 2, "needs_review": 0},
            "documents": [
                {
                    "supplier": "Reichelt",
                    "file_name": "newer.pdf",
                    "document_id": "Reichelt::I-2::newer.pdf",
                    "invoice_no": "I-2",
                    "invoice_date": "2024-01-02",
                    "document_type": "order_confirmation",
                    "warning": "",
                    "reference_note": "",
                    "whg_note": "",
                    "duplicate_invoice": False,
                    "lines": [
                        {
                            "index": 0,
                            "field_prefix": "Reichelt::I-2::newer.pdf::0",
                            "description": "Part A",
                            "supplier": "Reichelt",
                            "supplier_part_no": "A-1",
                            "mf_part_no": "",
                            "quantity": 5,
                            "unit_price": "1.0000",
                            "currency": "EUR",
                            "status": "new",
                            "candidates": [],
                            "part": None,
                        }
                    ],
                },
                {
                    "supplier": "Reichelt",
                    "file_name": "older.pdf",
                    "document_id": "Reichelt::I-1::older.pdf",
                    "invoice_no": "I-1",
                    "invoice_date": "2024-01-01",
                    "document_type": "order_confirmation",
                    "warning": "",
                    "reference_note": "",
                    "whg_note": "",
                    "duplicate_invoice": False,
                    "lines": [
                        {
                            "index": 0,
                            "field_prefix": "Reichelt::I-1::older.pdf::0",
                            "description": "Part A",
                            "supplier": "Reichelt",
                            "supplier_part_no": "A-1",
                            "mf_part_no": "",
                            "quantity": 2,
                            "unit_price": "0.5000",
                            "currency": "EUR",
                            "status": "new",
                            "candidates": [],
                            "part": None,
                        }
                    ],
                },
            ],
        }

    def test_create_edit_deactivate_project(self):
        response = self.client.post(reverse("parts:project_add"), {"name": "Neu", "client": "Kunde", "note": "", "active": True})
        self.assertRedirects(response, reverse("parts:projects"))
        created = Project.objects.get(name="Neu")
        self.assertTrue(created.active)

        response = self.client.post(
            reverse("parts:project_edit", args=[created.pk]),
            {"name": "Neu", "client": "Anderer Kunde", "note": "x", "active": False},
        )
        self.assertRedirects(response, reverse("parts:projects"))
        created.refresh_from_db()
        self.assertFalse(created.active)
        self.assertEqual(created.client, "Anderer Kunde")

        response = self.client.get(reverse("parts:project_toggle_active", args=[created.pk]))
        self.assertRedirects(response, reverse("parts:projects"))
        created.refresh_from_db()
        self.assertTrue(created.active)

    def test_delete_project_untags_lines_but_keeps_documents_and_lines(self):
        document_count = PurchaseDocument.objects.count()
        line_count = PurchaseLine.objects.count()
        response = self.client.post(reverse("parts:project_delete", args=[self.project.pk]))
        self.assertRedirects(response, reverse("parts:projects"))
        self.assertEqual(PurchaseDocument.objects.count(), document_count)
        self.assertEqual(PurchaseLine.objects.count(), line_count)
        self.assertIsNone(PurchaseLine.objects.get(pk=self.line.pk).project)

    def test_document_detail_tagging_and_bulk_actions(self):
        inactive = Project.objects.create(name="Inaktiv", active=False)
        active_b = Project.objects.create(name="Beta")
        self.line.project = inactive
        self.line.save(update_fields=["project"])
        second_line = PurchaseLine.objects.create(
            document=self.line.document,
            position="2",
            description="zweite",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("2.0000"),
            line_total=Decimal("2.0000"),
            currency="EUR",
            price_is_net=True,
            project=None,
        )
        response = self.client.get(reverse("parts:document_detail", args=[self.line.document.pk]))
        self.assertContains(response, "kein Projekt")
        self.assertContains(response, "Inaktiv")
        self.assertContains(response, "Inaktiv (inaktiv)")
        self.assertEqual(response.content.decode("utf-8").count("Inaktiv (inaktiv)"), 1)

        response = self.client.post(
            reverse("parts:document_detail", args=[self.line.document.pk]),
            {
                f"line_project::{self.line.pk}": "__none__",
                f"line_project::{second_line.pk}": str(active_b.pk),
                "bulk_project": str(self.project.pk),
                "apply_bulk_untagged": "yes",
            },
        )
        self.assertRedirects(response, reverse("parts:document_detail", args=[self.line.document.pk]))
        self.line.refresh_from_db()
        second_line.refresh_from_db()
        self.assertEqual(self.line.project, self.project)
        self.assertEqual(second_line.project, active_b)

    def test_project_view_filters_and_totals(self):
        response = self.client.get(reverse("parts:project_documents"), {"project": str(self.project.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Projekt-Teilsumme")
        self.assertContains(response, "10,0000")

        self.line.document.paper_filed = True
        self.line.document.save(update_fields=["paper_filed"])
        filtered = self.client.get(
            reverse("parts:project_documents"),
            {"project": str(self.project.pk), "paper_unfiled": "yes"},
        )
        self.assertContains(filtered, "Keine Belege")

    def test_document_list_filter_without_project_option(self):
        supplier = Supplier.objects.create(name="Conrad")
        document_without = PurchaseDocument.objects.create(
            supplier=supplier,
            document_type=PurchaseDocument.DocumentType.RECHNUNG,
            document_number="R-2",
        )
        PurchaseLine.objects.create(
            document=document_without,
            position="1",
            description="ohne projekt",
            quantity=Decimal("1.0000"),
            line_total=Decimal("1.0000"),
        )
        response = self.client.get(reverse("parts:documents"), {"project": "__none__"})
        self.assertContains(response, "R-2")


class StockCsvParserTests(TestCase):
    def test_parse_supports_utf8_sig_cp1252_and_delimiters(self):
        csv_semicolon = "\ufeffpart_type;value;quantity\nWiderstand;4,7k;10\n".encode("utf-8-sig")
        parsed = parse_csv_bytes(csv_semicolon)
        self.assertEqual(parsed["encoding"], "utf-8-sig")
        self.assertEqual(parsed["delimiter"], ";")

        csv_cp = "part_type,value,quantity\nGehäuse,3,2\n".encode("cp1252")
        parsed_cp = parse_csv_bytes(csv_cp)
        self.assertEqual(parsed_cp["encoding"], "cp1252")
        self.assertEqual(parsed_cp["delimiter"], ",")

        csv_tab = "part_type\tvalue\tquantity\nKondensator\t100nF\t5\n".encode("utf-8-sig")
        parsed_tab = parse_csv_bytes(csv_tab)
        self.assertEqual(parsed_tab["delimiter"], "\t")

    def test_header_aliases_and_unknown_columns_warning(self):
        content = "\ufeffTyp;Wert;Menge;Projekt;Foo\nWiderstand;4,7k;10;A;x\n".encode("utf-8-sig")
        parsed = parse_csv_bytes(content)
        warnings = "\n".join(parsed["header_warnings"])
        self.assertIn("Projekt", warnings)
        self.assertIn("Foo", warnings)

    def test_quoted_delimiter_in_field_is_parsed(self):
        content = '\ufeffpart_type;description;quantity\nWiderstand;"Text;mit;Semikolon";3\n'.encode("utf-8-sig")
        parsed = parse_csv_bytes(content)
        self.assertEqual(parsed["rows"][0]["data"]["description"], "Text;mit;Semikolon")

    def test_invalid_quantity_and_value_description_error(self):
        content = "\ufeffpart_type;value;description;quantity\nWiderstand;;;ca. 50\n".encode("utf-8-sig")
        parsed = parse_csv_bytes(content)
        preview = prepare_stock_preview(parsed, IMPORT_MODE_UPDATE)
        self.assertEqual(preview["counts"]["error"], 1)
        self.assertIn("Menge muss eine ganze Zahl", preview["errors"][0])

    def test_unknown_location_type_becomes_warning_and_blank(self):
        content = "\ufeffpart_type;value;quantity;location_type\nWiderstand;4,7k;10;xyz\n".encode("utf-8-sig")
        parsed = parse_csv_bytes(content)
        row = parsed["rows"][0]
        self.assertEqual(row["data"]["location_type"], "")
        self.assertTrue(row["warnings"])

    def test_duplicate_id_and_key_in_file_are_errors(self):
        p = Part.objects.create(part_type="Widerstand", value="4,7k", packaging="0805", quantity=1)
        content = (
            "\ufeffid;part_type;value;packaging;quantity\n"
            f"{p.pk};Widerstand;4,7k;0805;1\n"
            f"{p.pk};Widerstand;4,7k;0805;2\n"
        ).encode("utf-8-sig")
        parsed = parse_csv_bytes(content)
        preview = prepare_stock_preview(parsed, IMPORT_MODE_UPDATE)
        self.assertEqual(preview["counts"]["error"], 1)


class StockCsvImportFlowTests(TestCase):
    def setUp(self):
        self.existing = Part.objects.create(
            part_type="Widerstand",
            value="0.1µF",
            packaging="0805",
            description="Alt",
            quantity=5,
            location_type=Part.LocationType.DRAWER,
            location_name="Schublade 1",
            position="A1",
            supplier="Conrad",
            supplier_part_no="X",
            last_price=Decimal("1.0000"),
            last_document_no="D1",
        )

    def _upload(self, content, mode=IMPORT_MODE_UPDATE):
        upload = SimpleUploadedFile("stock.csv", content, content_type="text/csv")
        return self.client.post(reverse("parts:stock_csv_upload"), {"mode": mode, "csv_file": upload})

    def test_upload_preview_and_confirm_update_mode(self):
        content = (
            "\ufeffpart_type;value;packaging;description;quantity;location_name;position\n"
            "WIDERSTAND;0.1uF;0805;Neu Text;7;Schublade 1;A1\n"
        ).encode("utf-8-sig")
        response = self._upload(content, IMPORT_MODE_UPDATE)
        self.assertRedirects(response, reverse("parts:stock_csv_preview"))
        preview_page = self.client.get(reverse("parts:stock_csv_preview"))
        self.assertContains(preview_page, "Aktualisiert")
        self.assertContains(preview_page, "utf-8-sig")
        self.assertContains(preview_page, "Trennzeichen")

        confirm = self.client.post(reverse("parts:stock_csv_preview"), {"continue_without_errors": "yes"})
        self.assertRedirects(confirm, reverse("parts:list"))
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.quantity, 7)
        self.assertEqual(self.existing.description, "Neu Text")

    def test_skip_and_add_modes(self):
        content = (
            "\ufeffpart_type;value;packaging;quantity;location_name;position\n"
            "Widerstand;0.1uF;0805;3;Schublade 1;A1\n"
        ).encode("utf-8-sig")
        self._upload(content, IMPORT_MODE_SKIP)
        self.client.post(reverse("parts:stock_csv_preview"), {"continue_without_errors": "yes"})
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.quantity, 5)

        self._upload(content, IMPORT_MODE_ADD)
        self.client.post(reverse("parts:stock_csv_preview"), {"continue_without_errors": "yes"})
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.quantity, 8)

    def test_blank_cells_do_not_overwrite(self):
        content = (
            "\ufeffpart_type;value;packaging;description;quantity;location_name;position\n"
            "Widerstand;0.1uF;0805;;5;Schublade 1;A1\n"
        ).encode("utf-8-sig")
        self._upload(content, IMPORT_MODE_UPDATE)
        self.client.post(reverse("parts:stock_csv_preview"), {"continue_without_errors": "yes"})
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.description, "Alt")

    def test_no_supplier_document_line_project_changes(self):
        supplier_count = Supplier.objects.count()
        doc_count = PurchaseDocument.objects.count()
        line_count = PurchaseLine.objects.count()
        project_count = Project.objects.count()

        content = "\ufeffpart_type;value;quantity\nTransistor;BC547;2\n".encode("utf-8-sig")
        self._upload(content, IMPORT_MODE_UPDATE)
        self.client.post(reverse("parts:stock_csv_preview"), {"continue_without_errors": "yes"})
        self.assertEqual(Supplier.objects.count(), supplier_count)
        self.assertEqual(PurchaseDocument.objects.count(), doc_count)
        self.assertEqual(PurchaseLine.objects.count(), line_count)
        self.assertEqual(Project.objects.count(), project_count)

    def test_template_and_export_roundtrip(self):
        template = self.client.get(reverse("parts:stock_csv_template"))
        self.assertEqual(template.status_code, 200)
        self.assertIn("part_type;value;packaging", template.content.decode("utf-8-sig"))

        exported = self.client.get(reverse("parts:stock_csv_export"))
        self.assertEqual(exported.status_code, 200)
        response = self._upload(exported.content, IMPORT_MODE_UPDATE)
        self.assertRedirects(response, reverse("parts:stock_csv_preview"))
        preview = self.client.get(reverse("parts:stock_csv_preview"))
        self.assertContains(preview, "Unverändert")

    def test_template_examples_imported_twice_second_run_is_unchanged(self):
        template = self.client.get(reverse("parts:stock_csv_template"))
        csv_bytes = template.content

        self._upload(csv_bytes, IMPORT_MODE_UPDATE)
        first_preview = self.client.get(reverse("parts:stock_csv_preview"))
        self.assertContains(first_preview, "Neu")
        self.client.post(reverse("parts:stock_csv_preview"), {"continue_without_errors": "yes"})

        self._upload(csv_bytes, IMPORT_MODE_UPDATE)
        second_preview = self.client.get(reverse("parts:stock_csv_preview"))
        self.assertContains(second_preview, "Unverändert")
        preview_payload = self.client.session["stock_csv_preview"]["preview"]
        self.assertEqual(preview_payload["counts"]["unchanged"], 3)
        self.assertEqual(preview_payload["counts"]["updated"], 0)
        self.assertEqual(preview_payload["counts"]["new"], 0)

    def test_confirm_requires_checkbox_when_errors_exist(self):
        content = (
            "\ufeffpart_type;value;quantity\n"
            "Widerstand;4,7k;10\n"
            "Widerstand;4,7k;abc\n"
        ).encode("utf-8-sig")
        self._upload(content, IMPORT_MODE_UPDATE)
        response = self.client.post(reverse("parts:stock_csv_preview"), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bitte bestätigen Sie das Fortfahren")

    def test_all_error_rows_cannot_be_confirmed(self):
        content = "\ufeffpart_type;value;quantity\nWiderstand;4,7k;abc\n".encode("utf-8-sig")
        self._upload(content, IMPORT_MODE_UPDATE)
        response = self.client.post(reverse("parts:stock_csv_preview"), {"continue_without_errors": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "keine fehlerfreien Zeilen")

    def test_export_edit_location_by_id_updates_instead_of_duplicate(self):
        exported = self.client.get(reverse("parts:stock_csv_export")).content.decode("utf-8-sig")
        lines = exported.splitlines()
        header = lines[0]
        row = lines[1].split(";")
        row[7] = "Schublade Z"
        edited = "\n".join([header, ";".join(row)])
        self._upload(edited.encode("utf-8-sig"), IMPORT_MODE_UPDATE)
        self.client.post(reverse("parts:stock_csv_preview"), {"continue_without_errors": "yes"})
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.location_name, "Schublade Z")
        self.assertEqual(Part.objects.count(), 1)
