from __future__ import annotations

import io
import re
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from parsers import detect_parser_from_text, get_parser_for_supplier
from parsers.describe import suggest_part_fields
from parsers.formats import canonicalise_match_value, normalise_unicode_text
from .forms import InvoiceUploadForm, PartForm
from .models import ImportedDocument, Part

MAX_BATCH_FILES = 20
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
PREVIEW_TTL = timedelta(hours=24)
QUANTITY_MODE_ADD = "add_stock"
QUANTITY_MODE_KEEP = "no_stock_change"


def _canonicalise_search(value):
    return canonicalise_match_value(value)


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
        lines.append(
            {
                "index": index,
                "description": line_data.get("description", ""),
                "supplier": line_data.get("supplier", invoice_data.get("supplier", "")),
                "supplier_part_no": line_data.get("supplier_part_no", ""),
                "mf_part_no": line_data.get("mf_part_no", ""),
                "quantity": int(line_data.get("quantity", 0) or 0),
                "unit_price": str(line_data.get("unit_price", "0.00")),
                "currency": line_data.get("currency", "EUR"),
                "status": status,
                "status_label": status_label,
                "match_type": match_type,
                "part": {"pk": part.pk, "label": str(part)} if part else None,
                "candidates": candidates,
                "whg": line_data.get("whg", ""),
                "part_type": editable_type,
                "value": editable_value,
                "packaging": editable_packaging,
                "part_type_suggested": str(suggestions["part_type"] or ""),
                "value_suggested": str(suggestions["value"] or ""),
                "packaging_suggested": str(suggestions["packaging"] or ""),
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
        "warning": invoice_data.get("warning", ""),
        "warnings": invoice_data.get("warnings", []),
        "reference_note": invoice_data.get("reference_note", ""),
        "whg_note": invoice_data.get("whg_note", ""),
        "duplicate_invoice": bool(duplicate_existing),
        "duplicate_imported_at": duplicate_at,
        "document_type": invoice_data.get("document_type", ""),
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


class PartListView(ListView):
    model = Part
    template_name = "parts/part_list.html"
    context_object_name = "parts"

    def get_queryset(self):
        queryset = super().get_queryset()
        query = self.request.GET.get("q", "").strip()
        part_type = self.request.GET.get("part_type", "").strip()
        packaging = self.request.GET.get("packaging", "").strip()

        if query:
            query_fold = _canonicalise_search(query)
            filtered = []
            for part in queryset:
                haystack = " ".join(
                    (
                        part.part_type or "",
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
        if packaging:
            queryset = queryset.filter(packaging__iexact=packaging)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_q"] = self.request.GET.get("q", "").strip()
        context["current_part_type"] = self.request.GET.get("part_type", "").strip()
        context["current_packaging"] = self.request.GET.get("packaging", "").strip()
        context["part_types"] = (
            Part.objects.exclude(part_type="").order_by("part_type").values_list("part_type", flat=True).distinct()
        )
        context["packaging_options"] = (
            Part.objects.exclude(packaging="").order_by("packaging").values_list("packaging", flat=True).distinct()
        )
        return context


class PartCreateView(CreateView):
    model = Part
    form_class = PartForm
    template_name = "parts/part_form.html"
    success_url = reverse_lazy("parts:list")


class PartUpdateView(UpdateView):
    model = Part
    form_class = PartForm
    template_name = "parts/part_form.html"
    success_url = reverse_lazy("parts:list")


class PartDeleteView(DeleteView):
    model = Part
    template_name = "parts/part_confirm_delete.html"
    success_url = reverse_lazy("parts:list")


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
                    try:
                        invoice_data = _parse_uploaded_document(uploaded_file, supplier_override=supplier_override)
                        parsed_documents.append({"file_name": uploaded_file.name, "invoice_data": invoice_data})
                    except ValueError as exc:
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
):
    initial_quantity = quantity if quantity_mode == QUANTITY_MODE_ADD else 0
    return Part.objects.create(
        part_type=part_type,
        value=value,
        packaging=packaging,
        description=description,
        manufacturer="",
        mf_part_no=mf_part_no,
        supplier=supplier,
        supplier_part_no=supplier_part_no,
        last_price=unit_price,
        currency=currency or "EUR",
        last_purchased=document_date,
        last_document_no=document_no,
        quantity=initial_quantity,
        location_type="",
        location_name="",
        position="",
        comment="Per Belegimport angelegt",
    )


def invoice_preview(request):
    payload = _get_valid_preview_payload(request)
    if not payload:
        return redirect("parts:import")

    if request.method == "POST":
        quantity_mode = request.POST.get("quantity_mode", QUANTITY_MODE_ADD)
        if quantity_mode not in {QUANTITY_MODE_ADD, QUANTITY_MODE_KEEP}:
            quantity_mode = QUANTITY_MODE_ADD

        failed_files = payload.get("failed_files", [])
        if failed_files and request.POST.get("continue_without_failed") != "yes":
            messages.error(request, "Bitte bestätigen Sie, dass fehlerhafte Dateien ausgeschlossen werden sollen.")
            payload["quantity_mode"] = quantity_mode
            request.session["invoice_preview_batch"] = payload
            return render(request, "parts/invoice_preview.html", {"preview": payload})

        include_known_duplicates = request.POST.get("include_known_duplicates") == "yes"
        documents = payload.get("documents", [])
        resolved_parts_by_key = {}

        with transaction.atomic():
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
                if already_imported and not include_known_duplicates:
                    continue

                line_count = 0
                for line in doc.get("lines", []):
                    index = line["index"]
                    field_prefix = f"{doc['document_id']}::{index}"
                    if request.POST.get(f"skip::{field_prefix}") == "on":
                        continue

                    selected_match = request.POST.get(f"match::{field_prefix}")
                    quantity = int(request.POST.get(f"quantity::{field_prefix}", line["quantity"]) or 0)
                    unit_price = Decimal(request.POST.get(f"unit_price::{field_prefix}", line["unit_price"]) or "0")
                    line_supplier = request.POST.get(f"supplier::{field_prefix}", line["supplier"]).strip()
                    supplier_part_no = request.POST.get(f"supplier_part_no::{field_prefix}", line["supplier_part_no"]).strip()
                    description = request.POST.get(f"description::{field_prefix}", line["description"]).strip()
                    mf_part_no = (request.POST.get(f"mf_part_no::{field_prefix}", line["mf_part_no"]) or "").strip()
                    currency = request.POST.get(f"currency::{field_prefix}", line["currency"]).strip() or "EUR"
                    part_type = request.POST.get(f"part_type::{field_prefix}", line.get("part_type", "")).strip()
                    value = request.POST.get(f"value::{field_prefix}", line.get("value", "")).strip()
                    packaging = request.POST.get(f"packaging::{field_prefix}", line.get("packaging", "")).strip()

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
                    else:
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
                            part_type=part_type,
                            value=value,
                            packaging=packaging,
                        )
                    if supplier_part_no:
                        resolved_parts_by_key[key] = part
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

        request.session.pop("invoice_preview_batch", None)
        messages.success(request, "Import abgeschlossen.")
        return redirect("parts:list")

    return render(request, "parts/invoice_preview.html", {"preview": payload, "quantity_mode_choices": [
        (QUANTITY_MODE_ADD, "Mengen zum Bestand addieren"),
        (QUANTITY_MODE_KEEP, "Bestand nicht verändern"),
    ]})


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
