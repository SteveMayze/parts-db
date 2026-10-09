from __future__ import annotations

import re

from django.test import TestCase
from django.urls import reverse

from .models import Part


ENGLISH_UI_WORDS = [
    "Edit",
    "Delete",
    "Save",
    "Search",
    "Back",
    "Add",
    "New",
    "Cancel",
    "Part",
    "Quantity",
    "Location",
    "Supplier",
    "Price",
    "Upload",
    "Matched",
    "Needs review",
    "Clear",
]


def _extract_visible_text(html: str) -> str:
    without_scripts = re.sub(r"<script.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(r"<style.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL)
    without_tags = re.sub(r"<[^>]+>", " ", without_styles)
    return re.sub(r"\s+", " ", without_tags).strip()


class GermanUiLanguageTests(TestCase):
    def setUp(self):
        self.part = Part.objects.create(
            part_type="Widerstand",
            value="10k",
            packaging="0805",
            description="Testbauteil",
            quantity=1,
            location_type=Part.LocationType.FOLDER,
            location_name="Mappe 1",
            position="1",
        )

    def assert_no_common_english_words(self, response):
        text = _extract_visible_text(response.content.decode("utf-8"))
        for word in ENGLISH_UI_WORDS:
            self.assertNotRegex(text, rf"\b{re.escape(word)}\b", msg=f"English UI word found: {word}")

    def test_pages_render_without_common_english_words(self):
        list_response = self.client.get(reverse("parts:list"))
        self.assertEqual(list_response.status_code, 200)
        self.assert_no_common_english_words(list_response)

        add_response = self.client.get(reverse("parts:add"))
        self.assertEqual(add_response.status_code, 200)
        self.assert_no_common_english_words(add_response)

        edit_response = self.client.get(reverse("parts:edit", args=[self.part.pk]))
        self.assertEqual(edit_response.status_code, 200)
        self.assert_no_common_english_words(edit_response)

        delete_response = self.client.get(reverse("parts:delete", args=[self.part.pk]))
        self.assertEqual(delete_response.status_code, 200)
        self.assert_no_common_english_words(delete_response)

        upload_response = self.client.get(reverse("parts:import"))
        self.assertEqual(upload_response.status_code, 200)
        self.assert_no_common_english_words(upload_response)

        complete_response = self.client.get(reverse("parts:complete"))
        self.assertEqual(complete_response.status_code, 200)
        self.assert_no_common_english_words(complete_response)

        stock_upload_response = self.client.get(reverse("parts:stock_csv_upload"))
        self.assertEqual(stock_upload_response.status_code, 200)
        self.assert_no_common_english_words(stock_upload_response)

        session = self.client.session
        session["invoice_preview_batch"] = {
            "created_at": "2030-01-01T10:00:00+00:00",
            "quantity_mode": "add_stock",
            "supplier": "Reichelt",
            "failed_files": [],
            "skipped_duplicates": [],
            "summary": {"documents": 1, "lines": 1, "matched": 0, "new": 1, "needs_review": 0},
            "documents": [
                {
                    "supplier": "Reichelt",
                    "file_name": "demo.pdf",
                    "document_id": "Reichelt::I-1::demo.pdf",
                    "invoice_no": "I-1",
                    "invoice_date": "2024-01-01",
                    "document_type": "order_confirmation",
                    "warning": "",
                    "reference_note": "",
                    "whg_note": "",
                    "duplicate_invoice": False,
                    "duplicate_imported_at": "",
                    "counts": {"matched": 0, "new": 1, "needs_review": 0},
                    "lines": [
                        {
                            "index": 0,
                            "field_prefix": "Reichelt::I-1::demo.pdf::0",
                            "description": "Demo",
                            "supplier": "Reichelt",
                            "supplier_part_no": "A-1",
                            "mf_part_no": "",
                            "quantity": 1,
                            "unit_price": "1.0000",
                            "currency": "EUR",
                            "status": "new",
                            "status_label": "Neu",
                            "candidates": [],
                            "part": None,
                        }
                    ],
                }
            ],
        }
        session.save()
        preview_response = self.client.get(reverse("parts:invoice_preview"))
        self.assertEqual(preview_response.status_code, 200)
        self.assert_no_common_english_words(preview_response)

        session["stock_csv_preview"] = {
            "created_at": "2030-01-01T10:00:00+00:00",
            "preview": {
                "encoding": "utf-8-sig",
                "delimiter": ";",
                "mode": "update_existing",
                "mode_label": "Vorhandene aktualisieren",
                "warnings": [],
                "errors": [],
                "counts": {"new": 0, "updated": 0, "unchanged": 1, "error": 0, "warning": 0},
                "rows": [
                    {
                        "line_no": 2,
                        "status": "unchanged",
                        "status_label": "Unverändert",
                        "changes": [],
                        "warnings": [],
                        "errors": [],
                        "data": {"part_type": "Widerstand", "value": "10k", "packaging": "0805", "quantity": "1", "location_name": "Mappe 1"},
                    }
                ],
                "distinct": {"part_type": [], "packaging": [], "location_type": [], "location_name": []},
            },
        }
        session.save()
        stock_preview_response = self.client.get(reverse("parts:stock_csv_preview"))
        self.assertEqual(stock_preview_response.status_code, 200)
        self.assert_no_common_english_words(stock_preview_response)
