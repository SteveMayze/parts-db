"""Conrad invoice parser.

Conrad has changed its invoice layout over the years. Every layout has one
reader function that turns the PDF into a ``_RawDocument``; all checks, the
gross/net handling and the result format are shared in ``_finish``.

Adding a layout means: one reader function, one entry in ``_LAYOUTS`` (with the
markers that identify it) and one sample.

Conrad prints GROSS prices. The net unit price is derived per line from the
tax class legend of the document.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from .formats import normalise_unicode_text, parse_date, parse_decimal

AMOUNT = r"-?\d[\d.]*,\d{2}"
ARTICLE_NO = re.compile(r"\d{6,7}")
LEGEND_RE = re.compile(r"StK\.?\s*(\d+)\s*:\s*(\d+(?:,\d+)?)\s*%")
PARTIAL_DELIVERY_RE = re.compile(r"Teillieferungen\.?\s*\((\d+)\s*/\s*(\d+)\)")
PER_UNIT_RE = re.compile(r"Preis\s*/\s*(\d[\d.]*)\s*ST", re.IGNORECASE)
INFO_BLOCK_RE = re.compile(r"Folgende Artikel werden\s+(.+?)\s*:?$", re.IGNORECASE)
ROW_TOLERANCE = 3.0

_CENT = Decimal("0.01")
_FOUR = Decimal("0.0001")


@dataclass
class _RawLine:
    supplier_part_no: str
    description: str
    quantity: int
    tax_class: str
    printed_unit_price: Decimal
    total: Decimal


@dataclass
class _TaxRow:
    tax_class: str
    net: Decimal
    rate: Decimal
    vat: Decimal


@dataclass
class _RawDocument:
    invoice_no: str
    invoice_date: Any
    lines: list[_RawLine] = field(default_factory=list)
    discounts: list[tuple[str, Decimal]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tax_rows: list[_TaxRow] = field(default_factory=list)
    legend: dict[str, Decimal] = field(default_factory=dict)
    endbetrag: Decimal | None = None


@dataclass
class _Context:
    pages: list[dict[str, Any]]
    text: str


# --------------------------------------------------------------------------- shared helpers


def _money(value: str) -> Decimal:
    return parse_decimal(value, locale="de")


def _header_value(text: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        match = re.search(rf"{re.escape(label)}[ \t]*[:.]?[ \t]*(\S.*)$", text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
    return ""


def _per_unit_divisor(description: str) -> int:
    match = PER_UNIT_RE.search(description)
    return int(match.group(1).replace(".", "")) if match else 1


def _parse_legend(text: str) -> dict[str, Decimal]:
    return {tax_class: _money(rate) for tax_class, rate in LEGEND_RE.findall(text)}


def _partial_delivery_note(text: str) -> str | None:
    match = PARTIAL_DELIVERY_RE.search(text)
    return f"Teillieferung {match.group(1)}/{match.group(2)}" if match else None


def _separate_delivery_note(kind: str, quantity: int, article_no: str) -> str:
    return f"{kind}: {quantity} × {article_no}"


def _document_title(pages: list[dict[str, Any]]) -> str:
    """Title of the document: the largest bold text near the top of page 1."""
    words = pages[0].get("words") or []
    bold = [
        w
        for w in words
        if "bold" in str(w.get("fontname", "")).casefold() and float(w.get("size", 0)) >= 9.5 and float(w["top"]) < 250
    ]
    if bold:
        largest = max(float(w["size"]) for w in bold)
        title_words = [w for w in bold if float(w["size"]) == largest]
        first_top = min(float(w["top"]) for w in title_words)
        row = sorted((w for w in title_words if abs(float(w["top"]) - first_top) <= 2), key=lambda w: float(w["x0"]))
        return normalise_unicode_text(" ".join(w["text"] for w in row))
    for line in pages[0]["text"].splitlines():
        if line.strip():
            return normalise_unicode_text(line)
    return ""


# --------------------------------------------------------------------------- layout A (text based)


def _read_layout_a(ctx: _Context) -> _RawDocument:
    text = ctx.text
    invoice_no = _header_value(text, ("Rechnungs-Nr.", "Rechnung Nr"))
    date_text = _header_value(text, ("Rechnungs-/Leistungsdatum", "Rechnungsdatum"))
    if not invoice_no:
        raise ValueError("Conrad-Belegnummer konnte im Kopfbereich nicht gefunden werden.")
    if not date_text:
        raise ValueError("Conrad-Belegdatum konnte im Kopfbereich nicht gefunden werden.")
    raw = _RawDocument(invoice_no=invoice_no, invoice_date=parse_date(date_text, locale="de"))
    partial = _partial_delivery_note(text)
    if partial:
        raw.notes.append(partial)

    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"\s*Menge\s+StK\s+Einzelpreis\s+Gesamtpreis", line)), None)
    if start is None:
        start = next((i for i, line in enumerate(lines) if "art.-nr." in line.casefold()), None)
    if start is None:
        raise ValueError("Conrad-Tabellenkopf wurde nicht gefunden.")

    item_re = re.compile(
        r"^(?P<pos>\d+)\s+(?P<no>\S+)\s+(?P<desc>.+?)\s+(?P<qty>\d+)\s+(?P<tax>\d+)\s+"
        rf"(?P<unit>\d[\d.]*,\d+)\s+(?P<total>{AMOUNT})$"
    )
    info_re = re.compile(r"^(?P<no>\d{6,7})\s+(?P<desc>.+?)\s+(?P<qty>\d+)$")
    discount_re = re.compile(rf"^(?P<label>[^\d\s].*?)\s+(?P<amount>-\d[\d.]*,\d{{2}})$")

    info_kind = ""
    for line in lines[start + 1 :]:
        stripped = normalise_unicode_text(line)
        if not stripped:
            continue
        if stripped.casefold().startswith("summe nettowert"):
            break
        if re.match(r"Menge\s+StK\s+Einzelpreis\s+Gesamtpreis", stripped) or PARTIAL_DELIVERY_RE.search(stripped):
            continue
        item = item_re.match(stripped)
        if item:
            raw.lines.append(
                _RawLine(
                    supplier_part_no=item.group("no"),
                    description=item.group("desc").strip(),
                    quantity=int(item.group("qty")),
                    tax_class=item.group("tax"),
                    printed_unit_price=_money(item.group("unit")),
                    total=_money(item.group("total")),
                )
            )
            continue
        block = INFO_BLOCK_RE.match(stripped)
        if block:
            info_kind = block.group(1)
            continue
        info = info_re.match(stripped) if info_kind else None
        if info:
            raw.notes.append(_separate_delivery_note(info_kind, int(info.group("qty")), info.group("no")))
            continue
        discount = discount_re.match(stripped)
        if discount:
            raw.discounts.append((discount.group("label"), _money(discount.group("amount"))))
            continue
        raw.notes.append(stripped)

    raw.legend = _parse_legend(text)
    for rate_text, net, vat in re.findall(rf"USt\.\s*(\d+(?:,\d+)?)\s*%\s*aus\s*({AMOUNT})\s+({AMOUNT})", text):
        rate = _money(rate_text)
        tax_class = next((key for key, value in raw.legend.items() if value == rate), "")
        raw.tax_rows.append(_TaxRow(tax_class=tax_class, net=_money(net), rate=rate, vat=_money(vat)))

    endbetrag = re.search(rf"Endbetrag\s*({AMOUNT})", text) or re.search(rf"Summe\s+Bruttowert\s*({AMOUNT})", text)
    raw.endbetrag = _money(endbetrag.group(1)) if endbetrag else None
    return raw


# --------------------------------------------------------------------------- layout B (word positions)


@dataclass(frozen=True)
class _Columns:
    header_bottom: float
    article_limit: float
    desc_limit: float
    stk_right: float
    menge_right: float
    einzel_right: float
    gesamt_right: float

    def column(self, word: dict[str, Any]) -> str:
        x1 = float(word["x1"])
        if x1 <= self.article_limit:
            return "article"
        if x1 <= self.desc_limit:
            return "desc"
        if x1 <= (self.stk_right + self.menge_right) / 2:
            return "stk"
        if x1 <= (self.menge_right + self.einzel_right) / 2:
            return "menge"
        if x1 <= (self.einzel_right + self.gesamt_right) / 2:
            return "einzel"
        return "gesamt"


def _word_text(word: dict[str, Any]) -> str:
    return normalise_unicode_text(word["text"])


def _find_word(words: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    return next((w for w in words if _word_text(w).casefold() == text.casefold()), None)


def _locate_columns(words: list[dict[str, Any]]) -> _Columns | None:
    anchor = _find_word(words, "Bestell-Nr.")
    if anchor is None:
        return None
    row = [w for w in words if abs(float(w["top"]) - float(anchor["top"])) <= 3]
    by_text = {_word_text(w).casefold(): w for w in row}
    needed = ("artikelbezeichnung", "stk.", "menge", "einzelpreis", "gesamtpreis")
    if any(key not in by_text for key in needed):
        return None
    gesamt_right = max(float(by_text[key]["x1"]) for key in ("gesamtpreis", "eur") if key in by_text)
    return _Columns(
        header_bottom=max(float(w["bottom"]) for w in row),
        article_limit=float(by_text["artikelbezeichnung"]["x0"]) - 3,
        desc_limit=float(by_text["stk."]["x0"]) - 2,
        stk_right=float(by_text["stk."]["x1"]),
        menge_right=float(by_text["menge"]["x1"]),
        einzel_right=float(by_text["einzelpreis"]["x1"]),
        gesamt_right=gesamt_right,
    )


def _values_under_labels(words: list[dict[str, Any]], labels: tuple[str, ...]) -> list[str]:
    found = []
    for label in labels:
        word = _find_word(words, label)
        if word is None:
            raise ValueError(f"Conrad-Kopfzeile '{label}' wurde nicht gefunden.")
        found.append(word)
    order = sorted(found, key=lambda w: float(w["x0"]))
    values = {}
    for index, word in enumerate(order):
        limit = float(order[index + 1]["x0"]) - 3 if index + 1 < len(order) else float(word["x0"]) + 90
        below = [
            w
            for w in words
            if 0 < float(w["top"]) - float(word["top"]) < 25 and float(word["x0"]) - 6 <= float(w["x0"]) < limit
        ]
        if not below:
            values[id(word)] = ""
            continue
        first_top = min(float(w["top"]) for w in below)
        row = sorted((w for w in below if abs(float(w["top"]) - first_top) <= 2), key=lambda w: float(w["x0"]))
        values[id(word)] = " ".join(_word_text(w) for w in row)
    return [values[id(word)] for word in found]


def _cluster_rows(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        if rows and abs(float(word["top"]) - float(rows[-1][0]["top"])) <= 2.5:
            rows[-1].append(word)
        else:
            rows.append([word])
    return [sorted(row, key=lambda w: float(w["x0"])) for row in rows]


def _cells(words: list[dict[str, Any]], columns: _Columns) -> dict[str, str]:
    cells: dict[str, list[str]] = {name: [] for name in ("article", "desc", "stk", "menge", "einzel", "gesamt")}
    for word in sorted(words, key=lambda w: float(w["x0"])):
        cells[columns.column(word)].append(_word_text(word))
    return {name: " ".join(parts) for name, parts in cells.items()}


def _page_entries(page: dict[str, Any], columns: _Columns) -> list[dict[str, Any]]:
    words = page["words"]
    stops = [
        float(w["top"])
        for w in words
        if _word_text(w).casefold() in ("endbetrag", "bankeinzug") and float(w["top"]) > columns.header_bottom
    ]
    bottom = min(stops) if stops else float("inf")
    body = [
        w
        for w in words
        if float(w["top"]) >= columns.header_bottom - 0.5
        and float(w["top"]) < bottom
        and float(w["x1"]) <= columns.gesamt_right + 8
    ]
    anchors = sorted(
        (w for w in body if columns.column(w) == "article" and ARTICLE_NO.fullmatch(_word_text(w))),
        key=lambda w: float(w["top"]),
    )
    anchor_ids = {id(w) for w in anchors}
    groups: dict[int, list[dict[str, Any]]] = {id(a): [a] for a in anchors}
    free = []
    for word in body:
        if id(word) in anchor_ids:
            continue
        nearest = min(anchors, key=lambda a: abs(float(a["top"]) - float(word["top"])), default=None)
        if nearest is not None and abs(float(nearest["top"]) - float(word["top"])) <= ROW_TOLERANCE:
            groups[id(nearest)].append(word)
        else:
            free.append(word)

    entries = []
    for anchor in anchors:
        entries.append({"kind": "item", "top": float(anchor["top"]), "cells": _cells(groups[id(anchor)], columns)})
    for row in _cluster_rows(free):
        entries.append(
            {
                "kind": "free",
                "top": float(row[0]["top"]),
                "cells": _cells(row, columns),
                "text": " ".join(_word_text(w) for w in row),
            }
        )
    return sorted(entries, key=lambda e: e["top"])


def _read_layout_b(ctx: _Context) -> _RawDocument:
    pages = ctx.pages
    if not any(page.get("words") for page in pages):
        raise ValueError("Conrad-Layout B benötigt die Wortpositionen aus der PDF-Datei.")

    first_words = pages[0].get("words") or []
    invoice_no, _customer_no, date_text = _values_under_labels(
        first_words, ("Rechnungs-Nr.", "Kundennummer", "Rechnungs-/Lief.datum")
    )
    if not invoice_no:
        raise ValueError("Conrad-Belegnummer konnte im Kopfbereich nicht gefunden werden.")
    if not date_text:
        raise ValueError("Conrad-Belegdatum konnte im Kopfbereich nicht gefunden werden.")
    raw = _RawDocument(invoice_no=invoice_no, invoice_date=parse_date(date_text, locale="de"))

    partial = _partial_delivery_note(ctx.text)
    if partial:
        raw.notes.append(partial)

    columns = None
    entries: list[dict[str, Any]] = []
    for page in pages:
        words = page.get("words") or []
        columns = _locate_columns(words) or columns
        if columns is None:
            continue
        entries.extend(_page_entries(page, columns))
    if columns is None:
        raise ValueError("Conrad-Tabellenkopf (Bestell-Nr.) wurde nicht gefunden.")

    info_kind = ""
    in_totals = False
    for entry in entries:
        cells = entry["cells"]
        if entry["kind"] == "item":
            if not cells["gesamt"]:
                quantity = cells["menge"]
                if not quantity.isdigit():
                    raise ValueError(f"Zeile {cells['article']} ohne Preis hat keine gültige Menge ({quantity!r}).")
                raw.notes.append(_separate_delivery_note(info_kind or "separat geliefert", int(quantity), cells["article"]))
                continue
            if not cells["menge"].isdigit() or not re.fullmatch(r"\d+", cells["stk"]):
                raise ValueError(
                    f"Zeile {cells['article']}: Menge ({cells['menge']!r}) oder Steuerklasse ({cells['stk']!r}) nicht lesbar."
                )
            raw.lines.append(
                _RawLine(
                    supplier_part_no=cells["article"],
                    description=cells["desc"],
                    quantity=int(cells["menge"]),
                    tax_class=cells["stk"],
                    printed_unit_price=_money(cells["einzel"]),
                    total=_money(cells["gesamt"]),
                )
            )
            continue

        text = entry["text"]
        netto = re.match(rf"NETTO\s+(?P<net>{AMOUNT})", text)
        if netto:
            in_totals = True
            rate = re.search(r"(\d+(?:,\d+)?)\s*%", text)
            if not rate or not cells["stk"] or not re.fullmatch(AMOUNT, cells["einzel"]):
                raise ValueError(f"NETTO-Zeile ist unvollständig: {text!r}.")
            raw.tax_rows.append(
                _TaxRow(tax_class=cells["stk"], net=_money(netto.group("net")), rate=_money(rate.group(1)), vat=_money(cells["einzel"]))
            )
        elif LEGEND_RE.match(text):
            in_totals = True
            raw.legend.update(_parse_legend(text))
        elif INFO_BLOCK_RE.match(text) and not in_totals:
            info_kind = INFO_BLOCK_RE.match(text).group(1)
        elif in_totals:
            continue
        elif re.fullmatch(r"-\d[\d.]*,\d{2}", cells["gesamt"]) and not cells["menge"]:
            raw.discounts.append((cells["desc"], _money(cells["gesamt"])))
        else:
            raw.notes.append(text)

    endbetrag = None
    for page in pages:
        words = page.get("words") or []
        label = _find_word(words, "Endbetrag")
        if label is None:
            continue
        amounts = [
            _word_text(w)
            for w in words
            if abs(float(w["top"]) - float(label["top"])) <= 4
            and float(w["x0"]) > float(label["x1"])
            and re.fullmatch(AMOUNT, _word_text(w))
        ]
        if amounts:
            endbetrag = _money(amounts[-1])
    raw.endbetrag = endbetrag
    return raw


# --------------------------------------------------------------------------- layout registry


@dataclass(frozen=True)
class _Layout:
    name: str
    markers: tuple[tuple[str, Callable[[str], bool]], ...]
    reader: Callable[[_Context], _RawDocument]


def _contains(needle: str) -> Callable[[str], bool]:
    return lambda text: needle.casefold() in text.casefold()


def _matches(pattern: str) -> Callable[[str], bool]:
    return lambda text: re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) is not None


_LAYOUTS = (
    _Layout(
        name="A",
        markers=(("Art.-Nr.", _contains("Art.-Nr.")), ("Summe Nettowert", _matches(r"Summe\s+Nettowert"))),
        reader=_read_layout_a,
    ),
    _Layout(
        name="B",
        markers=(("Bestell-Nr.", _contains("Bestell-Nr.")), ("NETTO-Summenzeile", _matches(rf"^\s*NETTO\s+{AMOUNT}"))),
        reader=_read_layout_b,
    ),
)


def detect_layout(text: str) -> _Layout:
    cleaned = normalise_unicode_text(text)
    complete = []
    report = []
    for layout in _LAYOUTS:
        found = [label for label, check in layout.markers if check(cleaned)]
        missing = [label for label, _check in layout.markers if label not in found]
        report.append(
            f"Layout {layout.name}: gefunden: {', '.join(found) or 'nichts'}; fehlend: {', '.join(missing) or 'nichts'}"
        )
        if not missing:
            complete.append(layout)
    if len(complete) == 1:
        return complete[0]
    reason = "Das Conrad-Layout ist mehrdeutig" if complete else "Unbekanntes Conrad-Layout"
    raise ValueError(f"{reason}. " + " | ".join(report))


# --------------------------------------------------------------------------- shared checks and result


def _finish(raw: _RawDocument, layout: str, filename: str, supplier: str) -> dict[str, Any]:
    label = f"{filename} ({raw.invoice_no})" if filename else raw.invoice_no
    if not raw.lines:
        raise ValueError(f"{label}: Es konnten keine Conrad-Zeilen gelesen werden.")
    if not raw.tax_rows:
        raise ValueError(f"{label}: Der Steuerblock (Nettowert und Umsatzsteuer) wurde nicht gefunden.")
    if raw.endbetrag is None:
        raise ValueError(f"{label}: Der Endbetrag wurde nicht gefunden.")

    warnings = []
    discount_total = sum((amount for _text, amount in raw.discounts), Decimal("0.00"))
    lines_total = sum((line.total for line in raw.lines), Decimal("0.00"))
    if lines_total + discount_total != raw.endbetrag:
        warnings.append(
            f"{label}: Prüfung a fehlgeschlagen (Summe Gesamtpreise {lines_total:.2f} + Rabatte {discount_total:.2f} "
            f"!= Endbetrag {raw.endbetrag:.2f})."
        )
    net_sum = sum((row.net for row in raw.tax_rows), Decimal("0.00"))
    vat_sum = sum((row.vat for row in raw.tax_rows), Decimal("0.00"))
    if net_sum + vat_sum != raw.endbetrag:
        warnings.append(
            f"{label}: Prüfung b fehlgeschlagen (Summe NETTO {net_sum:.2f} + Umsatzsteuer {vat_sum:.2f} "
            f"!= Endbetrag {raw.endbetrag:.2f})."
        )
    for row in raw.tax_rows:
        expected = row.net * row.rate / Decimal(100)
        if abs(expected - row.vat) > _CENT:
            warnings.append(
                f"{label}: Prüfung c fehlgeschlagen für Steuerklasse {row.tax_class or '?'} "
                f"(NETTO × Satz = {expected:.2f}, gedruckte Umsatzsteuer {row.vat:.2f})."
            )

    lines = []
    missing_classes: dict[str, list[str]] = {}
    for position, raw_line in enumerate(raw.lines, start=1):
        if raw_line.quantity <= 0:
            raise ValueError(f"{label}: Menge von {raw_line.supplier_part_no} ist nicht positiv.")
        gross_unit = (raw_line.total / Decimal(raw_line.quantity)).quantize(_FOUR, rounding=ROUND_HALF_UP)
        printed_per_piece = raw_line.printed_unit_price / Decimal(_per_unit_divisor(raw_line.description))
        deviation = abs(raw_line.total / Decimal(raw_line.quantity) - printed_per_piece)
        if deviation > Decimal("0.005"):
            warnings.append(
                f"{label}: Prüfung d fehlgeschlagen für {raw_line.supplier_part_no} "
                f"(Gesamtpreis ÷ Menge = {gross_unit:.4f}, gedruckter Einzelpreis {printed_per_piece:.4f})."
            )
        rate = raw.legend.get(raw_line.tax_class)
        if rate is None:
            missing_classes.setdefault(raw_line.tax_class, []).append(raw_line.supplier_part_no)
            net_unit = gross_unit
        else:
            net_unit = (gross_unit / (Decimal(1) + rate / Decimal(100))).quantize(_FOUR, rounding=ROUND_HALF_UP)
        lines.append(
            {
                "supplier": supplier,
                "position": str(position),
                "supplier_part_no": raw_line.supplier_part_no,
                "mf_part_no": "",
                "description": raw_line.description,
                "quantity": raw_line.quantity,
                "unit_price": net_unit,
                "gross_unit_price": gross_unit,
                "printed_unit_price_gross": raw_line.printed_unit_price,
                "line_total_gross": raw_line.total,
                "vat_rate": rate,
                "tax_class": raw_line.tax_class,
                "currency": "EUR",
            }
        )

    for tax_class, article_numbers in missing_classes.items():
        warnings.append(
            f"{label}: Prüfung e fehlgeschlagen (Steuerklasse {tax_class} fehlt in der Legende; betroffen: "
            f"{', '.join(article_numbers)}; Nettopreis nicht ableitbar, der Bruttopreis wird als Nettopreis angezeigt)."
        )

    notes = list(raw.notes)
    for text, amount in raw.discounts:
        shown = f"{amount:.2f}".replace(".", ",")
        notes.append(f"Rabatt: {text} {shown}" if text else f"Rabatt: {shown}")

    return {
        "supplier": supplier,
        "document_type": "invoice",
        "document_title": "Rechnung",
        "layout": layout,
        "invoice_no": raw.invoice_no,
        "invoice_date": raw.invoice_date,
        "price_basis": "gross",
        "net_total": net_sum,
        "gross_total": raw.endbetrag,
        "document_total": raw.endbetrag,
        "discount_total": discount_total,
        "notes": notes,
        "lines": lines,
        "warning": "\n".join(warnings),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- parser


class ConradInvoiceParser:
    supplier = "Conrad Electronic SE"

    @staticmethod
    def detect_supplier(text: str) -> str | None:
        cleaned = normalise_unicode_text(text).casefold()
        if "conrad" in cleaned:
            return "Conrad Electronic SE"
        return None

    def parse_pdf(self, raw_bytes: bytes, filename: str = "") -> dict[str, Any]:
        try:
            import pdfplumber
        except ImportError as exc:
            raise ValueError("Für den PDF-Import wird pdfplumber benötigt.") from exc

        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            pages = [
                {
                    "text": normalise_unicode_text(page.extract_text() or ""),
                    "words": page.extract_words(x_tolerance=1.5, y_tolerance=2.0, extra_attrs=["fontname", "size"]) or [],
                }
                for page in pdf.pages
            ]
        return self.parse_pages(pages, filename=filename)

    @classmethod
    def parse_invoice_text(cls, text: str) -> dict[str, Any]:
        """Text only entry point; works for layouts that need no word positions."""
        return cls().parse_pages([{"text": normalise_unicode_text(text), "words": []}])

    def parse_pages(self, pages: list[dict[str, Any]], filename: str = "") -> dict[str, Any]:
        if not pages:
            raise ValueError("Die PDF-Datei konnte nicht gelesen werden (keine Seiten erkannt).")
        text = normalise_unicode_text("\n".join(page["text"] for page in pages))
        if not text:
            raise ValueError("Der Belegtext ist leer.")

        title = _document_title(pages)
        if title.casefold() != "rechnung":
            raise ValueError(f"Nicht unterstützter Belegtyp bei Conrad: {title or 'Titel nicht erkannt'} (erwartet: Rechnung).")

        layout = detect_layout(text)
        raw = layout.reader(_Context(pages=pages, text=text))
        return _finish(raw, layout=layout.name, filename=filename, supplier=self.supplier)
