#!/usr/bin/env python3
"""Deterministic monthly cash-flow projection and dashboard generator."""

from __future__ import annotations

import html
import os
import re
import sys
import tempfile
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from string import Template
from typing import Any, TextIO

try:
    import yaml
except ModuleNotFoundError:
    raise SystemExit(
        "PyYAML is required. Install it with: python -m pip install -r requirements.txt"
    )


ZERO = Decimal("0")
CENT = Decimal("0.01")
MAX_MONEY_INTEGER_DIGITS = 256
# Annual Keez totals aggregate unrounded values while the PDF exposes whole RON.
ACCOUNTING_ROUNDING_TOLERANCE = Decimal("6")
MONTH_PATTERN = re.compile(r"^(\d{4})-(\d{2})$")
ACTIVITY_ORDER = ("operating", "investing", "financing")
ALLOWED_ACTIVITIES = set(ACTIVITY_ORDER)
ROW_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAX_TIMELINE_MONTHS = 1200
DERIVED_ROWS = {"vat": "vat", "taxes": "taxes", "dividends-paid": "dividends"}
TAX_COMPONENTS = {"vat", "profit", "dividend", "other"}


class ConfigError(ValueError):
    """Raised when the input file does not match the cash-flow schema."""


class DecimalSafeLoader(yaml.SafeLoader):
    """A safe YAML loader that preserves decimal scalars exactly."""


def _construct_decimal(loader: DecimalSafeLoader, node: yaml.Node) -> Decimal:
    value = loader.construct_scalar(node).replace("_", "")
    special_values = {
        ".inf": "Infinity",
        "+.inf": "Infinity",
        "-.inf": "-Infinity",
        ".nan": "NaN",
    }
    value = special_values.get(value.lower(), value)
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise yaml.constructor.ConstructorError(
            None, None, f"invalid decimal value {value!r}", node.start_mark
        ) from exc


DecimalSafeLoader.add_constructor(
    "tag:yaml.org,2002:float", _construct_decimal
)


def _construct_unique_mapping(
    loader: DecimalSafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "YAML merge keys are not supported",
                key_node.start_mark,
            )
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate mapping key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


DecimalSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _decimal_precision(values: list[Decimal], term_count: int = 1) -> int:
    """Return enough precision to add the values without losing decimal places."""
    highest_place = max((value.adjusted() for value in values if value), default=0)
    lowest_place = min((value.as_tuple().exponent for value in values), default=0)
    lowest_place = min(lowest_place, -2)
    carry_digits = len(str(max(1, term_count))) + 2
    return max(28, highest_place - lowest_place + 1 + carry_digits)


@dataclass(frozen=True)
class Settings:
    company_name: str
    registration_number: str
    currency: str
    ron_per_eur: Decimal
    start_month: int
    projection_months: int
    initial_balance: Decimal
    history_start: int


@dataclass(frozen=True)
class Row:
    id: str
    name: str
    activity: str
    forecast: str
    profit_weight: Decimal
    seed: Decimal | None
    seed_source: str
    overrides: dict[int, Decimal]


@dataclass(frozen=True)
class ActualMonth:
    source: str
    basis: str
    values: dict[str, Decimal]
    opening_balance: Decimal | None
    closing_balance: Decimal
    note: str


@dataclass(frozen=True)
class TaxCheckpoint:
    source: str
    kind: str
    vat_credit: Decimal
    profit_loss: Decimal
    payments: dict[int, dict[str, Decimal]]


@dataclass(frozen=True)
class TaxRules:
    vat_rates: tuple[tuple[int, Decimal], ...]
    profit_rate: Decimal
    dividend_rate: Decimal
    reported_cash_seed_basis: str
    checkpoints: dict[int, TaxCheckpoint]


@dataclass(frozen=True)
class SuppliedPayment:
    source: str
    amounts: dict[str, Decimal]


@dataclass(frozen=True)
class Dividend:
    gross: Decimal
    source: str


@dataclass(frozen=True)
class Config:
    settings: Settings
    rows: tuple[Row, ...]
    actuals: dict[int, ActualMonth]
    taxes: TaxRules
    tax_payments: dict[int, SuppliedPayment]
    dividends: dict[int, Dividend]

    @property
    def actual_through(self) -> int:
        return max(self.actuals, default=self.settings.history_start - 1)


@dataclass(frozen=True)
class CellResult:
    value: Decimal
    provenance: str
    editable: bool = False
    note: str = ""
    override: str | None = None


@dataclass(frozen=True)
class ProjectedRow:
    id: str
    name: str
    activity: str
    cells: tuple[CellResult, ...]

    @property
    def values(self) -> tuple[Decimal, ...]:
        return tuple(cell.value for cell in self.cells)


@dataclass(frozen=True)
class MonthResult:
    month: int
    opening_balance: Decimal
    inflows: Decimal
    outflows: Decimal
    net: Decimal
    closing_balance: Decimal
    provenance: str = "derived"
    opening_note: str = ""
    closing_note: str = ""


@dataclass(frozen=True)
class Projection:
    """Cash timeline with net-presented rows; original actuals remain in Config."""

    settings: Settings
    rows: tuple[ProjectedRow, ...]
    months: tuple[MonthResult, ...]
    actual_through: int
    tax_details: tuple[dict[str, str], ...]

    @property
    def trough(self) -> MonthResult:
        return min(self.months, key=lambda month: month.closing_balance)

    @property
    def ending_balance(self) -> Decimal:
        return self.months[-1].closing_balance

    @property
    def average_net(self) -> Decimal:
        values = [month.net for month in self.months]
        with localcontext() as context:
            context.prec = _decimal_precision(values, len(values)) + 20
            total = sum(values, ZERO)
            return total / Decimal(len(self.months))


@dataclass(frozen=True)
class HistoricalSeries:
    months: tuple[Decimal, ...]
    total: Decimal


@dataclass(frozen=True)
class HistoricalRow:
    id: str
    name: str
    values: HistoricalSeries


@dataclass(frozen=True)
class HistoricalSection:
    id: str
    name: str
    reported: HistoricalSeries
    rows: tuple[HistoricalRow, ...]


@dataclass(frozen=True)
class HistoricalCashflow:
    opening_balance: HistoricalSeries
    sections: tuple[HistoricalSection, ...]
    closing_balance: HistoricalSeries


@dataclass(frozen=True)
class HistoricalProfit:
    sections: tuple[HistoricalSection, ...]
    summaries: tuple[HistoricalRow, ...]


@dataclass(frozen=True)
class ActualsReport:
    company_name: str
    registration_number: str
    start_month: int
    end_month: int
    currency: str
    cashflow_source: str
    profit_source: str
    cashflow: HistoricalCashflow
    profit: HistoricalProfit


@dataclass(frozen=True)
class ReconciliationDifference:
    scope: str
    period: str
    difference: Decimal


@dataclass(frozen=True)
class MoneyCell:
    """A source amount and its engine-formatted display currencies."""

    source: str
    ron: str
    eur: str
    provenance: str = "derived"
    editable: bool = False
    note: str = ""
    override: str | None = None


@dataclass(frozen=True)
class ReportRow:
    id: str
    name: str
    cells: tuple[MoneyCell, ...]


@dataclass(frozen=True)
class ReportGroup:
    id: str
    name: str
    rows: tuple[ReportRow, ...]
    subtotal: ReportRow


@dataclass(frozen=True)
class ReportView:
    """Presentation-ready report values shared by the HTML and HTTP views."""

    mode: str
    currency: str
    months: tuple[str, ...]
    opening_balance: ReportRow
    activity_groups: tuple[ReportGroup, ...]
    closing_balance: ReportRow
    summary: dict[str, MoneyCell | str | int]
    navigation: dict[str, str | None] = field(default_factory=dict)
    tax_details: tuple[dict[str, str], ...] = ()


def parse_month(value: Any, path: str) -> int:
    if not isinstance(value, str):
        raise ConfigError(f"{path} must be a string in YYYY-MM format")
    match = MONTH_PATTERN.fullmatch(value)
    if not match:
        raise ConfigError(f"{path} must use YYYY-MM format")
    year, month = (int(part) for part in match.groups())
    if year < 1 or not 1 <= month <= 12:
        raise ConfigError(f"{path} must be a valid calendar month")
    return year * 12 + month - 1


def format_month(month_index: int) -> str:
    year, zero_based_month = divmod(month_index, 12)
    return f"{year:04d}-{zero_based_month + 1:02d}"


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be a list")
    return value


def _check_keys(
    value: dict[str, Any], required: set[str], allowed: set[str], path: str
) -> None:
    non_string_keys = [repr(key) for key in value if not isinstance(key, str)]
    if non_string_keys:
        raise ConfigError(
            f"{path} field names must be strings: {', '.join(non_string_keys)}"
        )
    missing = sorted(required - value.keys())
    if missing:
        raise ConfigError(f"{path} is missing required field(s): {', '.join(missing)}")
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise ConfigError(f"{path} has unknown field(s): {', '.join(unknown)}")


def _require_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} must be a non-empty string")
    return value.strip()


def _require_decimal(value: Any, path: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
        raise ConfigError(f"{path} must be a number")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"{path} must be a number") from exc
    if not amount.is_finite():
        raise ConfigError(f"{path} must be finite")
    if amount == ZERO:
        return ZERO
    digits = list(amount.as_tuple().digits)
    exponent = amount.as_tuple().exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if digits:
        fractional_places = max(0, -exponent)
        integer_digits = max(1, exponent + len(digits))
        if fractional_places > 2:
            raise ConfigError(f"{path} must have at most two decimal places")
        if integer_digits > MAX_MONEY_INTEGER_DIGITS:
            raise ConfigError(
                f"{path} must have at most {MAX_MONEY_INTEGER_DIGITS} integer digits"
            )
    return amount


def validate_config(raw: Any) -> Config:
    root = _require_mapping(raw, "cashflow")
    root_keys = {"schema_version", "settings", "rows", "actuals", "taxes", "tax_payments", "dividends"}
    _check_keys(root, root_keys, root_keys, "cashflow")
    if type(root["schema_version"]) is not int or root["schema_version"] != 2:
        raise ConfigError("schema_version must be 2")
    settings_raw = _require_mapping(root["settings"], "settings")
    settings_keys = {
        "company_name", "registration_number", "currency", "ron_per_eur",
        "start_date", "projection_months", "initial_balance", "history_start",
    }
    _check_keys(settings_raw, settings_keys, settings_keys, "settings")
    company_name = _require_text(settings_raw["company_name"], "settings.company_name")
    registration_number = _require_text(settings_raw["registration_number"], "settings.registration_number")
    currency = _require_text(settings_raw["currency"], "settings.currency")
    if currency != "RON":
        raise ConfigError("settings.currency must be RON; EUR is display-only")
    ron_per_eur = _require_decimal(settings_raw["ron_per_eur"], "settings.ron_per_eur")
    if ron_per_eur <= ZERO:
        raise ConfigError("settings.ron_per_eur must be positive")
    start_month = parse_month(settings_raw["start_date"], "settings.start_date")
    history_start = parse_month(settings_raw["history_start"], "settings.history_start")
    projection_months = settings_raw["projection_months"]
    if type(projection_months) is not int or projection_months != 12:
        raise ConfigError("settings.projection_months must be exactly 12")
    initial_balance = _require_decimal(settings_raw["initial_balance"], "settings.initial_balance")
    settings = Settings(
        company_name, registration_number, currency, ron_per_eur,
        start_month, projection_months, initial_balance, history_start,
    )
    validate_window(settings, format_month(start_month))

    def dated(mapping: Any, path: str) -> dict[int, Any]:
        result = {}
        for key, value in _require_mapping(mapping, path).items():
            month = parse_month(key, f"{path} key")
            if not history_start <= month < timeline_end(settings):
                raise ConfigError(f"{path}[{key}] is outside the supported timeline")
            result[month] = value
        return result

    rows: list[Row] = []
    row_keys = {"id", "name", "activity", "forecast", "profit_weight", "seed", "overrides"}
    for index, item in enumerate(_require_list(root["rows"], "rows")):
        path = f"rows[{index}]"
        row = _require_mapping(item, path)
        _check_keys(row, row_keys, row_keys, path)
        row_id = _require_text(row["id"], f"{path}.id")
        if not ROW_ID_PATTERN.fullmatch(row_id):
            raise ConfigError(f"{path}.id must be lowercase hyphenated")
        if row_id in {"opening-balance", "closing-balance", "subtotal"} or row_id.startswith("subtotal-"):
            raise ConfigError(f"{path}.id is reserved")
        name = _require_text(row["name"], f"{path}.name")
        activity = row["activity"]
        if not isinstance(activity, str) or activity not in ALLOWED_ACTIVITIES:
            raise ConfigError(f"{path}.activity must be 'operating', 'investing', or 'financing'")
        method = _require_text(row["forecast"], f"{path}.forecast")
        if method not in {"carry", "zero", *DERIVED_ROWS.values()}:
            raise ConfigError(f"{path}.forecast is not a supported method")
        if row_id in DERIVED_ROWS and method != DERIVED_ROWS[row_id]:
            raise ConfigError(f"{row_id} must be derived using {DERIVED_ROWS[row_id]}")
        if method not in {"carry", "zero"} and DERIVED_ROWS.get(row_id) != method:
            raise ConfigError(f"{path}.forecast uses a reserved derived method")
        weight = _require_rate(row["profit_weight"], f"{path}.profit_weight")
        if weight and (method not in {"carry", "zero"} or row_id in {
            "advances", "fixed-assets", "short-term-debt", "shareholders", "intercompany-settlements",
        }):
            raise ConfigError(f"{row_id} cannot contribute to the profit proxy")
        seed, seed_source = None, ""
        if row["seed"] is not None:
            seed_raw = _require_mapping(row["seed"], f"{path}.seed")
            _check_keys(seed_raw, {"value", "source"}, {"value", "source"}, f"{path}.seed")
            seed = _require_decimal(seed_raw["value"], f"{path}.seed.value")
            seed_source = _require_text(seed_raw["source"], f"{path}.seed.source")
            if method != "carry":
                raise ConfigError(f"{path}.seed is only for recurring rows")
        overrides = {
            month: _require_decimal(value, f"{path}.overrides[{format_month(month)}]")
            for month, value in dated(row["overrides"], f"{path}.overrides").items()
        }
        if method not in {"carry", "zero"} and overrides:
            raise ConfigError(f"{row_id} is derived and cannot have overrides")
        rows.append(Row(row_id, name, activity, method, weight, seed, seed_source, overrides))

    duplicate_ids = sorted(row_id for row_id, count in Counter(row.id for row in rows).items() if count > 1)
    if duplicate_ids:
        raise ConfigError(f"row ids must be unique: {', '.join(duplicate_ids)}")
    ids = {row.id for row in rows}
    if not {"clients", "suppliers", *DERIVED_ROWS}.issubset(ids):
        raise ConfigError("rows must include clients, suppliers, vat, taxes, and dividends-paid")
    for row in rows:
        if row.id in {"clients", "suppliers"} and row.forecast != "carry":
            raise ConfigError(f"{row.id} must use carry forecasting")

    actuals = {}
    for month, item in dated(root["actuals"], "actuals").items():
        path = f"actuals[{format_month(month)}]"
        record = _require_mapping(item, path)
        required = {"source", "basis", "values", "closing_balance"}
        _check_keys(record, required, required | {"opening_balance", "note"}, path)
        source = _require_text(record["source"], f"{path}.source")
        basis = _require_text(record["basis"], f"{path}.basis")
        if basis not in {"reported", "net"}:
            raise ConfigError(f"{path}.basis must be reported or net")
        amounts = _require_mapping(record["values"], f"{path}.values")
        _check_keys(amounts, ids, ids, f"{path}.values (complete months only)")
        actuals[month] = ActualMonth(
            source, basis,
            {key: _require_decimal(value, f"{path}.values.{key}") for key, value in amounts.items()},
            _require_decimal(record["opening_balance"], f"{path}.opening_balance") if "opening_balance" in record else None,
            _require_decimal(record["closing_balance"], f"{path}.closing_balance"),
            _require_text(record["note"], f"{path}.note") if "note" in record else "",
        )
    if actuals and set(actuals) != set(range(history_start, max(actuals) + 1)):
        raise ConfigError("actuals must be contiguous complete months from history_start")

    tax_raw = _require_mapping(root["taxes"], "taxes")
    tax_keys = {"vat_rates", "profit_rate", "dividend_rate", "reported_cash_seed_basis", "checkpoints"}
    _check_keys(tax_raw, tax_keys, tax_keys, "taxes")
    rates = tuple(sorted(
        (parse_month(key, "taxes.vat_rates key"), _require_rate(value, "taxes.vat_rates value"))
        for key, value in _require_mapping(tax_raw["vat_rates"], "taxes.vat_rates").items()
    ))
    if not rates or rates[0][0] > history_start:
        raise ConfigError("VAT rate schedule must cover history_start")
    seed_basis = _require_text(tax_raw["reported_cash_seed_basis"], "taxes.reported_cash_seed_basis")
    if seed_basis not in {"gross-standard", "net"}:
        raise ConfigError("reported_cash_seed_basis must be gross-standard or net")
    checkpoints = {}
    for month, item in dated(tax_raw["checkpoints"], "taxes.checkpoints").items():
        path = f"taxes.checkpoints[{format_month(month)}]"
        checkpoint = _require_mapping(item, path)
        keys = {"source", "kind", "vat_credit", "profit_loss", "payments"}
        _check_keys(checkpoint, keys, keys, path)
        kind = _require_text(checkpoint["kind"], f"{path}.kind")
        if kind not in {"assumption", "accounting"}:
            raise ConfigError(f"{path}.kind must be assumption or accounting")
        payments = {}
        for due, payment in dated(checkpoint["payments"], f"{path}.payments").items():
            if due < month:
                raise ConfigError(f"{path}.payments cannot be due before the checkpoint")
            payments[due] = _payment_amounts(payment, f"{path}.payments", allow_total=False)
        checkpoints[month] = TaxCheckpoint(
            _require_text(checkpoint["source"], f"{path}.source"), kind,
            _nonnegative(checkpoint["vat_credit"], f"{path}.vat_credit"),
            _nonnegative(checkpoint["profit_loss"], f"{path}.profit_loss"), payments,
        )
    forecast_start = max(actuals, default=history_start - 1) + 1
    if not checkpoints or min(checkpoints) > forecast_start:
        raise ConfigError("taxes.checkpoints must establish state by the first forecast month")
    for month, actual in actuals.items():
        if actual.basis == "reported" and month >= min(checkpoints):
            raise ConfigError("actuals in the tax-model timeline must use net basis; normalize cash reports explicitly")

    supplied = {}
    for month, item in dated(root["tax_payments"], "tax_payments").items():
        record = _require_mapping(item, "tax_payments[]")
        _check_keys(record, {"source"}, {"source", "total", *TAX_COMPONENTS}, "tax_payments[]")
        supplied[month] = SuppliedPayment(
            _require_text(record["source"], "tax_payments[].source"),
            _payment_amounts({key: value for key, value in record.items() if key != "source"}, "tax_payments[]"),
        )
    dividends = {}
    for month, item in dated(root["dividends"], "dividends").items():
        record = _require_mapping(item, "dividends[]")
        _check_keys(record, {"gross", "source"}, {"gross", "source"}, "dividends[]")
        if month < min(checkpoints):
            raise ConfigError("dividend events require initialized tax state")
        gross = _nonnegative(record["gross"], "dividends[].gross")
        if gross == ZERO:
            raise ConfigError("dividends[].gross must be positive; remove a cancelled event")
        dividends[month] = Dividend(gross, _require_text(record["source"], "dividends[].source"))
    dividend_rate = _require_rate(tax_raw["dividend_rate"], "taxes.dividend_rate")
    for month, actual in actuals.items():
        if actual.basis != "net" or actual.values["dividends-paid"] == ZERO:
            continue
        event = dividends.get(month)
        next_payment = supplied.get(month + 1)
        exact_next = month + 1 in checkpoints or (
            next_payment is not None and bool({"dividend", "total"} & next_payment.amounts.keys())
        )
        with localcontext() as context:
            context.prec = MAX_MONEY_INTEGER_DIGITS + 50
            matches = event is not None and actual.values["dividends-paid"] == -(
                event.gross - _rounded_money(event.gross * dividend_rate)
            )
        if not matches and not exact_next:
            raise ConfigError(
                f"actual dividend payment in {format_month(month)} requires a matching gross event "
                "or an explicit following-month tax payment/checkpoint"
            )
    return Config(settings, tuple(rows), actuals, TaxRules(
        rates, _require_rate(tax_raw["profit_rate"], "taxes.profit_rate"),
        dividend_rate, seed_basis, checkpoints,
    ), supplied, dividends)


def _nonnegative(value: Any, path: str) -> Decimal:
    amount = _require_decimal(value, path)
    if amount < ZERO:
        raise ConfigError(f"{path} must be nonnegative")
    return amount


def _require_rate(value: Any, path: str) -> Decimal:
    rate = _nonnegative(value, path)
    if rate > 1:
        raise ConfigError(f"{path} must be between 0 and 1")
    return rate


def _payment_amounts(value: Any, path: str, *, allow_total: bool = True) -> dict[str, Decimal]:
    record = _require_mapping(value, path)
    _check_keys(record, set(), TAX_COMPONENTS | ({"total"} if allow_total else set()), path)
    if not record:
        raise ConfigError(f"{path} requires a payment component")
    if "total" in record and (set(record) - {"total", "vat"}):
        raise ConfigError(f"{path} cannot combine total and Taxes components")
    return {key: _nonnegative(amount, f"{path}.{key}") for key, amount in record.items()}


def timeline_end(settings: Settings) -> int:
    return min(settings.history_start + MAX_TIMELINE_MONTHS, 10000 * 12)


def validate_window(settings: Settings, start: str | None) -> int:
    month = settings.start_month if start is None else parse_month(start, "start")
    if not settings.history_start <= month <= timeline_end(settings) - 12:
        raise ConfigError("start must select twelve months inside the supported timeline")
    return month


def replace_input(
    raw: Any,
    *,
    row_id: str | None = None,
    month: str | None = None,
    value: Any = None,
) -> dict[str, Any]:
    """Return a copy with one forecast override set, or cleared with None."""
    config = validate_config(raw)
    target = parse_month(month, "replace_input.month")
    row = next((row for row in config.rows if row.id == row_id), None)
    if row is None:
        raise ConfigError(f"replace_input references unknown row {row_id!r}")
    if row.forecast not in {"carry", "zero"}:
        raise ConfigError(f"{row_id} is derived and read-only")
    if not config.actual_through < target < timeline_end(config.settings):
        raise ConfigError("only forecast months inside the supported timeline are editable")
    updated = deepcopy(raw)
    candidate = next(row for row in updated["rows"] if row["id"] == row_id)
    if value is None:
        candidate["overrides"].pop(month, None)
    else:
        candidate["overrides"][month] = _require_decimal(value, "replace_input.value")
    validate_config(updated)
    return updated


def import_actual_month(raw: Any, month: str, record: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete accounting update; incomplete imports never become facts."""
    validate_config(raw)
    updated = deepcopy(raw)
    updated["actuals"][month] = deepcopy(record)
    config = validate_config(updated)
    evaluate_config(config)
    return updated


def normalize_cash_actual(
    record: dict[str, Any], *, clients_vat: Any, suppliers_vat: Any
) -> dict[str, Any]:
    """Reclassify reported cash using supplied signed VAT components, not a guessed rate.

    The source cash total and reported balances are preserved. Pass the returned
    complete record to import_actual_month for scenario-wide validation.
    """
    record = _require_mapping(record, "accounting month")
    if record.get("basis") != "reported":
        raise ConfigError("normalization requires reported cash basis")
    updated = deepcopy(record)
    values = _require_mapping(updated.get("values"), "accounting month.values")
    client_cash = _require_decimal(values.get("clients"), "accounting month.clients")
    supplier_cash = _require_decimal(values.get("suppliers"), "accounting month.suppliers")
    remittance = _require_decimal(values.get("vat"), "accounting month.vat")
    output_vat = _require_decimal(clients_vat, "clients_vat")
    input_vat = _require_decimal(suppliers_vat, "suppliers_vat")
    with localcontext() as context:
        context.prec = MAX_MONEY_INTEGER_DIGITS + 50
        values["clients"] = client_cash - output_vat
        values["suppliers"] = supplier_cash - input_vat
        values["vat"] = remittance + output_vat + input_vat
    updated["basis"] = "net"
    updated["note"] = (
        f"{record.get('note', '')} Normalized from reported cash using supplied signed VAT components: "
        f"Clients cash {client_cash}, VAT {output_vat}; Suppliers cash {supplier_cash}, VAT {input_vat}; "
        f"tax-authority VAT cash {remittance}. Source total and reported balances preserved."
    ).strip()
    return updated


def load_config_bytes(data: bytes, path: Path) -> Config:
    """Strictly parse and validate configuration bytes from *path*."""
    try:
        raw = yaml.load(data.decode("utf-8"), Loader=DecimalSafeLoader)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"could not decode {path} as UTF-8") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc
    return validate_config(raw)


def load_config(path: Path) -> Config:
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"input file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    return load_config_bytes(data, path)


def _parse_historical_series(value: Any, path: str) -> HistoricalSeries:
    series = _require_mapping(value, path)
    keys = {"months", "total"}
    _check_keys(series, keys, keys, path)
    month_values = _require_list(series["months"], f"{path}.months")
    if len(month_values) != 12:
        raise ConfigError(f"{path}.months must contain exactly 12 values")
    months = tuple(
        _require_decimal(amount, f"{path}.months[{index}]")
        for index, amount in enumerate(month_values)
    )
    total = _require_decimal(series["total"], f"{path}.total")
    return HistoricalSeries(months, total)


def _parse_historical_rows(value: Any, path: str) -> tuple[HistoricalRow, ...]:
    rows: list[HistoricalRow] = []
    keys = {"id", "name", "values"}
    for index, item in enumerate(_require_list(value, path)):
        row_path = f"{path}[{index}]"
        row = _require_mapping(item, row_path)
        _check_keys(row, keys, keys, row_path)
        rows.append(
            HistoricalRow(
                _require_text(row["id"], f"{row_path}.id"),
                _require_text(row["name"], f"{row_path}.name"),
                _parse_historical_series(row["values"], f"{row_path}.values"),
            )
        )
    duplicate_ids = sorted(
        row_id
        for row_id, count in Counter(row.id for row in rows).items()
        if count > 1
    )
    if duplicate_ids:
        raise ConfigError(f"{path} ids must be unique: {', '.join(duplicate_ids)}")
    return tuple(rows)


def _parse_historical_sections(
    value: Any, path: str
) -> tuple[HistoricalSection, ...]:
    sections: list[HistoricalSection] = []
    keys = {"id", "name", "reported", "rows"}
    for index, item in enumerate(_require_list(value, path)):
        section_path = f"{path}[{index}]"
        section = _require_mapping(item, section_path)
        _check_keys(section, keys, keys, section_path)
        sections.append(
            HistoricalSection(
                _require_text(section["id"], f"{section_path}.id"),
                _require_text(section["name"], f"{section_path}.name"),
                _parse_historical_series(
                    section["reported"], f"{section_path}.reported"
                ),
                _parse_historical_rows(section["rows"], f"{section_path}.rows"),
            )
        )
    duplicate_ids = sorted(
        section_id
        for section_id, count in Counter(section.id for section in sections).items()
        if count > 1
    )
    if duplicate_ids:
        raise ConfigError(f"{path} ids must be unique: {', '.join(duplicate_ids)}")
    return tuple(sections)


def validate_actuals(raw: Any) -> ActualsReport:
    root = _require_mapping(raw, "actuals")
    root_keys = {"company", "report", "sources", "cashflow", "profit"}
    _check_keys(root, root_keys, root_keys, "actuals")

    company = _require_mapping(root["company"], "actuals.company")
    company_keys = {"name", "registration_number"}
    _check_keys(company, company_keys, company_keys, "actuals.company")
    company_name = _require_text(company["name"], "actuals.company.name")
    registration_number = _require_text(
        company["registration_number"], "actuals.company.registration_number"
    )

    report = _require_mapping(root["report"], "actuals.report")
    report_keys = {"start_date", "end_date", "currency"}
    _check_keys(report, report_keys, report_keys, "actuals.report")
    start_month = parse_month(report["start_date"], "actuals.report.start_date")
    end_month = parse_month(report["end_date"], "actuals.report.end_date")
    if end_month != start_month + 11:
        raise ConfigError("actuals.report must cover exactly 12 consecutive months")
    currency = _require_text(report["currency"], "actuals.report.currency")
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ConfigError("actuals.report.currency must be a three-letter uppercase code")

    sources = _require_mapping(root["sources"], "actuals.sources")
    source_keys = {"cashflow", "profit"}
    _check_keys(sources, source_keys, source_keys, "actuals.sources")
    cashflow_source = _require_text(
        sources["cashflow"], "actuals.sources.cashflow"
    )
    profit_source = _require_text(sources["profit"], "actuals.sources.profit")

    cashflow_raw = _require_mapping(root["cashflow"], "actuals.cashflow")
    cashflow_keys = {"opening_balance", "sections", "closing_balance"}
    _check_keys(
        cashflow_raw, cashflow_keys, cashflow_keys, "actuals.cashflow"
    )
    cashflow_sections = _parse_historical_sections(
        cashflow_raw["sections"], "actuals.cashflow.sections"
    )
    if {section.id for section in cashflow_sections} != ALLOWED_ACTIVITIES:
        raise ConfigError(
            "actuals.cashflow.sections must contain operating, investing, and financing"
        )
    cashflow = HistoricalCashflow(
        _parse_historical_series(
            cashflow_raw["opening_balance"], "actuals.cashflow.opening_balance"
        ),
        cashflow_sections,
        _parse_historical_series(
            cashflow_raw["closing_balance"], "actuals.cashflow.closing_balance"
        ),
    )

    profit_raw = _require_mapping(root["profit"], "actuals.profit")
    profit_keys = {"sections", "summaries"}
    _check_keys(profit_raw, profit_keys, profit_keys, "actuals.profit")
    profit_sections = _parse_historical_sections(
        profit_raw["sections"], "actuals.profit.sections"
    )
    if {section.id for section in profit_sections} != {"revenues", "expenses"}:
        raise ConfigError(
            "actuals.profit.sections must contain revenues and expenses"
        )
    profit_summaries = _parse_historical_rows(
        profit_raw["summaries"], "actuals.profit.summaries"
    )
    required_summaries = {
        "ebitda",
        "amortization",
        "financial",
        "taxes-and-fees",
        "net-profit",
    }
    if {row.id for row in profit_summaries} != required_summaries:
        raise ConfigError(
            "actuals.profit.summaries must contain EBITDA, amortization, financial, "
            "taxes-and-fees, and net-profit rows"
        )

    return ActualsReport(
        company_name,
        registration_number,
        start_month,
        end_month,
        currency,
        cashflow_source,
        profit_source,
        cashflow,
        HistoricalProfit(profit_sections, profit_summaries),
    )


def load_actuals(path: Path) -> ActualsReport:
    try:
        with path.open("r", encoding="utf-8") as source:
            raw = yaml.load(source, Loader=DecimalSafeLoader)
    except FileNotFoundError as exc:
        raise ConfigError(f"actuals file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc
    return validate_actuals(raw)


def vat_rate_at(rules: TaxRules, month: int) -> Decimal:
    return next(rate for effective, rate in reversed(rules.vat_rates) if effective <= month)


def _model_end(config: Config, start: int) -> int:
    dates = [config.actual_through, *config.dividends, *config.tax_payments, *config.taxes.checkpoints]
    dates.extend(month for row in config.rows for month in row.overrides)
    dates.extend(month for checkpoint in config.taxes.checkpoints.values() for month in checkpoint.payments)
    return min(timeline_end(config.settings), max(start + 12, max(dates) + 2))


def _net_actual_presentation(
    cells: dict[str, CellResult], rate: Decimal, currency: str,
) -> dict[str, CellResult]:
    """Present reported cash net, transferring its estimated VAT without losing cents."""
    displayed = dict(cells)
    with localcontext() as context:
        context.prec = _decimal_precision([cells[key].value for key in ("clients", "suppliers", "vat")]) + 20
        components = {}
        for row_id in ("clients", "suppliers"):
            original = cells[row_id]
            net = _rounded_money(original.value / (1 + rate))
            # Use the residual, not VAT recomputed from rounded net: each cash
            # payment must retain all its source cents during reclassification.
            components[row_id] = original.value - net
            deduction = "; supplier VAT assumed fully deductible" if row_id == "suppliers" else ""
            displayed[row_id] = CellResult(
                net, "actual-net-estimate", note=(
                    f"Estimated net presentation of closed actual: original reported cash "
                    f"{format_currency(original.value, currency)}; assumed VAT {rate:.0%}{deduction}; "
                    f"signed VAT portion {format_currency(components[row_id], currency)} moved to VAT. "
                    f"{original.note}"
                ),
            )
        original_vat = cells["vat"]
        displayed["vat"] = CellResult(
            original_vat.value + components["clients"] + components["suppliers"],
            "actual-net-estimate", note=(
                f"Estimated VAT presentation of closed actual: original reported VAT cash "
                f"{format_currency(original_vat.value, currency)} plus signed VAT portions from "
                f"Clients {format_currency(components['clients'], currency)} and "
                f"Suppliers {format_currency(components['suppliers'], currency)}. "
                f"Assumed VAT {rate:.0%}; supplier VAT assumed fully deductible. "
                f"{original_vat.note}"
            ),
        )
    return displayed


def calculate_projection(config: Config, start: str | None = None) -> Projection:
    """Evaluate the full dated timeline, then select a twelve-month view."""
    view_start = validate_window(config.settings, start)
    end = _model_end(config, view_start)
    # All accepted amounts have <=256 integer digits. Leave room for up to 1200
    # months of sums, currency conversion, and weighted tax calculations.
    with localcontext() as context:
        context.prec = MAX_MONEY_INTEGER_DIGITS + 50
        balance = config.settings.initial_balance
        carry = {row.id: row.seed for row in config.rows}
        carry_notes = {row.id: f"Starting assumption: {row.seed_source}" for row in config.rows}
        cells: dict[str, list[CellResult]] = {row.id: [] for row in config.rows}
        results: list[MonthResult] = []
        details: list[dict[str, str]] = []
        vat_credit = profit_loss = ZERO
        payments: dict[int, dict[str, Decimal]] = {}
        state_source = "Historical report; forecast tax state not yet initialized"
        tax_start = min(config.taxes.checkpoints)

        for month in range(config.settings.history_start, end):
            date = format_month(month)
            actual = config.actuals.get(month)
            checkpoint = config.taxes.checkpoints.get(month)
            if checkpoint:
                vat_credit, profit_loss = checkpoint.vat_credit, checkpoint.profit_loss
                payments = deepcopy(checkpoint.payments)
                state_source = f"{checkpoint.kind.title()} at {date}: {checkpoint.source}"
            amounts: dict[str, Decimal] = {}
            month_cells: dict[str, CellResult] = {}
            for row in config.rows:
                if actual:
                    value = actual.values[row.id]
                    basis_note = "Reported cash (VAT-inclusive where applicable)" if actual.basis == "reported" else "Net Clients/Suppliers; VAT row includes VAT cash activity"
                    if actual.basis == "reported" and row.id in {"clients", "suppliers"} and config.taxes.reported_cash_seed_basis == "net":
                        basis_note = "Reported amount assumed net by configuration"
                    if actual.basis == "reported" and row.id in {"shareholders", "dividends-paid"}:
                        basis_note += "; historical Shareholders is unsplit; Dividends Paid adds no separate reclassification"
                    month_cells[row.id] = CellResult(value, "actual", note=f"Actual: {actual.source}. {basis_note}. {actual.note}")
                    amounts[row.id] = value
                    if row.forecast == "carry":
                        carry[row.id] = value
                        carry_notes[row.id] = f"Carried from actual {date}: {actual.source}"
                        if actual.basis == "reported" and row.id in {"clients", "suppliers"}:
                            if config.taxes.reported_cash_seed_basis == "gross-standard":
                                carry[row.id] = _rounded_money(value / (1 + vat_rate_at(config.taxes, month)))
                                carry_notes[row.id] += "; estimated net conversion of reported cash at the standard VAT rate, not an accounting net figure"
                            else:
                                carry_notes[row.id] += "; reported amount assumed net by configuration"
                elif row.forecast in {"carry", "zero"}:
                    if month in row.overrides:
                        value = row.overrides[month]
                        note = "Manual override; clear this cell to restore estimation"
                        month_cells[row.id] = CellResult(value, "override", True, note, f"{value:.2f}")
                        if row.forecast == "carry":
                            carry[row.id], carry_notes[row.id] = value, f"Carried from override {date}"
                    else:
                        value = carry[row.id] if row.forecast == "carry" else ZERO
                        if value is None:
                            raise ConfigError(f"{row.id} needs an explicit net seed before forecasting {date}")
                        note = carry_notes[row.id] if row.forecast == "carry" else "No event planned; this row defaults to zero"
                        month_cells[row.id] = CellResult(value, "estimate", True, note)
                    amounts[row.id] = value
                    if row.id in {"clients", "suppliers"}:
                        current = month_cells[row.id]
                        month_cells[row.id] = CellResult(
                            current.value, current.provenance, current.editable,
                            current.note + "; net of VAT", current.override,
                        )

            tax_detail = {"month": date, "state_source": state_source}
            if month >= tax_start:
                rate = vat_rate_at(config.taxes, month)
                vat_activity = _rounded_money(amounts["clients"] * rate) + _rounded_money(amounts["suppliers"] * rate)
                vat_accrual = max(vat_activity - vat_credit, ZERO)
                vat_credit = max(vat_credit - vat_activity, ZERO)
                proxy = _rounded_money(_sum_money([
                    amounts[row.id] * row.profit_weight for row in config.rows if row.profit_weight
                ]))
                taxable = max(proxy - profit_loss, ZERO)
                profit_loss = max(profit_loss - proxy, ZERO)
                profit_accrual = _rounded_money(taxable * config.taxes.profit_rate)
                event = config.dividends.get(month)
                if actual and event:
                    planned_net = -(event.gross - _rounded_money(event.gross * config.taxes.dividend_rate))
                    if actual.values["dividends-paid"] != planned_net:
                        # Actual cash supersedes a cancelled/changed forecast event.
                        # Nonzero mismatches require explicit next-month state above.
                        event = None
                gross = event.gross if event else ZERO
                withholding = _rounded_money(gross * config.taxes.dividend_rate)
                next_due = payments.setdefault(month + 1, {})
                for component, amount in (("vat", vat_accrual), ("profit", profit_accrual), ("dividend", withholding)):
                    next_due[component] = next_due.get(component, ZERO) + amount
                due = {component: payments.get(month, {}).get(component, ZERO) for component in TAX_COMPONENTS}
                confirmed = config.tax_payments.get(month)
                if confirmed:
                    due.update(confirmed.amounts)
                total_tax = due.get("total", due["profit"] + due["dividend"] + due["other"])
                computed = {"vat": vat_activity - due["vat"], "taxes": -total_tax, "dividends-paid": -(gross - withholding)}
                numeric_details = {
                    "vat_activity": vat_activity, "vat_payment": due["vat"], "vat_credit": vat_credit,
                    "vat_accrued": vat_accrual, "profit_proxy": proxy, "profit_loss": profit_loss,
                    "profit_accrued": profit_accrual, "profit_payment": due["profit"],
                    "dividend_withholding": withholding, "dividend_payment": due["dividend"],
                    "other_payment": due["other"], "tax_payment_total": total_tax,
                }
                tax_detail.update({key: f"{value:.2f}" for key, value in numeric_details.items()})
                tax_detail["payment_source"] = confirmed.source if confirmed else "Estimated payment schedule"
                tax_detail["tax_components"] = "unallocated total" if "total" in due or actual else "scheduled components"
                if not actual:
                    amounts.update(computed)
                    vat_note = f"VAT activity {format_number(vat_activity)} minus remittance {format_number(due['vat'])}; credit carried {format_number(vat_credit)}. {state_source}"
                    if confirmed and "vat" in confirmed.amounts:
                        vat_note += f". Confirmed remittance: {confirmed.source}"
                    if confirmed and "total" in confirmed.amounts:
                        taxes_note = f"Accountant-confirmed total: {confirmed.source}; replaces estimated components"
                    else:
                        taxes_note = f"Profit tax {format_number(due['profit'])}; dividend tax {format_number(due['dividend'])}; other {format_number(due['other'])}. Cash-flow proxy estimate; {state_source}"
                        if confirmed:
                            taxes_note += f". Confirmed components: {confirmed.source}"
                    month_cells["vat"] = CellResult(computed["vat"], "derived", note=vat_note)
                    month_cells["taxes"] = CellResult(computed["taxes"], "confirmed" if confirmed and "total" in confirmed.amounts else "derived", note=taxes_note)
                    month_cells["dividends-paid"] = CellResult(computed["dividends-paid"], "derived", note=f"Gross dividend less withholding; {event.source}" if event else "No gross dividend event planned")
                else:
                    tax_detail["reported_tax_cash"] = f"{actual.values['taxes']:.2f}"
                    tax_detail["reported_vat_cash"] = f"{actual.values['vat']:.2f}"
                    tax_detail["payment_source"] = f"Actual cash rows take precedence: {actual.source}; carry state remains a proxy until an accounting checkpoint"

            movements = list(amounts.values())
            inflows = _sum_money([value for value in movements if value > ZERO])
            outflows = _sum_money([-value for value in movements if value < ZERO])
            net = inflows - outflows
            opening = actual.opening_balance if actual and actual.opening_balance is not None else balance
            closing = actual.closing_balance if actual else opening + net
            opening_note = f"Rolled forward from {format_month(month - 1)}" if month > config.settings.history_start else "Dated starting cash balance"
            closing_note = "Opening balance plus signed cash movements"
            if actual:
                opening_note = f"Reported opening balance: {actual.source}" if actual.opening_balance is not None else f"{actual.source}. " + opening_note
                closing_note = f"Reported closing balance: {actual.source}"
                if opening != balance:
                    opening_note += f". Source reconciliation: reported opening differs from preceding balance by {format_number(opening - balance, signed=True)} RON"
                if closing != opening + net:
                    closing_note += f". Source reconciliation: closing differs from displayed row roll-forward by {format_number(closing - opening - net, signed=True)} RON"
            if view_start <= month < view_start + 12:
                # Source cash drives reconciliation and run rates above. Only
                # its presentation is reclassified; no historical tax state is
                # inferred from this assumed VAT split.
                if actual and actual.basis == "reported" and config.taxes.reported_cash_seed_basis == "gross-standard":
                    month_cells = _net_actual_presentation(
                        month_cells, vat_rate_at(config.taxes, month), config.settings.currency,
                    )
                for row in config.rows:
                    cells[row.id].append(month_cells[row.id])
                results.append(MonthResult(month, opening, inflows, outflows, net, closing, "actual" if actual else "derived", opening_note, closing_note))
                details.append(tax_detail)
            balance = closing

    return Projection(config.settings, tuple(
        ProjectedRow(row.id, row.name, row.activity, tuple(cells[row.id])) for row in config.rows
    ), tuple(results), config.actual_through, tuple(details))


def _rounded_money(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _decimal_precision([value])
        rounded = value.quantize(CENT, rounding=ROUND_HALF_UP)
        return rounded if rounded else ZERO


def format_number(value: Decimal, *, signed: bool = False) -> str:
    rounded = _rounded_money(value)
    if signed and rounded > ZERO:
        return f"+{rounded:,.2f}"
    return f"{rounded:,.2f}"


def format_currency(value: Decimal, currency: str, *, signed: bool = False) -> str:
    rounded = _rounded_money(value)
    if rounded < ZERO:
        return f"-{currency} {rounded.copy_abs():,.2f}"
    if signed and rounded > ZERO:
        return f"+{currency} {rounded:,.2f}"
    return f"{currency} {rounded:,.2f}"


def print_summary(projection: Projection, stream: TextIO = sys.stdout) -> None:
    currency = projection.settings.currency
    print(f"Cash flow projection ({currency})", file=stream)
    print(
        f"{'Month':<9} {'Opening':>15} {'Inflows':>15} {'Outflows':>15} "
        f"{'Net':>15} {'Closing':>15}",
        file=stream,
    )
    print("-" * 89, file=stream)
    for month in projection.months:
        print(
            f"{format_month(month.month):<9} "
            f"{format_number(month.opening_balance):>15} "
            f"{format_number(month.inflows):>15} "
            f"{format_number(month.outflows):>15} "
            f"{format_number(month.net, signed=True):>15} "
            f"{format_number(month.closing_balance):>15}",
            file=stream,
        )

    trough = projection.trough
    average = projection.average_net
    if average > ZERO:
        average_label = "Average monthly gain"
        average_value = average
    elif average < ZERO:
        average_label = "Average monthly burn"
        average_value = average.copy_abs()
    else:
        average_label = "Average monthly change"
        average_value = ZERO

    print(file=stream)
    print(
        f"Starting balance: {format_currency(projection.months[0].opening_balance, currency)}",
        file=stream,
    )
    print(
        f"Lowest projected balance: {format_currency(trough.closing_balance, currency)} "
        f"({format_month(trough.month)})",
        file=stream,
    )
    print(
        f"Ending projected balance: {format_currency(projection.ending_balance, currency)}",
        file=stream,
    )
    print(
        f"{average_label}: {format_currency(average_value, currency)}",
        file=stream,
    )


def _sum_money(values: list[Decimal]) -> Decimal:
    if not values:
        return ZERO
    with localcontext() as context:
        context.prec = _decimal_precision(values, len(values))
        return sum(values, ZERO)


def _record_difference(
    differences: list[ReconciliationDifference],
    scope: str,
    period: str,
    difference: Decimal,
) -> None:
    if difference != ZERO:
        differences.append(ReconciliationDifference(scope, period, difference))


def _check_series_total(
    differences: list[ReconciliationDifference],
    scope: str,
    series: HistoricalSeries,
) -> None:
    _record_difference(
        differences,
        scope,
        "Total",
        series.total - _sum_money(list(series.months)),
    )


def reconcile_actuals(actuals: ActualsReport) -> tuple[ReconciliationDifference, ...]:
    differences: list[ReconciliationDifference] = []
    cashflow = actuals.cashflow

    _record_difference(
        differences,
        "cashflow opening balance",
        "Total",
        cashflow.opening_balance.total - cashflow.opening_balance.months[0],
    )
    _record_difference(
        differences,
        "cashflow closing balance",
        "Total",
        cashflow.closing_balance.total - cashflow.closing_balance.months[-1],
    )

    for section in cashflow.sections:
        _check_series_total(
            differences, f"cashflow {section.id} reported", section.reported
        )
        for row in section.rows:
            _check_series_total(
                differences, f"cashflow {section.id}/{row.id}", row.values
            )
        for index, reported in enumerate(section.reported.months):
            detail_total = _sum_money(
                [row.values.months[index] for row in section.rows]
            )
            _record_difference(
                differences,
                f"cashflow {section.id} category sum",
                format_month(actuals.start_month + index),
                reported - detail_total,
            )
        _record_difference(
            differences,
            f"cashflow {section.id} category sum",
            "Total",
            section.reported.total
            - _sum_money([row.values.total for row in section.rows]),
        )

    for index, closing_balance in enumerate(cashflow.closing_balance.months):
        activity = _sum_money(
            [section.reported.months[index] for section in cashflow.sections]
        )
        calculated_closing = cashflow.opening_balance.months[index] + activity
        period = format_month(actuals.start_month + index)
        _record_difference(
            differences,
            "cashflow reported closing",
            period,
            closing_balance - calculated_closing,
        )
        if index > 0:
            _record_difference(
                differences,
                "cashflow opening rollforward",
                period,
                cashflow.opening_balance.months[index]
                - cashflow.closing_balance.months[index - 1],
            )

    profit = actuals.profit
    for section in profit.sections:
        _check_series_total(
            differences, f"profit {section.id} reported", section.reported
        )
        for row in section.rows:
            _check_series_total(
                differences, f"profit {section.id}/{row.id}", row.values
            )
        for index, reported in enumerate(section.reported.months):
            detail_total = _sum_money(
                [row.values.months[index] for row in section.rows]
            )
            _record_difference(
                differences,
                f"profit {section.id} category sum",
                format_month(actuals.start_month + index),
                reported - detail_total,
            )
        _record_difference(
            differences,
            f"profit {section.id} category sum",
            "Total",
            section.reported.total
            - _sum_money([row.values.total for row in section.rows]),
        )

    sections = {section.id: section for section in profit.sections}
    summaries = {row.id: row for row in profit.summaries}
    for summary in profit.summaries:
        _check_series_total(
            differences, f"profit summary/{summary.id}", summary.values
        )

    for index in range(12):
        period = format_month(actuals.start_month + index)
        expected_ebitda = _sum_money(
            [
                sections["revenues"].reported.months[index],
                sections["expenses"].reported.months[index],
            ]
        )
        _record_difference(
            differences,
            "profit EBITDA formula",
            period,
            summaries["ebitda"].values.months[index] - expected_ebitda,
        )
        expected_net_profit = _sum_money(
            [
                summaries["ebitda"].values.months[index],
                summaries["amortization"].values.months[index],
                summaries["financial"].values.months[index],
                summaries["taxes-and-fees"].values.months[index],
            ]
        )
        _record_difference(
            differences,
            "profit net formula",
            period,
            summaries["net-profit"].values.months[index] - expected_net_profit,
        )

    expected_ebitda_total = _sum_money(
        [sections["revenues"].reported.total, sections["expenses"].reported.total]
    )
    _record_difference(
        differences,
        "profit EBITDA formula",
        "Total",
        summaries["ebitda"].values.total - expected_ebitda_total,
    )
    expected_net_profit_total = _sum_money(
        [
            summaries["ebitda"].values.total,
            summaries["amortization"].values.total,
            summaries["financial"].values.total,
            summaries["taxes-and-fees"].values.total,
        ]
    )
    _record_difference(
        differences,
        "profit net formula",
        "Total",
        summaries["net-profit"].values.total - expected_net_profit_total,
    )
    return tuple(differences)


def compare_cash_to_revenue(
    actuals: ActualsReport, vat_rates: tuple[Decimal, ...]
) -> tuple[dict[str, str], ...]:
    """Diagnostic evidence only: revenue and cash need not share payment timing."""
    if len(vat_rates) != 12:
        raise ConfigError("reference VAT comparison requires twelve monthly rates")
    clients = next(
        row for section in actuals.cashflow.sections
        for row in section.rows if row.id == "clients"
    )
    revenue = next(section for section in actuals.profit.sections if section.id == "revenues")
    result = []
    for index, rate in enumerate(vat_rates):
        cash = clients.values.months[index]
        net = revenue.reported.months[index]
        with localcontext() as context:
            context.prec = _decimal_precision([cash, net, rate]) + 20
            gross = _rounded_money(net * (1 + rate))
            result.append({
                "month": format_month(actuals.start_month + index),
                "clients_cash": format_number(cash),
                "revenue": format_number(net),
                "revenue_plus_vat": format_number(gross),
                "cash_minus_gross": format_number(cash - gross),
            })
    return tuple(result)


def print_actuals_summary(
    actuals: ActualsReport, stream: TextIO = sys.stdout
) -> None:
    sections = {section.id: section for section in actuals.cashflow.sections}
    currency = actuals.currency
    print(f"Historical cash flow reference ({currency})", file=stream)
    print(
        f"{'Month':<9} {'Opening':>15} {'Operating':>15} {'Investing':>15} "
        f"{'Financing':>15} {'Closing':>15}",
        file=stream,
    )
    print("-" * 89, file=stream)
    for index in range(12):
        print(
            f"{format_month(actuals.start_month + index):<9} "
            f"{format_number(actuals.cashflow.opening_balance.months[index]):>15} "
            f"{format_number(sections['operating'].reported.months[index], signed=True):>15} "
            f"{format_number(sections['investing'].reported.months[index], signed=True):>15} "
            f"{format_number(sections['financing'].reported.months[index], signed=True):>15} "
            f"{format_number(actuals.cashflow.closing_balance.months[index]):>15}",
            file=stream,
        )

    closing_values = actuals.cashflow.closing_balance.months
    trough_index = min(range(12), key=lambda index: closing_values[index])
    differences = reconcile_actuals(actuals)
    rounding_count = sum(
        1
        for difference in differences
        if abs(difference.difference) <= ACCOUNTING_ROUNDING_TOLERANCE
    )
    material_count = len(differences) - rounding_count
    print(file=stream)
    print(
        f"Lowest reported closing balance: "
        f"{format_currency(closing_values[trough_index], currency)} "
        f"({format_month(actuals.start_month + trough_index)})",
        file=stream,
    )
    print(
        f"Ending reported balance: {format_currency(closing_values[-1], currency)}",
        file=stream,
    )
    print(
        f"Source reconciliation differences: {rounding_count} rounding, "
        f"{material_count} material",
        file=stream,
    )


def _currency_values(
    value: Decimal, source_currency: str, ron_per_eur: Decimal
) -> tuple[Decimal, Decimal]:
    with localcontext() as context:
        context.prec = _decimal_precision([value, ron_per_eur]) + 20
        if source_currency == "RON":
            return value, value / ron_per_eur
        if source_currency == "EUR":
            return value * ron_per_eur, value
    raise ConfigError("dashboard currency must be 'RON' or 'EUR'")


def _money_cell(
    value: Decimal, source_currency: str, ron_per_eur: Decimal,
    *, provenance: str = "derived", editable: bool = False,
    note: str = "", override: str | None = None,
) -> MoneyCell:
    ron_value, eur_value = _currency_values(value, source_currency, ron_per_eur)
    return MoneyCell(f"{value:.2f}", format_number(ron_value), format_number(eur_value), provenance, editable, note, override)


def _report_row(
    row_id: str,
    name: str,
    values: tuple[Decimal, ...],
    source_currency: str,
    ron_per_eur: Decimal,
) -> ReportRow:
    return ReportRow(
        row_id,
        name,
        tuple(_money_cell(value, source_currency, ron_per_eur) for value in values),
    )


def _report_group(
    group_id: str,
    name: str,
    rows: list[tuple[str, str, tuple[Decimal, ...]]],
    source_currency: str,
    ron_per_eur: Decimal,
) -> ReportGroup:
    report_rows = tuple(
        _report_row(row_id, row_name, values, source_currency, ron_per_eur)
        for row_id, row_name, values in rows
    )
    subtotals = tuple(
        _sum_money([values[index] for _, _, values in rows]) for index in range(12)
    )
    return ReportGroup(
        group_id,
        name,
        report_rows,
        _report_row(f"subtotal-{group_id}", "Subtotal", subtotals, source_currency, ron_per_eur),
    )


def report_view(report: Projection | ActualsReport, ron_per_eur: Decimal) -> ReportView:
    """Return the engine-calculated values used by dashboard consumers."""
    if not ron_per_eur.is_finite() or ron_per_eur <= ZERO:
        raise ConfigError("ron_per_eur must be a positive finite number")

    if isinstance(report, Projection):
        source_currency = report.settings.currency
        months = tuple(format_month(month.month) for month in report.months)
        groups: list[ReportGroup] = []
        for activity in ACTIVITY_ORDER:
            rows = [
                (row.id, row.name, row.values)
                for row in report.rows
                if row.activity == activity
            ]
            group = _report_group(activity, activity.title(), rows, source_currency, ron_per_eur)
            report_rows = tuple(ReportRow(row.id, row.name, tuple(
                _money_cell(cell.value, source_currency, ron_per_eur,
                            provenance=cell.provenance, editable=cell.editable,
                            note=cell.note, override=cell.override)
                for cell in row.cells
            )) for row in report.rows if row.activity == activity)
            groups.append(ReportGroup(group.id, group.name, report_rows, group.subtotal))
        trough = report.trough
        first = report.months[0].month
        last_start = timeline_end(report.settings) - 12

        def balance_row(row_id: str, name: str, attribute: str, note_attribute: str) -> ReportRow:
            return ReportRow(row_id, name, tuple(
                _money_cell(getattr(month, attribute), source_currency, ron_per_eur,
                            provenance=month.provenance, note=getattr(month, note_attribute))
                for month in report.months
            ))

        return ReportView(
            "forecast",
            source_currency,
            months,
            balance_row("opening-balance", "Opening Balance", "opening_balance", "opening_note"),
            tuple(groups),
            balance_row("closing-balance", "Closing Balance", "closing_balance", "closing_note"),
            {
                "ending_balance": _money_cell(
                    report.ending_balance, source_currency, ron_per_eur
                ),
                "lowest_balance": _money_cell(
                    trough.closing_balance, source_currency, ron_per_eur
                ),
                "lowest_month": format_month(trough.month),
            },
            {
                "start": months[0], "end": months[-1],
                "previous": format_month(first - 1) if first > report.settings.history_start else None,
                "next": format_month(first + 1) if first < last_start else None,
                "min_start": format_month(report.settings.history_start),
                "max_start": format_month(last_start),
                "actual_through": format_month(report.actual_through) if report.actual_through >= report.settings.history_start else None,
            },
            report.tax_details,
        )

    source_currency = report.currency
    groups = tuple(
        _report_group(
            section.id,
            section.name,
            [
                (row.id, row.name, row.values.months)
                for row in section.rows
            ],
            source_currency,
            ron_per_eur,
        )
        for section in report.cashflow.sections
    )
    closing_values = report.cashflow.closing_balance.months
    trough_index = min(range(12), key=lambda index: closing_values[index])
    differences = reconcile_actuals(report)
    rounding_count = sum(
        1
        for difference in differences
        if abs(difference.difference) <= ACCOUNTING_ROUNDING_TOLERANCE
    )
    return ReportView(
        "actuals",
        source_currency,
        tuple(format_month(report.start_month + index) for index in range(12)),
        _report_row(
            "opening-balance",
            "Opening Balance",
            report.cashflow.opening_balance.months,
            source_currency,
            ron_per_eur,
        ),
        groups,
        _report_row(
            "closing-balance",
            "Closing Balance",
            closing_values,
            source_currency,
            ron_per_eur,
        ),
        {
            "ending_reported_balance": _money_cell(
                closing_values[-1], source_currency, ron_per_eur
            ),
            "lowest_reported_balance": _money_cell(
                closing_values[trough_index], source_currency, ron_per_eur
            ),
            "lowest_month": format_month(report.start_month + trough_index),
            "rounding_reconciliation_count": rounding_count,
            "material_reconciliation_count": len(differences) - rounding_count,
        },
    )


def _render_amount_cell(
    cell: MoneyCell, source_currency: str
) -> str:
    value_class = " negative" if cell.source.startswith("-") else ""
    ron_text = html.escape(cell.ron, quote=True)
    eur_text = html.escape(cell.eur, quote=True)
    visible_text = ron_text if source_currency == "RON" else eur_text
    return (
        f'<td class="amount{value_class}" data-ron="{ron_text}" '
        f'data-eur="{eur_text}" data-provenance="{html.escape(cell.provenance, quote=True)}" '
        f'title="{html.escape(cell.note, quote=True)}">{visible_text}</td>'
    )


def _render_balance_body(
    row: ReportRow, source_currency: str
) -> str:
    row_class = "opening-row" if row.id == "opening-balance" else "closing-row"
    return (
        '<tbody class="balance-section">'
        f'<tr class="balance-row {row_class}"><th scope="row">{html.escape(row.name)}</th>'
        + "".join(
            _render_amount_cell(cell, source_currency) for cell in row.cells
        )
        + "</tr></tbody>"
    )


def _render_activity_group(
    group: ReportGroup, source_currency: str
) -> str:
    escaped_id = html.escape(group.id, quote=True)
    escaped_name = html.escape(group.name)
    heading = (
        '<tbody class="activity-heading">'
        f'<tr class="activity-heading-row {escaped_id}-heading">'
        '<th scope="row">'
        f'<button class="group-toggle" type="button" aria-expanded="true" '
        f'aria-controls="group-{escaped_id}-children">'
        '<span class="toggle-mark" aria-hidden="true">-</span>'
        f"<span>{escaped_name}</span></button></th>"
        + '<td class="activity-fill" aria-hidden="true"></td>' * 12
        + "</tr></tbody>"
    )
    children = (
        f'<tbody class="activity-children" id="group-{escaped_id}-children">'
        + "".join(
            f'<tr class="subcategory-row {escaped_id}-row">'
            f'<th scope="row">{html.escape(row.name)}</th>'
            + "".join(
                _render_amount_cell(cell, source_currency) for cell in row.cells
            )
            + "</tr>"
            for row in group.rows
        )
        + "</tbody>"
    )
    subtotal = (
        '<tbody class="activity-subtotal">'
        f'<tr class="subtotal-row {escaped_id}-subtotal">'
        f'<th scope="row">{html.escape(group.subtotal.name)}</th>'
        + "".join(
            _render_amount_cell(cell, source_currency) for cell in group.subtotal.cells
        )
        + "</tr></tbody>"
    )
    return heading + children + subtotal


def _render_report_rows(view: ReportView) -> str:
    return "".join(
        [
            _render_balance_body(view.opening_balance, view.currency),
            *[
                _render_activity_group(group, view.currency)
                for group in view.activity_groups
            ],
            _render_balance_body(view.closing_balance, view.currency),
        ]
    )


def render_dashboard(
    report: Projection | ActualsReport,
    template_path: Path,
    ron_per_eur: Decimal,
) -> str:
    try:
        template = Template(template_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"template file not found: {template_path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {template_path}: {exc}") from exc

    view = report_view(report, ron_per_eur)
    source_currency = view.currency
    first_month = view.months[0]
    last_month = view.months[-1]
    table_rows = _render_report_rows(view)
    if source_currency not in {"RON", "EUR"}:
        raise ConfigError("dashboard currency must be 'RON' or 'EUR'")
    values = {
        "start_month": html.escape(first_month),
        "end_month": html.escape(last_month),
        "initial_currency": source_currency,
        "ron_pressed": str(source_currency == "RON").lower(),
        "eur_pressed": str(source_currency == "EUR").lower(),
        "table_rows": table_rows,
        "month_headers": "".join(f'<th scope="col">{month}</th>' for month in view.months),
    }
    return template.substitute(values)


def write_dashboard(content: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def run(
    input_path: Path,
    template_path: Path,
    output_path: Path,
    stream: TextIO = sys.stdout,
) -> Projection:
    config = load_config(input_path)
    result = evaluate_config(config, input_path)

    dashboard = render_dashboard(result, template_path, config.settings.ron_per_eur)
    write_dashboard(dashboard, output_path)
    print_summary(result, stream)
    print(f"Dashboard written: {output_path}", file=stream)
    return result


def evaluate_config(config: Config, input_path: Path | None = None, start: str | None = None) -> Projection:
    """Evaluate validated configuration without rendering, writing, or printing."""
    return calculate_projection(config, start)


def main() -> int:
    project_directory = Path(__file__).resolve().parent
    try:
        run(
            project_directory / "cashflow.yaml",
            project_directory / "template.html",
            project_directory / "dashboard.html",
        )
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error writing dashboard: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
