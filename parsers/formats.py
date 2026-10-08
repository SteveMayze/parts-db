from __future__ import annotations

import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def normalise_unicode_text(value: str | None) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value)).strip()


def canonicalise_match_value(value: str | None) -> str:
    text = normalise_unicode_text(value).casefold().replace("µ", "u").replace("μ", "u")
    return text.replace("\u00a0", " ").strip()


def parse_decimal(value: str | Decimal | int | float | None, locale: str = "de") -> Decimal:
    if value is None:
        return Decimal("0")

    text = normalise_unicode_text(value).replace("€", "").replace("EUR", "").replace(" ", "")
    if text in {"", "-"}:
        return Decimal("0")

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        if locale == "de":
            text = text.replace(".", "").replace(",", ".")
        else:
            if text.count(",") > 1:
                text = text.replace(",", "")
            else:
                text = text.replace(",", ".")
    elif "." in text:
        if locale == "de":
            text = text.replace(".", "")

    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc


def parse_date(value: str | None, locale: str = "de") -> date | None:
    text = normalise_unicode_text(value)
    if not text:
        return None

    text = text.replace("/", ".")
    patterns = [
        "%d.%m.%Y",
        "%d.%m.%y",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%y %H:%M:%S",
        "%Y-%m-%d",
        "%Y.%m.%d",
    ]
    for fmt in patterns:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Nicht unterstütztes Datumsformat: {value!r}")


def decode_upload_bytes(raw_bytes: bytes, encodings: tuple[str, ...] = ("utf-8-sig", "cp1252")) -> tuple[str, str]:
    for encoding in encodings:
        try:
            return raw_bytes.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("Hochgeladener Belegtext konnte weder mit utf-8-sig noch mit cp1252 dekodiert werden")


def listify_row(row: str) -> list[str]:
    if not row:
        return []
    if ";" in row:
        return [part.strip() for part in row.split(";") if part.strip()]
    if "\t" in row:
        return [part.strip() for part in row.split("\t") if part.strip()]
    return [part.strip() for part in row.split()] if row.strip() else []


def detect_delimiter(sample: str) -> str:
    if not sample:
        return ";"
    semicolon_count = sample.count(";")
    comma_count = sample.count(",")
    if semicolon_count > comma_count:
        return ";"
    return "," if comma_count else ";"
