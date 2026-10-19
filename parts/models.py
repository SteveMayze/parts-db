from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.db.models.functions import Lower


class Supplier(models.Model):
    name = models.CharField("Name", max_length=255)
    url = models.URLField("URL", blank=True)
    created_at = models.DateTimeField("Erstellt am", auto_now_add=True)
    updated_at = models.DateTimeField("Aktualisiert am", auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="uniq_supplier_name_casefold"),
        ]
        verbose_name = "Lieferant"
        verbose_name_plural = "Lieferanten"

    def __str__(self) -> str:
        return self.name

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValidationError({"name": "Der Lieferantenname ist erforderlich."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Project(models.Model):
    name = models.CharField("Projektname", max_length=255)
    client = models.CharField("Kunde", max_length=255, blank=True, db_index=True)
    note = models.TextField("Notiz", blank=True)
    active = models.BooleanField("Aktiv", default=True, db_index=True)
    created_at = models.DateTimeField("Erstellt am", auto_now_add=True)
    updated_at = models.DateTimeField("Aktualisiert am", auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="uniq_project_name_casefold"),
        ]
        verbose_name = "Projekt"
        verbose_name_plural = "Projekte"

    def __str__(self) -> str:
        return self.name

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValidationError({"name": "Der Projektname ist erforderlich."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class PurchaseDocument(models.Model):
    class DocumentType(models.TextChoices):
        RECHNUNG = "Rechnung", "Rechnung"
        BESTELLUNG = "Bestellung", "Bestellung"
        AUFTRAGSBESTATIGUNG = "Auftragsbestätigung", "Auftragsbestätigung"
        SONSTIGES = "Sonstiges", "Sonstiges"

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_documents", verbose_name="Lieferant")
    document_type = models.CharField(
        "Belegtyp",
        max_length=40,
        choices=DocumentType.choices,
        default=DocumentType.RECHNUNG,
    )
    document_number = models.CharField("Belegnummer", max_length=200)
    document_date = models.DateField("Belegdatum", blank=True, null=True)
    related_order_number = models.CharField("Auftragsnummer", max_length=200, blank=True)
    currency = models.CharField("Währung", max_length=3, default="EUR", blank=True)
    shipping_cost = models.DecimalField("Versandkosten", max_digits=12, decimal_places=4, default=Decimal("0.00"), blank=True)
    discount_total = models.DecimalField("Rabatt (Beleg, brutto)", max_digits=12, decimal_places=4, default=Decimal("0.00"), blank=True)
    document_total = models.DecimalField("Belegsumme", max_digits=12, decimal_places=4, default=Decimal("0.00"), blank=True)
    pdf_file = models.FileField(
        "PDF-Datei",
        upload_to="purchase_documents/%Y/%m/",
        blank=True,
        null=True,
        validators=[FileExtensionValidator(allowed_extensions=["pdf"])],
    )
    filing_location = models.CharField("Einlagerungsort", max_length=255, blank=True)
    paper_invoice_no = models.CharField("Papierbelegnummer", max_length=200, blank=True)
    paper_filed = models.BooleanField("Papier abgelegt", default=False)
    notes = models.TextField("Notizen", blank=True)
    created_at = models.DateTimeField("Erstellt am", auto_now_add=True)

    class Meta:
        ordering = ["-document_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["supplier", "document_type", "document_number"], name="uniq_purchase_document_supplier_type_no"),
        ]
        verbose_name = "Kaufbeleg"
        verbose_name_plural = "Kaufbelege"

    def __str__(self) -> str:
        return f"{self.supplier} {self.document_type} {self.document_number}".strip()


class PurchaseLine(models.Model):
    document = models.ForeignKey(PurchaseDocument, on_delete=models.CASCADE, related_name="lines", verbose_name="Beleg")
    position = models.CharField("Position", max_length=50, blank=True)
    supplier_part_no = models.CharField("Lieferantenteilnummer", max_length=200, blank=True)
    description = models.TextField("Beschreibung", blank=True)
    manufacturer_part_no = models.CharField("Herstellerteilnummer", max_length=200, blank=True)
    quantity = models.DecimalField("Menge", max_digits=12, decimal_places=4, default=Decimal("0.0000"))
    unit_price = models.DecimalField("Einzelpreis", max_digits=12, decimal_places=4, blank=True, null=True)
    line_total = models.DecimalField("Gesamtpreis", max_digits=12, decimal_places=4, blank=True, null=True)
    currency = models.CharField("Währung", max_length=3, default="EUR", blank=True)
    price_is_net = models.BooleanField("Preis ist netto", default=True)
    price_is_derived = models.BooleanField("Preis ist abgeleitet", default=False)
    part = models.ForeignKey(
        "Part",
        on_delete=models.SET_NULL,
        related_name="purchase_lines",
        null=True,
        blank=True,
        verbose_name="Bauteil",
    )
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, related_name="purchase_lines", null=True, blank=True, verbose_name="Projekt")
    notes = models.TextField("Notizen", blank=True)
    created_at = models.DateTimeField("Erstellt am", auto_now_add=True)

    class Meta:
        ordering = ["document", "position", "id"]
        verbose_name = "Kaufzeile"
        verbose_name_plural = "Kaufzeilen"

    def __str__(self) -> str:
        summary = self.description or self.supplier_part_no or "Unbenannte Zeile"
        return f"{self.document} / {summary}"


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
    part_subtype = models.CharField("Untertyp", max_length=100, blank=True, default="", db_index=True)
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
        ordering = ["part_type", "part_subtype", "value", "packaging", "location_name"]
        verbose_name = "Bauteil"
        verbose_name_plural = "Bauteile"

    def __str__(self) -> str:
        type_label = self.part_type or "Nicht zugeordnet"
        if self.part_subtype:
            type_label = f"{type_label}/{self.part_subtype}"
        return f"{type_label} {self.value or ''} ({self.packaging or 'Nicht zugeordnet'})".strip()

    def save(self, *args, **kwargs):
        self.part_type = (self.part_type or "").strip()
        self.part_subtype = (self.part_subtype or "").strip()
        return super().save(*args, **kwargs)

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

    @property
    def purchase_history(self):
        return self.purchase_lines.select_related("document", "document__supplier", "project").order_by("-document__document_date", "-document__created_at", "-created_at")

    @property
    def last_purchase_document(self):
        return self.purchase_history.first()

    @property
    def last_purchase_date(self):
        latest = self.purchase_history.first()
        if latest and latest.document and latest.document.document_date:
            return latest.document.document_date
        return self.last_purchased


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
