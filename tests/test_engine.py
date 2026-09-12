from __future__ import annotations

import io
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from engine import (
    ACCOUNTING_ROUNDING_TOLERANCE,
    ActualsReport,
    ConfigError,
    calculate_projection,
    format_month,
    load_actuals,
    load_config,
    print_summary,
    reconcile_actuals,
    render_dashboard,
    run,
    validate_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "template.html"
ACTUALS_PATH = PROJECT_ROOT / "accounting" / "actuals-2025.yaml"


def base_config() -> dict:
    return {
        "settings": {
            "company_name": "PLANEMO SOFTWARE LABS S.R.L.",
            "registration_number": "42131419",
            "currency": "EUR",
            "ron_per_eur": Decimal("5.25"),
            "start_date": "2026-12",
            "projection_months": 12,
            "initial_balance": 100,
            "dashboard_mode": "projection",
            "actuals_file": "accounting/actuals-2025.yaml",
        },
        "categories": [
            {"id": "sales", "name": "Sales", "activity": "operating"},
            {"id": "tools", "name": "Tools", "activity": "operating"},
            {"id": "capex", "name": "Capex", "activity": "investing"},
        ],
        "recurring": [
            {
                "id": "revenue",
                "name": "Revenue",
                "type": "inflow",
                "amount": 10,
                "start_date": "2026-12",
                "end_date": "2027-01",
                "category": "sales",
            },
            {
                "id": "software",
                "name": "Software",
                "type": "outflow",
                "amount": 3,
                "start_date": "2027-01",
                "end_date": None,
                "category": "tools",
            },
        ],
        "events": [
            {
                "id": "purchase",
                "name": "Purchase",
                "type": "outflow",
                "amount": 5,
                "date": "2027-01",
                "category": "capex",
            }
        ],
    }


class ProjectionTests(unittest.TestCase):
    def test_rolls_balances_and_honors_inclusive_boundaries(self) -> None:
        projection = calculate_projection(validate_config(base_config()))

        self.assertEqual(
            [format_month(month.month) for month in projection.months[:3]],
            ["2026-12", "2027-01", "2027-02"],
        )
        self.assertEqual(
            [month.inflows for month in projection.months[:3]],
            [Decimal("10"), Decimal("10"), Decimal("0")],
        )
        self.assertEqual(
            [month.outflows for month in projection.months[:3]],
            [Decimal("0"), Decimal("8"), Decimal("3")],
        )
        self.assertEqual(
            [month.closing_balance for month in projection.months[:3]],
            [Decimal("110"), Decimal("112"), Decimal("109")],
        )
        self.assertEqual(format_month(projection.trough.month), "2027-11")
        self.assertEqual(projection.ending_balance, Decimal("82"))
        self.assertEqual(projection.average_net, Decimal("-1.5"))

    def test_loader_preserves_decimal_values(self) -> None:
        source = """\
settings:
  company_name: PLANEMO SOFTWARE LABS S.R.L.
  registration_number: "42131419"
  currency: EUR
  ron_per_eur: 5.25
  start_date: "2026-01"
  projection_months: 12
  initial_balance: 0.00
  dashboard_mode: projection
  actuals_file: accounting/actuals-2025.yaml
categories:
  - id: test
    name: Test
    activity: operating
recurring:
  - id: precise
    name: Precise income
    type: inflow
    amount: 0.10
    start_date: "2026-01"
    end_date: "2026-03"
    category: test
events: []
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cashflow.yaml"
            path.write_text(source, encoding="utf-8")
            config = load_config(path)

        self.assertIsInstance(config.recurring[0].amount, Decimal)
        self.assertEqual(config.recurring[0].amount, Decimal("0.10"))
        self.assertIsInstance(config.settings.ron_per_eur, Decimal)
        self.assertEqual(config.settings.ron_per_eur, Decimal("5.25"))
        projection = calculate_projection(config)
        self.assertEqual(projection.ending_balance, Decimal("0.30"))

    def test_large_values_do_not_lose_small_movements(self) -> None:
        raw = base_config()
        raw["settings"]["initial_balance"] = Decimal(
            "1234567890123456789012345678.12"
        )
        raw["recurring"] = [
            {
                "id": "cent",
                "name": "Cent",
                "type": "inflow",
                "amount": Decimal("0.01"),
                "start_date": "2026-12",
                "end_date": None,
                "category": "sales",
            }
        ]
        raw["events"] = []

        projection = calculate_projection(validate_config(raw))

        self.assertEqual(
            projection.ending_balance,
            Decimal("1234567890123456789012345678.24"),
        )

    def test_zero_with_extreme_exponent_is_canonicalized(self) -> None:
        raw = base_config()
        raw["settings"]["initial_balance"] = Decimal("0e-999999999")

        projection = calculate_projection(validate_config(raw))

        self.assertEqual(projection.months[0].opening_balance, Decimal("0"))

    def test_summary_exposes_llm_report_values(self) -> None:
        projection = calculate_projection(validate_config(base_config()))
        output = io.StringIO()
        print_summary(projection, output)

        summary = output.getvalue()
        self.assertIn("Lowest projected balance: EUR 82.00 (2027-11)", summary)
        self.assertIn("Ending projected balance: EUR 82.00", summary)
        self.assertIn("Average monthly burn: EUR 1.50", summary)


class ValidationTests(unittest.TestCase):
    def assert_config_error(self, raw: dict, message: str) -> None:
        with self.assertRaisesRegex(ConfigError, message):
            validate_config(raw)

    def test_rejects_invalid_dates(self) -> None:
        raw = base_config()
        raw["settings"]["start_date"] = "2026-13"
        self.assert_config_error(raw, "valid calendar month")

    def test_rejects_non_positive_amounts(self) -> None:
        raw = base_config()
        raw["recurring"][0]["amount"] = 0
        self.assert_config_error(raw, "amount must be positive")

        raw = base_config()
        raw["settings"]["ron_per_eur"] = 0
        self.assert_config_error(raw, "ron_per_eur must be positive")

    def test_rejects_invalid_recurring_range(self) -> None:
        raw = base_config()
        raw["recurring"][0]["end_date"] = "2026-11"
        self.assert_config_error(raw, "cannot be before start_date")

    def test_rejects_duplicate_ids_across_entry_types(self) -> None:
        raw = base_config()
        raw["events"][0]["id"] = "revenue"
        self.assert_config_error(raw, "entry ids must be unique: revenue")

    def test_rejects_unknown_fields(self) -> None:
        raw = base_config()
        raw["settings"]["starting_date"] = "2026-01"
        self.assert_config_error(raw, "unknown field.*starting_date")

    def test_rejects_non_string_fields_and_types_cleanly(self) -> None:
        non_string_field = base_config()
        non_string_field["settings"][1] = "invalid"
        self.assert_config_error(non_string_field, "field names must be strings")

        unhashable_type = base_config()
        unhashable_type["events"][0]["type"] = []
        self.assert_config_error(unhashable_type, "must be 'inflow' or 'outflow'")

    def test_loader_rejects_duplicate_yaml_keys(self) -> None:
        source = """\
settings:
  currency: EUR
  currency: USD
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cashflow.yaml"
            path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "duplicate mapping key 'currency'"):
                load_config(path)

    def test_loader_rejects_yaml_merge_keys(self) -> None:
        source = """\
defaults: &defaults
  currency: EUR
settings:
  <<: *defaults
  currency: USD
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cashflow.yaml"
            path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "YAML merge keys are not supported"):
                load_config(path)

    def test_rejects_unsupported_money_precision_and_magnitude(self) -> None:
        excessive_scale = base_config()
        excessive_scale["recurring"][0]["amount"] = Decimal("0.001")
        self.assert_config_error(excessive_scale, "at most two decimal places")

        excessive_magnitude = base_config()
        excessive_magnitude["settings"]["initial_balance"] = Decimal("1e256")
        self.assert_config_error(excessive_magnitude, "at most 256 integer digits")

    def test_rejects_invalid_type_and_projection_length(self) -> None:
        invalid_type = base_config()
        invalid_type["events"][0]["type"] = "expense"
        self.assert_config_error(invalid_type, "must be 'inflow' or 'outflow'")

        invalid_length = base_config()
        invalid_length["settings"]["projection_months"] = 0
        self.assert_config_error(invalid_length, "must be exactly 12")

    def test_rejects_invalid_mode_activity_and_category_reference(self) -> None:
        invalid_mode = base_config()
        invalid_mode["settings"]["dashboard_mode"] = []
        self.assert_config_error(invalid_mode, "must be 'actuals' or 'projection'")

        invalid_currency = base_config()
        invalid_currency["settings"]["currency"] = "USD"
        self.assert_config_error(invalid_currency, "must be 'RON' or 'EUR'")

        invalid_activity = base_config()
        invalid_activity["categories"][0]["activity"] = "sales"
        self.assert_config_error(invalid_activity, "must be 'operating', 'investing'")

        unknown_category = base_config()
        unknown_category["events"][0]["category"] = "unknown"
        self.assert_config_error(unknown_category, "references unknown category")


class AccountingActualsTests(unittest.TestCase):
    def test_loads_authoritative_2025_reports(self) -> None:
        actuals = load_actuals(ACTUALS_PATH)

        self.assertEqual(actuals.company_name, "PLANEMO SOFTWARE LABS S.R.L.")
        self.assertEqual(actuals.registration_number, "42131419")
        self.assertEqual(actuals.currency, "RON")
        self.assertEqual(format_month(actuals.start_month), "2025-01")
        self.assertEqual(format_month(actuals.end_month), "2025-12")
        self.assertEqual(len(actuals.cashflow.opening_balance.months), 12)
        self.assertEqual(actuals.cashflow.closing_balance.total, Decimal("3758"))

    def test_reconciliation_limits_material_differences_to_cash_rollforward(self) -> None:
        differences = reconcile_actuals(load_actuals(ACTUALS_PATH))
        material = [
            difference
            for difference in differences
            if abs(difference.difference) > ACCOUNTING_ROUNDING_TOLERANCE
        ]

        self.assertTrue(material)
        self.assertEqual(len(material), 2)
        self.assertEqual(
            {difference.scope for difference in material},
            {"cashflow opening rollforward"},
        )
        self.assertEqual(
            {difference.period for difference in material},
            {"2025-04", "2025-05"},
        )

    def test_actuals_dashboard_uses_reported_cash_rows(self) -> None:
        dashboard = render_dashboard(
            load_actuals(ACTUALS_PATH), TEMPLATE_PATH, Decimal("5.25")
        )

        self.assertEqual(dashboard.count('scope="col"'), 13)
        self.assertNotIn("PLANEMO SOFTWARE LABS S.R.L. - 42131419", dashboard)
        self.assertNotIn("Historical actuals reference", dashboard)
        self.assertNotIn("<header>", dashboard)
        self.assertNotIn("<h1>", dashboard)
        self.assertNotIn("Start:", dashboard)
        self.assertIn('<th scope="row">Clients</th>', dashboard)
        self.assertIn('data-ron="125,723.00"', dashboard)
        self.assertIn(
            '<td class="amount negative" data-ron="-94,689.00"', dashboard
        )
        self.assertNotIn(">Total<", dashboard)
        self.assertIn('data-display-currency="RON"', dashboard)
        self.assertIn(
            'data-currency="RON" aria-pressed="true">RON</button>', dashboard
        )
        self.assertIn(
            'data-currency="EUR" aria-pressed="false">EUR</button>', dashboard
        )

    def test_actuals_groups_are_expanded_and_subtotals_follow_children(self) -> None:
        dashboard = render_dashboard(
            load_actuals(ACTUALS_PATH), TEMPLATE_PATH, Decimal("5.25")
        )

        for activity in ("operating", "investing", "financing"):
            title = activity.title()
            heading_position = dashboard.index(f'<span>{title}</span></button>')
            children_position = dashboard.index(
                f'id="group-{activity}-children"'
            )
            subtotal_position = dashboard.index(
                f'class="subtotal-row {activity}-subtotal"'
            )
            self.assertLess(heading_position, children_position)
            self.assertLess(children_position, subtotal_position)
            self.assertIn(
                f'aria-expanded="true" aria-controls="group-{activity}-children"',
                dashboard,
            )
            self.assertNotIn(
                f'id="group-{activity}-children" hidden', dashboard
            )

        self.assertEqual(dashboard.count('<th scope="row">Subtotal</th>'), 3)

        operating_subtotal = re.search(
            r'<tr class="subtotal-row operating-subtotal">(.*?)</tr>',
            dashboard,
            re.DOTALL,
        )
        self.assertIsNotNone(operating_subtotal)
        values = re.findall(
            r'<td class="amount(?: negative)?"[^>]*>([^<]+)</td>',
            operating_subtotal.group(1),
        )
        self.assertEqual(values[0], "16,420.00")
        self.assertEqual(values[4], "8,910.00")

    def test_future_catalog_excludes_zero_and_non_cash_profit_categories(self) -> None:
        config = load_config(PROJECT_ROOT / "cashflow.yaml")
        category_ids = {category.id for category in config.categories}

        self.assertNotIn("altele", category_ids)
        self.assertNotIn("amortization", category_ids)
        self.assertTrue(
            {
                "software",
                "salarii",
                "servicii-terti",
                "it",
                "autovehicule",
                "diverse",
                "administrative",
                "marketing",
                "deplasari-and-transport",
                "taxe-si-impozite",
                "advances",
                "vat",
                "fixed-assets",
                "short-term-debt",
                "interest-and-bank-charges",
                "shareholders",
                "intercompany-settlements",
            }.issubset(category_ids)
        )


class DashboardTests(unittest.TestCase):
    def test_dashboard_is_offline_escaped_and_deterministic(self) -> None:
        raw = base_config()
        raw["categories"][0]["name"] = "Sales <script>alert(1)</script>"
        projection = calculate_projection(validate_config(raw))

        first_render = render_dashboard(
            projection, TEMPLATE_PATH, projection.settings.ron_per_eur
        )
        second_render = render_dashboard(
            projection, TEMPLATE_PATH, projection.settings.ron_per_eur
        )

        self.assertEqual(first_render, second_render)
        self.assertIn("Sales &lt;script&gt;alert(1)&lt;/script&gt;", first_render)
        self.assertNotIn("Sales <script>alert(1)</script>", first_render)
        self.assertNotIn('src="http', first_render)
        self.assertNotIn("<svg", first_render)
        self.assertEqual(first_render.count("<script>"), 1)
        self.assertIn('class="group-toggle"', first_render)
        self.assertIn("children.hidden = expanded", first_render)
        self.assertIn('aria-label="Display currency"', first_render)
        self.assertIn("cell.dataset[valueKey]", first_render)
        self.assertNotIn("kpi", first_render.lower())
        self.assertNotIn("detail-", first_render)
        self.assertEqual(first_render.count('scope="col"'), 13)
        self.assertIn("Month 1", first_render)
        self.assertIn("Month 12", first_render)
        self.assertNotIn(">Total<", first_render)

    def test_aggregates_rows_by_activity_and_category(self) -> None:
        raw = base_config()
        raw["recurring"].append(
            {
                "id": "second-sale",
                "name": "Second sale",
                "type": "inflow",
                "amount": 2,
                "start_date": "2026-12",
                "end_date": "2026-12",
                "category": "sales",
            }
        )
        raw["events"].append(
            {
                "id": "sales-bonus",
                "name": "Sales bonus",
                "type": "inflow",
                "amount": 4,
                "date": "2027-01",
                "category": "sales",
            }
        )
        projection = calculate_projection(validate_config(raw))

        dashboard = render_dashboard(
            projection, TEMPLATE_PATH, projection.settings.ron_per_eur
        )

        sales_label = '<th scope="row">Sales</th>'
        self.assertEqual(dashboard.count(sales_label), 1)
        self.assertIn(
            f'{sales_label}<td class="amount" data-ron="63.00" '
            'data-eur="12.00">12.00</td>'
            '<td class="amount" data-ron="73.50" '
            'data-eur="14.00">14.00</td>',
            dashboard,
        )
        self.assertIn(
            '<th scope="row">Tools</th>'
            '<td class="amount" data-ron="0.00" data-eur="0.00">0.00</td>'
            '<td class="amount negative" data-ron="-15.75" '
            'data-eur="-3.00">-3.00</td>',
            dashboard,
        )
        self.assertIn(
            '<th scope="row">Subtotal</th>'
            '<td class="amount" data-ron="63.00" '
            'data-eur="12.00">12.00</td>'
            '<td class="amount" data-ron="57.75" '
            'data-eur="11.00">11.00</td>',
            dashboard,
        )

    def test_currency_toggle_uses_engine_computed_values(self) -> None:
        projection = calculate_projection(validate_config(base_config()))

        dashboard = render_dashboard(
            projection, TEMPLATE_PATH, projection.settings.ron_per_eur
        )

        self.assertIn('data-display-currency="EUR"', dashboard)
        self.assertIn(
            'data-currency="RON" aria-pressed="false">RON</button>', dashboard
        )
        self.assertIn(
            'data-currency="EUR" aria-pressed="true">EUR</button>', dashboard
        )
        self.assertIn(
            'data-ron="525.00" data-eur="100.00">100.00</td>', dashboard
        )

    def test_table_has_only_balance_and_category_rows(self) -> None:
        projection = calculate_projection(validate_config(base_config()))

        dashboard = render_dashboard(
            projection, TEMPLATE_PATH, projection.settings.ron_per_eur
        )

        self.assertIn("Opening Balance", dashboard)
        self.assertIn("Closing Balance", dashboard)
        self.assertNotIn("Net Delta", dashboard)
        self.assertNotIn("Average Monthly", dashboard)
        self.assertNotIn("Lowest Projected", dashboard)
        self.assertNotIn("Currency:", dashboard)
        self.assertNotIn("<td>EUR ", dashboard)
        self.assertIn(".closing-row .negative { color: #b42318; }", dashboard)

    def test_every_row_has_twelve_two_decimal_values(self) -> None:
        raw = base_config()
        raw["events"].append(
            {
                "id": "sales-refund",
                "name": "Sales refund",
                "type": "outflow",
                "amount": 1,
                "date": "2027-01",
                "category": "sales",
            }
        )
        projection = calculate_projection(validate_config(raw))

        dashboard = render_dashboard(
            projection, TEMPLATE_PATH, projection.settings.ron_per_eur
        )
        rows = re.findall(
            r'<tr class="(?:balance-row|subcategory-row|subtotal-row)[^"]*"'
            r"[^>]*>(.*?)</tr>",
            dashboard,
            re.DOTALL,
        )
        self.assertGreaterEqual(len(rows), 2)
        for row in rows:
            values = re.findall(
                r'<td class="amount(?: negative)?"[^>]*>([^<]+)</td>', row
            )
            self.assertEqual(len(values), 12)
            for value in values:
                self.assertRegex(value, r"^-?\d{1,3}(?:,\d{3})*\.\d{2}$")

        operating_sales = '<th scope="row">Sales</th>'
        self.assertEqual(dashboard.count(operating_sales), 1)

    def test_dashboard_formats_large_exponents(self) -> None:
        raw = base_config()
        raw["settings"]["initial_balance"] = Decimal("1e100")
        raw["recurring"] = []
        raw["events"] = []
        projection = calculate_projection(validate_config(raw))

        dashboard = render_dashboard(
            projection, TEMPLATE_PATH, projection.settings.ron_per_eur
        )

        self.assertIn("10,000,000,000", dashboard)

    def test_run_writes_dashboard_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            output_path = directory_path / "dashboard.html"
            output = io.StringIO()

            result = run(
                PROJECT_ROOT / "cashflow.yaml", TEMPLATE_PATH, output_path, output
            )

            self.assertTrue(output_path.is_file())
            self.assertIn("Dashboard written:", output.getvalue())
            self.assertIsInstance(result, ActualsReport)
            self.assertIn("Ending reported balance:", output.getvalue())

    def test_invalid_input_preserves_existing_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            input_path = directory_path / "cashflow.yaml"
            output_path = directory_path / "dashboard.html"
            input_path.write_text("settings: invalid\n", encoding="utf-8")
            output_path.write_text("existing dashboard", encoding="utf-8")

            with self.assertRaises(ConfigError):
                run(input_path, TEMPLATE_PATH, output_path, io.StringIO())

            self.assertEqual(
                output_path.read_text(encoding="utf-8"), "existing dashboard"
            )


if __name__ == "__main__":
    unittest.main()
