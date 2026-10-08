from __future__ import annotations

import unicodedata
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from parsers.describe import suggest_part_fields
from .models import Part


class DescriptionSuggestionRuleTests(TestCase):
    def test_resistor_suggestion(self):
        result = suggest_part_fields("SMD Widerstand 0805, 2,4 kOhm, 100 mW, 1%", existing_part_types=[])
        self.assertEqual(result["part_type"], "Widerstand")
        self.assertEqual(result["value"], "2,4k")
        self.assertEqual(result["packaging"], "0805")

    def test_fuse_suggestion(self):
        result = suggest_part_fields("Kleinstsicherung kurz flink 0,5A", existing_part_types=[])
        self.assertEqual(result["part_type"], "Sicherung")
        self.assertEqual(result["value"], "0,5A")
        self.assertEqual(result["packaging"], "")

    def test_terminal_block_suggestion(self):
        result = suggest_part_fields("Federkraftklemme, lötbar, 2-pol, RM 5,00", existing_part_types=[])
        self.assertEqual(result["part_type"], "Klemme")
        self.assertEqual(result["value"], "")
        self.assertEqual(result["packaging"], "")

    def test_female_header_suggestion(self):
        result = suggest_part_fields("Buchsenleisten 2,54 mm, 1X06, gerade", existing_part_types=[])
        self.assertEqual(result["part_type"], "Buchsenleiste")
        self.assertEqual(result["value"], "")
        self.assertEqual(result["packaging"], "")

    def test_no_suggestion_for_unrecognized_descriptions(self):
        result = suggest_part_fields("IC-Adapterleiste, 20-polig, einreihig, RM 2,54", existing_part_types=[])
        self.assertEqual(result["part_type"], "")
        self.assertEqual(result["value"], "")
        self.assertEqual(result["packaging"], "")

        result = suggest_part_fields("Flexible Drahtbrücken,15cm,Buchse-Buchse,bl", existing_part_types=[])
        self.assertEqual(result["part_type"], "")
        self.assertEqual(result["value"], "")
        self.assertEqual(result["packaging"], "")

    def test_relay_ambiguous_value_stays_blank(self):
        result = suggest_part_fields("Steck-/Printrelais, 1x UM, 250V/10A, 12V, RM3,2", existing_part_types=[])
        self.assertEqual(result["part_type"], "Relais")
        self.assertEqual(result["value"], "")

    def test_capacitor_and_inductor_u_variants(self):
        cap = suggest_part_fields("Kondensator 0,1 uF 50V", existing_part_types=[])
        self.assertEqual(cap["part_type"], "Kondensator")
        self.assertEqual(cap["value"], "0,1µF")

        ind = suggest_part_fields("SMD Spule 0805 10 uH", existing_part_types=[])
        self.assertEqual(ind["part_type"], "Spule")
        self.assertEqual(ind["value"], "10µH")

    def test_bare_0805_not_used_for_non_passive_without_smd(self):
        result = suggest_part_fields("Diode 0805 schnell", existing_part_types=[])
        self.assertEqual(result["part_type"], "Diode")
        self.assertEqual(result["packaging"], "")

    def test_existing_type_name_reused_case_insensitive(self):
        result = suggest_part_fields("SMD Widerstand 0805 10 kOhm", existing_part_types=["widerstand"])
        self.assertEqual(result["part_type"], "widerstand")
        self.assertFalse(result["is_new_type"])

    def test_nfc_input_supported(self):
        text = unicodedata.normalize("NFD", "Federkraftklemme, lötbar, 2-pol")
        result = suggest_part_fields(text, existing_part_types=[])
        self.assertEqual(result["part_type"], "Klemme")


class ImportSuggestionFlowTests(TestCase):
    def test_new_part_uses_editable_suggestions_from_preview(self):
        session = self.client.session
        session["invoice_preview_batch"] = {
            "created_at": "2030-01-01T10:00:00+00:00",
            "quantity_mode": "add_stock",
            "supplier": "Reichelt",
            "failed_files": [],
            "skipped_duplicates": [],
            "new_type_names": [],
            "summary": {"documents": 1, "lines": 1, "matched": 0, "new": 1, "needs_review": 0},
            "documents": [
                {
                    "supplier": "Reichelt",
                    "file_name": "doc.pdf",
                    "document_id": "Reichelt::I-1::doc.pdf",
                    "invoice_no": "I-1",
                    "invoice_date": "2024-01-01",
                    "document_type": "order_confirmation",
                    "warning": "",
                    "reference_note": "",
                    "whg_note": "",
                    "duplicate_invoice": False,
                    "counts": {"matched": 0, "new": 1, "needs_review": 0},
                    "lines": [
                        {
                            "index": 0,
                            "field_prefix": "Reichelt::I-1::doc.pdf::0",
                            "description": "SMD Widerstand 0805 2,4 kOhm",
                            "supplier": "Reichelt",
                            "supplier_part_no": "R-1",
                            "mf_part_no": "",
                            "quantity": 3,
                            "unit_price": "0.1000",
                            "currency": "EUR",
                            "status": "new",
                            "status_label": "Neu",
                            "part_type": "Widerstand",
                            "value": "2,4k",
                            "packaging": "0805",
                            "part_type_suggested": "Widerstand",
                            "value_suggested": "2,4k",
                            "packaging_suggested": "0805",
                            "candidates": [],
                            "part": None,
                        }
                    ],
                }
            ],
        }
        session.save()

        response = self.client.post(
            reverse("parts:invoice_preview"),
            {
                "part_type::Reichelt::I-1::doc.pdf::0": "Widerstand",
                "value::Reichelt::I-1::doc.pdf::0": "2,4k",
                "packaging::Reichelt::I-1::doc.pdf::0": "0805",
            },
        )
        self.assertRedirects(response, reverse("parts:list"))
        part = Part.objects.get()
        self.assertEqual(part.part_type, "Widerstand")
        self.assertEqual(part.value, "2,4k")
        self.assertEqual(part.packaging, "0805")


class CompletePartsActionTests(TestCase):
    def test_complete_parts_preview_and_apply(self):
        part = Part.objects.create(
            part_type="",
            value="",
            packaging="",
            description="SMD Widerstand 0805, 2,4 kOhm, 100 mW",
            quantity=1,
            location_type=Part.LocationType.FOLDER,
            location_name="A",
        )
        response = self.client.get(reverse("parts:complete"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Widerstand")

        response = self.client.post(
            reverse("parts:complete"),
            {
                f"apply_{part.id}": "on",
                f"part_type_{part.id}": "Widerstand",
                f"value_{part.id}": "2,4k",
                f"packaging_{part.id}": "0805",
            },
        )
        self.assertRedirects(response, reverse("parts:list"))
        part.refresh_from_db()
        self.assertEqual(part.part_type, "Widerstand")
        self.assertEqual(part.value, "2,4k")
        self.assertEqual(part.packaging, "0805")

    def test_complete_parts_never_overwrites_existing_fields(self):
        part = Part.objects.create(
            part_type="Bereits gesetzt",
            value="",
            packaging="",
            description="SMD Widerstand 0805, 2,4 kOhm",
            quantity=1,
            location_type=Part.LocationType.FOLDER,
            location_name="A",
        )
        self.client.post(
            reverse("parts:complete"),
            {
                f"apply_{part.id}": "on",
                f"part_type_{part.id}": "Widerstand",
                f"value_{part.id}": "2,4k",
                f"packaging_{part.id}": "0805",
            },
        )
        part.refresh_from_db()
        self.assertEqual(part.part_type, "Bereits gesetzt")
        self.assertEqual(part.value, "2,4k")
        self.assertEqual(part.packaging, "0805")

    def test_complete_parts_transaction_rolls_back(self):
        first = Part.objects.create(
            part_type="",
            value="",
            packaging="",
            description="SMD Widerstand 0805, 2,4 kOhm",
            quantity=1,
            location_type=Part.LocationType.FOLDER,
            location_name="A",
        )
        second = Part.objects.create(
            part_type="",
            value="",
            packaging="",
            description="Kondensator 100 nF",
            quantity=1,
            location_type=Part.LocationType.FOLDER,
            location_name="B",
        )

        with patch("parts.views.Part.save", autospec=True, side_effect=[None, RuntimeError("boom")]):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("parts:complete"),
                    {
                        f"apply_{first.id}": "on",
                        f"apply_{second.id}": "on",
                        f"part_type_{first.id}": "Widerstand",
                        f"value_{first.id}": "2,4k",
                        f"packaging_{first.id}": "0805",
                        f"part_type_{second.id}": "Kondensator",
                        f"value_{second.id}": "100nF",
                        f"packaging_{second.id}": "0805",
                    },
                )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.part_type, "")
        self.assertEqual(second.part_type, "")
