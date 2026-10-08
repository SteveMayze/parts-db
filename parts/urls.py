from django.urls import path

from .views import (
    PartCreateView,
    PartDeleteView,
    PartListView,
    PartUpdateView,
    invoice_preview,
    invoice_upload,
    parts_complete_preview,
)

app_name = "parts"

urlpatterns = [
    path("", PartListView.as_view(), name="list"),
    path("add/", PartCreateView.as_view(), name="add"),
    path("complete/", parts_complete_preview, name="complete"),
    path("import/", invoice_upload, name="import"),
    path("import/preview/", invoice_preview, name="invoice_preview"),
    path("<int:pk>/edit/", PartUpdateView.as_view(), name="edit"),
    path("<int:pk>/delete/", PartDeleteView.as_view(), name="delete"),
]
