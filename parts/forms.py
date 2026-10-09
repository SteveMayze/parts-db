from django import forms

from .models import Part, Project
from .stock_csv import IMPORT_MODE_ADD, IMPORT_MODE_SKIP, IMPORT_MODE_UPDATE


class MultiPDFInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    widget = MultiPDFInput

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_clean(item, initial) for item in data]
            if not result:
                raise forms.ValidationError(self.error_messages["required"], code="required")
            return result
        return single_clean(data, initial)


class PartForm(forms.ModelForm):
    class Meta:
        model = Part
        fields = [
            "part_type",
            "value",
            "packaging",
            "description",
            "manufacturer",
            "mf_part_no",
            "supplier",
            "supplier_part_no",
            "last_price",
            "currency",
            "last_purchased",
            "last_document_no",
            "quantity",
            "location_type",
            "location_name",
            "position",
            "comment",
        ]
        widgets = {
            "last_purchased": forms.DateInput(attrs={"type": "date"}),
        }


class InvoiceUploadForm(forms.Form):
    invoice_files = MultiFileField(
        label="PDF-Dateien",
        help_text="Mehrfachauswahl möglich (maximal 20 Dateien, jeweils bis 10 MB).",
        widget=MultiPDFInput(attrs={"accept": ".pdf,application/pdf"}),
    )
    supplier = forms.ChoiceField(
        required=False,
        label="Lieferant (optional für alle Dateien)",
        choices=(
            ("", "Automatisch erkennen"),
            ("Conrad Electronic SE", "Conrad Electronic SE"),
            ("Reichelt", "Reichelt"),
        ),
    )
    include_known_duplicates = forms.BooleanField(
        required=False,
        label="Bereits importierte Belege trotzdem einbeziehen",
    )
    continue_without_failed = forms.BooleanField(
        required=False,
        label="Mit den erfolgreich analysierten Dateien fortfahren",
    )


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "client", "note", "active"]


class StockCsvUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="CSV-Datei",
        help_text="Unterstützt UTF-8 (mit BOM) und cp1252. Trennzeichen: ; , oder Tabulator.",
    )
    mode = forms.ChoiceField(
        label="Importmodus",
        choices=(
            (IMPORT_MODE_UPDATE, "Vorhandene aktualisieren (Standard)"),
            (IMPORT_MODE_SKIP, "Vorhandene überspringen"),
            (IMPORT_MODE_ADD, "Mengen zu vorhandenen addieren"),
        ),
        initial=IMPORT_MODE_UPDATE,
    )
    continue_without_errors = forms.BooleanField(
        required=False,
        label="Mit den fehlerfreien Zeilen fortfahren",
    )
