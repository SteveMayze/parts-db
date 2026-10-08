from decimal import Decimal

from django.db import models


class Part(models.Model):
    class LocationType(models.TextChoices):
        FOLDER = "folder", "Mappe"
        DRAWER = "drawer", "Schublade"
        STORAGE_BOX = "storage_box", "Sortierbox"

    part_type = models.CharField(
        "Typ",
        max_length=100,
        blank=True,
        db_index=True,
    )
    value = models.CharField("Wert", max_length=100, blank=True, db_index=True)
    packaging = models.CharField(
        "Verpackung",
        max_length=100,
        blank=True,
        db_index=True,
    )
    description = models.TextField("Beschreibung", blank=True)
    manufacturer = models.CharField("Hersteller", max_length=200, blank=True, db_index=True)
    mf_part_no = models.CharField("Herstellerteilnummer", max_length=200, blank=True, db_index=True)
    supplier = models.CharField("Lieferant", max_length=200, blank=True, db_index=True)
    supplier_part_no = models.CharField(
        "Lieferantenteilnummer",
        max_length=200,
        blank=True,
        db_index=True,
    )
    last_price = models.DecimalField(
        "Letzter Nettopreis",
        max_digits=12,
        decimal_places=4,
        default=Decimal("0.00"),
        blank=True,
    )
    currency = models.CharField("Währung", max_length=3, default="EUR", blank=True)
    last_purchased = models.DateField("Zuletzt gekauft", blank=True, null=True)
    last_document_no = models.CharField("Beleg-Nr.", max_length=200, blank=True, db_index=True)
    quantity = models.PositiveIntegerField("Menge", default=0)
    location_type = models.CharField(
        "Lagerort-Typ",
        max_length=20,
        choices=LocationType.choices,
        blank=True,
        db_index=True,
    )
    location_name = models.CharField("Lagerort", max_length=200, blank=True, db_index=True)
    position = models.CharField("Position", max_length=200, blank=True)
    comment = models.TextField("Kommentar", blank=True)
    created_at = models.DateTimeField("Erstellt am", auto_now_add=True)
    updated_at = models.DateTimeField("Aktualisiert am", auto_now=True)

    class Meta:
        ordering = ["part_type", "value", "packaging", "location_name"]
        verbose_name = "Bauteil"
        verbose_name_plural = "Bauteile"

    def __str__(self) -> str:
        return f"{self.part_type or 'Nicht zugeordnet'} {self.value or ''} ({self.packaging or 'Nicht zugeordnet'})".strip()

    @property
    def full_location(self) -> str:
        pieces = []
        if self.location_type:
            pieces.append(self.get_location_type_display())
        if self.location_name:
            pieces.append(self.location_name)
        if self.position:
            pieces.append(self.position)
        return " / ".join(pieces) if pieces else "Nicht zugeordnet"


class ImportedDocument(models.Model):
    supplier = models.CharField("Lieferant", max_length=200, db_index=True)
    document_no = models.CharField("Beleg-Nr.", max_length=200, db_index=True)
    document_date = models.DateField("Belegdatum", blank=True, null=True)
    document_type = models.CharField("Belegtyp", max_length=50, blank=True)
    original_filename = models.CharField("Dateiname", max_length=255)
    imported_at = models.DateTimeField("Importiert am", auto_now_add=True)
    line_count = models.PositiveIntegerField("Zeilenanzahl", default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["supplier", "document_no"], name="uniq_imported_document_supplier_no"),
        ]
        ordering = ["-imported_at", "supplier", "document_no"]
        verbose_name = "Importierter Beleg"
        verbose_name_plural = "Importierte Belege"

    def __str__(self) -> str:
        return f"{self.supplier} {self.document_no}"
