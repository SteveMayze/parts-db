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
from .models import ImportedDocument, Part
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
        with pdfplumber.open("samples/Conrad-Invoice.pdf") as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        parsed = ConradInvoiceParser().parse_invoice_text(text)
        self.assertEqual(parsed["supplier"], "Conrad Electronic SE")
        self.assertEqual(parsed["invoice_no"], "9732893499")
        self.assertEqual(parsed["invoice_date"].isoformat(), "2021-12-09")
        self.assertEqual(len(parsed["lines"]), 6)

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

    def test_older_document_does_not_overwrite_newer_price(self):
        existing = Part.objects.create(
            part_type="",
            value="",
            packaging="",
            description="Part A",
            supplier="Reichelt",
            supplier_part_no="A-1",
            last_price=Decimal("2.0000"),
            currency="EUR",
            last_purchased=date(2024, 1, 5),
            last_document_no="I-9",
            quantity=10,
            location_type="",
            location_name="",
            position="",
            comment="",
        )
        session = self.client.session
        session["invoice_preview_batch"] = self._preview_payload()
        session.save()

        response = self.client.post(reverse("parts:invoice_preview"), {})
        self.assertRedirects(response, reverse("parts:list"))
        existing.refresh_from_db()
        self.assertEqual(existing.quantity, 17)
        self.assertEqual(existing.last_price, Decimal("2.0000"))
        self.assertEqual(existing.last_document_no, "I-9")

    def test_quantity_mode_no_stock_change(self):
        session = self.client.session
        payload = self._preview_payload()
        payload["quantity_mode"] = QUANTITY_MODE_KEEP
        session["invoice_preview_batch"] = payload
        session.save()

        response = self.client.post(reverse("parts:invoice_preview"), {"quantity_mode": QUANTITY_MODE_KEEP})
        self.assertRedirects(response, reverse("parts:list"))
        part = Part.objects.get()
        self.assertEqual(part.quantity, 0)

    def test_duplicate_document_excluded_without_opt_in(self):
        ImportedDocument.objects.create(
            supplier="Reichelt",
            document_no="I-1",
            document_date=date(2024, 1, 1),
            document_type="order_confirmation",
            original_filename="older.pdf",
            line_count=1,
        )
        session = self.client.session
        session["invoice_preview_batch"] = self._preview_payload()
        session.save()

        response = self.client.post(reverse("parts:invoice_preview"), {})
        self.assertRedirects(response, reverse("parts:list"))
        self.assertEqual(ImportedDocument.objects.filter(supplier="Reichelt", document_no="I-1").count(), 1)

    def test_transaction_rolls_back_on_failure(self):
        session = self.client.session
        session["invoice_preview_batch"] = self._preview_payload()
        session.save()

        with patch("parts.views._create_new_part", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("parts:invoice_preview"), {})
        self.assertEqual(Part.objects.count(), 0)
        self.assertEqual(ImportedDocument.objects.count(), 0)

    def test_failed_files_require_explicit_continue(self):
        session = self.client.session
        payload = self._preview_payload()
        payload["failed_files"] = [{"file_name": "bad.pdf", "error": "parse"}]
        session["invoice_preview_batch"] = payload
        session.save()

        response = self.client.post(reverse("parts:invoice_preview"), {})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Part.objects.count(), 0)
        response = self.client.post(reverse("parts:invoice_preview"), {"continue_without_failed": "yes"})
        self.assertRedirects(response, reverse("parts:list"))

    def test_mixed_suppliers_rejected(self):
        parsed_documents = [
            {"file_name": "a.pdf", "invoice_data": {"supplier": "Reichelt"}},
            {"file_name": "b.pdf", "invoice_data": {"supplier": "Conrad Electronic SE"}},
        ]
        with self.assertRaisesMessage(ValueError, "selben Lieferanten"):
            _validate_single_supplier(parsed_documents)

    def test_same_document_number_twice_keeps_first(self):
        docs, summary, skipped, _new_types = _build_batch_preview(
            [
                {
                    "file_name": "first.pdf",
                    "invoice_data": {
                        "supplier": "Reichelt",
                        "invoice_no": "I-1",
                        "invoice_date": date(2024, 1, 1),
                        "lines": [],
                    },
                },
                {
                    "file_name": "second.pdf",
                    "invoice_data": {
                        "supplier": "Reichelt",
                        "invoice_no": "I-1",
                        "invoice_date": date(2024, 1, 2),
                        "lines": [],
                    },
                },
            ]
        )
        self.assertEqual(summary["documents"], 1)
        self.assertEqual(len(docs), 1)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["file_name"], "second.pdf")

    def test_real_reichelt_samples_can_import_as_single_batch(self):
        samples_dir = Path("samples")
        sample1 = samples_dir / "Reichelt-215124.pdf"
        sample2 = samples_dir / "Reichelt-306393.pdf"
        if not sample1.exists() or not sample2.exists():
            self.skipTest("Reichelt sample files not available")

        parser = ReicheltInvoiceParser()
        docs, summary, skipped, _new_types = _build_batch_preview(
            [
                {"file_name": sample1.name, "invoice_data": parser.parse_pdf(sample1.read_bytes(), filename=sample1.name)},
                {"file_name": sample2.name, "invoice_data": parser.parse_pdf(sample2.read_bytes(), filename=sample2.name)},
            ]
        )
        self.assertEqual(summary["documents"], 2)
        self.assertEqual(summary["lines"], 9)
        self.assertEqual(len(skipped), 0)

        session = self.client.session
        session["invoice_preview_batch"] = {
            "created_at": "2030-01-01T10:00:00+00:00",
            "quantity_mode": "add_stock",
            "supplier": "Reichelt",
            "failed_files": [],
            "skipped_duplicates": skipped,
            "summary": summary,
            "documents": docs,
        }
        session.save()

        response = self.client.post(reverse("parts:invoice_preview"), {})
        self.assertRedirects(response, reverse("parts:list"))
        self.assertEqual(ImportedDocument.objects.count(), 2)
        self.assertEqual(Part.objects.count(), 9)
        self.assertEqual(sum(Part.objects.values_list("quantity", flat=True)), 108)
