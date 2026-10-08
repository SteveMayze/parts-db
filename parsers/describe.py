from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Iterable

from .formats import canonicalise_match_value, parse_decimal


TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Widerstand": ("widerstand",),
    "Kondensator": ("kondensator", "ceramic capacitor", "kerko"),
    "Spule": ("spule", "induktivität"),
    "Drossel": ("drossel",),
    "Diode": ("diode", "gleichrichter"),
    "LED": ("led", "lichtemittierende diode"),
    "Transistor": ("transistor",),
    "Relais": ("relais",),
    "Sicherung": ("sicherung", "kleinstsicherung"),
    "Klemme": ("reihenklemme", "federkraftklemme", "klemme"),
    "Buchsenleiste": ("buchsenleiste", "buchsenleisten"),
    "Stiftleiste": ("stiftleiste", "stiftleisten"),
    "Steckverbinder": ("steckverbinder",),
    "Taster": ("taster",),
    "Schalter": ("schalter",),
    "Quarz": ("quarz",),
}

PACKAGING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bSOT-?\d+\b", re.IGNORECASE),
    re.compile(r"\bSOIC-?\d+\b", re.IGNORECASE),
    re.compile(r"\bSO-?\d+\b", re.IGNORECASE),
    re.compile(r"\bTSSOP-?\d+\b", re.IGNORECASE),
    re.compile(r"\bQFN-?\d+\b", re.IGNORECASE),
    re.compile(r"\bQFP-?\d+\b", re.IGNORECASE),
    re.compile(r"\bLQFP-?\d+\b", re.IGNORECASE),
    re.compile(r"\bDIP-?\d+\b", re.IGNORECASE),
    re.compile(r"\bDIL-?\d+\b", re.IGNORECASE),
    re.compile(r"\bTO-?\d+\b", re.IGNORECASE),
)

PASSIVE_TYPES = {"widerstand", "kondensator", "spule", "drossel"}


def _normalise_text(value: str) -> str:
    return unicodedata.normalize("NFC", value or "")


def _reuse_existing_type(type_name: str, existing_part_types: Iterable[str]) -> tuple[str, bool]:
    existing_map = {canonicalise_match_value(item): item for item in existing_part_types if item}
    key = canonicalise_match_value(type_name)
    if key in existing_map:
        return existing_map[key], False
    return type_name, True


def _extract_type(description: str, existing_part_types: Iterable[str]) -> tuple[str, bool]:
    text = canonicalise_match_value(description)
    matched: list[str] = []
    for type_name, keywords in TYPE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            matched.append(type_name)
    unique = list(dict.fromkeys(matched))
    if len(unique) != 1:
        return "", False
    return _reuse_existing_type(unique[0], existing_part_types)


def _normalise_number(raw: str) -> str:
    value = parse_decimal(raw, locale="de")
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized.replace(".", ",")


def _single_or_blank(values: list[str]) -> str:
    uniques = list(dict.fromkeys(item for item in values if item))
    if len(uniques) != 1:
        return ""
    return uniques[0]


def _extract_packaging(description: str, type_name: str) -> str:
    text = _normalise_text(description)
    lowered = canonicalise_match_value(description)
    candidates: list[str] = []
    has_explicit_smd = bool(re.search(r"\bSMD\b", text, flags=re.IGNORECASE))
    has_explicit_tht = bool(re.search(r"\bTHT\b", text, flags=re.IGNORECASE))

    for pattern in PACKAGING_PATTERNS:
        for match in pattern.findall(text):
            token = match if isinstance(match, str) else match[0]
            candidates.append(token.upper())

    passive_sizes = []
    for raw in re.findall(r"\b\d{4}\b", text):
        if raw not in {"0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512"}:
            continue
        if "smd" in lowered or canonicalise_match_value(type_name) in PASSIVE_TYPES:
            passive_sizes.append(raw)

    if passive_sizes:
        candidates.extend(passive_sizes)
    elif not candidates:
        if has_explicit_smd:
            candidates.append("SMD")
        elif has_explicit_tht:
            candidates.append("THT")

    return _single_or_blank(candidates)


def _extract_resistor_value(description: str) -> str:
    matches = []
    for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(k|m)?\s*ohm\b", description, flags=re.IGNORECASE):
        number = _normalise_number(match.group(1))
        prefix = (match.group(2) or "").upper()
        suffix = "k" if prefix == "K" else ("M" if prefix == "M" else "")
        matches.append(f"{number}{suffix}")
    return _single_or_blank(matches)


def _extract_capacitor_value(description: str) -> str:
    matches = []
    for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(pF|nF|uF|µF|μF)\b", description, flags=re.IGNORECASE):
        number = _normalise_number(match.group(1))
        unit = match.group(2)
        if canonicalise_match_value(unit) in {"uf", "µf"}:
            unit = "µF"
        else:
            unit = unit.upper().replace("PF", "pF").replace("NF", "nF")
        matches.append(f"{number}{unit}")
    return _single_or_blank(matches)


def _extract_inductor_value(description: str) -> str:
    matches = []
    for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(uH|µH|μH|mH|H)\b", description, flags=re.IGNORECASE):
        number = _normalise_number(match.group(1))
        unit = match.group(2)
        key = canonicalise_match_value(unit)
        if key in {"uh", "µh"}:
            unit = "µH"
        elif key == "mh":
            unit = "mH"
        else:
            unit = "H"
        matches.append(f"{number}{unit}")
    return _single_or_blank(matches)


def _extract_rating_value(description: str) -> str:
    ratings = []
    for match in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(A|V)\b", description, flags=re.IGNORECASE):
        number = _normalise_number(match.group(1))
        ratings.append(f"{number}{match.group(2).upper()}")
    return _single_or_blank(ratings)


def _extract_value(description: str, type_name: str) -> str:
    lowered_type = canonicalise_match_value(type_name)
    if lowered_type == "widerstand":
        return _extract_resistor_value(description)
    if lowered_type == "kondensator":
        return _extract_capacitor_value(description)
    if lowered_type in {"spule", "drossel"}:
        return _extract_inductor_value(description)
    if lowered_type in {"sicherung", "relais"}:
        return _extract_rating_value(description)
    return ""


def suggest_part_fields(description: str, existing_part_types: Iterable[str]) -> dict[str, str | bool]:
    text = _normalise_text(description)
    type_name, is_new_type = _extract_type(text, existing_part_types)
    value = _extract_value(text, type_name) if type_name else ""
    packaging = _extract_packaging(text, type_name) if type_name else ""
    return {
        "part_type": type_name,
        "value": value,
        "packaging": packaging,
        "is_new_type": is_new_type and bool(type_name),
    }
