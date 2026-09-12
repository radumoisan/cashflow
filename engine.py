#!/usr/bin/env python3
"""Deterministic monthly cash-flow projection and dashboard generator."""

from __future__ import annotations

import html
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
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
ALLOWED_TYPES = {"inflow", "outflow"}
ACTIVITY_ORDER = ("operating", "investing", "financing")
ALLOWED_ACTIVITIES = set(ACTIVITY_ORDER)
ALLOWED_DASHBOARD_MODES = {"actuals", "projection"}


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
    dashboard_mode: str
    actuals_file: str


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    activity: str


@dataclass(frozen=True)
class RecurringEntry:
    id: str
    name: str
    type: str
    amount: Decimal
    start_month: int
    end_month: int | None
    category: str


@dataclass(frozen=True)
class EventEntry:
    id: str
    name: str
    type: str
    amount: Decimal
    month: int
    category: str


@dataclass(frozen=True)
class Config:
    settings: Settings
    categories: tuple[Category, ...]
    recurring: tuple[RecurringEntry, ...]
    events: tuple[EventEntry, ...]


@dataclass(frozen=True)
class Contribution:
    id: str
    name: str
    type: str
    amount: Decimal
    category_id: str
    category: str
    activity: str
    kind: str


@dataclass(frozen=True)
class MonthResult:
    month: int
    opening_balance: Decimal
    inflows: Decimal
    outflows: Decimal
    net: Decimal
    closing_balance: Decimal
    contributions: tuple[Contribution, ...]


@dataclass(frozen=True)
class Projection:
    settings: Settings
    categories: tuple[Category, ...]
    months: tuple[MonthResult, ...]

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


def _parse_common_entry(
    value: dict[str, Any], path: str
) -> tuple[str, str, str, Decimal, str]:
    entry_id = _require_text(value["id"], f"{path}.id")
    name = _require_text(value["name"], f"{path}.name")
    entry_type = value["type"]
    if not isinstance(entry_type, str) or entry_type not in ALLOWED_TYPES:
        raise ConfigError(f"{path}.type must be 'inflow' or 'outflow'")
    amount = _require_decimal(value["amount"], f"{path}.amount")
    if amount <= ZERO:
        raise ConfigError(f"{path}.amount must be positive")
    category = _require_text(value["category"], f"{path}.category")
    return entry_id, name, entry_type, amount, category


def validate_config(raw: Any) -> Config:
    root = _require_mapping(raw, "cashflow")
    root_keys = {"settings", "categories", "recurring", "events"}
    _check_keys(root, root_keys, root_keys, "cashflow")

    settings_raw = _require_mapping(root["settings"], "settings")
    settings_keys = {
        "company_name",
        "registration_number",
        "currency",
        "ron_per_eur",
        "start_date",
        "projection_months",
        "initial_balance",
        "dashboard_mode",
        "actuals_file",
    }
    _check_keys(settings_raw, settings_keys, settings_keys, "settings")

    company_name = _require_text(
        settings_raw["company_name"], "settings.company_name"
    )
    registration_number = _require_text(
        settings_raw["registration_number"], "settings.registration_number"
    )
    currency = _require_text(settings_raw["currency"], "settings.currency")
    if currency not in {"RON", "EUR"}:
        raise ConfigError("settings.currency must be 'RON' or 'EUR'")
    ron_per_eur = _require_decimal(
        settings_raw["ron_per_eur"], "settings.ron_per_eur"
    )
    if ron_per_eur <= ZERO:
        raise ConfigError("settings.ron_per_eur must be positive")
    start_month = parse_month(settings_raw["start_date"], "settings.start_date")
    projection_months = settings_raw["projection_months"]
    if (
        isinstance(projection_months, bool)
        or not isinstance(projection_months, int)
        or projection_months != 12
    ):
        raise ConfigError("settings.projection_months must be exactly 12")
    final_year = (start_month + projection_months - 1) // 12
    if final_year > 9999:
        raise ConfigError("settings projection window must end by 9999-12")
    initial_balance = _require_decimal(
        settings_raw["initial_balance"], "settings.initial_balance"
    )
    dashboard_mode = settings_raw["dashboard_mode"]
    if (
        not isinstance(dashboard_mode, str)
        or dashboard_mode not in ALLOWED_DASHBOARD_MODES
    ):
        raise ConfigError("settings.dashboard_mode must be 'actuals' or 'projection'")
    actuals_file = _require_text(settings_raw["actuals_file"], "settings.actuals_file")
    if Path(actuals_file).is_absolute() or ".." in Path(actuals_file).parts:
        raise ConfigError("settings.actuals_file must be a relative path inside the project")

    categories: list[Category] = []
    category_keys = {"id", "name", "activity"}
    for index, item in enumerate(_require_list(root["categories"], "categories")):
        path = f"categories[{index}]"
        category = _require_mapping(item, path)
        _check_keys(category, category_keys, category_keys, path)
        category_id = _require_text(category["id"], f"{path}.id")
        category_name = _require_text(category["name"], f"{path}.name")
        activity = category["activity"]
        if not isinstance(activity, str) or activity not in ALLOWED_ACTIVITIES:
            raise ConfigError(
                f"{path}.activity must be 'operating', 'investing', or 'financing'"
            )
        categories.append(Category(category_id, category_name, activity))

    category_ids = [category.id for category in categories]
    duplicate_category_ids = sorted(
        category_id
        for category_id, count in Counter(category_ids).items()
        if count > 1
    )
    if duplicate_category_ids:
        raise ConfigError(
            f"category ids must be unique: {', '.join(duplicate_category_ids)}"
        )
    known_categories = set(category_ids)

    recurring_entries: list[RecurringEntry] = []
    recurring_keys = {
        "id",
        "name",
        "type",
        "amount",
        "start_date",
        "end_date",
        "category",
    }
    for index, item in enumerate(_require_list(root["recurring"], "recurring")):
        path = f"recurring[{index}]"
        entry = _require_mapping(item, path)
        _check_keys(entry, recurring_keys, recurring_keys, path)
        entry_id, name, entry_type, amount, category = _parse_common_entry(
            entry, path
        )
        if category not in known_categories:
            raise ConfigError(f"{path}.category references unknown category {category!r}")
        entry_start = parse_month(entry["start_date"], f"{path}.start_date")
        end_value = entry["end_date"]
        entry_end = (
            None
            if end_value is None
            else parse_month(end_value, f"{path}.end_date")
        )
        if entry_end is not None and entry_end < entry_start:
            raise ConfigError(f"{path}.end_date cannot be before start_date")
        recurring_entries.append(
            RecurringEntry(
                entry_id,
                name,
                entry_type,
                amount,
                entry_start,
                entry_end,
                category,
            )
        )

    event_entries: list[EventEntry] = []
    event_keys = {"id", "name", "type", "amount", "date", "category"}
    for index, item in enumerate(_require_list(root["events"], "events")):
        path = f"events[{index}]"
        entry = _require_mapping(item, path)
        _check_keys(entry, event_keys, event_keys, path)
        entry_id, name, entry_type, amount, category = _parse_common_entry(
            entry, path
        )
        if category not in known_categories:
            raise ConfigError(f"{path}.category references unknown category {category!r}")
        event_entries.append(
            EventEntry(
                entry_id,
                name,
                entry_type,
                amount,
                parse_month(entry["date"], f"{path}.date"),
                category,
            )
        )

    all_ids = [entry.id for entry in recurring_entries]
    all_ids.extend(entry.id for entry in event_entries)
    duplicate_ids = sorted(
        entry_id for entry_id, count in Counter(all_ids).items() if count > 1
    )
    if duplicate_ids:
        raise ConfigError(f"entry ids must be unique: {', '.join(duplicate_ids)}")

    return Config(
        Settings(
            company_name,
            registration_number,
            currency,
            ron_per_eur,
            start_month,
            projection_months,
            initial_balance,
            dashboard_mode,
            actuals_file,
        ),
        tuple(categories),
        tuple(recurring_entries),
        tuple(event_entries),
    )


def load_config(path: Path) -> Config:
    try:
        with path.open("r", encoding="utf-8") as source:
            raw = yaml.load(source, Loader=DecimalSafeLoader)
    except FileNotFoundError as exc:
        raise ConfigError(f"input file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc
    return validate_config(raw)


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


def calculate_projection(config: Config) -> Projection:
    categories_by_id = {category.id: category for category in config.categories}
    monetary_values = [config.settings.initial_balance]
    monetary_values.extend(entry.amount for entry in config.recurring)
    monetary_values.extend(entry.amount for entry in config.events)
    maximum_terms = 1 + config.settings.projection_months * (
        len(config.recurring) + len(config.events)
    )

    with localcontext() as context:
        context.prec = _decimal_precision(monetary_values, maximum_terms)
        balance = config.settings.initial_balance
        results: list[MonthResult] = []

        for offset in range(config.settings.projection_months):
            month = config.settings.start_month + offset
            contributions: list[Contribution] = []

            for entry in config.recurring:
                if entry.start_month <= month and (
                    entry.end_month is None or entry.end_month >= month
                ):
                    category = categories_by_id[entry.category]
                    contributions.append(
                        Contribution(
                            entry.id,
                            entry.name,
                            entry.type,
                            entry.amount,
                            category.id,
                            category.name,
                            category.activity,
                            "Recurring",
                        )
                    )

            for entry in config.events:
                if entry.month == month:
                    category = categories_by_id[entry.category]
                    contributions.append(
                        Contribution(
                            entry.id,
                            entry.name,
                            entry.type,
                            entry.amount,
                            category.id,
                            category.name,
                            category.activity,
                            "One-off event",
                        )
                    )

            inflows = sum(
                (item.amount for item in contributions if item.type == "inflow"),
                ZERO,
            )
            outflows = sum(
                (item.amount for item in contributions if item.type == "outflow"),
                ZERO,
            )
            net = inflows - outflows
            closing_balance = balance + net
            results.append(
                MonthResult(
                    month,
                    balance,
                    inflows,
                    outflows,
                    net,
                    closing_balance,
                    tuple(contributions),
                )
            )
            balance = closing_balance

    return Projection(config.settings, config.categories, tuple(results))


def _rounded_money(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _decimal_precision([value])
        return value.quantize(CENT, rounding=ROUND_HALF_UP)


def format_number(value: Decimal, *, signed: bool = False) -> str:
    rounded = _rounded_money(value)
    if signed and rounded > ZERO:
        return f"+{rounded:,.2f}"
    return f"{rounded:,.2f}"


def format_currency(value: Decimal, currency: str, *, signed: bool = False) -> str:
    rounded = _rounded_money(value)
    if rounded < ZERO:
        return f"-{currency} {abs(rounded):,.2f}"
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
        average_value = abs(average)
    else:
        average_label = "Average monthly change"
        average_value = ZERO

    print(file=stream)
    print(
        f"Starting balance: {format_currency(projection.settings.initial_balance, currency)}",
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


def _render_amount_cell(
    value: Decimal, source_currency: str, ron_per_eur: Decimal
) -> str:
    value_class = " negative" if value < ZERO else ""
    ron_value, eur_value = _currency_values(value, source_currency, ron_per_eur)
    ron_text = html.escape(format_number(ron_value), quote=True)
    eur_text = html.escape(format_number(eur_value), quote=True)
    visible_text = ron_text if source_currency == "RON" else eur_text
    return (
        f'<td class="amount{value_class}" data-ron="{ron_text}" '
        f'data-eur="{eur_text}">{visible_text}</td>'
    )


def _render_balance_body(
    label: str,
    values: tuple[Decimal, ...],
    source_currency: str,
    ron_per_eur: Decimal,
) -> str:
    row_class = "opening-row" if label == "Opening Balance" else "closing-row"
    return (
        '<tbody class="balance-section">'
        f'<tr class="balance-row {row_class}"><th scope="row">{html.escape(label)}</th>'
        + "".join(
            _render_amount_cell(value, source_currency, ron_per_eur)
            for value in values
        )
        + "</tr></tbody>"
    )


def _render_activity_group(
    activity_id: str,
    activity_name: str,
    category_rows: list[tuple[str, tuple[Decimal, ...]]],
    source_currency: str,
    ron_per_eur: Decimal,
) -> str:
    escaped_id = html.escape(activity_id, quote=True)
    escaped_name = html.escape(activity_name)
    subtotals = tuple(
        _sum_money([values[index] for _, values in category_rows])
        for index in range(12)
    )
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
            f'<th scope="row">{html.escape(category_name)}</th>'
            + "".join(
                _render_amount_cell(value, source_currency, ron_per_eur)
                for value in values
            )
            + "</tr>"
            for category_name, values in category_rows
        )
        + "</tbody>"
    )
    subtotal = (
        '<tbody class="activity-subtotal">'
        f'<tr class="subtotal-row {escaped_id}-subtotal">'
        '<th scope="row">Subtotal</th>'
        + "".join(
            _render_amount_cell(value, source_currency, ron_per_eur)
            for value in subtotals
        )
        + "</tr></tbody>"
    )
    return heading + children + subtotal


def _render_projection_rows(
    projection: Projection, ron_per_eur: Decimal
) -> str:
    source_currency = projection.settings.currency
    sections = [
        _render_balance_body(
            "Opening Balance",
            tuple(month.opening_balance for month in projection.months),
            source_currency,
            ron_per_eur,
        )
    ]
    for activity in ACTIVITY_ORDER:
        category_rows: list[tuple[str, tuple[Decimal, ...]]] = []
        for category in projection.categories:
            if category.activity != activity:
                continue
            values = tuple(
                _sum_money(
                    [
                        contribution.amount
                        if contribution.type == "inflow"
                        else -contribution.amount
                        for contribution in month.contributions
                        if contribution.category_id == category.id
                    ]
                )
                for month in projection.months
            )
            category_rows.append((category.name, values))
        sections.append(
            _render_activity_group(
                activity,
                activity.title(),
                category_rows,
                source_currency,
                ron_per_eur,
            )
        )

    sections.append(
        _render_balance_body(
            "Closing Balance",
            tuple(month.closing_balance for month in projection.months),
            source_currency,
            ron_per_eur,
        )
    )
    return "".join(sections)


def _render_actuals_rows(
    actuals: ActualsReport, ron_per_eur: Decimal
) -> str:
    sections = [
        _render_balance_body(
            "Opening Balance",
            actuals.cashflow.opening_balance.months,
            actuals.currency,
            ron_per_eur,
        )
    ]
    for section in actuals.cashflow.sections:
        sections.append(
            _render_activity_group(
                section.id,
                section.name,
                [(row.name, row.values.months) for row in section.rows],
                actuals.currency,
                ron_per_eur,
            )
        )
    sections.append(
        _render_balance_body(
            "Closing Balance",
            actuals.cashflow.closing_balance.months,
            actuals.currency,
            ron_per_eur,
        )
    )
    return "".join(sections)


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

    if not ron_per_eur.is_finite() or ron_per_eur <= ZERO:
        raise ConfigError("ron_per_eur must be a positive finite number")

    if isinstance(report, ActualsReport):
        source_currency = report.currency
        first_month = format_month(report.start_month)
        last_month = format_month(report.end_month)
        table_rows = _render_actuals_rows(report, ron_per_eur)
    else:
        source_currency = report.settings.currency
        first_month = format_month(report.months[0].month)
        last_month = format_month(report.months[-1].month)
        table_rows = _render_projection_rows(report, ron_per_eur)
    if source_currency not in {"RON", "EUR"}:
        raise ConfigError("dashboard currency must be 'RON' or 'EUR'")
    values = {
        "start_month": html.escape(first_month),
        "end_month": html.escape(last_month),
        "initial_currency": source_currency,
        "ron_pressed": str(source_currency == "RON").lower(),
        "eur_pressed": str(source_currency == "EUR").lower(),
        "table_rows": table_rows,
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
) -> Projection | ActualsReport:
    config = load_config(input_path)
    if config.settings.dashboard_mode == "actuals":
        actuals_path = (input_path.parent / config.settings.actuals_file).resolve()
        actuals = load_actuals(actuals_path)
        expected_end_month = config.settings.start_month + 11
        if (
            actuals.company_name != config.settings.company_name
            or actuals.registration_number != config.settings.registration_number
            or actuals.currency != config.settings.currency
            or actuals.start_month != config.settings.start_month
            or actuals.end_month != expected_end_month
        ):
            raise ConfigError(
                "settings metadata must match the selected accounting actuals report"
            )
        result: Projection | ActualsReport = actuals
    else:
        projection = calculate_projection(config)
        result = projection

    dashboard = render_dashboard(result, template_path, config.settings.ron_per_eur)
    write_dashboard(dashboard, output_path)
    if isinstance(result, ActualsReport):
        print_actuals_summary(result, stream)
    else:
        print_summary(result, stream)
    print(f"Dashboard written: {output_path}", file=stream)
    return result


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
