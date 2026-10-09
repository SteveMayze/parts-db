"""Synthetic Conrad invoices for tests (needs reportlab, a dev-only dependency).

The PDFs reproduce the geometry of the older Conrad layout ("layout B"):
right aligned numeric columns, article numbers and numbers set slightly lower
than the description, totals block with one NETTO row per tax class. They
contain no real names, addresses, account data or customer numbers.
"""

from __future__ import annotations

import io

PAGE_WIDTH = 595.0
PAGE_HEIGHT = 842.0
ROW_PITCH = 7.1
ARTICLE_OFFSET = 1.0
NUMBER_OFFSET = 1.5

X_ARTICLE_RIGHT = 72.0
X_DESCRIPTION = 75.1
X_STK = 249.4
X_MENGE_RIGHT = 289.1
X_EINZEL_RIGHT = 340.2
X_GESAMT_RIGHT = 399.7


def item(article, description, tax_class, quantity, unit, total):
    return {"kind": "item", "article": article, "description": description, "stk": tax_class, "menge": quantity, "einzel": unit, "gesamt": total}


def note(text):
    return {"kind": "note", "text": text}


def discount(label, amount):
    return {"kind": "discount", "label": label, "amount": amount}


def info_heading(text="Folgende Artikel werden separat geliefert:"):
    return {"kind": "info_heading", "text": text}


def info_item(article, description, quantity):
    return {"kind": "info_item", "article": article, "description": description, "menge": quantity}


def netto(net, rate, tax_class, vat):
    return {"kind": "netto", "net": net, "rate": rate, "stk": tax_class, "vat": vat}


def legend(tax_class, rate):
    return {"kind": "legend", "stk": tax_class, "rate": rate}


def make_layout_b_pdf(
    invoice_no: str,
    date: str,
    pages: list[list[dict]],
    endbetrag: str,
    partial: str | None = None,
    title: str = "Rechnung",
) -> bytes:
    """``pages`` is a list of row lists; the Endbetrag is printed on the last page."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))

    def baseline(top: float, size: float) -> float:
        return PAGE_HEIGHT - top - size * 0.8

    def left(x, top, text, font="Helvetica", size=7.0):
        pdf.setFont(font, size)
        pdf.drawString(x, baseline(top, size), text)

    def right(x, top, text, font="Courier", size=7.0):
        pdf.setFont(font, size)
        pdf.drawRightString(x, baseline(top, size), text)

    for page_number, rows in enumerate(pages, start=1):
        is_last = page_number == len(pages)
        left(420.0, 70.0, "Conrad Electronic SE", size=6.0)
        left(235.0, 92.6, title, font="Helvetica-Bold", size=10.0)
        left(352.9, 101.0, f"Seite {page_number} / {len(pages)}", size=5.0)
        left(241.8, 109.1, "Rechnungs-Nr.", size=5.0)
        left(299.9, 109.1, "Kundennummer", size=5.0)
        left(352.9, 109.1, "Rechnungs-/Lief.datum", size=4.5)
        left(241.2, 117.6, invoice_no, font="Helvetica-Bold", size=8.0)
        left(299.3, 117.6, "10000001", font="Helvetica-Bold", size=8.0)
        left(354.5, 117.6, date, font="Helvetica-Bold", size=8.0)
        if partial and page_number == 1:
            left(42.5, 133.2, f"Auftrag besteht aus mehreren Teillieferungen. ({partial})", font="Helvetica-Bold", size=6.0)

        left(41.4, 146.7, "Bestell-Nr.")
        left(77.7, 146.7, "Artikelbezeichnung")
        left(250.1, 147.7, "StK.", size=5.0)
        right(289.2, 146.7, "Menge", font="Helvetica")
        right(340.2, 146.7, "Einzelpreis", font="Helvetica")
        left(346.2, 146.7, "Gesamtpreis")
        left(389.5, 146.6, "EUR")

        top = 158.7
        for row in rows:
            kind = row["kind"]
            if kind in ("info_heading", "netto"):
                top += 3.0
            if kind == "item":
                right(X_ARTICLE_RIGHT, top + ARTICLE_OFFSET, row["article"], font="Courier-Bold")
                left(X_DESCRIPTION, top, row["description"])
                left(X_STK, top + NUMBER_OFFSET, row["stk"], font="Courier")
                right(X_MENGE_RIGHT, top + NUMBER_OFFSET, row["menge"])
                right(X_EINZEL_RIGHT, top + NUMBER_OFFSET, row["einzel"])
                right(X_GESAMT_RIGHT, top + NUMBER_OFFSET, row["gesamt"])
            elif kind == "note":
                left(X_DESCRIPTION, top, row["text"])
            elif kind == "discount":
                left(X_DESCRIPTION, top, row["label"])
                right(X_GESAMT_RIGHT, top + NUMBER_OFFSET, row["amount"])
            elif kind == "info_heading":
                left(X_DESCRIPTION, top, row["text"], font="Helvetica-Bold", size=8.0)
                top += 3.0
            elif kind == "info_item":
                right(X_ARTICLE_RIGHT, top + ARTICLE_OFFSET, row["article"], font="Courier-Bold")
                left(X_DESCRIPTION, top, row["description"])
                right(X_MENGE_RIGHT, top + NUMBER_OFFSET, row["menge"])
            elif kind == "netto":
                left(X_DESCRIPTION, top, "NETTO")
                left(112.5, top, row["net"])
                right(232.4, top + NUMBER_OFFSET, f"{row['rate']}%")
                left(X_STK, top + NUMBER_OFFSET, row["stk"], font="Courier")
                right(X_EINZEL_RIGHT, top + NUMBER_OFFSET, row["vat"])
            elif kind == "legend":
                left(X_DESCRIPTION, top, f"StK. {row['stk']}: {row['rate']}% Umsatzsteuer")
            top += ROW_PITCH

        if is_last:
            top += 14.0
            left(X_DESCRIPTION, top, "Vielen Dank für Ihren Einkauf.")
            left(297.6, 459.2, "Endbetrag")
            right(399.7, 459.8, endbetrag, font="Courier-Bold", size=8.0)
            left(42.0, 480.0, "Bankeinzug nach 16 Tagen: 01.01.2030", font="Helvetica-Bold", size=8.0)
            left(42.0, 500.0, "IBAN DE00 0000 0000 0000 0000 00", size=6.0)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()
