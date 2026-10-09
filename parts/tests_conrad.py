from __future__ import annotations

import re
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from parsers.conrad import ConradInvoiceParser
from .models import Part, Project, PurchaseLine
from .tests_ui_language import ENGLISH_UI_WORDS, _extract_visible_text
from .views import QUANTITY_MODE_ADD, _build_batch_preview

LAYOUT_A_TEXT = """Rechnung
Rechnungs-/Leistungsdatum 18.06.2024
Rechnungs-Nr. 9700000001
Pos. Art.-Nr. Artikelbezeichnung
Menge StK Einzelpreis Gesamtpreis
10 1258976 IC HEX SCHMITT-TR TESTTYP SOIC-14 50 19 0,11 5,50
20 1492354 Buchsenleiste 1x6 pol gewinkelt Preis / 1.000 ST 40 19 147,50 5,90
Summe Nettowert 9,58
USt. 19,0 % aus 9,58 1,82
StK 19: 19% Umsatzsteuer
Summe Bruttowert 11,40
Endbetrag 11,40EUR
"""


def import_batch(client, texts):
    return import_parsed(client, [ConradInvoiceParser().parse_invoice_text(text) for text in texts])


def import_parsed(client, parsed_documents):
    documents = [{"file_name": f"beleg{i}.pdf", "invoice_data": parsed} for i, parsed in enumerate(parsed_documents)]
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
    }
    session.save()
    return docs


class ConradGrossBasisTests(TestCase):
    def test_price_basis_is_gross_and_net_is_derived_per_line_vat(self):
        parsed = ConradInvoiceParser().parse_invoice_text(LAYOUT_A_TEXT)
        self.assertEqual(parsed["price_basis"], "gross")
        self.assertEqual(parsed["gross_total"], Decimal("11.40"))
        self.assertEqual(parsed["net_total"], Decimal("9.58"))
        self.assertEqual(parsed["warning"], "")
        first, second = parsed["lines"]
        self.assertEqual(first["gross_unit_price"], Decimal("0.1100"))
        self.assertEqual(first["unit_price"], Decimal("0.0924"))  # 0,11 ÷ 1,19
        self.assertEqual(first["vat_rate"], Decimal("19"))
        # Preis je 1.000 Stück: Einzelpreis aus Gesamtpreis ÷ Menge, nicht der gedruckte Preis
        self.assertEqual(second["gross_unit_price"], Decimal("0.1475"))
        self.assertEqual(second["printed_unit_price_gross"], Decimal("147.50"))
        self.assertEqual(second["line_total_gross"], Decimal("5.90"))

    def test_check_compares_line_totals_with_gross_total_not_net(self):
        # Summe der Zeilen (11,40) stimmt mit dem Bruttobetrag überein, nicht mit dem Nettowert (9,58).
        self.assertEqual(ConradInvoiceParser().parse_invoice_text(LAYOUT_A_TEXT)["warnings"], [])
        tampered = LAYOUT_A_TEXT.replace("Endbetrag 11,40EUR", "Endbetrag 11,41EUR")
        parsed = ConradInvoiceParser().parse_invoice_text(tampered)
        self.assertTrue(all("9700000001" in warning for warning in parsed["warnings"]))
        self.assertIn("Prüfung a", parsed["warnings"][0])

    def test_unknown_tax_class_is_a_visible_warning(self):
        broken = LAYOUT_A_TEXT.replace("StK 19: 19% Umsatzsteuer\n", "")
        parsed = ConradInvoiceParser().parse_invoice_text(broken)
        self.assertTrue(any("Prüfung e" in warning and "9700000001" in warning for warning in parsed["warnings"]))

    def test_import_stores_gross_lines_and_net_part_price(self):
        docs = import_batch(self.client, [LAYOUT_A_TEXT])
        self.assertEqual(docs[0]["document_total"], "11.40")
        response = self.client.post(reverse("parts:invoice_preview"), {})
        self.assertRedirects(response, reverse("parts:list"))

        line = PurchaseLine.objects.get(supplier_part_no="1258976")
        self.assertFalse(line.price_is_net)
        self.assertTrue(line.price_is_derived)
        self.assertEqual(line.unit_price, Decimal("0.1100"))
        self.assertEqual(line.line_total, Decimal("5.50"))
        self.assertEqual(Part.objects.get(supplier_part_no="1258976").last_price, Decimal("0.0924"))
        self.assertEqual(PurchaseLine.objects.get(supplier_part_no="1492354").line_total, Decimal("5.90"))
        self.assertEqual(line.document.document_total, Decimal("11.40"))

    def test_project_view_shows_conrad_lines_as_gross(self):
        project = Project.objects.create(name="Test")
        import_batch(self.client, [LAYOUT_A_TEXT])
        self.client.post(reverse("parts:invoice_preview"), {"batch_project_id": str(project.pk)})
        response = self.client.get(reverse("parts:project_documents"), {"project": str(project.pk)})
        self.assertContains(response, "11,40")
        self.assertEqual(response.context["gross_total_sum"], Decimal("11.4000"))
        self.assertEqual(response.context["net_total_sum"], Decimal("0.0000"))

    def test_preview_labels_are_german_and_show_both_prices(self):
        import_batch(self.client, [LAYOUT_A_TEXT])
        response = self.client.get(reverse("parts:invoice_preview"))
        self.assertContains(response, "Nettopreis")
        self.assertContains(response, "Bruttopreis")
        self.assertContains(response, "abgeleitet")
        text = _extract_visible_text(response.content.decode("utf-8"))
        for word in ENGLISH_UI_WORDS:
            self.assertNotRegex(text, rf"\b{re.escape(word)}\b")
