from __future__ import annotations

import csv
import io
import re
from collections import Counter

from parsers.formats import canonicalise_match_value, normalise_unicode_text
from .models import Part

MAX_CSV_ROWS = 5000
MAX_CSV_SIZE_BYTES = 5 * 1024 * 1024
IMPORT_MODE_UPDATE = "update_existing"
IMPORT_MODE_SKIP = "skip_existing"
IMPORT_MODE_ADD = "add_quantities"
STATUS_LABELS = {
    "new": "Neu",
    "updated": "Aktualisiert",
    "unchanged": "Unverändert",
    "error": "Fehler",
}

CSV_FIELDS = [
    "id",
    "part_type",
    "value",
    "packaging",
    "description",
    "quantity",
    "location_type",
    "location_name",
    "position",
    "manufacturer",
    "mf_part_no",
    "comment",
]

HEADER_ALIASES = {
    "id": {"id"},
    "part_type": {"part_type", "typ"},
    "value": {"value", "wert"},
    "packaging": {"packaging", "verpackung"},
    "description": {"description", "beschreibung"},
    "quantity": {"quantity", "menge"},
    "location_type": {"location_type", "lagerort_typ", "lagerort-typ"},
    "location_name": {"location_name", "lagerort"},
    "position": {"position"},
    "manufacturer": {"manufacturer", "hersteller"},
    "mf_part_no": {"mf_part_no", "herstellerteilnummer"},
    "comment": {"comment", "kommentar"},
}

IGNORED_COLUMN_HINTS = {
    "supplier",
    "supplier_part_no",
    "last_price",
    "last_purchased",
    "project",
    "projekt",
    "client",
    "price",
    "purchase_date",
}


def _collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _norm_key(value: str) -> str:
    return canonicalise_match_value(_collapse_spaces(value))


def _clean_text(value: str) -> str:
    cleaned = normalise_unicode_text(value).replace("\ufeff", "")
    return _collapse_spaces(cleaned)


def detect_csv_format(raw_bytes: bytes):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Datei konnte nicht als utf-8-sig oder cp1252 gelesen werden.")

    sample = text[:8000]
    candidates = [";", ",", "\t"]
    counts = {d: sample.count(d) for d in candidates}
    delimiter = max(candidates, key=lambda d: counts[d])
    if counts[delimiter] == 0:
        delimiter = ";"
    return text, enc, delimiter


def map_headers(raw_headers):
    canonical = {}
    warnings = []
    unknown = []
    for header in raw_headers:
        cleaned = _clean_text(header)
        norm = _norm_key(cleaned).replace("-", "_")
        found = None
        for target, aliases in HEADER_ALIASES.items():
            if norm in {a.replace("-", "_") for a in aliases}:
                found = target
                break
        if found:
            canonical[header] = found
        else:
            unknown.append(cleaned)
            if norm in IGNORED_COLUMN_HINTS:
                warnings.append(f"Spalte '{cleaned}' wird ignoriert (nicht Teil des Lagerimports).")
            else:
                warnings.append(f"Unbekannte Spalte '{cleaned}' wird ignoriert.")

    return canonical, unknown, warnings


def location_type_maps():
    valid = {}
    labels = {}
    for value, label in Part.LocationType.choices:
        valid[_norm_key(value)] = value
        labels[_norm_key(label)] = value
    return valid, labels


def validate_and_normalize_row(row, line_no, location_values, location_labels):
    data = {field: "" for field in CSV_FIELDS}
    errors = []
    warnings = []

    for field in CSV_FIELDS:
        raw = row.get(field, "")
        data[field] = _clean_text(str(raw)) if raw is not None else ""

    if not data["part_type"]:
        errors.append("Pflichtfeld 'part_type/Typ' ist leer.")

    if data["quantity"] == "":
        errors.append("Pflichtfeld 'quantity/Menge' ist leer.")
    elif not re.fullmatch(r"\d+", data["quantity"]):
        errors.append("Menge muss eine ganze Zahl >= 0 sein.")

    if not data["value"] and not data["description"]:
        errors.append("Mindestens 'value/Wert' oder 'description/Beschreibung' muss gesetzt sein.")

    if data["location_type"]:
        key = _norm_key(data["location_type"])
        mapped = location_values.get(key) or location_labels.get(key)
        if mapped:
            data["location_type"] = mapped
        else:
            valid_labels = ", ".join(label for _, label in Part.LocationType.choices)
            warnings.append(
                f"Unbekannter location_type '{data['location_type']}', Feld wird geleert. Gültig: {valid_labels}."
            )
            data["location_type"] = ""

    packaging_digits = data["packaging"]
    if re.fullmatch(r"\d{3}", packaging_digits) and packaging_digits in {"402", "603", "805"}:
        suggestions = {"402": "0402", "603": "0603", "805": "0805"}
        warnings.append(f"Verpackung '{packaging_digits}' sieht gekürzt aus, gemeint ist evtl. '{suggestions[packaging_digits]}'.")

    date_like = re.compile(r"^\d{1,2}[./-]\d{1,2}([./-]\d{2,4})?$|^\d{4}-\d{2}-\d{2}$")
    if data["value"] and date_like.fullmatch(data["value"]):
        warnings.append("Wert sieht wie ein automatisch konvertiertes Datum aus.")
    if data["position"] and date_like.fullmatch(data["position"]):
        warnings.append("Position sieht wie ein automatisch konvertiertes Datum aus.")

    if errors:
        return {"line_no": line_no, "errors": errors, "warnings": warnings, "data": data}
    return {"line_no": line_no, "errors": [], "warnings": warnings, "data": data}


def normalized_match_key(data):
    value = data.get("value", "")
    base = (
        _norm_key(data.get("part_type", "")),
        _norm_key(value),
        _norm_key(data.get("packaging", "")),
        _norm_key(data.get("location_name", "")),
        _norm_key(data.get("position", "")),
    )
    if not _norm_key(value):
        return base + (_norm_key(data.get("description", "")),)
    return base


def build_existing_part_index():
    key_map = {}
    for part in Part.objects.all():
        sample = {
            "part_type": part.part_type,
            "value": part.value,
            "packaging": part.packaging,
            "location_name": part.location_name,
            "position": part.position,
            "description": part.description,
        }
        key = normalized_match_key(sample)
        key_map.setdefault(key, []).append(part)
    return key_map


def summarize_distinct(rows):
    fields = ["part_type", "packaging", "location_type", "location_name"]
    summary = {}
    for field in fields:
        counter = Counter()
        for row in rows:
            value = row["data"].get(field, "")
            if value:
                counter[value] += 1
        summary[field] = [{"value": value, "count": count} for value, count in sorted(counter.items())]
    return summary


def parse_csv_bytes(raw_bytes):
    if len(raw_bytes) > MAX_CSV_SIZE_BYTES:
        raise ValueError("Datei ist zu groß (maximal 5 MB).")
    text, encoding, delimiter = detect_csv_format(raw_bytes)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("CSV enthält keine Kopfzeile.")
    mapped_headers, unknown_columns, header_warnings = map_headers(reader.fieldnames)
    required = {"part_type", "quantity"}
    present = set(mapped_headers.values())
    missing_required = sorted(required - present)
    if missing_required:
        raise ValueError(f"Pflichtspalten fehlen: {', '.join(missing_required)}.")

    normalized_rows = []
    location_values, location_labels = location_type_maps()
    for index, raw_row in enumerate(reader, start=2):
        if len(normalized_rows) >= MAX_CSV_ROWS:
            raise ValueError(f"Zu viele Zeilen. Maximum ist {MAX_CSV_ROWS}.")
        translated = {mapped_headers[key]: value for key, value in raw_row.items() if key in mapped_headers}
        normalized_rows.append(validate_and_normalize_row(translated, index, location_values, location_labels))

    return {
        "encoding": encoding,
        "delimiter": delimiter,
        "rows": normalized_rows,
        "header_warnings": header_warnings,
        "unknown_columns": unknown_columns,
    }


def _part_updates_for_row(part, data):
    changes = []
    update_fields = []

    for field in [
        "part_type",
        "value",
        "packaging",
        "description",
        "location_type",
        "location_name",
        "position",
        "manufacturer",
        "mf_part_no",
        "comment",
    ]:
        incoming = data.get(field, "")
        if incoming == "":
            continue
        previous = getattr(part, field)
        if previous != incoming:
            changes.append({"field": field, "old": str(previous or ""), "new": incoming})
            setattr(part, field, incoming)
            update_fields.append(field)

    return changes, update_fields


def prepare_stock_preview(parsed, mode=IMPORT_MODE_UPDATE):
    existing_by_id = {part.pk: part for part in Part.objects.all()}
    existing_by_key = build_existing_part_index()
    preview_rows = []
    file_warnings = list(parsed.get("header_warnings", []))
    errors = []
    id_lines = {}
    key_lines = {}

    for row in parsed["rows"]:
        row_data = row["data"]
        line_no = row["line_no"]
        line_errors = list(row["errors"])
        line_warnings = list(row["warnings"])
        row_status = "error" if line_errors else "new"
        action = "create"
        matched_part = None
        changes = []
        quantity_int = int(row_data["quantity"]) if row_data["quantity"].isdigit() else 0

        row_id = row_data.get("id", "")
        key = normalized_match_key(row_data)

        if row_id:
            if row_id in id_lines:
                first = id_lines[row_id]
                line_errors.append(f"Doppelte id '{row_id}' in Zeile {first} und {line_no}.")
            else:
                id_lines[row_id] = line_no
            if row_id.isdigit():
                matched_part = existing_by_id.get(int(row_id))
            if not matched_part:
                line_errors.append(f"id '{row_id}' existiert nicht.")
        else:
            if key in key_lines:
                first = key_lines[key]
                line_errors.append(f"Doppelter Schlüssel in Zeile {first} und {line_no}.")
            else:
                key_lines[key] = line_no
            found = existing_by_key.get(key, [])
            if len(found) > 1:
                line_errors.append("Schlüssel ist nicht eindeutig und matcht mehrere vorhandene Bauteile.")
            elif len(found) == 1:
                matched_part = found[0]

        if line_errors:
            row_status = "error"
            errors.extend([f"Zeile {line_no}: {msg}" for msg in line_errors])
        elif matched_part is not None:
            action = "match"
            if mode == IMPORT_MODE_SKIP:
                row_status = "unchanged"
            elif mode == IMPORT_MODE_ADD:
                new_qty = int(matched_part.quantity) + quantity_int
                if new_qty != int(matched_part.quantity):
                    changes.append({"field": "quantity", "old": str(matched_part.quantity), "new": str(new_qty)})
                    row_status = "updated"
                else:
                    row_status = "unchanged"
            else:
                if int(matched_part.quantity) != quantity_int:
                    changes.append({"field": "quantity", "old": str(matched_part.quantity), "new": str(quantity_int)})
                field_changes, _unused = _part_updates_for_row(matched_part, row_data)
                changes.extend(field_changes)
                row_status = "updated" if changes else "unchanged"
        else:
            row_status = "new"

        preview_rows.append(
            {
                "line_no": line_no,
                "status": row_status,
                "status_label": STATUS_LABELS.get(row_status, row_status),
                "action": action,
                "errors": line_errors,
                "warnings": line_warnings,
                "data": row_data,
                "changes": changes,
                "matched_part_id": matched_part.pk if matched_part else None,
                "quantity_int": quantity_int,
            }
        )

    counts = {"new": 0, "updated": 0, "unchanged": 0, "error": 0, "warning": 0}
    for row in preview_rows:
        counts[row["status"]] += 1
        if row["warnings"]:
            counts["warning"] += len(row["warnings"])

    distinct = summarize_distinct(preview_rows)
    existing_values = {
        "part_type": {_clean_text(value) for value in Part.objects.exclude(part_type="").values_list("part_type", flat=True)},
        "packaging": {_clean_text(value) for value in Part.objects.exclude(packaging="").values_list("packaging", flat=True)},
        "location_type": {label for _, label in Part.LocationType.choices} | {value for value, _ in Part.LocationType.choices},
        "location_name": {_clean_text(value) for value in Part.objects.exclude(location_name="").values_list("location_name", flat=True)},
    }
    for field, items in distinct.items():
        for item in items:
            item["is_new"] = _clean_text(item["value"]) not in existing_values[field]

    return {
        "encoding": parsed["encoding"],
        "delimiter": parsed["delimiter"],
        "mode": mode,
        "mode_label": {
            IMPORT_MODE_UPDATE: "Vorhandene aktualisieren",
            IMPORT_MODE_SKIP: "Vorhandene überspringen",
            IMPORT_MODE_ADD: "Mengen addieren",
        }.get(mode, mode),
        "rows": preview_rows,
        "counts": counts,
        "errors": errors,
        "warnings": file_warnings,
        "distinct": distinct,
    }


def apply_stock_preview(preview):
    created = 0
    updated = 0
    mode = preview.get("mode", IMPORT_MODE_UPDATE)
    for row in preview["rows"]:
        if row["status"] == "error":
            continue
        data = row["data"]
        qty = row["quantity_int"]
        part = None
        if row["matched_part_id"]:
            part = Part.objects.get(pk=row["matched_part_id"])
        if part is None:
            Part.objects.create(
                part_type=data["part_type"],
                value=data["value"],
                packaging=data["packaging"],
                description=data["description"],
                quantity=qty,
                location_type=data["location_type"],
                location_name=data["location_name"],
                position=data["position"],
                manufacturer=data["manufacturer"],
                mf_part_no=data["mf_part_no"],
                comment=data["comment"],
            )
            created += 1
            continue

        if mode == IMPORT_MODE_SKIP:
            continue
        changed = False
        if mode == IMPORT_MODE_ADD:
            part.quantity = int(part.quantity) + qty
            changed = qty != 0
        else:
            if int(part.quantity) != qty:
                part.quantity = qty
                changed = True
            for field in [
                "part_type",
                "value",
                "packaging",
                "description",
                "location_type",
                "location_name",
                "position",
                "manufacturer",
                "mf_part_no",
                "comment",
            ]:
                incoming = data.get(field, "")
                if incoming == "":
                    continue
                if getattr(part, field) != incoming:
                    setattr(part, field, incoming)
                    changed = True
        if changed:
            part.save()
            updated += 1
    return created, updated


def delimiter_label(delimiter):
    if delimiter == "\t":
        return "Tabulator"
    if delimiter == ";":
        return ";"
    if delimiter == ",":
        return ","
    return delimiter
