from django.contrib import admin

from .models import ImportedDocument, Part


@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    list_display = (
        "part_type",
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
    list_filter = ("part_type", "packaging", "location_type", "supplier", "currency")
    search_fields = (
        "part_type",
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
    ordering = ("part_type", "value", "packaging", "location_name")


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
