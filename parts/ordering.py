"""Explicit, engine-independent text ordering (ADR 0003).

SQLite sorts text by bytes, PostgreSQL by the locale of the database. Anything the user sees in an order is therefore
sorted here, in Python, with one key: case and accents ignored (a, ä and A sort together), digit runs compared as
numbers ("R2" before "R10"), ties broken by the raw text and then the primary key so the order is fully determined.
"""
import re
import unicodedata


def text_sort_key(value):
    text = unicodedata.normalize("NFC", str(value or "")).casefold().replace("µ", "u").replace("μ", "u")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    parts = []
    for chunk in re.split(r"(\d+)", text):
        if not chunk:
            continue
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        else:
            parts.append((1, chunk))
    return tuple(parts)


def sort_texts(values):
    """Distinct text values in display order (a list)."""
    return sorted(values, key=lambda v: (text_sort_key(v), v))


def sort_by_text(objects, attr="name"):
    """Model instances in display order by one text attribute (a list; the database order is not relied on)."""
    return sorted(objects, key=lambda o: (text_sort_key(getattr(o, attr)), getattr(o, attr), o.pk))
