"""Canonical units of account for constraint ledgers.

One ledger holds exactly one unit. Money remains ISO 4217 plus minor units.
Time is whole seconds of attention or wall-clock. Cycles and tries are counts.
``TRY`` (Turkish lira) is money; attempts use unit ``tries``.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .errors import ValidationError

MONEY = "money"
TIME = "time"
CYCLES = "cycles"
TRIES = "tries"
UNITS = frozenset({MONEY, TIME, CYCLES, TRIES})
TIME_SCALE = "seconds"

_LEDGER_IDENTITY = tuple[str, str | None]


def normalize_unit(value: object | None, *, default: str = MONEY) -> str:
    if value is None or value == "":
        return default
    unit = str(value).strip().lower()
    if unit not in UNITS:
        raise ValidationError("unit must be money, time, cycles, or tries")
    return unit


def normalize_ledger_identity(
    unit: object | None,
    currency: object | None,
    *,
    default_unit: str = MONEY,
) -> _LEDGER_IDENTITY:
    """Return ``(unit, currency)`` with money requiring a 3-letter ISO code."""

    resolved_unit = normalize_unit(unit, default=default_unit)
    if resolved_unit == MONEY:
        if currency is None or str(currency).strip() == "":
            raise ValidationError("money ledgers require an ISO 4217 currency")
        code = str(currency).strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValidationError("currency must be a three-letter ISO 4217 code")
        return resolved_unit, code
    if currency not in (None, ""):
        raise ValidationError("currency is only valid when unit is money")
    return resolved_unit, None


def identity_of(record: Mapping[str, Any], *, default_unit: str = MONEY) -> _LEDGER_IDENTITY:
    return normalize_ledger_identity(
        record.get("unit"),
        record.get("currency"),
        default_unit=default_unit,
    )


def same_ledger_identity(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    default_unit: str = MONEY,
) -> bool:
    return identity_of(left, default_unit=default_unit) == identity_of(
        right, default_unit=default_unit
    )


def ledger_label(unit: str, currency: str | None = None) -> str:
    if unit == MONEY:
        return currency or MONEY
    if unit == TIME:
        return TIME_SCALE
    return unit


def quantity_to_ledger(amount_minor: int, unit: str) -> Decimal:
    """Convert a wire integer into the persisted ledger amount.

    Money keeps the Phase 1 two-decimal major-unit representation. Time, cycles,
    and tries persist the same integer that appears on the wire.
    """

    if unit == MONEY:
        return Decimal(amount_minor) / Decimal(100)
    return Decimal(amount_minor)


def ledger_to_quantity(value: object, unit: str, field: str) -> int:
    """Read a persisted ledger amount back into the wire integer."""

    if unit == MONEY:
        if isinstance(value, (bool, float)):
            raise ValidationError(f"{field} must be a decimal string or integer")
        try:
            amount = value if isinstance(value, Decimal) else Decimal(str(value))
        except Exception as exc:
            raise ValidationError(f"{field} must be a decimal amount") from exc
        if not amount.is_finite():
            raise ValidationError(f"{field} must be finite")
        minor = amount * Decimal(100)
        if minor != minor.to_integral_value():
            raise ValidationError(f"{field} contains a fractional minor unit")
        if minor < 0:
            raise ValidationError(f"{field} must not be negative")
        return int(minor)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
        raise ValidationError(f"{field} must be a whole {unit} quantity")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValidationError(f"{field} must be a whole {unit} quantity") from exc
    if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
        raise ValidationError(f"{field} must be a whole {unit} quantity")
    return int(amount)


def parse_limit_to_minor(value: object, unit: str, field: str) -> int:
    """Parse a bootstrap or cell limit into the unit's canonical integer."""

    if unit == MONEY:
        return ledger_to_quantity(value, MONEY, field)
    return ledger_to_quantity(value, unit, field)


def dump_budget_amount(
    *,
    unit: str,
    amount_minor: int,
    currency: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"amount_minor": amount_minor}
    if unit == MONEY:
        payload["currency"] = currency
    else:
        payload["unit"] = unit
    return payload
