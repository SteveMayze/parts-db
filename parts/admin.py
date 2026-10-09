from django.contrib import admin

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
