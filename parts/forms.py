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
            "part_subtype",
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
        localized_fields = ("last_price",)
        widgets = {
            "part_type": forms.TextInput(attrs={"list": "known-part-types", "autocomplete": "off"}),
            "part_subtype": forms.TextInput(attrs={"list": "known-part-subtypes", "autocomplete": "off"}),
            "last_purchased": forms.DateInput(format="%d.%m.%Y", attrs={"placeholder": "TT.MM.JJJJ"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["last_purchased"].input_formats = ["%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"]


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
