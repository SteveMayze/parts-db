from __future__ import annotations

from datetime import date

from django.test import TestCase
from django.urls import reverse

from .models import PurchaseDocument, Supplier


class DocumentListSortTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reichelt = Supplier.objects.create(name="Reichelt")
        cls.conrad = Supplier.objects.create(name="Conrad")
        make = PurchaseDocument.objects.create
        cls.d1 = make(supplier=cls.reichelt, document_type="invoice", document_number="9", document_date=date(2024, 1, 5))
        cls.d2 = make(supplier=cls.reichelt, document_type="invoice", document_number="100", document_date=date(2024, 3, 1))
        cls.d3 = make(supplier=cls.conrad, document_type="invoice", document_number="25", document_date=date(2023, 6, 1))

    def numbers(self, **params):
        response = self.client.get(reverse("parts:documents"), params)
        self.assertEqual(response.status_code, 200)
        return [d.document_number for d in response.context["documents"]]

    def test_default_order_unchanged(self):
        self.assertEqual(self.numbers(), ["100", "9", "25"])

    def test_sort_by_date(self):
        self.assertEqual(self.numbers(sort="date", dir="asc"), ["25", "9", "100"])
        self.assertEqual(self.numbers(sort="date", dir="desc"), ["100", "9", "25"])

    def test_sort_by_number_is_numeric_for_different_lengths(self):
        self.assertEqual(self.numbers(sort="number", dir="asc"), ["9", "25", "100"])
        self.assertEqual(self.numbers(sort="number", dir="desc"), ["100", "25", "9"])

    def test_invalid_values_fall_back_to_default(self):
        default = self.numbers()
        self.assertEqual(self.numbers(sort="document_date; drop", dir="asc"), default)
        self.assertEqual(self.numbers(sort="date", dir="sideways"), default)
        self.assertEqual(self.numbers(sort="supplier", dir="asc"), default)

    def test_equal_dates_have_stable_order(self):
        PurchaseDocument.objects.create(supplier=self.conrad, document_type="invoice", document_number="7", document_date=date(2024, 1, 5))
        first = self.numbers(sort="date", dir="asc")
        self.assertEqual(first, self.numbers(sort="date", dir="asc"))
        self.assertEqual(first, ["25", "9", "7", "100"])

    def test_sort_preserves_filter(self):
        self.assertEqual(self.numbers(sort="number", dir="desc", supplier=self.reichelt.pk), ["100", "9"])

    def test_header_links_and_form_keep_filter_and_sort(self):
        response = self.client.get(reverse("parts:documents"), {"supplier": self.reichelt.pk, "sort": "date", "dir": "asc"})
        headers = response.context["sort_headers"]
        self.assertIn(f"supplier={self.reichelt.pk}", headers["date"]["url"])
        self.assertIn("dir=desc", headers["date"]["url"])
        self.assertEqual(headers["date"]["arrow"], "▲")
        self.assertIn("dir=asc", headers["number"]["url"])
        self.assertContains(response, '<input type="hidden" name="sort" value="date">')
