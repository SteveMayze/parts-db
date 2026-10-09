from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
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
        printed_unit_price = parse_decimal(match.group("unit_price"), locale="de")
        total = parse_decimal(match.group("total"), locale="de")
        # Conrad druckt Bruttopreise; der gedruckte Einzelpreis kann gerundet sein
        # oder sich auf 1.000 Stück beziehen, daher Einzelpreis aus Gesamtpreis ÷ Menge.
        unit_price = (total / Decimal(quantity)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if quantity else printed_unit_price

        return {
            "supplier_part_no": match.group("supplier_part_no"),
            "mf_part_no": "",
            "description": match.group("description").strip(),
            "quantity": quantity,
            "unit_price": unit_price,
            "printed_unit_price": printed_unit_price,
            "total": total,
            "tax_class": match.group("tax_class"),
        }

    @staticmethod
    def _parse_tax_legend(text: str) -> dict[str, Decimal]:
        legend = {}
        for tax_class, rate in re.findall(r"StK\.?\s*(\d+)\s*:\s*(\d+(?:,\d+)?)\s*%", text):
            legend[tax_class] = parse_decimal(rate, locale="de")
        return legend

    @staticmethod
    def _find_amount(text: str, label: str) -> Decimal | None:
        match = re.search(rf"{label}\s*[:.]?\s*(-?[0-9][0-9.,]*)", text, flags=re.IGNORECASE)
        return parse_decimal(match.group(1), locale="de") if match else None

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

        name = invoice_no
        legend = cls._parse_tax_legend(cleaned_text)
        for row in rows:
            if row["tax_class"] not in legend:
                raise ValueError(f"Steuerklasse {row['tax_class']} fehlt in der Legende des Belegs {name}.")

        net_total = cls._find_amount(cleaned_text, r"Summe\s+Nettowert")
        gross_total = cls._find_amount(cleaned_text, r"Summe\s+Bruttowert")
        if gross_total is None:
            gross_total = cls._find_amount(cleaned_text, r"Endbetrag")

        warnings = []
        parsed_total = sum((line["total"] for line in rows), Decimal("0.00"))
        if gross_total is not None and parsed_total != gross_total:
            warnings.append(
                f"{name}: Prüfung Bruttosumme fehlgeschlagen (Summe Gesamtpreis {parsed_total:.2f} != Summe Bruttowert {gross_total:.2f})."
            )

        for row in rows:
            rate = legend[row["tax_class"]]
            row["net_unit_price"] = (row["unit_price"] / (Decimal(1) + rate / Decimal(100))).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            row["vat_rate"] = rate

        return {
            "supplier": cls.supplier,
            "document_type": "invoice",
            "invoice_no": invoice_no,
            "invoice_date": invoice_date,
            "price_basis": "gross",
            "net_total": net_total,
            "gross_total": gross_total,
            "document_total": gross_total,
            "lines": [
                {
                    "supplier": cls.supplier,
                    "supplier_part_no": line["supplier_part_no"],
                    "mf_part_no": line.get("mf_part_no", ""),
                    "description": line["description"],
                    "quantity": line["quantity"],
                    "unit_price": line["net_unit_price"],
                    "gross_unit_price": line["unit_price"],
                    "printed_unit_price_gross": line["printed_unit_price"],
                    "line_total_gross": line["total"],
                    "vat_rate": line["vat_rate"],
                    "tax_class": line["tax_class"],
                    "currency": "EUR",
                }
                for line in rows
            ],
            "warning": "\n".join(warnings),
            "warnings": warnings,
        }
