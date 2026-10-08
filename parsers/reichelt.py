from __future__ import annotations

import io
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .formats import normalise_unicode_text, parse_date, parse_decimal


@dataclass(frozen=True)
class _ColumnAnchors:
    article: float
    description: float
    whg: float
    quantity: float
    unit_price: float
    total_price: float


class ReicheltInvoiceParser:
    supplier = "Reichelt"
    _SUPPLIER_MARKER = "reichelt elektronik gmbh & co. kg"

    @classmethod
    def detect_supplier(cls, text: str) -> str | None:
        if cls._SUPPLIER_MARKER in normalise_unicode_text(text).casefold():
            return cls.supplier
        return None

    def parse_pdf(self, raw_bytes: bytes, filename: str = "") -> dict[str, Any]:
        try:
            import pdfplumber
        except ImportError as exc:
            raise ValueError("Für den PDF-Import wird pdfplumber benötigt.") from exc

        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            pages = []
            for page in pdf.pages:
                pages.append({
                    "text": normalise_unicode_text(page.extract_text() or ""),
                    "words": page.extract_words(x_tolerance=1.5, y_tolerance=2.0) or [],
                })
        return self.parse_pages(pages, filename=filename)

    def parse_pages(self, pages: list[dict[str, Any]], filename: str = "") -> dict[str, Any]:
        if not pages:
            raise ValueError("Die PDF-Datei konnte nicht gelesen werden (keine Seiten erkannt).")

        full_text = normalise_unicode_text("\n".join(page["text"] for page in pages))
        if not full_text:
            raise ValueError("Die PDF-Datei konnte nicht gelesen werden (kein Text extrahiert).")
        if self.detect_supplier(full_text) != self.supplier:
            raise ValueError("Nicht unterstützter Lieferantenbeleg.")

        doc_type, references = self._extract_title_references(full_text)
        if doc_type != "order_confirmation":
            raise ValueError("Nicht unterstützter Reichelt-Belegtyp.")

        document_no = ""
        for reference in references:
            if re.fullmatch(r"I-\d+", reference):
                document_no = reference
                break
        if not document_no:
            raise ValueError("Reichelt-Belegnummer mit Präfix 'I-' wurde nicht gefunden.")
        reference_note = ", ".join(ref for ref in references if ref != document_no)

        date_match = re.search(r"\bDatum:\s*(\d{2}\.\d{2}\.\d{4})\b", full_text)
        if not date_match:
            raise ValueError("Reichelt-Belegdatum wurde im Kopfbereich nicht gefunden.")
        document_date = parse_date(date_match.group(1), locale="de")

        lines = self._extract_table_lines(pages, filename=filename, document_no=document_no)
        if not lines:
            raise ValueError("Es konnten keine Reichelt-Positionen aus der Datei gelesen werden.")

        warenwert_gross = self._extract_total(full_text, "Warenwert")
        warenwert_net = self._extract_total(full_text, "Warenwert", net=True)
        if warenwert_gross is None or warenwert_net is None:
            raise ValueError("Warenwert-Summen konnten aus dem Reichelt-Beleg nicht gelesen werden.")

        vat_factor_raw = warenwert_gross / warenwert_net if warenwert_net else Decimal("0")
        vat_nominal = self._resolve_nominal_vat_factor(vat_factor_raw)

        warnings = []
        gross_sum = sum((line["total_price_gross"] for line in lines), Decimal("0.00"))
        if gross_sum != warenwert_gross:
            warnings.append(
                f"{filename or document_no}: Prüfung a fehlgeschlagen (Summe Gesamtpreis {gross_sum:.2f} != Warenwert brutto {warenwert_gross:.2f})."
            )

        net_total_sum = Decimal("0.00")
        parsed_lines = []
        for line in lines:
            quantity = line["quantity"]
            derived_gross_unit = (line["total_price_gross"] / Decimal(quantity)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            delta = abs(derived_gross_unit - line["unit_price_gross_printed"])
            if delta > Decimal("0.005"):
                warnings.append(
                    f"{filename or document_no}: Prüfung b fehlgeschlagen für {line['supplier_part_no']} (Abweichung {delta:.4f} > 0,005)."
                )

            net_unit_price = (derived_gross_unit / vat_nominal).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            net_total_sum += (net_unit_price * Decimal(quantity)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            parsed_lines.append({
                "supplier": self.supplier,
                "supplier_part_no": line["supplier_part_no"],
                "mf_part_no": "",
                "description": line["description"],
                "quantity": quantity,
                "unit_price": net_unit_price,
                "currency": "EUR",
                "whg": line["whg"],
                "printed_unit_price_gross": line["unit_price_gross_printed"],
                "line_total_gross": line["total_price_gross"],
            })

        if abs(net_total_sum.quantize(Decimal("0.01")) - warenwert_net) > Decimal("0.02"):
            warnings.append(
                f"{filename or document_no}: Prüfung c fehlgeschlagen (abgeleitete Nettosumme {net_total_sum.quantize(Decimal('0.01')):.2f} weicht von Warenwert netto {warenwert_net:.2f} ab)."
            )

        distinct_whg = {line["whg"] for line in parsed_lines if line["whg"]}
        whg_note = ""
        if len(distinct_whg) > 1:
            whg_note = f"WHG-Werte unterscheiden sich in diesem Beleg: {', '.join(sorted(distinct_whg))}"

        return {
            "supplier": self.supplier,
            "document_type": "order_confirmation",
            "document_title": "Eingangsbestätigung der Bestellung",
            "invoice_no": document_no,
            "invoice_date": document_date,
            "reference_note": reference_note,
            "warning": "\n".join(warnings),
            "warnings": warnings,
            "whg_note": whg_note,
            "lines": parsed_lines,
            "gross_total": warenwert_gross,
            "net_total": warenwert_net,
            "vat_factor": vat_nominal,
        }

    def _extract_title_references(self, text: str) -> tuple[str, list[str]]:
        title_match = re.search(r"Eingangsbestätigung der Bestellung\s*\(([^)]*)\)", text, flags=re.IGNORECASE)
        if title_match:
            refs = [normalise_unicode_text(part) for part in title_match.group(1).split(",") if normalise_unicode_text(part)]
            return "order_confirmation", refs

        generic_match = re.search(r"^\s*([^\n(]+)\(([^)]*)\)", text, flags=re.MULTILINE)
        if generic_match and "reichelt" not in generic_match.group(1).casefold():
            raise ValueError("Nicht unterstützter Reichelt-Belegtyp.")
        raise ValueError("Nicht unterstützter Reichelt-Belegtyp.")

    def _extract_total(self, text: str, label: str, net: bool = False) -> Decimal | None:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if label.casefold() not in line.casefold():
                continue
            if ":" in line:
                value_match = re.search(r"([0-9][0-9\.,]*)\s*€", line)
                if value_match and not net:
                    return parse_decimal(value_match.group(1), locale="de")
            for offset in range(0, 3):
                look_line = lines[index + offset] if index + offset < len(lines) else ""
                if net and "netto" in look_line.casefold():
                    net_match = re.search(r"([0-9][0-9\.,]*)\s*€\s*netto", look_line, flags=re.IGNORECASE)
                    if net_match:
                        return parse_decimal(net_match.group(1), locale="de")
        return None

    def _resolve_nominal_vat_factor(self, factor: Decimal) -> Decimal:
        candidates = (Decimal("1.19"), Decimal("1.07"))
        for nominal in candidates:
            if abs(factor - nominal) <= Decimal("0.005"):
                return nominal
        raise ValueError(f"Abgeleiteter MwSt-Faktor {factor:.4f} wird nicht unterstützt (erwartet nahe 1,19 oder 1,07).")

    def _extract_table_lines(self, pages: list[dict[str, Any]], filename: str, document_no: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        current_line: dict[str, Any] | None = None
        done = False

        for page in pages:
            words = page["words"]
            if not words:
                continue

            anchors = self._locate_column_anchors(words)
            if anchors is None:
                continue

            grouped_rows = self._group_words_by_line(words)
            for row in grouped_rows:
                row_text = normalise_unicode_text(" ".join(word["text"] for word in row["words"]))
                if not row_text:
                    continue
                if "Artikel-Nr." in row_text and "Bezeichnung" in row_text and "Gesamtpreis" in row_text:
                    continue
                if row_text.casefold().startswith("warenwert"):
                    done = True
                    break

                parsed = self._parse_table_row(row["words"], anchors, filename=filename, document_no=document_no)
                if parsed is None:
                    continue

                if parsed["quantity"] is None and parsed["total_price_gross"] is None:
                    if current_line is not None and parsed["description"]:
                        current_line["description"] = normalise_unicode_text(
                            f"{current_line['description']} {parsed['description']}"
                        )
                    continue

                if parsed["quantity"] is None or parsed["total_price_gross"] is None or parsed["unit_price_gross_printed"] is None:
                    continue

                current_line = {
                    "supplier_part_no": parsed["supplier_part_no"],
                    "description": parsed["description"],
                    "whg": parsed["whg"],
                    "quantity": parsed["quantity"],
                    "unit_price_gross_printed": parsed["unit_price_gross_printed"],
                    "total_price_gross": parsed["total_price_gross"],
                }
                rows.append(current_line)

            if done:
                break

        return rows

    def _locate_column_anchors(self, words: list[dict[str, Any]]) -> _ColumnAnchors | None:
        grouped_rows = self._group_words_by_line(words)
        for row in grouped_rows:
            row_words = row["words"]
            lowered = [normalise_unicode_text(word["text"]).casefold() for word in row_words]
            if not {"artikel-nr.", "bezeichnung", "whg", "menge", "einzelpreis", "gesamtpreis"}.issubset(set(lowered)):
                continue
            positions = {normalise_unicode_text(word["text"]).casefold(): float(word["x0"]) for word in row_words}
            return _ColumnAnchors(
                article=positions["artikel-nr."],
                description=positions["bezeichnung"],
                whg=positions["whg"],
                quantity=positions["menge"],
                unit_price=positions["einzelpreis"],
                total_price=positions["gesamtpreis"],
            )
        return None

    def _group_words_by_line(self, words: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[int, list[dict[str, Any]]] = {}
        for word in words:
            key = int(round(float(word["top"])))
            buckets.setdefault(key, []).append(word)
        rows = []
        for key in sorted(buckets):
            row_words = sorted(buckets[key], key=lambda item: float(item["x0"]))
            rows.append({"top": key, "words": row_words})
        return rows

    def _parse_table_row(
        self,
        row_words: list[dict[str, Any]],
        anchors: _ColumnAnchors,
        filename: str,
        document_no: str,
    ) -> dict[str, Any] | None:
        cells = {
            "article": [],
            "description": [],
            "whg": [],
            "quantity": [],
            "unit_price": [],
            "total_price": [],
        }
        for word in row_words:
            text = normalise_unicode_text(word["text"])
            if not text:
                continue
            x0 = float(word["x0"])
            if x0 < anchors.description:
                cells["article"].append(text)
            elif x0 < anchors.whg:
                cells["description"].append(text)
            elif x0 < anchors.quantity:
                cells["whg"].append(text)
            elif x0 < anchors.unit_price:
                cells["quantity"].append(text)
            elif x0 < anchors.total_price:
                cells["unit_price"].append(text)
            else:
                cells["total_price"].append(text)

        supplier_part_no = normalise_unicode_text(" ".join(cells["article"]))
        description = normalise_unicode_text(" ".join(cells["description"]))
        whg = normalise_unicode_text(" ".join(cells["whg"]))
        quantity_text = normalise_unicode_text(" ".join(cells["quantity"]))
        unit_text = normalise_unicode_text(" ".join(cells["unit_price"]))
        total_text = normalise_unicode_text(" ".join(cells["total_price"]))

        if not supplier_part_no and not description:
            return None

        quantity = None
        if quantity_text:
            if not quantity_text.isdigit():
                return None
            quantity = int(quantity_text)

        unit_price = self._parse_euro_price(unit_text, filename=filename, document_no=document_no, field_name="Einzelpreis")
        total_price = self._parse_euro_price(total_text, filename=filename, document_no=document_no, field_name="Gesamtpreis")

        return {
            "supplier_part_no": supplier_part_no,
            "description": description,
            "whg": whg,
            "quantity": quantity,
            "unit_price_gross_printed": unit_price,
            "total_price_gross": total_price,
        }

    def _parse_euro_price(self, value: str, filename: str, document_no: str, field_name: str) -> Decimal | None:
        text = normalise_unicode_text(value)
        if not text:
            return None
        foreign_currency_patterns = (
            r"\bCHF\b",
            r"\bUSD\b",
            r"\bGBP\b",
            r"\$",
            r"£",
            r"¥",
        )
        for pattern in foreign_currency_patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                raise ValueError(
                    f"{filename or document_no}: {field_name} enthält eine Nicht-EUR-Währung ({text!r})."
                )
        return parse_decimal(text, locale="de").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
