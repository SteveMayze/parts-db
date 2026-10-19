from __future__ import annotations

from dataclasses import dataclass
import io
import re
import csv
import unicodedata
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timedelta
from decimal import Decimal
from functools import cmp_to_key

from django.contrib import messages
from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q
from django.db.models.functions import Length
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from parsers import detect_parser_from_text, get_parser_for_supplier
from parsers.describe import suggest_part_fields
from parsers.formats import canonicalise_match_value, normalise_unicode_text
from .forms import InvoiceUploadForm, PartForm, ProjectForm, StockCsvUploadForm
from .models import ImportedDocument, Part, Project, PurchaseDocument, PurchaseLine, Supplier
from .stock_csv import (
    IMPORT_MODE_UPDATE,
    apply_stock_preview,
    delimiter_label,
    parse_csv_bytes,
    prepare_stock_preview,
)

MAX_BATCH_FILES = 20
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
PREVIEW_TTL = timedelta(hours=24)
STOCK_PREVIEW_TTL = timedelta(hours=24)
QUANTITY_MODE_ADD = "add_stock"
QUANTITY_MODE_KEEP = "no_stock_change"
DOC_TYPE_MAP = {
    "invoice": PurchaseDocument.DocumentType.RECHNUNG,
    "order": PurchaseDocument.DocumentType.BESTELLUNG,
    "order_confirmation": PurchaseDocument.DocumentType.AUFTRAGSBESTATIGUNG,
}


def _storage_document_type(raw_value):
    if not raw_value:
        return PurchaseDocument.DocumentType.SONSTIGES
    return DOC_TYPE_MAP.get(str(raw_value).strip().casefold(), PurchaseDocument.DocumentType.SONSTIGES)


def _tmp_import_dir() -> Path:
    target = Path(settings.MEDIA_ROOT) / "tmp_imports"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _save_temp_pdf(uploaded_file):
    suffix = Path(uploaded_file.name).suffix or ".pdf"
    tmp_name = f"{timezone.now().strftime('%Y%m%d%H%M%S')}-{uuid4().hex}{suffix}"
    target = _tmp_import_dir() / tmp_name
    with target.open("wb") as handle:
        for chunk in uploaded_file.chunks():
            handle.write(chunk)
    return str(target)


def _project_choices_with_current(current_project=None):
    active_projects = list(Project.objects.filter(active=True).order_by("name"))
    if current_project and current_project not in active_projects:
        active_projects.append(current_project)
    return active_projects


def _resolve_project_selection(raw_value, fallback=None):
    value = (raw_value or "").strip()
    if not value:
        return fallback
    if value == "__none__":
        return None
    return Project.objects.filter(pk=value).first()


def _canonicalise_search(value):
    return canonicalise_match_value(value)


def _sort_text_key(value):
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


def _empty_last_compare(left, right):
    left_empty = left in (None, "")
    right_empty = right in (None, "")
    if left_empty and right_empty:
        return 0
    if left_empty:
        return 1
    if right_empty:
        return -1
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def _compare_part_sort_values(left, right, *, direction="asc"):
    left_value = left
    right_value = right
    left_is_empty = left_value in (None, "")
    right_is_empty = right_value in (None, "")
    if left_is_empty and right_is_empty:
        return 0
    if left_is_empty:
        return 1
    if right_is_empty:
        return -1

    left_key = _sort_text_key(left_value)
    right_key = _sort_text_key(right_value)
    if left_key < right_key:
        result = -1
    elif left_key > right_key:
        result = 1
    else:
        result = 0
    return -result if direction == "desc" else result


def _part_secondary_sort_tuple(part):
    return (
        _sort_text_key(part.part_type),
        _sort_text_key(part.part_subtype),
        _sort_text_key(part.value),
        _sort_text_key(part.packaging),
        part.pk,
    )


def _part_sort_key(part, sort_key):
    if sort_key == "type":
        return _sort_text_key(part.part_type)
    if sort_key == "value":
        return _sort_text_key(part.value)
    if sort_key == "description":
        return _sort_text_key(part.description)
    if sort_key == "part_no":
        return _sort_text_key(part.mf_part_no)
    if sort_key == "location":
        return (
            _sort_text_key(part.location_name),
            _sort_text_key(part.position),
            _sort_text_key(part.get_location_type_display()),
        )
    return ()


def _compare_parts(left, right, *, sort_key, direction="asc"):
    if sort_key == "type":
        left_value = left.part_type
        right_value = right.part_type
    elif sort_key == "value":
        left_value = left.value
        right_value = right.value
    elif sort_key == "description":
        left_value = left.description
        right_value = right.description
    elif sort_key == "part_no":
        left_value = left.mf_part_no
        right_value = right.mf_part_no
    elif sort_key == "location":
        left_value = (left.location_name, left.position, left.get_location_type_display())
        right_value = (right.location_name, right.position, right.get_location_type_display())
        if left_value == right_value:
            return _compare_part_secondary_sort(left, right)
        left_value_key = tuple(_sort_text_key(item) for item in left_value)
        right_value_key = tuple(_sort_text_key(item) for item in right_value)
        if left_value_key < right_value_key:
            result = -1
        elif left_value_key > right_value_key:
            result = 1
        else:
            result = 0
        if direction == "desc":
            result *= -1
        if result == 0:
            return _compare_part_secondary_sort(left, right)
        return result
    else:
        return 0

    left_is_empty = left_value in (None, "")
    right_is_empty = right_value in (None, "")
    if left_is_empty and right_is_empty:
        return _compare_part_secondary_sort(left, right)
    if left_is_empty:
        return 1
    if right_is_empty:
        return -1

    left_key = _sort_text_key(left_value)
    right_key = _sort_text_key(right_value)
    if left_key < right_key:
        result = -1
    elif left_key > right_key:
        result = 1
    else:
        result = 0
    if direction == "desc":
        result *= -1
    if result == 0:
        return _compare_part_secondary_sort(left, right)
    return result


def _compare_part_secondary_sort(left, right):
    left_key = _part_secondary_sort_tuple(left)
    right_key = _part_secondary_sort_tuple(right)
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


def _serialise_preview_date(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _extract_pdf_text(raw_bytes):
    try:
        import pdfplumber
    except ImportError as exc:
        raise ValueError("Für den PDF-Import wird pdfplumber benötigt.") from exc

    try:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                pages.append(text)
            text = "\n".join(pages)
    except Exception as exc:
        raise ValueError(f"PDF-Auswertung fehlgeschlagen: {exc}") from exc

    if not text:
        raise ValueError("Die PDF-Datei konnte nicht gelesen werden (kein Text extrahiert).")
    text = normalise_unicode_text(text)
    if "�" in text or "Ã" in text:
        raise ValueError("Der extrahierte PDF-Text enthält fehlerhafte Zeichen und wurde sicherheitshalber verworfen.")
    return text


def _match_candidate_parts(line_data):
    supplier = (line_data.get("supplier") or "").strip()
    supplier_part_no = (line_data.get("supplier_part_no") or "").strip()
    mf_part_no = (line_data.get("mf_part_no") or "").strip()

    if supplier and supplier_part_no:
        match = Part.objects.filter(
            supplier__iexact=supplier,
            supplier_part_no__iexact=supplier_part_no,
        ).first()
        if match:
            return match, "supplier_part_no"
    if mf_part_no:
        match = Part.objects.filter(mf_part_no__iexact=mf_part_no).first()
        if match:
            return match, "mf_part_no"
    return None, None


def _build_line_candidates(line_data):
    part, match_type = _match_candidate_parts(line_data)
    status = "matched" if part else "new"
    candidates = []
    if not part:
        description = line_data.get("description", "")
        tokens = [
            token
            for token in re.split(r"[^\w\u00C0-\uFFFF]+", description, flags=re.UNICODE)
            if token and len(token) > 2
        ]
        text_matches = []
        for part_candidate in Part.objects.all():
            haystack = " ".join(
                [
                    part_candidate.description or "",
                    part_candidate.value or "",
                    part_candidate.packaging or "",
                ]
            )
            score = 0
            for token in tokens:
                if _canonicalise_search(token) in _canonicalise_search(haystack):
                    score += 1
            if score:
                text_matches.append((score, part_candidate))
        for _, candidate in sorted(text_matches, key=lambda item: item[0], reverse=True)[:3]:
            candidates.append({"pk": candidate.pk, "label": str(candidate)})
        if candidates:
            status = "needs_review"
    status_label = {
        "matched": "Treffer",
        "new": "Neu",
        "needs_review": "Prüfen",
    }.get(status, status)
    return part, match_type, status, status_label, candidates


def _get_existing_part_types() -> list[str]:
    return list(Part.objects.exclude(part_type="").values_list("part_type", flat=True).distinct())


def _classification_suggestions() -> dict:
    """Distinct type/subtype values already in use, for <datalist> autocomplete.

    ``subtypes_by_type`` maps the casefolded type to the subtypes used with it, so the
    form can narrow the subtype suggestions once a type is entered.
    """
    pairs = Part.objects.values_list("part_type", "part_subtype").distinct()
    types = set()
    subtypes = set()
    by_type: dict[str, set[str]] = {}
    for part_type, part_subtype in pairs:
        if part_type:
            types.add(part_type)
        if part_subtype:
            subtypes.add(part_subtype)
            by_type.setdefault(part_type.casefold(), set()).add(part_subtype)
    return {
        "known_part_types": sorted(types, key=str.casefold),
        "known_part_subtypes": sorted(subtypes, key=str.casefold),
        "subtypes_by_type": {key: sorted(values, key=str.casefold) for key, values in by_type.items()},
    }


def build_invoice_preview(invoice_data):
    existing_part_types = _get_existing_part_types()
    new_type_names: set[str] = set()
    lines = []
    for index, line_data in enumerate(invoice_data.get("lines", [])):
        part, match_type, status, status_label, candidates = _build_line_candidates(line_data)
        suggestions = suggest_part_fields(line_data.get("description", ""), existing_part_types)
        if suggestions["is_new_type"]:
            new_type_names.add(str(suggestions["part_type"]))
        editable_type = (part.part_type if part else "") or str(suggestions["part_type"] or "")
        editable_value = (part.value if part else "") or str(suggestions["value"] or "")
        editable_packaging = (part.packaging if part else "") or str(suggestions["packaging"] or "")
        editable_subtype = part.part_subtype if part else ""
        lines.append(
            {
                "index": index,
                "description": line_data.get("description", ""),
                "supplier": line_data.get("supplier", invoice_data.get("supplier", "")),
                "supplier_part_no": line_data.get("supplier_part_no", ""),
                "mf_part_no": line_data.get("mf_part_no", ""),
                "quantity": int(line_data.get("quantity", 0) or 0),
                "unit_price": str(line_data.get("unit_price", "0.00")),
                "gross_unit_price": str(line_data["gross_unit_price"]) if line_data.get("gross_unit_price") is not None else "",
                "line_total_gross": str(line_data["line_total_gross"]) if line_data.get("line_total_gross") is not None else "",
                "vat_rate": str(line_data["vat_rate"]) if line_data.get("vat_rate") is not None else "",
                "currency": line_data.get("currency", "EUR"),
                "status": status,
                "status_label": status_label,
                "match_type": match_type,
                "part": (
                    {
                        "pk": part.pk,
                        "label": str(part),
                        "part_type": part.part_type,
                        "part_subtype": part.part_subtype,
                        "manufacturer": part.manufacturer,
                        "mf_part_no": part.mf_part_no,
                    }
                    if part
                    else None
                ),
                "candidates": candidates,
                "whg": line_data.get("whg", ""),
                "part_type": editable_type,
                "part_subtype": editable_subtype,
                "manufacturer": "",
                "edit_mf_part_no": line_data.get("mf_part_no", ""),
                "value": editable_value,
                "packaging": editable_packaging,
                "part_type_suggested": str(suggestions["part_type"] or ""),
                "value_suggested": str(suggestions["value"] or ""),
                "packaging_suggested": str(suggestions["packaging"] or ""),
                "project_id": "",
                "edit_description": line_data.get("description", ""),
                "edit_quantity": str(int(line_data.get("quantity", 0) or 0)),
                "part_location": _describe_location(part) if part else "",
                "part_has_location": bool(part and (part.location_type or part.location_name)),
                "location_type": "",
                "location_name": "",
                "location_position": "",
                "change_location": False,
                "skip": False,
                "selected_match": "",
            }
        )

    duplicate_existing = ImportedDocument.objects.filter(
        supplier__iexact=invoice_data.get("supplier", ""),
        document_no__iexact=invoice_data.get("invoice_no", ""),
    ).first()
    duplicate_at = duplicate_existing.imported_at.isoformat() if duplicate_existing else ""
    return {
        "supplier": invoice_data.get("supplier", ""),
        "invoice_no": invoice_data.get("invoice_no", ""),
        "invoice_date": _serialise_preview_date(invoice_data.get("invoice_date", "")),
        "pdf_temp_path": invoice_data.get("pdf_temp_path", ""),
        "pdf_original_name": invoice_data.get("pdf_original_name", ""),
        "related_order_number": invoice_data.get("reference_note", ""),
        "warning": invoice_data.get("warning", ""),
        "warnings": invoice_data.get("warnings", []),
        "reference_note": invoice_data.get("reference_note", ""),
        "whg_note": invoice_data.get("whg_note", ""),
        "duplicate_invoice": bool(duplicate_existing),
        "duplicate_imported_at": duplicate_at,
        "document_type": invoice_data.get("document_type", ""),
        "price_basis": invoice_data.get("price_basis", "net"),
        "layout": invoice_data.get("layout", ""),
        "notes": [str(note) for note in invoice_data.get("notes", [])],
        "discount_total": str(invoice_data.get("discount_total") or Decimal("0.00")),
        "document_total": str(
            invoice_data.get("document_total") or invoice_data.get("net_total") or invoice_data.get("gross_total") or Decimal("0.00")
        ),
        "new_type_names": sorted(new_type_names),
        "lines": lines,
    }


def _parse_uploaded_document(uploaded_file, supplier_override: str):
    if not uploaded_file.name.lower().endswith(".pdf"):
        raise ValueError("Es werden nur PDF-Dateien unterstützt.")
    raw_bytes = uploaded_file.read()
    text = _extract_pdf_text(raw_bytes)
    detected_parser = detect_parser_from_text(text)

    parser = detected_parser
    if supplier_override:
        parser = get_parser_for_supplier(supplier_override)
        if parser.supplier != detected_parser.supplier:
            raise ValueError(
                f"Lieferant passt nicht zu {uploaded_file.name}: erkannt {detected_parser.supplier}, erwartet {parser.supplier}."
            )

    if hasattr(parser, "parse_pdf"):
        invoice_data = parser.parse_pdf(raw_bytes, filename=uploaded_file.name)
    else:
        invoice_data = parser.parse_invoice_text(text)
    invoice_data["supplier"] = parser.supplier
    for line in invoice_data["lines"]:
        line["supplier"] = parser.supplier
    return invoice_data


def _sort_documents(preview_docs: list[dict]) -> list[dict]:
    def key_fn(doc):
        date_value = doc.get("invoice_date") or "9999-12-31"
        return date_value, doc.get("invoice_no", "")

    return sorted(preview_docs, key=key_fn)


def _build_batch_preview(parsed_documents):
    duplicate_in_batch = []
    seen = set()
    preview_documents = []
    batch_new_type_names: set[str] = set()

    for item in parsed_documents:
        preview = build_invoice_preview(item["invoice_data"])
        doc_key = (preview["supplier"].casefold(), preview["invoice_no"].casefold())
        if doc_key in seen:
            duplicate_in_batch.append(
                {
                    "file_name": item["file_name"],
                    "supplier": preview["supplier"],
                    "document_no": preview["invoice_no"],
                    "reason": "Doppelte Belegnummer im selben Import; nur der erste Beleg wird verwendet.",
                }
            )
            continue
        seen.add(doc_key)
        preview["file_name"] = item["file_name"]
        preview["document_id"] = f"{preview['supplier']}::{preview['invoice_no']}::{preview['file_name']}"
        preview["excluded_duplicate_default"] = bool(preview["duplicate_invoice"])
        batch_new_type_names.update(preview.get("new_type_names", []))
        for line in preview["lines"]:
            line["field_prefix"] = f"{preview['document_id']}::{line['index']}"
        preview_documents.append(preview)

    sorted_documents = _sort_documents(preview_documents)
    summary = {"documents": len(sorted_documents), "lines": 0, "matched": 0, "new": 0, "needs_review": 0}
    for doc in sorted_documents:
        counts = {"matched": 0, "new": 0, "needs_review": 0}
        for line in doc["lines"]:
            counts[line["status"]] += 1
        doc["counts"] = counts
        summary["lines"] += len(doc["lines"])
        summary["matched"] += counts["matched"]
        summary["new"] += counts["new"]
        summary["needs_review"] += counts["needs_review"]

    return sorted_documents, summary, duplicate_in_batch, sorted(batch_new_type_names)


def _validate_single_supplier(parsed_documents: list[dict]) -> None:
    suppliers = {doc["invoice_data"].get("supplier", "") for doc in parsed_documents}
    if len(suppliers) <= 1:
        return
    details = ", ".join(
        f"{doc['file_name']} → {doc['invoice_data'].get('supplier', '')}"
        for doc in sorted(parsed_documents, key=lambda item: item["file_name"])
    )
    raise ValueError(f"Alle Dateien eines Imports müssen vom selben Lieferanten sein. Erkannt: {details}")


def _parse_preview_timestamp(payload):
    value = payload.get("created_at")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _get_valid_preview_payload(request):
    payload = request.session.get("invoice_preview_batch")
    if not payload:
        return None
    created_at = _parse_preview_timestamp(payload)
    if created_at is None:
        request.session.pop("invoice_preview_batch", None)
        return None
    if timezone.now() - created_at > PREVIEW_TTL:
        request.session.pop("invoice_preview_batch", None)
        messages.error(request, "Die Vorschau ist abgelaufen. Bitte die Dateien erneut hochladen.")
        return None
    return payload


def _get_valid_stock_preview(request):
    payload = request.session.get("stock_csv_preview")
    if not payload:
        return None
    created_at = _parse_preview_timestamp(payload)
    if created_at is None:
        request.session.pop("stock_csv_preview", None)
        return None
    if timezone.now() - created_at > STOCK_PREVIEW_TTL:
        request.session.pop("stock_csv_preview", None)
        messages.error(request, "Die CSV-Vorschau ist abgelaufen. Bitte Datei erneut hochladen.")
        return None
    return payload


def _parse_sort_params(request, allowed):
    sort_key = request.GET.get("sort", "").strip()
    direction = request.GET.get("dir", "asc").strip().lower()
    if sort_key not in allowed or direction not in {"asc", "desc"}:
        return "", "asc"
    return sort_key, direction


def _build_sort_headers(request, allowed, current_sort, current_dir):
    columns = []
    for key, label in allowed.items():
        is_active = current_sort == key
        direction = "desc" if is_active and current_dir == "asc" else "asc"
        params = request.GET.copy()
        params["sort"] = key
        params["dir"] = direction
        params["page"] = "1"
        columns.append(
            {
                "key": key,
                "label": label,
                "url": "?{}".format(params.urlencode()),
                "active": is_active,
                "aria_sort": ("ascending" if current_dir == "asc" else "descending") if is_active else None,
                "arrow": ("▲" if current_dir == "asc" else "▼") if is_active else "",
                "sort_title": f"Nach {label} sortieren",
            }
        )
    return columns


class PartListView(ListView):
    model = Part
    template_name = "parts/part_list.html"
    context_object_name = "parts"
    allowed_sort_keys = {
        "type": "Typ",
        "value": "Wert",
        "description": "Beschreibung",
        "part_no": "Teilenummer",
        "location": "Lagerort",
    }

    def get_queryset(self):
        queryset = super().get_queryset()
        query = self.request.GET.get("q", "").strip()
        part_type = self.request.GET.get("part_type", "").strip()
        part_subtype = self.request.GET.get("part_subtype", "").strip()
        packaging = self.request.GET.get("packaging", "").strip()

        if query:
            query_fold = _canonicalise_search(query)
            filtered = []
            for part in queryset:
                haystack = " ".join(
                    (
                        part.part_type or "",
                        part.part_subtype or "",
                        part.value or "",
                        part.packaging or "",
                        part.description or "",
                        part.manufacturer or "",
                        part.mf_part_no or "",
                        part.supplier or "",
                        part.supplier_part_no or "",
                    )
                )
                if query_fold in _canonicalise_search(haystack):
                    filtered.append(part.pk)
            queryset = queryset.filter(pk__in=filtered)
        if part_type:
            queryset = queryset.filter(part_type__iexact=part_type)
        if part_subtype:
            queryset = queryset.filter(part_subtype__iexact=part_subtype)
        if packaging:
            queryset = queryset.filter(packaging__iexact=packaging)

        sort_key, direction = _parse_sort_params(self.request, self.allowed_sort_keys)
        if sort_key:
            queryset = sorted(queryset, key=cmp_to_key(lambda left, right: _compare_parts(left, right, sort_key=sort_key, direction=direction)))
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_q"] = self.request.GET.get("q", "").strip()
        context["current_part_type"] = self.request.GET.get("part_type", "").strip()
        context["current_packaging"] = self.request.GET.get("packaging", "").strip()
        context["current_part_subtype"] = self.request.GET.get("part_subtype", "").strip()
        context["part_types"] = (
            Part.objects.exclude(part_type="").order_by("part_type").values_list("part_type", flat=True).distinct()
        )
        subtype_qs = Part.objects.exclude(part_subtype="")
        if context["current_part_type"]:
            subtype_qs = subtype_qs.filter(part_type__iexact=context["current_part_type"])
        context["part_subtypes"] = subtype_qs.order_by("part_subtype").values_list("part_subtype", flat=True).distinct()
        context["packaging_options"] = (
            Part.objects.exclude(packaging="").order_by("packaging").values_list("packaging", flat=True).distinct()
        )

        current_sort, current_dir = _parse_sort_params(self.request, self.allowed_sort_keys)
        columns = _build_sort_headers(self.request, self.allowed_sort_keys, current_sort, current_dir)
        context["sortable_columns"] = columns
        context["sort_headers"] = {column["key"]: column for column in columns}
        context["current_sort"] = current_sort
        context["current_dir"] = current_dir
        return context


class PartFormContextMixin:
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_classification_suggestions())
        return context


class PartCreateView(PartFormContextMixin, CreateView):
    model = Part
    form_class = PartForm
    template_name = "parts/part_form.html"
    success_url = reverse_lazy("parts:list")


class PartUpdateView(PartFormContextMixin, UpdateView):
    model = Part
    form_class = PartForm
    template_name = "parts/part_form.html"
    success_url = reverse_lazy("parts:list")


class PartDeleteView(DeleteView):
    model = Part
    template_name = "parts/part_confirm_delete.html"
    success_url = reverse_lazy("parts:list")


def stock_csv_upload(request):
    if request.method == "POST":
        form = StockCsvUploadForm(request.POST, request.FILES)
        if form.is_valid():
            upload = request.FILES["csv_file"]
            mode = form.cleaned_data["mode"]
            try:
                parsed = parse_csv_bytes(upload.read())
                preview = prepare_stock_preview(parsed, mode=mode)
            except ValueError as exc:
                form.add_error("csv_file", str(exc))
            else:
                request.session["stock_csv_preview"] = {
                    "created_at": timezone.now().isoformat(),
                    "preview": preview,
                }
                return redirect("parts:stock_csv_preview")
    else:
        form = StockCsvUploadForm()
    return render(request, "parts/stock_csv_upload.html", {"form": form})


def stock_csv_preview(request):
    payload = _get_valid_stock_preview(request)
    if not payload:
        return redirect("parts:stock_csv_upload")
    preview = payload["preview"]

    if request.method == "POST":
        continue_without_errors = request.POST.get("continue_without_errors") == "yes"
        has_errors = any(row["status"] == "error" for row in preview["rows"])
        valid_rows = [row for row in preview["rows"] if row["status"] != "error"]
        if has_errors and not continue_without_errors:
            messages.error(request, "Bitte bestätigen Sie das Fortfahren ohne fehlerhafte Zeilen.")
            return render(
                request,
                "parts/stock_csv_preview.html",
                {"preview": preview, "delimiter_label": delimiter_label(preview.get("delimiter", ";"))},
            )
        if not valid_rows:
            messages.error(request, "Es gibt keine fehlerfreien Zeilen zum Importieren.")
            return render(
                request,
                "parts/stock_csv_preview.html",
                {"preview": preview, "delimiter_label": delimiter_label(preview.get("delimiter", ";"))},
            )
        transactional_preview = dict(preview)
        transactional_preview["rows"] = valid_rows
        with transaction.atomic():
            created, updated = apply_stock_preview(transactional_preview)
        request.session.pop("stock_csv_preview", None)
        messages.success(request, f"CSV-Import abgeschlossen. Neu: {created}, aktualisiert: {updated}.")
        return redirect("parts:list")

    return render(
        request,
        "parts/stock_csv_preview.html",
        {"preview": preview, "delimiter_label": delimiter_label(preview.get("delimiter", ";"))},
    )


def stock_csv_template(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = 'attachment; filename="stock_vorlage.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow([
        "id",
        "part_type",
        "part_subtype",
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
    ])
    writer.writerow(["", "Widerstand", "", "4,7k", "0805", "SMD Widerstand 1%", "200", "drawer", "Schubladenblock A", "Fach 3", "Yageo", "RC0805", "Bestand Alt"])
    writer.writerow(["", "Kondensator", "Keramik", "100nF", "0603", "Kerko X7R", "350", "folder", "Mappe 2", "Seite 4", "Murata", "GRM188", ""])
    writer.writerow(["", "Transistor", "", "", "TO-92", "BC547B NPN", "50", "", "Sortierbox 1", "Reihe C", "onsemi", "BC547B", "ohne exakten Wert"])
    return response


def stock_csv_export(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = 'attachment; filename="bauteile_export.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow([
        "id",
        "part_type",
        "part_subtype",
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
    ])
    for part in Part.objects.order_by("id"):
        writer.writerow(
            [
                part.id,
                part.part_type,
                part.part_subtype,
                part.value,
                part.packaging,
                part.description,
                part.quantity,
                part.location_type,
                part.location_name,
                part.position,
                part.manufacturer,
                part.mf_part_no,
                part.comment,
            ]
        )
    return response


class ProjectListView(ListView):
    model = Project
    template_name = "parts/project_list.html"
    context_object_name = "projects"

    def get_queryset(self):
        queryset = Project.objects.annotate(
            tagged_lines_count=Count("purchase_lines", distinct=True),
            tagged_documents_count=Count("purchase_lines__document", distinct=True),
        )
        client = self.request.GET.get("client", "").strip()
        active = self.request.GET.get("active", "").strip()
        if client:
            queryset = queryset.filter(client__iexact=client)
        if active in {"yes", "no"}:
            queryset = queryset.filter(active=(active == "yes"))
        return queryset.order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_client"] = self.request.GET.get("client", "").strip()
        context["current_active"] = self.request.GET.get("active", "").strip()
        context["clients"] = (
            Project.objects.exclude(client="")
            .order_by("client")
            .values_list("client", flat=True)
            .distinct()
        )
        return context


class ProjectCreateView(CreateView):
    model = Project
    form_class = ProjectForm
    template_name = "parts/project_form.html"
    success_url = reverse_lazy("parts:projects")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["client_suggestions"] = (
            Project.objects.exclude(client="")
            .order_by("client")
            .values_list("client", flat=True)
            .distinct()
        )
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        next_url = self.request.GET.get("next", "").strip()
        if next_url:
            return redirect(next_url)
        messages.success(self.request, "Projekt wurde angelegt.")
        return response


class ProjectUpdateView(UpdateView):
    model = Project
    form_class = ProjectForm
    template_name = "parts/project_form.html"
    success_url = reverse_lazy("parts:projects")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["client_suggestions"] = (
            Project.objects.exclude(client="")
            .order_by("client")
            .values_list("client", flat=True)
            .distinct()
        )
        return context

    def form_valid(self, form):
        messages.success(self.request, "Projekt wurde aktualisiert.")
        return super().form_valid(form)


class ProjectDeleteView(DeleteView):
    model = Project
    template_name = "parts/project_confirm_delete.html"
    success_url = reverse_lazy("parts:projects")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = self.object
        context["tagged_lines_count"] = project.purchase_lines.count()
        context["tagged_documents_count"] = project.purchase_lines.values("document_id").distinct().count()
        return context

    def form_valid(self, form):
        project = self.object
        PurchaseLine.objects.filter(project=project).update(project=None)
        messages.success(self.request, "Projekt wurde gelöscht. Zugeordnete Zeilen sind jetzt ohne Projekt.")
        return super().form_valid(form)


def project_toggle_active(request, pk):
    project = get_object_or_404(Project, pk=pk)
    project.active = not project.active
    project.save(update_fields=["active", "updated_at"])
    state = "aktiviert" if project.active else "deaktiviert"
    messages.success(request, f"Projekt wurde {state}.")
    return redirect("parts:projects")


class PurchaseDocumentListView(ListView):
    model = PurchaseDocument
    template_name = "parts/purchase_document_list.html"
    context_object_name = "documents"
    allowed_sort_keys = {
        "date": "Datum",
        "number": "Belegnummer",
    }

    def get_queryset(self):
        queryset = PurchaseDocument.objects.select_related("supplier").prefetch_related("lines", "lines__project")
        query = self.request.GET.get("q", "").strip()
        supplier = self.request.GET.get("supplier", "").strip()
        document_type = self.request.GET.get("document_type", "").strip()
        year = self.request.GET.get("year", "").strip()
        project = self.request.GET.get("project", "").strip()
        paper_filed = self.request.GET.get("paper_filed", "").strip()
        has_pdf = self.request.GET.get("has_pdf", "").strip()

        if query:
            queryset = queryset.filter(
                Q(document_number__icontains=query)
                | Q(paper_invoice_no__icontains=query)
                | Q(related_order_number__icontains=query)
                | Q(supplier__name__icontains=query)
                | Q(filing_location__icontains=query)
                | Q(lines__description__icontains=query)
            )
        if supplier:
            queryset = queryset.filter(supplier_id=supplier)
        if document_type:
            queryset = queryset.filter(document_type=document_type)
        if year:
            queryset = queryset.filter(document_date__year=year)
        if project:
            if project == "__none__":
                queryset = queryset.filter(lines__project__isnull=True)
            else:
                queryset = queryset.filter(lines__project_id=project)
        if paper_filed in {"yes", "no"}:
            queryset = queryset.filter(paper_filed=(paper_filed == "yes"))
        if has_pdf in {"yes", "no"}:
            queryset = queryset.filter(pdf_file__isnull=(has_pdf == "no"))
        sort_key, direction = _parse_sort_params(self.request, self.allowed_sort_keys)
        if sort_key == "date":
            field = F("document_date")
            order = [field.asc(nulls_last=True) if direction == "asc" else field.desc(nulls_last=True)]
        elif sort_key == "number":
            # Belegnummer is free text: order by length first so "9" sorts before "10".
            queryset = queryset.annotate(document_number_len=Length("document_number"))
            order = [
                getattr(F("document_number_len"), direction)(),
                getattr(F("document_number"), direction)(),
            ]
        else:
            return queryset.distinct().order_by("-document_date", "-created_at")
        return queryset.distinct().order_by(*order, getattr(F("pk"), direction)())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_q"] = self.request.GET.get("q", "").strip()
        context["current_supplier"] = self.request.GET.get("supplier", "").strip()
        context["current_document_type"] = self.request.GET.get("document_type", "").strip()
        context["current_year"] = self.request.GET.get("year", "").strip()
        context["current_project"] = self.request.GET.get("project", "").strip()
        context["current_paper_filed"] = self.request.GET.get("paper_filed", "").strip()
        context["current_has_pdf"] = self.request.GET.get("has_pdf", "").strip()
        current_sort, current_dir = _parse_sort_params(self.request, self.allowed_sort_keys)
        columns = _build_sort_headers(self.request, self.allowed_sort_keys, current_sort, current_dir)
        context["sort_headers"] = {column["key"]: column for column in columns}
        context["current_sort"] = current_sort
        context["current_dir"] = current_dir
        context["suppliers"] = Supplier.objects.order_by("name")
        context["projects"] = Project.objects.filter(active=True).order_by("name")
        context["document_types"] = PurchaseDocument.DocumentType.choices
        context["years"] = (
            PurchaseDocument.objects.exclude(document_date__isnull=True)
            .dates("document_date", "year", order="DESC")
        )
        return context


class PurchaseDocumentDetailView(DetailView):
    model = PurchaseDocument
    template_name = "parts/purchase_document_detail.html"
    context_object_name = "document"

    def get_queryset(self):
        return PurchaseDocument.objects.select_related("supplier").prefetch_related("lines", "lines__part", "lines__project")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        document = self.object
        context["line_rows"] = [
            {
                "line": line,
                "choices": _project_choices_with_current(line.project),
            }
            for line in document.lines.all()
        ]
        context["bulk_projects"] = Project.objects.filter(active=True).order_by("name")
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        bulk_project = request.POST.get("bulk_project", "").strip()
        apply_bulk_marked = request.POST.get("apply_bulk_marked") == "yes"
        apply_bulk_untagged = request.POST.get("apply_bulk_untagged") == "yes"

        with transaction.atomic():
            for line in self.object.lines.all():
                field = f"line_project::{line.pk}"
                project = _resolve_project_selection(request.POST.get(field), fallback=line.project)
                line.project = project
                line.save(update_fields=["project"])

            if bulk_project:
                bulk_target = _resolve_project_selection(bulk_project, fallback=None)
                if apply_bulk_marked:
                    marked_ids = [
                        int(key.split("::", 1)[1])
                        for key in request.POST.keys()
                        if key.startswith("mark_line::")
                    ]
                    if marked_ids:
                        PurchaseLine.objects.filter(document=self.object, pk__in=marked_ids).update(project=bulk_target)
                if apply_bulk_untagged:
                    PurchaseLine.objects.filter(document=self.object, project__isnull=True).update(project=bulk_target)

        messages.success(request, "Projektzuordnungen wurden gespeichert.")
        return redirect("parts:document_detail", pk=self.object.pk)


class PartDetailView(DetailView):
    model = Part
    template_name = "parts/part_detail.html"
    context_object_name = "part"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["purchase_history"] = self.object.purchase_history
        return context


def purchase_document_download(request, pk):
    document = get_object_or_404(PurchaseDocument, pk=pk)
    if not document.pdf_file:
        raise Http404("Zu diesem Beleg ist keine PDF-Datei gespeichert.")
    filename = document.pdf_file.name.rsplit("/", 1)[-1]
    return FileResponse(document.pdf_file.open("rb"), as_attachment=True, filename=filename, content_type="application/pdf")


def project_documents(request):
    project_id = request.GET.get("project", "").strip()
    client = request.GET.get("client", "").strip()
    paper_unfiled = request.GET.get("paper_unfiled", "").strip() == "yes"
    selected_project = None
    selected_client = client
    documents = []
    line_items = []
    net_total_sum = Decimal("0.0000")
    gross_total_sum = Decimal("0.0000")

    lines = PurchaseLine.objects.none()
    if project_id:
        selected_project = get_object_or_404(Project, pk=project_id)
        lines = PurchaseLine.objects.filter(project=selected_project)
    elif client:
        lines = PurchaseLine.objects.filter(project__client__iexact=client)

    if lines.exists():
        if paper_unfiled:
            lines = lines.filter(document__paper_filed=False)
        lines = lines.select_related("document", "document__supplier", "project").order_by(
            "-document__document_date", "document__document_number", "position"
        )

        by_document = {}
        for line in lines:
            entry = by_document.setdefault(
                line.document_id,
                {
                    "document": line.document,
                    "project_subtotal": Decimal("0.0000"),
                    "line_count": 0,
                    "untagged_lines": 0,
                    "mixed_projects_hint": False,
                    "has_gross_lines": False,
                },
            )
            entry["line_count"] += 1
            if line.line_total is not None:
                entry["project_subtotal"] += line.line_total
            if not line.price_is_net:
                entry["has_gross_lines"] = True
            line_items.append(line)

        documents_scope = PurchaseDocument.objects.filter(pk__in=by_document.keys()).prefetch_related("lines")
        for document in documents_scope:
            untagged_count = document.lines.filter(project__isnull=True).count()
            other_project_lines = document.lines.exclude(project__isnull=True)
            if selected_project:
                other_project_lines = other_project_lines.exclude(project=selected_project)
            if client:
                other_project_lines = other_project_lines.exclude(project__client__iexact=client)
            by_document[document.pk]["untagged_lines"] = untagged_count
            by_document[document.pk]["mixed_projects_hint"] = other_project_lines.exists() or untagged_count > 0

        documents = sorted(
            by_document.values(),
            key=lambda entry: (
                entry["document"].document_date or datetime.min.date(),
                entry["document"].document_number or "",
            ),
            reverse=True,
        )

        for entry in documents:
            subtotal = entry["project_subtotal"]
            if entry["has_gross_lines"]:
                gross_total_sum += subtotal
            else:
                net_total_sum += subtotal

    return render(
        request,
        "parts/project_documents.html",
        {
            "projects": Project.objects.order_by("name"),
            "clients": (
                Project.objects.exclude(client="")
                .order_by("client")
                .values_list("client", flat=True)
                .distinct()
            ),
            "selected_project": selected_project,
            "selected_client": selected_client,
            "documents": documents,
            "line_items": line_items,
            "current_project": project_id,
            "current_client": client,
            "paper_unfiled": paper_unfiled,
            "net_total_sum": net_total_sum,
            "gross_total_sum": gross_total_sum,
        },
    )


def invoice_upload(request):
    if request.method == "POST":
        form = InvoiceUploadForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded_files = request.FILES.getlist("invoice_files")
            supplier_override = (form.cleaned_data.get("supplier") or "").strip()
            if not uploaded_files:
                form.add_error("invoice_files", "Bitte mindestens eine PDF-Datei auswählen.")
            elif len(uploaded_files) > MAX_BATCH_FILES:
                form.add_error("invoice_files", f"Zu viele Dateien. Maximal erlaubt: {MAX_BATCH_FILES}.")
            else:
                parsed_documents = []
                failed_files = []
                for uploaded_file in uploaded_files:
                    if uploaded_file.size > MAX_FILE_SIZE_BYTES:
                        failed_files.append({"file_name": uploaded_file.name, "error": "Datei überschreitet das 10-MB-Limit."})
                        continue
                    temp_pdf_path = ""
                    try:
                        temp_pdf_path = _save_temp_pdf(uploaded_file)
                        uploaded_file.seek(0)
                        invoice_data = _parse_uploaded_document(uploaded_file, supplier_override=supplier_override)
                        invoice_data["pdf_temp_path"] = temp_pdf_path
                        invoice_data["pdf_original_name"] = uploaded_file.name
                        parsed_documents.append({"file_name": uploaded_file.name, "invoice_data": invoice_data})
                    except ValueError as exc:
                        if temp_pdf_path:
                            temp_path = Path(temp_pdf_path)
                            if temp_path.exists():
                                temp_path.unlink()
                        failed_files.append({"file_name": uploaded_file.name, "error": str(exc)})

                if parsed_documents:
                    try:
                        _validate_single_supplier(parsed_documents)
                    except ValueError as exc:
                        form.add_error(None, str(exc))
                        return render(request, "parts/invoice_upload.html", {"form": form})

                documents, summary, skipped_duplicates, new_type_names = _build_batch_preview(parsed_documents)
                if not documents:
                    form.add_error(None, "Aus diesem Import konnten keine Belege erfolgreich gelesen werden.")
                    return render(request, "parts/invoice_upload.html", {"form": form})

                request.session["invoice_preview_batch"] = {
                    "created_at": timezone.now().isoformat(),
                    "quantity_mode": QUANTITY_MODE_ADD,
                    "project_id": "",
                    "default_location": request.session.get("last_import_location", {}),
                    "supplier": documents[0]["supplier"] if documents else "",
                    "documents": documents,
                    "failed_files": failed_files,
                    "skipped_duplicates": skipped_duplicates,
                    "new_type_names": new_type_names,
                    "summary": summary,
                }
                return HttpResponseRedirect(reverse("parts:invoice_preview"))
    else:
        form = InvoiceUploadForm()
    return render(request, "parts/invoice_upload.html", {"form": form})


def _existing_classification_values() -> dict[str, list[str]]:
    return {
        "part_type": list(Part.objects.exclude(part_type="").values_list("part_type", flat=True).distinct()),
        "part_subtype": list(Part.objects.exclude(part_subtype="").values_list("part_subtype", flat=True).distinct()),
        "packaging": list(Part.objects.exclude(packaging="").values_list("packaging", flat=True).distinct()),
    }


def _reuse_spelling(value: str, existing_values) -> str:
    """Use the existing spelling when a value differs only by case, spacing or umlaut form."""
    cleaned = " ".join(normalise_unicode_text(value).split())
    key = canonicalise_match_value(cleaned)
    for existing in existing_values:
        if canonicalise_match_value(existing) == key:
            return existing
    return cleaned


def _correct_part_classification(part, part_type, value, packaging, existing_types):
    """Corrections typed in the preview for a matched part; a blank entry never clears a value."""
    changed = []
    for field, new_value, known in (
        ("part_type", _reuse_spelling(part_type, existing_types["part_type"]), existing_types["part_type"]),
        ("value", " ".join(normalise_unicode_text(value).split()), ()),
        ("packaging", _reuse_spelling(packaging, existing_types["packaging"]), existing_types["packaging"]),
    ):
        if new_value and new_value != getattr(part, field):
            setattr(part, field, new_value)
            changed.append(field)
    if changed:
        part.save(update_fields=changed + ["updated_at"])


def _apply_line_to_part(part, quantity, unit_price, supplier, supplier_part_no, description, mf_part_no, currency, document_date, document_no, quantity_mode):
    should_add_quantity = quantity_mode == QUANTITY_MODE_ADD
    if should_add_quantity:
        part.quantity = int(part.quantity) + quantity

    is_newer_or_equal = part.last_purchased is None or document_date is None or document_date >= part.last_purchased
    if is_newer_or_equal:
        part.supplier = supplier or part.supplier
        part.supplier_part_no = supplier_part_no or part.supplier_part_no
        part.last_price = unit_price
        part.currency = currency or part.currency or "EUR"
        part.last_purchased = document_date or part.last_purchased
        part.last_document_no = document_no or part.last_document_no

    if not part.mf_part_no and mf_part_no:
        part.mf_part_no = mf_part_no
    if description and not part.description:
        part.description = description
    part.save()


def _create_new_part(
    quantity,
    unit_price,
    supplier,
    supplier_part_no,
    description,
    mf_part_no,
    currency,
    document_date,
    document_no,
    quantity_mode,
    part_type,
    value,
    packaging,
    part_subtype="",
    manufacturer="",
    location_type="",
    location_name="",
    location_position="",
):
    initial_quantity = quantity if quantity_mode == QUANTITY_MODE_ADD else 0
    return Part.objects.create(
        part_type=part_type,
        part_subtype=part_subtype,
        value=value,
        packaging=packaging,
        description=description,
        manufacturer=manufacturer,
        mf_part_no=mf_part_no,
        supplier=supplier,
        supplier_part_no=supplier_part_no,
        last_price=unit_price,
        currency=currency or "EUR",
        last_purchased=document_date,
        last_document_no=document_no,
        quantity=initial_quantity,
        location_type=location_type,
        location_name=location_name,
        position=location_position,
        comment="Per Belegimport angelegt",
    )


def _create_purchase_document(doc, supplier):
    purchase_document = PurchaseDocument.objects.create(
        supplier=supplier,
        document_type=_storage_document_type(doc.get("document_type")),
        document_number=doc.get("invoice_no", ""),
        document_date=datetime.strptime(doc["invoice_date"], "%Y-%m-%d").date() if doc.get("invoice_date") else None,
        related_order_number=doc.get("related_order_number", ""),
        currency="EUR",
        shipping_cost=Decimal("0.0000"),
        document_total=Decimal(doc.get("document_total") or "0.00"),
        discount_total=Decimal(doc.get("discount_total") or "0.00"),
        filing_location="",
        paper_invoice_no="",
        # Hinweise des Belegs (Teillieferung, separat geliefert, ...); Prüfwarnungen bleiben in der Vorschau.
        notes="\n".join(doc["notes"]) if doc.get("notes") else doc.get("warning", ""),
    )
    temp_path = doc.get("pdf_temp_path", "")
    if temp_path:
        source = Path(temp_path)
        if source.exists():
            with source.open("rb") as handle:
                purchase_document.pdf_file.save(doc.get("pdf_original_name", source.name), handle, save=True)
    return purchase_document


@dataclass
class _Location:
    type: str = ""
    name: str = ""
    position: str = ""

    @property
    def given(self) -> bool:
        return bool(self.type or self.name)


def _clean_text(value) -> str:
    return " ".join(normalise_unicode_text(value).split())


def _parse_location_type(raw) -> str:
    cleaned = _clean_text(raw)
    if not cleaned:
        return ""
    key = canonicalise_match_value(cleaned)
    for value, label in Part.LocationType.choices:
        if key in {canonicalise_match_value(value), canonicalise_match_value(label)}:
            return value
    valid = ", ".join(f"{value} ({label})" for value, label in Part.LocationType.choices)
    raise ValueError(f"Unbekannter Lagerort-Typ '{cleaned}'. Gültig: {valid}.")


def _read_location(post, type_key, name_key, position_key) -> tuple[str, str, str]:
    return post.get(type_key, ""), post.get(name_key, ""), post.get(position_key, "")


def _normalise_location(raw, known_names, notes) -> _Location:
    location_type = _parse_location_type(raw[0])
    name = _clean_text(raw[1])
    position = _clean_text(raw[2])
    if len(name) > 200 or len(position) > 200:
        raise ValueError("Lagerort und Position dürfen höchstens 200 Zeichen lang sein.")
    if name:
        spelled = _reuse_spelling(name, known_names)
        if spelled != name:
            notes.append(f"Lagerort '{name}' wurde als vorhandene Schreibweise '{spelled}' übernommen.")
        name = spelled
    return _Location(location_type, name, position)


def _same_text(left: str, right: str) -> bool:
    return canonicalise_match_value(_clean_text(left)) == canonicalise_match_value(_clean_text(right))


def _location_relation(part, location: _Location) -> str:
    """'fill' (part has no location), 'same' or 'differs'."""
    if not (part.location_type or part.location_name):
        return "fill"
    same = (
        (not location.type or location.type == part.location_type)
        and (not location.name or _same_text(location.name, part.location_name))
        and (not location.position or _same_text(location.position, part.position))
    )
    return "same" if same else "differs"


def _set_part_location(part, location: _Location, known_names) -> None:
    changed = []
    for field, value in (("location_type", location.type), ("location_name", location.name), ("position", location.position)):
        if value and getattr(part, field) != value:
            setattr(part, field, value)
            changed.append(field)
    if changed:
        part.save(update_fields=changed + ["updated_at"])
    if location.name and not any(_same_text(location.name, known) for known in known_names):
        known_names.append(location.name)


def _describe_location(part) -> str:
    return part.full_location if (part.location_type or part.location_name) else ""


class _ConfirmFailed(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def _overlay_post(payload, post):
    """Carry the values typed in the preview over, so a rejected confirm does not lose them."""
    mode = post.get("quantity_mode", payload.get("quantity_mode", QUANTITY_MODE_ADD))
    payload["quantity_mode"] = mode if mode in {QUANTITY_MODE_ADD, QUANTITY_MODE_KEEP} else QUANTITY_MODE_ADD
    payload["project_id"] = post.get("batch_project_id", payload.get("project_id", "")).strip()
    payload["default_location"] = dict(
        zip(("type", "name", "position"), _read_location(post, "batch_location_type", "batch_location_name", "batch_location_position"))
    )
    for doc in payload.get("documents", []):
        doc_id = doc["document_id"]
        doc["default_location"] = dict(
            zip(
                ("type", "name", "position"),
                _read_location(post, f"doc_location_type::{doc_id}", f"doc_location_name::{doc_id}", f"doc_location_position::{doc_id}"),
            )
        )
        for line in doc.get("lines", []):
            prefix = f"{doc_id}::{line['index']}"
            line["edit_quantity"] = post.get(f"quantity::{prefix}", str(line["quantity"]))
            line["edit_description"] = post.get(f"description::{prefix}", line.get("edit_description", line["description"]))
            for field in ("part_type", "part_subtype", "value", "packaging", "manufacturer"):
                line[field] = post.get(f"{field}::{prefix}", line.get(field, ""))
            line["edit_mf_part_no"] = post.get(f"mf_part_no::{prefix}", line.get("edit_mf_part_no", ""))
            line["project_id"] = post.get(f"project::{prefix}", "")
            line["location_type"] = post.get(f"location_type::{prefix}", "")
            line["location_name"] = post.get(f"location_name::{prefix}", "")
            line["location_position"] = post.get(f"location_position::{prefix}", "")
            line["change_location"] = post.get(f"change_location::{prefix}") == "on"
            line["skip"] = post.get(f"skip::{prefix}") == "on"
            line["selected_match"] = post.get(f"match::{prefix}", "")
    return payload


def _preview_context(payload):
    existing = _existing_classification_values()
    return {
        "location_type_choices": Part.LocationType.choices,
        "known_location_names": sorted(
            Part.objects.exclude(location_name="").values_list("location_name", flat=True).distinct(), key=str.casefold
        ),
        "preview": payload,
        "quantity_mode_choices": [
            (QUANTITY_MODE_ADD, "Mengen zum Bestand addieren"),
            (QUANTITY_MODE_KEEP, "Bestand nicht verändern"),
        ],
        "active_projects": Project.objects.filter(active=True).order_by("name"),
        "project_new_link": f"{reverse('parts:project_add')}?next={reverse('parts:invoice_preview')}",
        "known_part_types": sorted(existing["part_type"], key=str.casefold),
        "known_packagings": sorted(existing["packaging"], key=str.casefold),
        "known_part_subtypes": sorted(existing["part_subtype"], key=str.casefold),
    }


def invoice_preview(request):
    payload = _get_valid_preview_payload(request)
    if not payload:
        return redirect("parts:import")

    if request.method == "POST":
        quantity_mode = request.POST.get("quantity_mode", QUANTITY_MODE_ADD)
        batch_project_id = request.POST.get("batch_project_id", payload.get("project_id", "")).strip()
        if quantity_mode not in {QUANTITY_MODE_ADD, QUANTITY_MODE_KEEP}:
            quantity_mode = QUANTITY_MODE_ADD

        failed_files = payload.get("failed_files", [])
        if failed_files and request.POST.get("continue_without_failed") != "yes":
            messages.error(request, "Bitte bestätigen Sie, dass fehlerhafte Dateien ausgeschlossen werden sollen.")
            payload["quantity_mode"] = quantity_mode
            payload["project_id"] = batch_project_id
            request.session["invoice_preview_batch"] = payload
            return render(request, "parts/invoice_preview.html", _preview_context(payload))

        existing_types = _existing_classification_values()
        include_known_duplicates = request.POST.get("include_known_duplicates") == "yes"
        documents = payload.get("documents", [])
        resolved_parts_by_key = {}
        errors: list[str] = []
        notes: list[str] = []
        known_location_names = list(
            Part.objects.exclude(location_name="").values_list("location_name", flat=True).distinct()
        )
        batch_raw = _read_location(request.POST, "batch_location_type", "batch_location_name", "batch_location_position")
        created_documents = []
        temp_files = []
        last_location = None

        try:
            with transaction.atomic():
                last_location = _confirm_documents(
                    request, documents, payload, quantity_mode, batch_project_id, include_known_duplicates,
                    existing_types, resolved_parts_by_key, known_location_names, batch_raw,
                    errors, notes, created_documents, temp_files,
                )
                if errors:
                    raise _ConfirmFailed(errors)
        except _ConfirmFailed as failure:
            _discard_created_files(created_documents)
            for error in failure.errors:
                messages.error(request, error)
            request.session["invoice_preview_batch"] = _overlay_post(payload, request.POST)
            return render(request, "parts/invoice_preview.html", _preview_context(request.session["invoice_preview_batch"]))
        except Exception:
            _discard_created_files(created_documents)
            raise

        for source in temp_files:
            if source.exists():
                source.unlink()
        request.session.pop("invoice_preview_batch", None)
        chosen = last_location or _normalise_location(batch_raw, known_location_names, [])
        if chosen.given:
            request.session["last_import_location"] = {"type": chosen.type, "name": chosen.name, "position": chosen.position}
        for note in notes:
            messages.info(request, note)
        messages.success(request, "Import abgeschlossen.")
        return redirect("parts:list")

    return render(request, "parts/invoice_preview.html", _preview_context(payload))


def _discard_created_files(created_documents):
    for document in created_documents:
        if document.pdf_file:
            document.pdf_file.storage.delete(document.pdf_file.name)


def _confirm_documents(
    request, documents, payload, quantity_mode, batch_project_id, include_known_duplicates, existing_types,
    resolved_parts_by_key, known_location_names, batch_raw, errors, notes, created_documents, temp_files,
):
    """Write parts, documents and lines. Runs inside one transaction; problems are collected in ``errors``."""
    last_location = None
    try:
        batch_location = _normalise_location(batch_raw, known_location_names, [])
    except ValueError as exc:
        errors.append(f"Lagerort für alle Zeilen: {exc}")
        batch_location = _Location()

    for doc in documents:
        supplier = doc.get("supplier", "")
        document_no = doc.get("invoice_no", "")
        document_date = None
        if doc.get("invoice_date"):
            try:
                document_date = datetime.strptime(doc["invoice_date"], "%Y-%m-%d").date()
            except ValueError:
                document_date = None
        already_imported = ImportedDocument.objects.filter(
            supplier__iexact=supplier,
            document_no__iexact=document_no,
        ).exists()

        supplier_obj, _supplier_created = Supplier.objects.get_or_create(name=supplier)
        existing_purchase_document = PurchaseDocument.objects.filter(
            supplier=supplier_obj,
            document_type=_storage_document_type(doc.get("document_type")),
            document_number=document_no,
        ).first()

        temp_path = doc.get("pdf_temp_path", "")
        if temp_path:
            temp_files.append(Path(temp_path))

        if already_imported and not include_known_duplicates and existing_purchase_document is not None:
            continue

        purchase_document = existing_purchase_document
        if purchase_document is None:
            purchase_document = _create_purchase_document(doc, supplier_obj)
            created_documents.append(purchase_document)
        write_purchase_lines = purchase_document.lines.count() == 0

        doc_id = doc["document_id"]
        doc_label = doc.get("file_name") or document_no
        try:
            doc_location = _normalise_location(
                _read_location(request.POST, f"doc_location_type::{doc_id}", f"doc_location_name::{doc_id}", f"doc_location_position::{doc_id}"),
                known_location_names,
                [],
            )
        except ValueError as exc:
            errors.append(f"{doc_label}: Lagerort für alle Zeilen ohne Lagerort: {exc}")
            doc_location = _Location()

        line_count = 0
        for line in doc.get("lines", []):
            index = line["index"]
            field_prefix = f"{doc_id}::{index}"
            if request.POST.get(f"skip::{field_prefix}") == "on":
                continue
            line_label = f"{doc_label}, Zeile {index + 1}"

            selected_match = request.POST.get(f"match::{field_prefix}")
            quantity = int(request.POST.get(f"quantity::{field_prefix}", line["quantity"]) or 0)
            unit_price = Decimal(line["unit_price"] or "0")
            line_supplier = (line.get("supplier") or supplier).strip()
            supplier_part_no = line["supplier_part_no"].strip()
            description = request.POST.get(f"description::{field_prefix}", line["description"]).strip()
            mf_part_no = (line["mf_part_no"] or "").strip()
            currency = (line["currency"] or "").strip() or "EUR"
            part_type = request.POST.get(f"part_type::{field_prefix}", line.get("part_type", "")).strip()
            value = request.POST.get(f"value::{field_prefix}", line.get("value", "")).strip()
            packaging = request.POST.get(f"packaging::{field_prefix}", line.get("packaging", "")).strip()
            # Untertyp, Hersteller and Hersteller-Nr. only apply to newly created parts.
            part_subtype = request.POST.get(f"part_subtype::{field_prefix}", line.get("part_subtype", ""))
            manufacturer = request.POST.get(f"manufacturer::{field_prefix}", line.get("manufacturer", ""))
            edited_mf_part_no = request.POST.get(f"mf_part_no::{field_prefix}", line.get("edit_mf_part_no", mf_part_no))
            line_project = _resolve_project_selection(
                request.POST.get(f"project::{field_prefix}", ""),
                fallback=_resolve_project_selection(batch_project_id, fallback=None),
            )

            own_raw = _read_location(
                request.POST, f"location_type::{field_prefix}", f"location_name::{field_prefix}", f"location_position::{field_prefix}"
            )
            from_default = False
            try:
                location = _normalise_location(own_raw, known_location_names, notes)
            except ValueError as exc:
                errors.append(f"{line_label}: {exc}")
                location = _Location()
            if not location.given:
                location = doc_location if doc_location.given else batch_location
                from_default = location.given
            change_location = request.POST.get(f"change_location::{field_prefix}") == "on"

            key = (line_supplier.casefold(), supplier_part_no.casefold())
            if selected_match:
                part = Part.objects.get(pk=selected_match)
            elif supplier_part_no and key in resolved_parts_by_key:
                part = resolved_parts_by_key[key]
            else:
                part = Part.objects.filter(
                    supplier__iexact=line_supplier,
                    supplier_part_no__iexact=supplier_part_no,
                ).first()

            if part is not None:
                _apply_line_to_part(
                    part=part,
                    quantity=quantity,
                    unit_price=unit_price,
                    supplier=line_supplier,
                    supplier_part_no=supplier_part_no,
                    description=description,
                    mf_part_no=mf_part_no,
                    currency=currency,
                    document_date=document_date,
                    document_no=document_no,
                    quantity_mode=quantity_mode,
                )
                _correct_part_classification(part, part_type, value, packaging, existing_types)
                if location.given:
                    relation = _location_relation(part, location)
                    if relation == "fill" or (relation == "differs" and not from_default and change_location):
                        _set_part_location(part, location, known_location_names)
                        last_location = location
                    elif relation == "differs" and not from_default:
                        errors.append(
                            f"{line_label}: Bauteil liegt bereits in '{_describe_location(part)}'. "
                            "Zum Ändern bitte 'Lagerort ändern' ankreuzen oder den Lagerort leeren."
                        )
            else:
                mf_part_no = " ".join(normalise_unicode_text(edited_mf_part_no).split())
                part = _create_new_part(
                    quantity=quantity,
                    unit_price=unit_price,
                    supplier=line_supplier,
                    supplier_part_no=supplier_part_no,
                    description=description,
                    mf_part_no=mf_part_no,
                    currency=currency,
                    document_date=document_date,
                    document_no=document_no,
                    quantity_mode=quantity_mode,
                    part_type=_reuse_spelling(part_type, existing_types["part_type"]),
                    value=value,
                    packaging=_reuse_spelling(packaging, existing_types["packaging"]),
                    part_subtype=_reuse_spelling(part_subtype, existing_types["part_subtype"]),
                    manufacturer=" ".join(normalise_unicode_text(manufacturer).split()),
                    location_type=location.type,
                    location_name=location.name,
                    location_position=location.position,
                )
                if location.given:
                    last_location = location
                    if location.name and not any(_same_text(location.name, known) for known in known_location_names):
                        known_location_names.append(location.name)
            if supplier_part_no:
                resolved_parts_by_key[key] = part

            if write_purchase_lines:
                price_is_gross = doc.get("price_basis") == "gross" and bool(line.get("gross_unit_price"))
                if price_is_gross:
                    # Gespeichert wird der Bruttopreis des Belegs; Part.last_price bleibt netto.
                    stored_unit_price = Decimal(line["gross_unit_price"])
                    printed_total = Decimal(line["line_total_gross"]) if line.get("line_total_gross") else None
                    if printed_total is not None and quantity == line["quantity"]:
                        line_total = printed_total
                    else:
                        line_total = (Decimal(quantity) * stored_unit_price).quantize(Decimal("0.0001"))
                else:
                    stored_unit_price = unit_price
                    line_total = (Decimal(quantity) * unit_price).quantize(Decimal("0.0001"))
                PurchaseLine.objects.create(
                    document=purchase_document,
                    position=str(index + 1),
                    supplier_part_no=supplier_part_no,
                    description=line["description"],
                    manufacturer_part_no=mf_part_no,
                    quantity=Decimal(quantity),
                    unit_price=stored_unit_price,
                    line_total=line_total,
                    currency=currency,
                    price_is_net=not price_is_gross,
                    price_is_derived=price_is_gross or (doc.get("document_type") == "order_confirmation"),
                    part=part,
                    project=line_project,
                    notes="",
                )
            line_count += 1

        if not already_imported:
            ImportedDocument.objects.create(
                supplier=supplier,
                document_no=document_no,
                document_date=document_date,
                document_type=doc.get("document_type", ""),
                original_filename=doc.get("file_name", ""),
                line_count=line_count,
            )
    return last_location




def parts_complete_preview(request):
    candidates = []
    existing_types = _get_existing_part_types()
    queryset = Part.objects.filter(Q(part_type="") | Q(value="") | Q(packaging="")).order_by("id")
    for part in queryset:
        suggestions = suggest_part_fields(part.description or "", existing_types)
        suggested_type = suggestions["part_type"] if not part.part_type else ""
        suggested_value = suggestions["value"] if not part.value else ""
        suggested_packaging = suggestions["packaging"] if not part.packaging else ""
        if not any([suggested_type, suggested_value, suggested_packaging]):
            continue
        candidates.append(
            {
                "part": part,
                "part_type_old": part.part_type,
                "value_old": part.value,
                "packaging_old": part.packaging,
                "part_type_new": suggested_type,
                "value_new": suggested_value,
                "packaging_new": suggested_packaging,
            }
        )

    if request.method == "POST":
        selected_ids = [int(key.split("_", 1)[1]) for key in request.POST.keys() if key.startswith("apply_")]
        if not selected_ids:
            messages.error(request, "Bitte mindestens ein Bauteil auswählen.")
            return render(request, "parts/parts_complete_preview.html", {"candidates": candidates})
        with transaction.atomic():
            parts_to_update = Part.objects.select_for_update().filter(id__in=selected_ids)
            parts_by_id = {part.id: part for part in parts_to_update}
            for candidate in candidates:
                part = parts_by_id.get(candidate["part"].id)
                if part is None:
                    continue
                new_type = request.POST.get(f"part_type_{part.id}", "").strip()
                new_value = request.POST.get(f"value_{part.id}", "").strip()
                new_packaging = request.POST.get(f"packaging_{part.id}", "").strip()
                if not part.part_type and new_type:
                    part.part_type = new_type
                if not part.value and new_value:
                    part.value = new_value
                if not part.packaging and new_packaging:
                    part.packaging = new_packaging
                part.save()
        messages.success(request, "Ausgewählte Bauteile wurden vervollständigt.")
        return redirect("parts:list")

    return render(request, "parts/parts_complete_preview.html", {"candidates": candidates})
