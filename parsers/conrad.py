from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from .formats import canonicalise_match_value, normalise_unicode_text, parse_date, parse_decimal


class ConradInvoiceParser:
    supplier = "Conrad Electronic SE"

    @staticmethod
    def detect_supplier(text: str) -> str | None:
        cleaned = normalise_unicode_text(text).casefold()
        if "conrad" in cleaned:
            return "Conrad Electronic SE"
        return None

    @staticmethod
    def _find_header_value(text: str, label: str) -> str:
        pattern = rf"{re.escape(label)}\s*[:.]?\s*(.+)"
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            candidate = candidate.splitlines()[0].strip()
            return candidate
        return ""

    @staticmethod
    def _parse_line(row: str) -> dict[str, Any] | None:
        cleaned = normalise_unicode_text(row)
        if not cleaned:
            return None

        pattern = re.compile(
            r"^(?P<pos>\d+)\s+(?P<supplier_part_no>\S+)\s+(?P<description>.+?)\s+"
            r"(?P<quantity>\d+)\s+(?P<tax_class>\d+)\s+(?P<unit_price>[\d.,]+)\s+(?P<total>[\d.,]+)$"
        )
        match = pattern.match(cleaned)
        if not match:
            return None

        quantity = int(match.group("quantity"))
        unit_price = parse_decimal(match.group("unit_price"), locale="de")
        total = quantity * unit_price

        return {
            "supplier_part_no": match.group("supplier_part_no"),
            "mf_part_no": "",
            "description": match.group("description").strip(),
            "quantity": quantity,
            "unit_price": unit_price,
            "total": total,
            "tax_class": match.group("tax_class"),
        }

    @classmethod
    def parse_invoice_text(cls, text: str) -> dict[str, Any]:
        cleaned_text = normalise_unicode_text(text)
        if not cleaned_text:
            raise ValueError("Der Belegtext ist leer.")

        invoice_no = cls._find_header_value(cleaned_text, "Rechnungs-Nr.") or cls._find_header_value(cleaned_text, "Rechnung Nr")
        invoice_date_text = cls._find_header_value(cleaned_text, "Rechnungs-/Leistungsdatum") or cls._find_header_value(cleaned_text, "Rechnungsdatum")
        if not invoice_no:
            raise ValueError("Conrad-Belegnummer konnte im Kopfbereich nicht gefunden werden.")
        if not invoice_date_text:
            raise ValueError("Conrad-Belegdatum konnte im Kopfbereich nicht gefunden werden.")

        invoice_date = parse_date(invoice_date_text, locale="de")
        rows = []
        lines = cleaned_text.splitlines()
        in_table = False
        for line in lines:
            stripped = normalise_unicode_text(line)
            lowered = stripped.casefold()
            if "art.-nr." in lowered or "artikelbezeichnung" in lowered:
                in_table = True
                continue
            if not in_table:
                continue
            if "summe nettowert" in lowered or "summe" in lowered and "netto" in lowered:
                break
            if not stripped:
                continue
            parsed = cls._parse_line(stripped)
            if parsed:
                rows.append(parsed)

        if not rows:
            raise ValueError("Es konnten keine Conrad-Zeilen aus dem hochgeladenen Text gelesen werden.")

        net_total = None
        net_total_match = re.search(r"Summe\s+Nettowert\s*[:.]?\s*([0-9.,]+)", cleaned_text, flags=re.IGNORECASE)
        if net_total_match:
            net_total = parse_decimal(net_total_match.group(1), locale="de")

        parsed_total = sum((line["total"] for line in rows), Decimal("0.00"))
        warning = None
        if net_total is not None and abs(parsed_total - net_total) > Decimal("0.01"):
            warning = f"Gelesene Belegsumme {parsed_total:.2f} EUR weicht von der Kopf-Summe {net_total:.2f} EUR ab."

        return {
            "supplier": cls.supplier,
            "document_type": "invoice",
            "invoice_no": invoice_no,
            "invoice_date": invoice_date,
            "lines": [
                {
                    "supplier": cls.supplier,
                    "supplier_part_no": line["supplier_part_no"],
                    "mf_part_no": line.get("mf_part_no", ""),
                    "description": line["description"],
                    "quantity": line["quantity"],
                    "unit_price": line["unit_price"],
                    "currency": "EUR",
                }
                for line in rows
            ],
            "warning": warning,
        }
