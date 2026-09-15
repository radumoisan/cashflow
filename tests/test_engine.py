from __future__ import annotations

import io
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from engine import (
    ACCOUNTING_ROUNDING_TOLERANCE, ConfigError, calculate_projection,
    compare_cash_to_revenue, evaluate_config, format_accounting_number, format_currency, format_month, format_number, import_actual_month,
    load_actuals, load_config, load_config_bytes, normalize_cash_actual, parse_month, print_summary,
    reconcile_actuals, render_dashboard, replace_input, report_view, run, validate_config,
)
from tests.fixtures import actual_record, base_config, row, zero_config

ROOT = Path(__file__).resolve().parents[1]


def project(raw, start=None):
    return calculate_projection(validate_config(raw), start)


def cells(projection, id):
    return next(row.cells for row in projection.rows if row.id == id)


class ForecastTests(unittest.TestCase):
    def test_signed_net_inputs_vat_and_next_month_profit_tax_roll_cash(self):
        p = project(base_config())
        self.assertEqual([m.closing_balance for m in p.months[:2]], [Decimal("484"), Decimal("736")])
        self.assertEqual([c.value for c in cells(p, "vat")[:2]], [84, 0])
        self.assertEqual([c.value for c in cells(p, "taxes")[:2]], [0, -48])
        self.assertEqual(p.months[1].opening_balance, p.months[0].closing_balance)
        self.assertEqual(p.tax_details[0]["profit_proxy"], "300.00")

    def test_recurring_overrides_zero_and_clearing_change_the_run_rate(self):
        raw = base_config()
        row(raw, "clients")["overrides"] = {"2026-03": 2000, "2026-06": 0}
        p = project(raw)
        self.assertEqual([c.value for c in cells(p, "clients")[:7]], [1000, 1000, 2000, 2000, 2000, 0, 0])
        self.assertEqual(cells(p, "clients")[2].provenance, "override")
        self.assertEqual(cells(p, "clients")[3].provenance, "estimate")
        cleared = replace_input(raw, row_id="clients", month="2026-03", value=None)
        self.assertEqual(cells(project(cleared), "clients")[4].value, 1000)
        self.assertIn("2026-03", row(raw, "clients")["overrides"])
        self.assertNotIn("2026-03", row(cleared, "clients")["overrides"])

    def test_event_override_does_not_repeat_or_contribute_to_profit(self):
        raw = base_config()
        row(raw, "fixed-assets")["overrides"] = {"2026-02": -5000}
        p = project(raw)
        self.assertEqual([c.value for c in cells(p, "fixed-assets")[:3]], [0, -5000, 0])
        self.assertEqual(p.tax_details[1]["profit_proxy"], "300.00")

    def test_missing_recurring_seed_is_an_error_not_zero(self):
        raw = base_config()
        row(raw, "clients")["seed"] = None
        with self.assertRaisesRegex(ConfigError, "clients needs an explicit net seed"):
            project(raw)

    def test_actual_import_locks_complete_month_and_supersedes_its_override(self):
        raw = base_config()
        row(raw, "clients")["overrides"] = {"2026-01": 9999, "2026-03": 800}
        imported = import_actual_month(raw, "2026-01", actual_record(raw, clients=200, suppliers=-50))
        p = project(imported)
        self.assertEqual([c.value for c in cells(p, "clients")[:4]], [200, 200, 800, 800])
        self.assertFalse(cells(p, "clients")[0].editable)
        self.assertTrue(cells(p, "clients")[1].editable)
        self.assertEqual(cells(p, "clients")[0].provenance, "actual")
        self.assertEqual(p.months[1].opening_balance, 500)
        self.assertEqual(raw["actuals"], {})

    def test_complete_month_only_import_is_atomic_at_the_model_boundary(self):
        raw = base_config()
        record = actual_record(raw)
        del record["values"]["vat"]
        with self.assertRaisesRegex(ConfigError, "complete months only.*missing"):
            import_actual_month(raw, "2026-01", record)
        self.assertEqual(raw["actuals"], {})

    def test_cash_report_normalization_preserves_cash_and_does_not_guess_vat(self):
        raw = base_config()
        record = actual_record(raw, clients=1210, suppliers=-605, vat=-70)
        record["basis"] = "reported"
        normalized = normalize_cash_actual(record, clients_vat=210, suppliers_vat=-105)
        self.assertEqual(normalized["values"]["clients"], 1000)
        self.assertEqual(normalized["values"]["suppliers"], -500)
        self.assertEqual(normalized["values"]["vat"], 35)
        self.assertEqual(sum(record["values"].values()), sum(normalized["values"].values()))
        self.assertEqual(normalized["closing_balance"], record["closing_balance"])
        self.assertEqual(record["basis"], "reported")
        imported = import_actual_month(raw, "2026-01", normalized)
        self.assertEqual(cells(project(imported), "clients")[1].value, 1000)

    def test_overlapping_views_include_hidden_overrides_and_tax_state(self):
        raw = base_config()
        row(raw, "clients")["overrides"] = {"2026-02": 1700}
        raw["dividends"]["2026-03"] = {"gross": 1000, "source": "Planned distribution"}
        first, shifted = project(raw), project(raw, "2026-04")
        self.assertEqual(first.months[3:], shifted.months[:9])
        for original, other in zip(first.rows, shifted.rows):
            self.assertEqual(original.cells[3:], other.cells[:9])
        self.assertEqual(first.tax_details[3:], shifted.tax_details[:9])

    def test_supported_window_bounds_and_dates(self):
        raw = base_config()
        for start in ("2025-12", "2125-02", "2026-13", "0000-01", "bad"):
            with self.subTest(start=start), self.assertRaises(ConfigError):
                project(raw, start)

    def test_large_values_keep_cent_precision(self):
        raw = zero_config()
        raw["settings"]["initial_balance"] = Decimal("9" * 256 + ".99")
        row(raw, "fixed-assets")["overrides"]["2026-01"] = Decimal("0.01")
        self.assertEqual(project(raw).ending_balance, Decimal("1" + "0" * 256))
        negative = Decimal("-" + "9" * 100 + ".99")
        self.assertEqual(format_currency(negative, "RON"), "-RON " + f"{negative.copy_abs():,.2f}")


class AccountingPresentationTests(unittest.TestCase):
    def test_accounting_rounding_grouping_and_large_values(self):
        for value, expected in (
            ("1234.565", "1,234.57"), ("-1234.565", "(1,234.57)"),
            ("0", "0.00"), ("-0.00", "0.00"), ("-0.004", "0.00"),
            ("-0.005", "(0.01)"), ("0.005", "0.01"),
        ):
            with self.subTest(value=value):
                self.assertEqual(format_accounting_number(Decimal(value)), expected)
        huge = Decimal("-" + "9" * 100 + ".99")
        self.assertEqual(format_accounting_number(huge), f"({huge.copy_abs():,.2f})")
        self.assertEqual(format_number(Decimal("-1234.565")), "-1,234.57")
        self.assertEqual(format_currency(Decimal("-1234.565"), "RON"), "-RON 1,234.57")

    def test_report_formats_both_currencies_but_preserves_signed_sources_and_cli(self):
        raw = zero_config()
        row(raw, "fixed-assets")["overrides"] = {"2026-01": Decimal("-1234.57"), "2026-02": Decimal("-0.01")}
        projection = project(raw)
        view = report_view(projection, Decimal("5"))
        amounts = next(r for r in view.activity_groups[1].rows if r.id == "fixed-assets").cells
        self.assertEqual([(c.source, c.ron, c.eur) for c in amounts[:3]], [
            ("-1234.57", "(1,234.57)", "(246.91)"), ("-0.01", "(0.01)", "0.00"), ("0.00", "0.00", "0.00"),
        ])
        self.assertTrue(amounts[0].editable)
        self.assertEqual(amounts[0].override, "-1234.57")
        rendered = render_dashboard(projection, ROOT / "template.html", Decimal("5"))
        self.assertIn('class="amount negative accounting-negative" data-ron="(1,234.57)" data-eur="(246.91)"', rendered)
        output = io.StringIO()
        print_summary(projection, output)
        self.assertIn("-1,234.57", output.getvalue())
        self.assertNotIn("(1,234.57)", output.getvalue())


class TaxTests(unittest.TestCase):
    def test_vat_rate_change_uses_activity_month_and_separate_components(self):
        raw = zero_config("2025-07")
        row(raw, "clients")["seed"]["value"] = 100
        p = project(raw)
        self.assertEqual([c.value for c in cells(p, "vat")[:3]], [19, 2, 0])
        self.assertEqual(p.tax_details[1]["vat_payment"], "19.00")

    def test_vat_credit_offsets_later_liability_without_refund(self):
        raw = zero_config()
        row(raw, "clients")["overrides"] = {"2026-02": 100, "2026-03": 200}
        row(raw, "suppliers")["overrides"] = {"2026-01": -200, "2026-02": 0}
        p = project(raw)
        self.assertEqual([item["vat_credit"] for item in p.tax_details[:3]], ["42.00", "21.00", "0.00"])
        self.assertEqual(p.tax_details[3]["vat_payment"], "21.00")
        self.assertEqual(p.tax_details[1]["vat_payment"], "0.00")

    def test_current_credit_does_not_cancel_prior_due_payment(self):
        raw = zero_config()
        row(raw, "clients")["overrides"] = {"2026-01": 100, "2026-02": 0}
        row(raw, "suppliers")["overrides"] = {"2026-02": -100}
        p = project(raw)
        self.assertEqual(cells(p, "vat")[1].value, -42)
        self.assertEqual(p.tax_details[1]["vat_credit"], "21.00")

    def test_losses_carry_across_years_until_absorbed(self):
        raw = zero_config("2026-12")
        row(raw, "suppliers")["overrides"] = {"2026-12": -200, "2027-01": 0}
        row(raw, "clients")["overrides"] = {"2027-01": 100}
        p = project(raw)
        self.assertEqual([item["profit_loss"] for item in p.tax_details[:3]], ["200.00", "100.00", "0.00"])
        self.assertEqual([c.value for c in cells(p, "taxes")[:5]], [0, 0, 0, 0, -16])

    def test_profit_weights_and_excluded_financing(self):
        raw = base_config()
        row(raw, "payroll")["profit_weight"] = Decimal("0.5")
        row(raw, "short-term-debt")["overrides"]["2026-01"] = 50000
        self.assertEqual(project(raw).tax_details[0]["profit_proxy"], "350.00")

    def test_gross_dividend_reconciles_and_withholding_crosses_window(self):
        raw = zero_config()
        raw["dividends"]["2026-12"] = {"gross": Decimal("100.03"), "source": "Gross after-profit-tax distribution"}
        first, next_year = project(raw), project(raw, "2027-01")
        self.assertEqual(cells(first, "dividends-paid")[-1].value, Decimal("-84.03"))
        self.assertEqual(cells(next_year, "taxes")[0].value, -16)
        self.assertEqual(first.tax_details[-1]["profit_proxy"], "0.00")

    def test_actual_no_dividend_supersedes_a_planned_event_and_its_tax(self):
        raw = zero_config()
        raw["dividends"]["2026-01"] = {"gross": 100, "source": "Earlier plan"}
        raw["actuals"]["2026-01"] = actual_record(raw)
        self.assertEqual(cells(project(raw), "taxes")[1].value, 0)

    def test_actual_dividend_requires_gross_or_exact_next_month_state(self):
        raw = zero_config()
        raw["actuals"]["2026-01"] = actual_record(raw, **{"dividends-paid": -84})
        with self.assertRaisesRegex(ConfigError, "requires a matching gross event"):
            project(raw)
        raw["dividends"]["2026-01"] = {"gross": 100, "source": "Confirmed gross dividend"}
        self.assertEqual(cells(project(raw), "taxes")[1].value, -16)
        raw["dividends"] = {}
        raw["tax_payments"]["2026-02"] = {"dividend": 16, "source": "Accountant withholding"}
        self.assertEqual(cells(project(raw), "taxes")[1].value, -16)

    def test_confirmed_component_or_total_replaces_not_adds(self):
        raw = base_config()
        raw["dividends"]["2026-01"] = {"gross": 100, "source": "Plan"}
        raw["tax_payments"]["2026-02"] = {"source": "Accountant", "profit": 10}
        p = project(raw)
        self.assertEqual(cells(p, "taxes")[1].value, -26)
        raw["tax_payments"]["2026-02"] = {"source": "Accountant", "total": 7, "vat": 3}
        p = project(raw)
        self.assertEqual(cells(p, "taxes")[1].value, -7)
        self.assertEqual(cells(p, "taxes")[1].provenance, "confirmed")
        self.assertEqual(cells(p, "vat")[1].value, 81)

    def test_confirmed_zero_is_an_explicit_replacement(self):
        raw = base_config()
        raw["tax_payments"]["2026-02"] = {"source": "Nothing payable", "total": 0}
        self.assertEqual(cells(project(raw), "taxes")[1].value, 0)

    def test_dated_accounting_checkpoint_replaces_carry_and_schedules(self):
        raw = zero_config()
        raw["taxes"]["checkpoints"]["2026-03"] = {
            "kind": "accounting", "source": "Accountant opening March state", "vat_credit": 30,
            "profit_loss": 100, "payments": {"2026-04": {"profit": 7, "vat": 4}},
        }
        p = project(raw, "2026-04")
        self.assertEqual(cells(p, "taxes")[0].value, -7)
        self.assertEqual(cells(p, "vat")[0].value, -4)
        self.assertEqual(p.tax_details[0]["profit_loss"], "100.00")

    def test_round_half_up_on_each_vat_component(self):
        raw = zero_config()
        raw["taxes"]["vat_rates"] = {"2025-01": Decimal("0.10")}
        row(raw, "clients")["seed"]["value"] = Decimal("0.05")
        row(raw, "suppliers")["seed"]["value"] = Decimal("-0.04")
        self.assertEqual(cells(project(raw), "vat")[0].value, Decimal("0.01"))


class ValidationTests(unittest.TestCase):
    def test_derived_actual_and_balance_targets_rejected(self):
        raw = base_config()
        raw["actuals"]["2026-01"] = actual_record(raw)
        for id, month in (("vat", "2026-02"), ("taxes", "2026-02"), ("dividends-paid", "2026-02"),
                          ("clients", "2026-01"), ("opening-balance", "2026-02"), ("unknown", "2026-02")):
            with self.subTest(id=id), self.assertRaises(ConfigError):
                replace_input(raw, row_id=id, month=month, value=1)

    def test_invalid_money_and_rates(self):
        for amount in (Decimal("0.001"), Decimal("1e256"), Decimal("NaN"), float("inf"), True, "10"):
            raw = base_config()
            row(raw, "clients")["seed"]["value"] = amount
            with self.subTest(amount=amount), self.assertRaises(ConfigError):
                validate_config(raw)
        for rate in (-1, Decimal("1.01")):
            raw = base_config()
            raw["taxes"]["profit_rate"] = rate
            with self.assertRaises(ConfigError):
                validate_config(raw)

    def test_unknown_fields_derived_methods_and_sparse_actuals_rejected(self):
        mutations = [
            lambda r: r.update(unexpected=True),
            lambda r: row(r, "vat").update(forecast="carry"),
            lambda r: row(r, "taxes")["overrides"].update({"2026-01": 1}),
            lambda r: r["settings"].update(projection_months=13),
            lambda r: r["settings"].update(currency="EUR"),
            lambda r: r["actuals"].update({"2026-02": actual_record(r)}),
            lambda r: r["tax_payments"].update({"2026-02": {"source": "x", "total": 10, "profit": 3}}),
            lambda r: row(r, "shareholders").update(profit_weight=1),
            lambda r: r["taxes"].update(checkpoints={}),
            lambda r: r["dividends"].update({"2026-01": {"gross": 0, "source": "Cancelled"}}),
        ]
        for mutate in mutations:
            raw = base_config()
            mutate(raw)
            with self.assertRaises(ConfigError):
                validate_config(raw)

    def test_duplicate_yaml_keys_and_merge_keys_rejected(self):
        for data in (b"settings: {}\nsettings: {}", b"settings: {<<: {currency: RON}}"):
            with self.assertRaises(ConfigError):
                load_config_bytes(data, Path("test.yaml"))


class NetPresentationTests(unittest.TestCase):
    def reported_config(self):
        raw = zero_config("2025-07")
        checkpoint = raw["taxes"]["checkpoints"].pop("2025-07")
        raw["taxes"]["checkpoints"]["2025-09"] = checkpoint
        for month, clients, suppliers, vat in (
            ("2025-07", 119, Decimal("-59.50"), -3),
            ("2025-08", 121, Decimal("-60.50"), -7),
        ):
            record = actual_record(raw, clients=clients, suppliers=suppliers, vat=vat)
            record["basis"] = "reported"
            raw["actuals"][month] = record
        return raw

    def test_date_based_net_display_reclassifies_vat_and_reconciles_source_cash(self):
        raw = self.reported_config()
        original = deepcopy(raw)
        p = project(raw)
        self.assertEqual([c.value for c in cells(p, "clients")[:2]], [100, 100])
        self.assertEqual([c.value for c in cells(p, "suppliers")[:2]], [-50, -50])
        self.assertEqual([c.value for c in cells(p, "vat")[:2]], [Decimal("6.50"), Decimal("3.50")])
        for index, month in enumerate(("2025-07", "2025-08")):
            with self.subTest(month=month):
                total = sum(record.cells[index].value for record in p.rows)
                self.assertEqual(total, sum(raw["actuals"][month]["values"].values()))
                self.assertEqual(total, p.months[index].net)
                self.assertEqual(p.months[index].closing_balance, raw["actuals"][month]["closing_balance"])
                for id in ("clients", "suppliers", "vat"):
                    cell = cells(p, id)[index]
                    self.assertFalse(cell.editable)
                    self.assertEqual(cell.provenance, "actual-net-estimate")
                    self.assertIn("Accountant monthly cash report", cell.note)
        self.assertIn("RON 119.00", cells(p, "clients")[0].note)
        self.assertIn("19%", cells(p, "clients")[0].note)
        self.assertIn("21%", cells(p, "clients")[1].note)
        self.assertIn("fully deductible", cells(p, "suppliers")[0].note)
        self.assertEqual(raw, original)
        view = report_view(p, Decimal("2"))
        self.assertEqual(view.activity_groups[0].rows[0].cells[0].eur, "50.00")
        self.assertEqual(view.activity_groups[1].rows[0].cells[0].eur, "(25.00)")
        self.assertEqual(sum(
            Decimal((group.subtotal or group.rows[0]).cells[0].source)
            for group in view.activity_groups
        ), Decimal("56.50"))
        rendered = render_dashboard(p, ROOT / "template.html", Decimal("2"))
        self.assertIn('data-ron="100.00" data-eur="50.00" data-provenance="actual-net-estimate"', rendered)
        self.assertIn("original reported cash RON 119.00", rendered)

    def test_cent_residuals_zero_and_refunds_preserve_the_source_cash(self):
        for clients, suppliers, vat, net_clients, net_suppliers in (
            ("0.03", "0", "-0.01", "0.02", "0"),
            ("-0.03", "0", "0.01", "-0.02", "0"),
            ("0", "-0.03", "0.01", "0", "-0.02"),
            ("0", "0.03", "-0.01", "0", "0.02"),
            ("0", "0", "0", "0", "0"),
        ):
            with self.subTest(clients=clients, suppliers=suppliers):
                raw = self.reported_config()
                source = raw["actuals"]["2025-08"]["values"]
                source.update(clients=Decimal(clients), suppliers=Decimal(suppliers), vat=Decimal(vat))
                p = project(raw)
                self.assertEqual(cells(p, "clients")[1].value, Decimal(net_clients))
                self.assertEqual(cells(p, "suppliers")[1].value, Decimal(net_suppliers))
                self.assertEqual(cells(p, "vat")[1].value, 0)
                self.assertEqual(sum(r.cells[1].value for r in p.rows), sum(source.values()))

    def test_supplied_net_corrections_and_new_months_supersede_estimated_splits(self):
        raw = self.reported_config()
        corrected = import_actual_month(raw, "2025-08", actual_record(raw, clients=60, suppliers=-20, vat=2))
        p = project(corrected)
        self.assertEqual([c.value for c in cells(p, "clients")[:3]], [100, 60, 60])
        self.assertEqual(cells(p, "vat")[1].value, 2)
        self.assertEqual(cells(p, "clients")[1].provenance, "actual")
        imported = import_actual_month(corrected, "2025-09", actual_record(raw, clients=200, suppliers=-100, vat=10))
        p = project(imported)
        self.assertEqual([c.value for c in cells(p, "clients")[:4]], [100, 60, 200, 200])
        self.assertEqual(cells(p, "vat")[2].value, 10)
        self.assertFalse(cells(p, "clients")[2].editable)
        self.assertTrue(cells(p, "clients")[3].editable)
        self.assertEqual(raw["actuals"]["2025-08"]["basis"], "reported")

    def test_reported_amounts_configured_as_net_are_not_converted_again(self):
        raw = self.reported_config()
        raw["taxes"]["reported_cash_seed_basis"] = "net"
        p = project(raw)
        self.assertEqual([c.value for c in cells(p, "clients")[:3]], [119, 121, 121])
        self.assertEqual(cells(p, "vat")[0].value, -3)
        self.assertIn("assumed net by configuration", cells(p, "clients")[0].note)

    def test_mixed_windows_agree_and_do_not_double_convert_forecasts_or_overrides(self):
        c = load_config(ROOT / "cashflow.yaml")
        first, shifted = calculate_projection(c, "2025-07"), calculate_projection(c, "2025-12")
        for original, other in zip(first.rows, shifted.rows):
            self.assertEqual(original.cells[5:], other.cells[:7])
        self.assertEqual(first.months[5:], shifted.months[:7])
        self.assertEqual(first.tax_details[5:], shifted.tax_details[:7])
        self.assertEqual([cell.value for cell in cells(shifted, "clients")[:2]], [Decimal("73304.96")] * 2)
        self.assertEqual(cells(shifted, "clients")[0].provenance, "actual-net-estimate")
        self.assertEqual(cells(shifted, "clients")[1].provenance, "estimate")
        raw = self.reported_config()
        row(raw, "clients")["overrides"]["2025-09"] = 250
        self.assertEqual([cell.value for cell in cells(project(raw), "clients")[2:4]], [250, 250])


class MigrationAndReportTests(unittest.TestCase):
    def test_cashflow_layout_preserves_every_row_and_reconciles_all_months(self):
        config = load_config(ROOT / "cashflow.yaml")
        expected_expenses = [
            "suppliers", "net-salaries-and-taxes", "taxes", "fixed-assets", "advances", "miscelaneous",
        ]
        for start in ("2025-01", "2025-12", "2026-01"):
            with self.subTest(start=start):
                projection = calculate_projection(config, start)
                view = report_view(projection, config.settings.ron_per_eur)
                inflows, expenses, vat, financing = view.activity_groups
                self.assertEqual([g.id for g in view.activity_groups], ["inflows", "expenses", "vat", "financing"])
                self.assertEqual([r.id for r in inflows.rows], ["clients"])
                self.assertEqual([r.id for r in expenses.rows], expected_expenses)
                self.assertEqual([r.id for r in vat.rows], ["vat"])
                self.assertIsNone(vat.subtotal)
                self.assertEqual([r.id for r in financing.rows], [r.id for r in config.rows if r.activity == "financing"])
                self.assertEqual([g.subtotal.name for g in (inflows, expenses, financing)], ["Total Inflows", "Total Expenses", "Subtotal"])
                displayed_rows = [r for g in view.activity_groups for section in (g, *g.children) for r in section.rows]
                self.assertCountEqual([r.id for r in displayed_rows], [r.id for r in projection.rows])
                for displayed in displayed_rows:
                    for cell, original in zip(displayed.cells, cells(projection, displayed.id)):
                        self.assertEqual(Decimal(cell.source), original.value)
                        self.assertEqual((cell.provenance, cell.editable, cell.note, cell.override),
                                         (original.provenance, original.editable, original.note, original.override))
                for index, month in enumerate(projection.months):
                    totals = [Decimal(g.subtotal.cells[index].source) for g in (inflows, expenses, financing)]
                    self.assertEqual(sum(totals) + Decimal(vat.rows[0].cells[index].source), month.net)
                    self.assertTrue(all(not g.subtotal.cells[index].editable for g in (inflows, expenses, financing)))
                    self.assertEqual(Decimal(view.closing_balance.cells[index].source), month.closing_balance)
                    self.assertEqual(Decimal(financing.subtotal.cells[index].source), sum(
                        r.cells[index].value for r in projection.rows if r.activity == "financing"
                    ))

    def test_expense_receipts_offset_spending_without_changing_tax_roles(self):
        raw = zero_config()
        raw["rows"].append(dict(id="advances", name="Advances", activity="operating", forecast="zero",
                                profit_weight=0, seed=None, overrides={"2026-01": 40, "2026-02": -40}))
        row(raw, "fixed-assets")["overrides"] = {"2026-01": -200}
        row(raw, "misc")["overrides"] = {"2026-01": -10, "2026-02": 10}
        projection = project(raw)
        expenses = report_view(projection, Decimal("2")).activity_groups[1]
        self.assertEqual([cell.source for cell in expenses.subtotal.cells[:2]], ["-170.00", "-30.00"])
        self.assertEqual([cell.eur for cell in expenses.subtotal.cells[:2]], ["(85.00)", "(15.00)"])
        self.assertEqual([d["profit_proxy"] for d in projection.tax_details[:2]], ["-10.00", "10.00"])
        self.assertEqual(next(r.activity for r in projection.rows if r.id == "fixed-assets"), "investing")

    def test_every_reported_cash_value_and_balance_matches_reference(self):
        c = load_config(ROOT / "cashflow.yaml")
        reference = load_actuals(ROOT / "accounting/actuals-2025.yaml")
        for index in range(12):
            actual = c.actuals[reference.start_month + index]
            self.assertEqual(actual.opening_balance, reference.cashflow.opening_balance.months[index])
            self.assertEqual(actual.closing_balance, reference.cashflow.closing_balance.months[index])
            for section in reference.cashflow.sections:
                for source_row in section.rows:
                    self.assertEqual(actual.values[source_row.id], source_row.values.months[index])
        self.assertEqual(c.actual_through, parse_month("2025-12", "month"))
        self.assertEqual(calculate_projection(c).months[0].opening_balance, reference.cashflow.closing_balance.total)

    def test_historical_cells_show_estimated_net_and_preserve_discrepancy_notes(self):
        c = load_config(ROOT / "cashflow.yaml")
        p = calculate_projection(c, "2025-01")
        self.assertEqual(cells(p, "clients")[0].value, Decimal("105649.58"))
        self.assertEqual(c.actuals[c.settings.history_start].values["clients"], 125723)
        self.assertIn("Source reconciliation", p.months[3].opening_note)
        self.assertIn("Source reconciliation", p.months[4].opening_note)
        self.assertTrue(all(not cell.editable for result_row in p.rows for cell in result_row.cells))
        for index, month in enumerate(p.months):
            actual = c.actuals[month.month]
            self.assertEqual(sum(result_row.cells[index].value for result_row in p.rows), sum(actual.values.values()))
            self.assertEqual(month.closing_balance, actual.closing_balance)
        forecast = calculate_projection(c)
        self.assertEqual(cells(forecast, "clients")[0].value, Decimal("73304.96"))
        self.assertIn("estimated net conversion", cells(forecast, "suppliers")[0].note)
        self.assertEqual(forecast.tax_details[0]["vat_credit"], "7613.98")
        self.assertEqual(forecast.tax_details[0]["profit_loss"], "63558.02")

    def test_reference_reconciliation_and_vat_evidence_retained(self):
        actuals = load_actuals(ROOT / "accounting/actuals-2025.yaml")
        material = [d for d in reconcile_actuals(actuals) if abs(d.difference) > ACCOUNTING_ROUNDING_TOLERANCE]
        self.assertEqual({d.period for d in material}, {"2025-04", "2025-05"})
        evidence = compare_cash_to_revenue(actuals, (Decimal("0.19"),) * 7 + (Decimal("0.21"),) * 5)
        self.assertEqual(evidence[-1]["revenue_plus_vat"], "88,699.05")
        self.assertEqual(evidence[-1]["cash_minus_gross"], "-0.05")

    def test_report_metadata_and_static_export(self):
        raw = base_config()
        row(raw, "clients")["name"] = "Clients <script>alert(1)</script>"
        p = project(raw)
        view = report_view(p, Decimal("5.25"))
        self.assertEqual(view.navigation["next"], "2026-02")
        self.assertEqual(view.months[0], "2026-01")
        self.assertEqual(view.month_labels[:3], ("Jan 26", "Feb 26", "Mar 26"))
        self.assertIsNone(view.navigation["previous"])
        self.assertTrue(view.activity_groups[0].rows[0].cells[0].editable)
        self.assertFalse(view.opening_balance.cells[0].editable)
        html = render_dashboard(p, ROOT / "template.html", Decimal("5.25"))
        self.assertIn("Clients &lt;script&gt;", html)
        self.assertNotIn("Clients <script>", html)
        self.assertEqual(html.count('scope="col"'), 13)
        self.assertIn('scope="col">Jan 26', html)
        self.assertIn('scope="col">Dec 26', html)
        self.assertEqual(html.count('<th scope="row">Total Inflows</th>'), 1)
        self.assertEqual(html.count('<th scope="row">Total Expenses</th>'), 1)
        self.assertEqual(html.count('<th scope="row">Subtotal</th>'), 1)
        self.assertEqual(html.count('<tr class="standalone-row vat-row">'), 1)
        self.assertNotIn('id="group-vat-children"', html)
        self.assertNotIn('id="group-investing-children"', html)
        self.assertLess(html.index('expenses-subtotal'), html.index('<tr class="standalone-row vat-row">'))
        self.assertLess(html.index('<tr class="standalone-row vat-row">'), html.index('financing-heading'))

    def test_run_and_summary_use_engine_results(self):
        with tempfile.TemporaryDirectory() as temp:
            stream = io.StringIO()
            p = run(ROOT / "cashflow.yaml", ROOT / "template.html", Path(temp) / "dashboard.html", stream)
            self.assertTrue((Path(temp) / "dashboard.html").exists())
            self.assertIn("Starting balance: RON 3,758.00", stream.getvalue())
            self.assertIn("Ending projected balance:", stream.getvalue())
            self.assertEqual(p, evaluate_config(load_config(ROOT / "cashflow.yaml")))


if __name__ == "__main__":
    unittest.main()
