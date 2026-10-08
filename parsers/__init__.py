"""Invoice parser registry for supplier-specific imports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Type

from .conrad import ConradInvoiceParser
from .reichelt import ReicheltInvoiceParser


@dataclass(frozen=True)
class ParserRegistration:
    supplier: str
    parser: Type


PARSER_REGISTRY = (
    ParserRegistration(supplier=ConradInvoiceParser.supplier, parser=ConradInvoiceParser),
    ParserRegistration(supplier=ReicheltInvoiceParser.supplier, parser=ReicheltInvoiceParser),
)


def get_parser_for_supplier(supplier: str):
    wanted = (supplier or "").strip().casefold()
    for registration in PARSER_REGISTRY:
        if registration.supplier.casefold() == wanted:
            return registration.parser()
    raise ValueError(f"Nicht unterstützter Lieferant: {supplier!r}")


def detect_parser_from_text(text: str):
    matches = []
    for registration in PARSER_REGISTRY:
        parser = registration.parser()
        if parser.detect_supplier(text) == registration.supplier:
            matches.append(parser)
    if not matches:
        raise ValueError("Lieferant konnte aus der Datei nicht erkannt werden.")
    if len(matches) > 1:
        raise ValueError("Lieferantenerkennung ist mehrdeutig; bitte Lieferant explizit auswählen.")
    return matches[0]
