from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from .models import Part
from .tests_import_preview import invoice, prefix_of, store_preview


class PreviewManufacturerFieldTests(TestCase):
    def preview(self, number="200000"):
        return store_preview(self.client, [invoice([{"description": "Teil", "no": number}], number="9600000001")])

    def confirm(self, **post):
        return self.client.post(reverse("parts:invoice_preview"), post)

    def test_new_line_renders_editable_inputs(self):
        docs = self.preview()
        prefix = prefix_of(docs)
        html = self.client.get(reverse("parts:invoice_preview")).content.decode("utf-8")
        for field in ("part_subtype", "manufacturer", "mf_part_no"):
            self.assertIn(f'name="{field}::{prefix}"', html)
        self.assertIn("Hersteller-Nr.", html)

    def test_new_part_saves_subtype_manufacturer_and_mpn(self):
        prefix = prefix_of(self.preview())
        self.confirm(**{
            f"part_type::{prefix}": "Diode",
            f"part_subtype::{prefix}": "Zener",
            f"manufacturer::{prefix}": "  Nexperia ",
            f"mf_part_no::{prefix}": "BZX84-C5V1",
        })
        part = Part.objects.get(supplier_part_no="200000")
        self.assertEqual((part.part_subtype, part.manufacturer, part.mf_part_no), ("Zener", "Nexperia", "BZX84-C5V1"))

    def test_empty_values_are_accepted(self):
        prefix = prefix_of(self.preview())
        response = self.confirm(**{f"part_subtype::{prefix}": "", f"manufacturer::{prefix}": "", f"mf_part_no::{prefix}": ""})
        self.assertRedirects(response, reverse("parts:list"))
        part = Part.objects.get(supplier_part_no="200000")
        self.assertEqual((part.part_subtype, part.manufacturer, part.mf_part_no), ("", "", ""))

    def test_matched_line_is_read_only_and_post_does_not_change_part(self):
        Part.objects.create(
            supplier="Conrad Electronic SE", supplier_part_no="200000", part_type="Diode",
            part_subtype="TVS", manufacturer="Vishay", mf_part_no="SMBJ5.0A",
        )
        prefix = prefix_of(self.preview())
        html = self.client.get(reverse("parts:invoice_preview")).content.decode("utf-8")
        for field in ("part_subtype", "manufacturer", "mf_part_no"):
            self.assertNotIn(f'name="{field}::{prefix}"', html)
        for text in ("TVS", "Vishay", "SMBJ5.0A"):
            self.assertIn(text, html)
        self.confirm(**{f"part_subtype::{prefix}": "Zener", f"manufacturer::{prefix}": "X", f"mf_part_no::{prefix}": "Y"})
        part = Part.objects.get(supplier_part_no="200000")
        self.assertEqual((part.part_subtype, part.manufacturer, part.mf_part_no), ("TVS", "Vishay", "SMBJ5.0A"))
