from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.template.response import TemplateResponse

from .models import ImportedDocument, Part, Project, PurchaseDocument, PurchaseLine, Supplier


class PurchaseLineInline(admin.TabularInline):
    model = PurchaseLine
    extra = 1


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "url", "created_at")
    search_fields = ("name", "url")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "client", "active", "note", "created_at")
    search_fields = ("name", "client", "note")
    list_filter = ("active", "client")


@admin.register(PurchaseDocument)
class PurchaseDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "supplier",
        "document_type",
        "document_number",
        "document_date",
        "related_order_number",
        "paper_invoice_no",
        "paper_filed",
        "filing_location",
    )
    search_fields = ("supplier__name", "document_number", "related_order_number", "paper_invoice_no", "filing_location")
    list_filter = ("supplier", "document_type", "paper_filed")
    inlines = (PurchaseLineInline,)


@admin.register(PurchaseLine)
class PurchaseLineAdmin(admin.ModelAdmin):
    list_display = ("document", "position", "supplier_part_no", "description", "part", "project", "quantity", "unit_price")
    search_fields = ("supplier_part_no", "description", "manufacturer_part_no", "part__value")
    list_filter = ("document__supplier", "project", "price_is_net")


class SetClassificationForm(forms.Form):
    part_type = forms.CharField(label="Typ", max_length=100, required=False, help_text="Leer lassen = unverändert.")
    part_subtype = forms.CharField(label="Untertyp", max_length=100, required=False, help_text="Leer lassen = unverändert.")
    clear_subtype = forms.BooleanField(label="Untertyp leeren", required=False)


@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    actions = ("set_classification",)
    list_display = (
        "part_type",
        "part_subtype",
        "value",
        "packaging",
        "quantity",
        "supplier",
        "supplier_part_no",
        "last_price",
        "currency",
        "location_type",
        "location_name",
        "position",
        "manufacturer",
        "mf_part_no",
        "last_purchased",
        "updated_at",
    )
    list_filter = ("part_type", "part_subtype", "packaging", "location_type", "supplier", "currency")
    search_fields = (
        "part_type",
        "part_subtype",
        "value",
        "packaging",
        "description",
        "manufacturer",
        "mf_part_no",
        "supplier",
        "supplier_part_no",
        "last_document_no",
        "location_name",
        "position",
        "comment",
    )
    ordering = ("part_type", "part_subtype", "value", "packaging", "location_name")

    @admin.action(description="Typ/Untertyp der ausgewählten Bauteile setzen")
    def set_classification(self, request, queryset):
        form = SetClassificationForm(request.POST if "apply" in request.POST else None)
        if "apply" in request.POST and form.is_valid():
            new_type = form.cleaned_data["part_type"]
            new_subtype = form.cleaned_data["part_subtype"]
            clear_subtype = form.cleaned_data["clear_subtype"]
            changed = 0
            for part in queryset:
                if new_type:
                    part.part_type = new_type
                if new_subtype:
                    part.part_subtype = new_subtype
                elif clear_subtype:
                    part.part_subtype = ""
                part.save(update_fields=["part_type", "part_subtype", "updated_at"])
                changed += 1
            self.message_user(request, f"{changed} Bauteil(e) aktualisiert.", messages.SUCCESS)
            return None
        context = {
            **self.admin_site.each_context(request),
            "title": "Typ/Untertyp setzen",
            "form": form,
            "queryset": queryset,
            "opts": self.model._meta,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "action_name": "set_classification",
        }
        return TemplateResponse(request, "admin/parts/part/set_classification.html", context)


@admin.register(ImportedDocument)
class ImportedDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "supplier",
        "document_no",
        "document_date",
        "document_type",
        "original_filename",
        "line_count",
        "imported_at",
    )
    search_fields = ("supplier", "document_no", "original_filename")
    list_filter = ("supplier", "document_type", "imported_at")
