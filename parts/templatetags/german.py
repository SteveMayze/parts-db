"""German number, price and date formats for the templates.

Values may be Decimal, int, float, numeric strings (the import preview keeps
amounts as strings in the session), date objects or ISO date strings.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()

CURRENCY_SYMBOLS = {"EUR": "€"}


def _to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ".")) if isinstance(value, str) else Decimal(str(value))
    except InvalidOperation:
        return None


def _format(number: Decimal, places: int) -> str:
    quantized = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = f"{quantized:,.{places}f}"
    return text.replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")


def _with_currency(text: str, currency) -> str:
    code = currency or "EUR"
    if not code:
        return text
    return f"{text} {CURRENCY_SYMBOLS.get(code, code)}"


@register.filter
def de_number(value, places=2):
    """1234.5 -> '1.234,50'."""
    number = _to_decimal(value)
    return "-" if number is None else _format(number, int(places))


@register.filter
def de_plain(value):
    """Rates and similar: 16.0 -> '16', 5.5 -> '5,5'."""
    number = _to_decimal(value)
    if number is None:
        return "-"
    text = format(number.normalize(), "f")
    return text.replace(".", ",")


@register.filter
def eur4(value, currency="EUR"):
    """Unit price with four decimals: '4,5172 €'."""
    number = _to_decimal(value)
    return "-" if number is None else _with_currency(_format(number, 4), currency)


@register.filter
def eur2(value, currency="EUR"):
    """Total with two decimals: '17,04 €'."""
    number = _to_decimal(value)
    return "-" if number is None else _with_currency(_format(number, 2), currency)


@register.filter
def de_date(value):
    """date or ISO string -> '10.12.2020'."""
    if value in (None, ""):
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


@register.filter
def de_datetime(value):
    """datetime or ISO string -> '10.12.2020 14:30'."""
    if value in (None, ""):
        return "-"
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    return value.strftime("%d.%m.%Y %H:%M")
