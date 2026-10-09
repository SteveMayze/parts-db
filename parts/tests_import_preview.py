from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Part, PurchaseLine
from .tests_ui_language import ENGLISH_UI_WORDS, _extract_visible_text
from .views import QUANTITY_MODE_ADD, _build_batch_preview

LONG_DESCRIPTION = (
    "Präzisions-Metallschichtwiderstand 0,6 W, ±1 %, 4,7 kΩ, axial bedrahtet, Bauform 0207, "
    "RoHS-konform, Rollenware auf Gurt, Testbeschreibung zum Prüfen der Anzeige"
)
TRUNCATED_CONRAD = "Universalwerkzeug RESQME Rettung Or"


def invoice(lines, number="9600000001", supplier="Conrad Electronic SE", invoice_date=date(2024, 3, 1), **extra):
    data = {
        "supplier": supplier,
        "document_type": "invoice",
        "invoice_no": number,
        "invoice_date": invoice_date,
        "price_basis": "gross",
        "document_total": Decimal("11.90"),
        "layout": "A",
        "lines": [],
    }
    for index, line in enumerate(lines):
        data["lines"].append(
            {
                "supplier": supplier,
                "supplier_part_no": line.get("no", f"100000{index}"),
                "mf_part_no": "",
                "description": line["description"],
                "quantity": line.get("quantity", 2),
                "unit_price": Decimal("0.8403"),
                "gross_unit_price": Decimal("1.0000"),
                "line_total_gross": Decimal("2.00"),
                "vat_rate": Decimal("19"),
                "currency": "EUR",
            }
        )
    data.update(extra)
    return data


def store_preview(client, invoices, **payload_extra):
    documents = [{"file_name": f"datei{i}.pdf", "invoice_data": data} for i, data in enumerate(invoices)]
    docs, summary, skipped, new_types = _build_batch_preview(documents)
    session = client.session
    session["invoice_preview_batch"] = {
        "created_at": timezone.now().isoformat(),
        "quantity_mode": QUANTITY_MODE_ADD,
        "project_id": "",
        "supplier": docs[0]["supplier"],
        "documents": docs,
        "failed_files": [],
        "skipped_duplicates": skipped,
        "new_type_names": new_types,
        "summary": summary,
        **payload_extra,
    }
    session.save()
    return docs


def prefix_of(docs, doc_index=0, line_index=0):
    return docs[doc_index]["lines"][line_index]["field_prefix"]


class PreviewLayoutTests(TestCase):
    def get_page(self, lines):
        store_preview(self.client, [invoice(lines)])
        response = self.client.get(reverse("parts:invoice_preview"))
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode("utf-8")

    def test_full_description_is_rendered_without_length_limit(self):
        self.assertGreater(len(LONG_DESCRIPTION), 100)
        response, html = self.get_page([{"description": LONG_DESCRIPTION}, {"description": TRUNCATED_CONRAD}])
        self.assertContains(response, LONG_DESCRIPTION)
        self.assertContains(response, TRUNCATED_CONRAD)
        self.assertNotIn("maxlength", html)
        self.assertRegex(html, r"<textarea[^>]*rows=\"2\"[^>]*>" + re.escape(LONG_DESCRIPTION) + "</textarea>")

    def test_each_line_block_has_labelled_inputs(self):
        _response, html = self.get_page([{"description": "Testteil"}])
        block = re.search(r'<section class="import-line.*?</section>', html, flags=re.DOTALL).group(0)
        for label in ("Menge", "Beschreibung", "Typ", "Wert", "Verpackung", "Projekt", "Treffer", "Überspringen"):
            self.assertRegex(block, rf"<label[^>]*>\s*(<input[^>]*>)?\s*{label}", label)
        self.assertIn("1000000", block)  # Art.-Nr. als Text
        self.assertNotIn("<table", block)

    def test_supplier_is_in_document_header_and_not_a_line_field(self):
        _response, html = self.get_page([{"description": "A"}, {"description": "B"}])
        self.assertNotIn("supplier::", html)
        header = re.search(r'<dl class="doc-header">.*?</dl>', html, flags=re.DOTALL).group(0)
        self.assertIn("Lieferant", header)
        self.assertIn("Conrad Electronic SE", header)
        for block in re.findall(r'<section class="import-line.*?</section>', html, flags=re.DOTALL):
            self.assertNotIn("Conrad Electronic SE", re.sub(r'name="[^"]*"', "", block))

    def test_header_shows_document_facts_and_status_markers(self):
        Part.objects.create(supplier="Conrad Electronic SE", supplier_part_no="1000000", description="Bekannt")
        response, html = self.get_page([{"description": "Bekannt"}, {"description": "Neu"}])
        for text in ("datei0.pdf", "9600000001", "Erkanntes Layout", "Endbetrag (brutto)"):
            self.assertContains(response, text)
        self.assertIn("import-line status-matched", html)
        self.assertIn("import-line status-new", html)

    def test_type_and_packaging_offer_autocomplete_from_the_database(self):
        Part.objects.create(part_type="Widerstand", packaging="0805")
        _response, html = self.get_page([{"description": "X"}])
        self.assertRegex(html, r'<datalist id="known-part-types">.*Widerstand.*</datalist>')
        self.assertRegex(html, r'<datalist id="known-packagings">.*0805.*</datalist>')
        self.assertIn('list="known-part-types"', html)

    def test_no_english_ui_words(self):
        response, _html = self.get_page([{"description": "Testteil"}])
        text = _extract_visible_text(response.content.decode("utf-8"))
        for word in ENGLISH_UI_WORDS:
            self.assertNotRegex(text, rf"\b{re.escape(word)}\b")


class PreviewConfirmTests(TestCase):
    def confirm(self, docs, **post):
        return self.client.post(reverse("parts:invoice_preview"), post)

    def test_edited_description_feeds_new_part_and_purchase_line_keeps_printed_text(self):
        docs = store_preview(self.client, [invoice([{"description": TRUNCATED_CONRAD}])])
        prefix = prefix_of(docs)
        self.confirm(docs, **{f"description::{prefix}": "Universalwerkzeug RESQME Rettungswerkzeug, korrigiert"})
        part = Part.objects.get()
        self.assertEqual(part.description, "Universalwerkzeug RESQME Rettungswerkzeug, korrigiert")
        self.assertEqual(PurchaseLine.objects.get().description, TRUNCATED_CONRAD)

    def test_matched_part_classification_can_be_corrected_but_never_cleared(self):
        part = Part.objects.create(
            supplier="Conrad Electronic SE", supplier_part_no="1000000", part_type="Kondensator", value="10 nF", packaging="0805"
        )
        docs = store_preview(self.client, [invoice([{"description": "Kerko 100 nF"}])])
        prefix = prefix_of(docs)
        self.confirm(
            docs,
            **{
                f"part_type::{prefix}": "kondensator",  # nur Schreibweise: vorhandene bleibt
                f"value::{prefix}": "100 nF",
                f"packaging::{prefix}": "",
            },
        )
        part.refresh_from_db()
        self.assertEqual(part.part_type, "Kondensator")
        self.assertEqual(part.value, "100 nF")
        self.assertEqual(part.packaging, "0805")

    def test_new_part_reuses_existing_spelling_of_type_and_packaging(self):
        Part.objects.create(part_type="Widerstand", packaging="SMD 0805")
        docs = store_preview(self.client, [invoice([{"description": "Etwas"}])])
        prefix = prefix_of(docs)
        self.confirm(docs, **{f"part_type::{prefix}": "widerstand", f"packaging::{prefix}": "smd  0805"})
        new_part = Part.objects.exclude(part_type="Widerstand", description="").get()
        self.assertEqual((new_part.part_type, new_part.packaging), ("Widerstand", "SMD 0805"))
        self.assertEqual(Part.objects.filter(part_type="Widerstand").count(), 2)

    def test_line_supplier_is_the_document_supplier(self):
        docs = store_preview(self.client, [invoice([{"description": "Etwas"}])])
        self.confirm(docs)
        self.assertEqual(Part.objects.get().supplier, "Conrad Electronic SE")
