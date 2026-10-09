from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from parsers.conrad import ConradInvoiceParser
from . import conrad_fixtures as fx
from .models import ImportedDocument, Part, PurchaseDocument, PurchaseLine
from .tests_conrad import LAYOUT_A_TEXT, import_parsed
from .tests_ui_language import ENGLISH_UI_WORDS, _extract_visible_text

SAMPLES = Path("samples/conrad")


def parse(pdf: bytes, name: str = "beleg.pdf") -> dict:
    return ConradInvoiceParser().parse_pdf(pdf, filename=name)


def checks_failed(parsed: dict) -> list[str]:
    return sorted(re.search(r"Prüfung (\w)", warning).group(1) for warning in parsed["warnings"])


def discount_doc(**override):
    """Three lines, a discount and a note row; one tax class with 19%."""
    values = {
        "invoice_no": "9700000001",
        "date": "26.05.2020",
        "endbetrag": "91,21",
        "line_totals": ("9,99", "9,99", "79,00"),
        "printed_units": ("9,99", "9,99", "79,00"),
        "discount": "-7,77",
        "netto": ("76,65", "19,0", "19", "14,56"),
        "legend": True,
    }
    values.update(override)
    rows = [
        fx.note("Ihr Vorteilscode wurde erfolgreich eingelöst."),
        fx.item("1609627", "LADEGERÄT SECHSFACH TESTARTIKEL EINS", "19", "1", values["printed_units"][0], values["line_totals"][0]),
        fx.item("1420590", "TASCHE FÜR HARDCASE TESTARTIKEL ZWEI", "19", "1", values["printed_units"][1], values["line_totals"][1]),
        fx.item("1675147", "Tuner DAB Radio mit Bluetooth", "19", "1", values["printed_units"][2], values["line_totals"][2]),
        fx.discount("Sonderrabatt", values["discount"]),
        fx.netto(*values["netto"][:1], values["netto"][1], values["netto"][2], values["netto"][3]),
    ]
    if values["legend"]:
        rows.append(fx.legend("19", "19"))
    return fx.make_layout_b_pdf(values["invoice_no"], values["date"], [rows], values["endbetrag"])


def split_doc_2of2(invoice_no="9700000002"):
    rows = [
        fx.item("1490490", "Universalwerkzeug TESTWERKZEUG Rettung Or", "16", "4", "10,71", "42,84"),
        fx.item("1401762", "9POL LEITERPLATTENSTECKVERBINDER (65801-", "16", "5", "2,35", "11,75"),
        fx.item("747842", "STIFTKONTAKT 0,2 - 0,5", "16", "32", "0,07", "2,18"),
        fx.info_heading(),
        fx.info_item("747842", "STIFTKONTAKT 0,2 - 0,5", "68"),
        fx.netto("48,94", "16,0", "16", "7,83"),
        fx.legend("16", "16"),
    ]
    return fx.make_layout_b_pdf(invoice_no, "21.07.2020", [rows], "56,77", partial="2/2")


def split_doc_1of2(invoice_no="9700000003"):
    rows = [
        fx.item("747842", "STIFTKONTAKT 0,2 - 0,5", "16", "68", "0,07", "4,64"),
        fx.netto("4,00", "16,0", "16", "0,64"),
        fx.legend("16", "16"),
    ]
    return fx.make_layout_b_pdf(invoice_no, "21.07.2020", [rows], "4,64", partial="1/2")


class LayoutDetectionTests(SimpleTestCase):
    def test_layout_a_and_b_are_detected(self):
        self.assertEqual(ConradInvoiceParser().parse_invoice_text(LAYOUT_A_TEXT)["layout"], "A")
        self.assertEqual(parse(split_doc_1of2())["layout"], "B")

    def test_unknown_layout_lists_found_and_missing_markers(self):
        text = "Rechnung\nRechnungs-Nr. 1\nRechnungs-/Leistungsdatum 01.01.2024\nPos. Art.-Nr. Artikelbezeichnung\n"
        with self.assertRaises(ValueError) as caught:
            ConradInvoiceParser().parse_invoice_text(text)
        message = str(caught.exception)
        self.assertIn("Unbekanntes Conrad-Layout", message)
        self.assertIn("Layout A: gefunden: Art.-Nr.; fehlend: Summe Nettowert", message)
        self.assertIn("Layout B: gefunden: nichts; fehlend: Bestell-Nr., NETTO-Summenzeile", message)

    def test_title_other_than_rechnung_is_unsupported(self):
        rows = [fx.item("1609627", "Testartikel", "19", "1", "1,19", "1,19"), fx.netto("1,00", "19,0", "19", "0,19"), fx.legend("19", "19")]
        pdf = fx.make_layout_b_pdf("9700000009", "01.01.2020", [rows], "1,19", title="Gutschrift")
        with self.assertRaisesRegex(ValueError, "Nicht unterstützter Belegtyp bei Conrad: Gutschrift"):
            parse(pdf)
        with self.assertRaisesRegex(ValueError, "Nicht unterstützter Belegtyp"):
            ConradInvoiceParser().parse_invoice_text(LAYOUT_A_TEXT.replace("Rechnung\n", "Lieferschein\n", 1))


class LayoutBTests(SimpleTestCase):
    def test_rows_with_offset_numbers_discount_notes_and_header(self):
        parsed = parse(discount_doc())
        self.assertEqual(parsed["invoice_no"], "9700000001")
        self.assertEqual(parsed["invoice_date"].isoformat(), "2020-05-26")
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual(
            [(l["position"], l["supplier_part_no"], l["quantity"], l["line_total_gross"]) for l in parsed["lines"]],
            [("1", "1609627", 1, Decimal("9.99")), ("2", "1420590", 1, Decimal("9.99")), ("3", "1675147", 1, Decimal("79.00"))],
        )
        self.assertEqual(parsed["discount_total"], Decimal("-7.77"))
        self.assertEqual(parsed["gross_total"], Decimal("91.21"))
        self.assertEqual(parsed["net_total"], Decimal("76.65"))
        self.assertIn("Ihr Vorteilscode wurde erfolgreich eingelöst.", parsed["notes"])
        self.assertEqual(parsed["lines"][2]["unit_price"], Decimal("66.3866"))  # 79,00 ÷ 1,19

    def test_descriptions_stay_as_printed_and_customer_number_is_not_kept(self):
        parsed = parse(split_doc_2of2())
        self.assertEqual(parsed["lines"][1]["description"], "9POL LEITERPLATTENSTECKVERBINDER (65801-")
        self.assertNotIn("10000001", repr(parsed))

    def test_split_delivery_info_block_is_a_note_and_never_a_line(self):
        parsed = parse(split_doc_2of2())
        self.assertEqual([l["supplier_part_no"] for l in parsed["lines"]], ["1490490", "1401762", "747842"])
        self.assertEqual(sum(l["quantity"] for l in parsed["lines"]), 41)
        self.assertIn("separat geliefert: 68 × 747842", parsed["notes"])
        self.assertIn("Teillieferung 2/2", parsed["notes"])
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual(parsed["gross_total"], Decimal("56.77"))

    def test_rate_comes_from_legend_16_percent_and_unit_price_is_derived_from_total(self):
        parsed = parse(split_doc_2of2())
        self.assertTrue(all(l["vat_rate"] == Decimal("16") for l in parsed["lines"]))
        pins = parsed["lines"][2]
        self.assertEqual(pins["printed_unit_price_gross"], Decimal("0.07"))
        self.assertEqual(pins["gross_unit_price"], Decimal("0.0681"))  # 2,18 ÷ 32
        self.assertEqual(pins["unit_price"], Decimal("0.0587"))
        self.assertEqual(parsed["lines"][0]["unit_price"], Decimal("9.2328"))  # 10,71 ÷ 1,16

    def test_two_tax_classes(self):
        rows = [
            fx.item("1000001", "Artikel mit 16 Prozent", "16", "2", "5,80", "11,60"),
            fx.item("1000002", "Artikel mit 5 Prozent", "17", "1", "10,50", "10,50"),
            fx.netto("10,00", "16,0", "16", "1,60"),
            fx.netto("10,00", "5,0", "17", "0,50"),
            fx.legend("16", "16"),
            fx.legend("17", "5"),
        ]
        parsed = parse(fx.make_layout_b_pdf("9700000010", "01.10.2020", [rows], "22,10"))
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual([l["vat_rate"] for l in parsed["lines"]], [Decimal("16"), Decimal("5")])
        self.assertEqual(parsed["lines"][0]["unit_price"], Decimal("5.0000"))
        self.assertEqual(parsed["lines"][1]["unit_price"], Decimal("10.0000"))
        self.assertEqual(parsed["net_total"], Decimal("20.00"))

    def test_multi_page_document_totals_on_last_page(self):
        page_one = [
            fx.item("1000001", "Erster Artikel", "16", "1", "1,16", "1,16"),
            fx.item("1000002", "Zweiter Artikel", "16", "2", "1,16", "2,32"),
        ]
        page_two = [
            fx.item("1000003", "Dritter Artikel", "16", "3", "1,16", "3,48"),
            fx.netto("6,00", "16,0", "16", "0,96"),
            fx.legend("16", "16"),
        ]
        parsed = parse(fx.make_layout_b_pdf("9700000011", "01.10.2020", [page_one, page_two], "6,96"))
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual([(l["position"], l["supplier_part_no"]) for l in parsed["lines"]], [("1", "1000001"), ("2", "1000002"), ("3", "1000003")])
        self.assertEqual(parsed["gross_total"], Decimal("6.96"))

    def test_bank_block_is_ignored(self):
        self.assertNotIn("IBAN", repr(parse(discount_doc())["notes"]))


class SelfCheckTests(SimpleTestCase):
    def assert_only(self, parsed, letter):
        self.assertEqual(checks_failed(parsed), [letter], parsed["warnings"])
        self.assertIn("9700000001", parsed["warnings"][0])
        self.assertIn("beleg.pdf", parsed["warnings"][0])

    def test_untampered_document_has_no_warnings(self):
        self.assertEqual(parse(discount_doc())["warnings"], [])

    def test_check_a_line_total_sum(self):
        self.assert_only(parse(discount_doc(line_totals=("10,00", "9,99", "79,00"), printed_units=("10,00", "9,99", "79,00"))), "a")

    def test_check_b_net_plus_vat(self):
        self.assert_only(parse(discount_doc(netto=("76,66", "19,0", "19", "14,56"))), "b")

    def test_check_c_net_times_rate(self):
        self.assert_only(parse(discount_doc(netto=("76,63", "19,0", "19", "14,58"))), "c")

    def test_check_d_printed_unit_price(self):
        self.assert_only(parse(discount_doc(printed_units=("9,89", "9,99", "79,00"))), "d")

    def test_check_e_tax_class_missing_in_legend(self):
        self.assert_only(parse(discount_doc(legend=False)), "e")

    def test_wrong_endbetrag_fails_a_and_b(self):
        self.assertEqual(checks_failed(parse(discount_doc(endbetrag="91,22"))), ["a", "b"])


class SamplesTests(SimpleTestCase):
    """Real invoices; skipped when samples/conrad is not available."""

    def setUp(self):
        if not SAMPLES.exists():
            self.skipTest("Conrad-Beispielbelege nicht vorhanden")

    def parse_sample(self, number):
        path = SAMPLES / f"{number}.pdf"
        if not path.exists():
            self.skipTest(f"{number}.pdf nicht vorhanden")
        return parse(path.read_bytes(), path.name)

    def lines(self, parsed):
        return [(l["supplier_part_no"], l["quantity"], l["line_total_gross"]) for l in parsed["lines"]]

    def test_discount_invoice(self):
        parsed = self.parse_sample("9708923410")
        self.assertEqual(parsed["layout"], "B")
        self.assertEqual(self.lines(parsed), [("1609627", 1, Decimal("9.99")), ("1420590", 1, Decimal("9.99")), ("1675147", 1, Decimal("79.00"))])
        self.assertEqual((parsed["discount_total"], parsed["gross_total"], parsed["net_total"]), (Decimal("-7.77"), Decimal("91.21"), Decimal("76.65")))
        self.assertEqual(parsed["warnings"], [])

    def test_split_delivery_2_of_2(self):
        parsed = self.parse_sample("9711104108")
        self.assertEqual(self.lines(parsed), [("1490490", 4, Decimal("42.84")), ("1401762", 5, Decimal("11.75")), ("747842", 32, Decimal("2.18"))])
        self.assertIn("Teillieferung 2/2", parsed["notes"])
        self.assertIn("separat geliefert: 68 × 747842", parsed["notes"])
        self.assertEqual((parsed["gross_total"], parsed["net_total"]), (Decimal("56.77"), Decimal("48.94")))
        self.assertEqual(parsed["warnings"], [])

    def test_split_delivery_1_of_2(self):
        parsed = self.parse_sample("9711254476")
        self.assertEqual(self.lines(parsed), [("747842", 68, Decimal("4.64"))])
        self.assertEqual((parsed["gross_total"], parsed["net_total"]), (Decimal("4.64"), Decimal("4.00")))
        self.assertEqual(parsed["warnings"], [])

    def test_all_samples_parse_without_warnings(self):
        paths = sorted(SAMPLES.glob("*.pdf"))
        if not paths:
            self.skipTest("keine Beispielbelege")
        for path in paths:
            parsed = parse(path.read_bytes(), path.name)
            self.assertEqual(parsed["warnings"], [], path.name)
            self.assertIn(parsed["layout"], {"A", "B"}, path.name)

    def test_layout_a_samples(self):
        parsed = self.parse_sample("9764222324")
        self.assertEqual(parsed["layout"], "A")
        self.assertEqual(parsed["discount_total"], Decimal("-9.00"))
        self.assertEqual(parsed["lines"][1]["gross_unit_price"], Decimal("0.0129"))  # Preis / 1.000 ST
        self.assertEqual(parsed["gross_total"], Decimal("259.87"))
        split = self.parse_sample("9764901474")
        self.assertIn("Teillieferung 1/2", split["notes"])
        self.assertIn("separat geliefert: 2 × 604069", split["notes"])
        self.assertEqual(len(split["lines"]), 2)


class SplitDeliveryBatchTests(TestCase):
    def import_both(self):
        first = parse(split_doc_2of2(), "zweite.pdf")
        second = parse(split_doc_1of2(), "erste.pdf")
        import_parsed(self.client, [first, second])
        return self.client.post(reverse("parts:invoice_preview"), {})

    def test_two_documents_one_part_and_no_double_count(self):
        response = self.import_both()
        self.assertRedirects(response, reverse("parts:list"))
        self.assertEqual(PurchaseDocument.objects.count(), 2)
        self.assertEqual(ImportedDocument.objects.count(), 2)
        part = Part.objects.get(supplier_part_no="747842")
        self.assertEqual(Part.objects.filter(supplier_part_no="747842").count(), 1)
        self.assertEqual(part.quantity, 100)  # 32 + 68, nicht 168
        self.assertEqual(PurchaseLine.objects.filter(supplier_part_no="747842").count(), 2)
        self.assertEqual(sum(PurchaseLine.objects.filter(supplier_part_no="747842").values_list("quantity", flat=True)), Decimal("100"))

    def test_notes_and_discount_are_stored_on_the_document(self):
        self.import_both()
        second = PurchaseDocument.objects.get(document_number="9700000002")
        self.assertIn("Teillieferung 2/2", second.notes)
        self.assertIn("separat geliefert: 68 × 747842", second.notes)
        self.assertEqual(second.document_total, Decimal("56.77"))

        import_parsed(self.client, [parse(discount_doc(), "rabatt.pdf")])
        self.client.post(reverse("parts:invoice_preview"), {})
        discounted = PurchaseDocument.objects.get(document_number="9700000001")
        self.assertEqual(discounted.discount_total, Decimal("-7.77"))
        self.assertEqual(discounted.lines.count(), 3)
        self.assertEqual(sum(line.line_total for line in discounted.lines.all()), Decimal("98.98"))
        self.assertTrue(all(not line.price_is_net for line in discounted.lines.all()))

    def test_preview_shows_layout_notes_discount_in_german(self):
        import_parsed(self.client, [parse(split_doc_2of2(), "a.pdf"), parse(discount_doc(), "b.pdf")])
        response = self.client.get(reverse("parts:invoice_preview"))
        self.assertContains(response, "Erkanntes Layout")
        self.assertContains(response, "Teillieferung 2/2")
        self.assertContains(response, "separat geliefert: 68 × 747842")
        self.assertContains(response, "Rabatt (brutto")
        self.assertContains(response, "-7,77")
        text = _extract_visible_text(response.content.decode("utf-8"))
        for word in ENGLISH_UI_WORDS:
            self.assertNotRegex(text, rf"\b{re.escape(word)}\b")

    def test_real_split_deliveries_in_one_batch(self):
        paths = [SAMPLES / "9711104108.pdf", SAMPLES / "9711254476.pdf"]
        if not all(path.exists() for path in paths):
            self.skipTest("Beispielbelege nicht vorhanden")
        import_parsed(self.client, [parse(path.read_bytes(), path.name) for path in paths])
        self.client.post(reverse("parts:invoice_preview"), {})
        self.assertEqual(PurchaseDocument.objects.count(), 2)
        self.assertEqual(Part.objects.get(supplier_part_no="747842").quantity, 100)
