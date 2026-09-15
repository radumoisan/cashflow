from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import unittest

from engine import (ConfigError, allocation_status, calculate_projection, expense_command,
                    import_actual_month, import_expense_movements,
                    initialize_expense_project, load_config, parse_month, replace_input,
                    report_view, render_dashboard, validate_config)
from keez import parse_movements_pdf
from tests.fixtures import actual_record, base_config, zero_config

ROOT = Path(__file__).resolve().parents[1]


def scenario(zero=False):
    return initialize_expense_project(zero_config() if zero else base_config())


def project(raw, start=None):
    return calculate_projection(validate_config(raw), start)


def cells(raw, key, start=None):
    return next(r.cells for r in project(raw, start).rows if r.id == key)


def movement(key, cash, row="suppliers", date="2026-01-12", reference=None):
    return {key: dict(date=date, reference=reference or key, source="Accounting movements, page 1",
                     description=key, partner="Supplier", row_id=row, amount=Decimal(cash))}


def tag(raw, key="project-payment", category="suppliers", owner="regio", vat="none", component=None, vat_amount=None):
    return expense_command(raw, dict(action="assign", movement_id=key, project_id=owner,
        category_id=category if owner else None, vat=vat if owner else None,
        vat_amount=vat_amount, source_amount=component,
        allocation_note="Sourced accounting VAT timing explanation" if component is not None else ""))


def review(raw, month="2026-01", complete=True):
    return expense_command(raw, dict(action="review", month=month, complete=complete, source="User reviewed all movements"))


def closed_scenario():
    raw = scenario()
    raw = import_actual_month(raw, "2026-01", actual_record(raw, clients=1000, suppliers=-700, payroll=-100))
    return import_expense_movements(raw, {
        **movement("client", "1000", "clients"), **movement("regular", "-600"),
        **movement("project-payment", "-100"), **movement("salary", "-100", "payroll"),
    })


class RegioEngineTests(unittest.TestCase):
    def test_zero_initialization_preserves_all_rows_balances_and_tax_history(self):
        from server import _round_trip_yaml
        _, raw = _round_trip_yaml((ROOT / "cashflow.yaml").read_bytes(), ROOT / "cashflow.yaml")
        raw["schema_version"] = 2
        for key in ("expense_projects", "movements", "allocation_reviews"):
            raw.pop(key, None)
        initialized = initialize_expense_project(raw)
        for start in ("2025-01", "2025-12", "2026-01", "2026-03"):
            before, after = project(raw, start), project(initialized, start)
            self.assertEqual(before.rows, after.rows[:len(before.rows)])
            self.assertEqual(before.months, after.months)
            self.assertEqual(before.tax_details, after.tax_details)
            self.assertTrue(all(c.value == 0 for r in after.rows[len(before.rows):] for c in r.cells))
        self.assertEqual(allocation_status(validate_config(initialized), parse_month("2025-12", "month")), "assumption")

    def test_monthly_forecasts_add_once_and_follow_company_financial_mappings(self):
        raw = scenario()
        raw = replace_input(raw, row_id="project-regio-suppliers", month="2026-01", value=-100)
        raw = replace_input(raw, row_id="project-regio-fixed-assets", month="2026-01", value=-200)
        p = project(raw)
        self.assertEqual(cells(raw, "suppliers")[0].value, -600)
        self.assertEqual([c.value for c in cells(raw, "project-regio-suppliers")[:2]], [-100, 0])
        self.assertEqual(p.tax_details[0]["profit_proxy"], "200.00")
        self.assertEqual(p.tax_details[0]["vat_activity"], "63.00")
        view = report_view(p, Decimal("2"))
        expenses = view.activity_groups[1]
        self.assertEqual(expenses.subtotal.cells[0].source, "-1000.00")
        self.assertEqual(expenses.children[0].subtotal.cells[0].source, "-300.00")
        self.assertEqual(expenses.children[0].subtotal.cells[0].eur, "(150.00)")
        self.assertEqual(sum(r.cells[0].value for r in p.rows), p.months[0].net)
        self.assertEqual(p.months[0].closing_balance, Decimal("163"))

    def test_zero_clear_and_hidden_overrides_remain_distinct_and_window_invariant(self):
        raw = replace_input(scenario(), row_id="project-regio-suppliers", month="2026-02", value=0)
        self.assertEqual(cells(raw, "project-regio-suppliers")[1].override, "0.00")
        raw = replace_input(raw, row_id="project-regio-suppliers", month="2026-02", value=None)
        self.assertIsNone(cells(raw, "project-regio-suppliers")[1].override)
        raw = replace_input(raw, row_id="project-regio-suppliers", month="2026-02", value=-500)
        self.assertEqual(project(raw).months[2:], project(raw, "2026-03").months[:10])

    def test_actual_tagging_preserves_authoritative_cash_and_only_review_seeds_regular(self):
        raw = closed_scenario()
        raw = replace_input(raw, row_id="project-regio-suppliers", month="2026-02", value=-50)
        before = project(raw)
        self.assertEqual(report_view(before, Decimal("5.25")).activity_groups[1].children[0].rows[0].cells[0].ron, "—")
        tagged = tag(raw)
        after = project(tagged)
        self.assertEqual(before.months[0], after.months[0])
        self.assertEqual(before.tax_details, after.tax_details)
        self.assertEqual(cells(tagged, "suppliers")[0].value, -600)
        self.assertEqual(cells(tagged, "project-regio-suppliers")[0].value, -100)
        self.assertEqual(cells(tagged, "project-regio-suppliers")[0].provenance, "allocation-incomplete")
        self.assertEqual(cells(tagged, "suppliers")[1].value, -600)
        self.assertIn("review incomplete", cells(tagged, "suppliers")[1].note)
        reviewed = review(tagged)
        self.assertEqual(cells(reviewed, "suppliers")[1].value, -600)
        self.assertIn("reviewed project allocations excluded", cells(reviewed, "suppliers")[1].note)
        self.assertEqual(cells(reviewed, "project-regio-suppliers")[0].provenance, "actual-allocation")
        self.assertEqual(reviewed["actuals"], raw["actuals"])
        for key in ("vat", "taxes"):
            self.assertEqual(cells(raw, key)[0], cells(reviewed, key)[0])

    def test_actual_supersedes_project_plan_and_tags_in_open_month_are_provisional(self):
        raw = replace_input(scenario(), row_id="project-regio-suppliers", month="2026-01", value=-250)
        before = project(raw)
        raw = tag(import_expense_movements(raw, movement("project-payment", "-100")))
        self.assertEqual(project(raw), before)
        raw = import_actual_month(raw, "2026-01", actual_record(raw, suppliers=-100))
        self.assertEqual(cells(raw, "project-regio-suppliers")[0].value, -100)
        self.assertFalse(cells(raw, "project-regio-suppliers")[0].editable)
        with self.assertRaisesRegex(ConfigError, "forecast"):
            replace_input(raw, row_id="project-regio-suppliers", month="2026-01", value=0)

    def test_supplier_net_and_source_basis_bridge_preserve_vat_row(self):
        raw = scenario(True)
        raw = import_actual_month(raw, "2026-01", actual_record(raw, suppliers=-79, vat=-42))
        raw = import_expense_movements(raw, movement("project-payment", "-121"))
        raw = tag(raw, vat="standard", component="-79")
        raw = review(raw)
        self.assertEqual(cells(raw, "project-regio-suppliers")[0].value, -79)
        self.assertEqual(cells(raw, "suppliers")[0].value, 0)
        self.assertEqual(cells(raw, "vat")[0].value, -42)
        self.assertEqual(project(raw).months[0].net, -121)
        self.assertIn("VAT timing", cells(raw, "project-regio-suppliers")[0].note)

    def test_confirmed_vat_and_no_vat_whole_supplier_movements(self):
        for vat, vat_amount, expected in (("standard", None, -100), ("none", None, -121), ("confirmed", "-11", -110)):
            raw = import_expense_movements(scenario(True), movement("project-payment", "-121"))
            raw = tag(raw, vat=vat, vat_amount=vat_amount)
            raw = import_actual_month(raw, "2026-01", actual_record(raw, suppliers=expected, vat=-121-expected))
            self.assertEqual(cells(review(raw), "project-regio-suppliers")[0].value, expected)

    def test_assets_use_whole_cash_and_never_duplicate_vat(self):
        raw = import_expense_movements(scenario(True), movement("project-payment", "-1210", "fixed-assets"))
        raw = tag(raw, category="fixed-assets")
        raw = import_actual_month(raw, "2026-01", actual_record(raw, **{"fixed-assets": -1210}))
        raw = review(raw)
        self.assertEqual(cells(raw, "project-regio-fixed-assets")[0].value, -1210)
        self.assertEqual(cells(raw, "fixed-assets")[0].value, 0)
        self.assertEqual(cells(raw, "vat")[0].value, 0)

    def test_refunds_may_offset_larger_project_payment_than_net_category(self):
        raw = import_expense_movements(scenario(True), {**movement("project-payment", "-200"), **movement("refund", "100")})
        raw = tag(raw)
        raw = import_actual_month(raw, "2026-01", actual_record(raw, suppliers=-100))
        raw = review(raw)
        self.assertEqual(cells(raw, "suppliers")[0].value, 100)
        self.assertEqual(cells(raw, "project-regio-suppliers")[0].value, -200)
        self.assertEqual(project(raw).months[0].net, -100)

    def test_review_requires_reconciliation_and_rejects_wrong_category_overallocation(self):
        with self.assertRaisesRegex(ConfigError, "reconcile"):
            review(import_actual_month(scenario(), "2026-01", actual_record(scenario(), suppliers=-100)))
        raw = import_expense_movements(scenario(True), {**movement("project-payment", "-200"), **movement("other", "100", "misc")})
        raw = import_actual_month(raw, "2026-01", actual_record(raw, suppliers=-100))
        with self.assertRaisesRegex(ConfigError, "source category"):
            review(tag(raw))

    def test_closed_month_with_no_cash_movements_can_be_explicitly_reviewed(self):
        raw = import_actual_month(scenario(), "2026-01", actual_record(scenario()))
        self.assertEqual(allocation_status(validate_config(review(raw)), 2026 * 12), "reviewed")

    def test_untag_correction_and_raw_source_changes_reopen_review(self):
        raw = review(tag(closed_scenario()))
        self.assertEqual(allocation_status(validate_config(raw), 2026 * 12), "reviewed")
        cleared = tag(raw, owner=None)
        self.assertEqual(allocation_status(validate_config(cleared), 2026 * 12), "incomplete")
        self.assertEqual(cells(cleared, "project-regio-suppliers")[0].value, 0)
        self.assertEqual(cells(review(cleared), "suppliers")[1].value, -700)
        self.assertEqual(import_actual_month(raw, "2026-01", raw["actuals"]["2026-01"]), raw)
        changed = deepcopy(raw["actuals"]["2026-01"])
        changed["source"] = "Corrected accounting evidence"
        self.assertEqual(allocation_status(validate_config(import_actual_month(raw, "2026-01", changed)), 2026 * 12), "incomplete")
        direct = deepcopy(raw)
        direct["actuals"]["2026-01"]["source"] = "Externally changed evidence"
        self.assertEqual(allocation_status(validate_config(direct), 2026 * 12), "incomplete")

    def test_idempotent_import_preserves_assignments_and_rejects_changed_partial_batches(self):
        records = {**movement("a", "-100", reference="statement"), **movement("b", "-200", reference="statement")}
        raw = tag(import_expense_movements(scenario(), records), key="a")
        renamed = deepcopy(records)
        renamed["a"]["source"] = "same PDF renamed"
        self.assertEqual(import_expense_movements(raw, renamed), raw)
        with self.assertRaisesRegex(ConfigError, "batch changed"):
            import_expense_movements(raw, {"a": records["a"]})
        changed = deepcopy(records)
        changed["a"]["amount"] = -500
        with self.assertRaisesRegex(ConfigError, "immutable"):
            import_expense_movements(raw, changed)

    def test_invalid_assignments_and_namespace_collisions_rejected(self):
        raw = import_expense_movements(scenario(), movement("project-payment", "-121"))
        for kwargs in ({"category": "fixed-assets"}, {"owner": "unknown"}, {"vat": "confirmed", "vat_amount": "21"},
                       {"vat": "confirmed", "vat_amount": "-122"}, {"component": "-122"}, {"component": "NaN"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigError):
                tag(raw, **kwargs)
        duplicate = scenario()
        duplicate["rows"].append(dict(duplicate["rows"][1], id="project-regio-suppliers"))
        with self.assertRaisesRegex(ConfigError, "unique"):
            validate_config(duplicate)
        for field, value in (("row_id", "taxes"), ("id", "bad:id")):
            bad = scenario()
            bad["expense_projects"]["regio"]["categories"][0][field] = value
            with self.assertRaises(ConfigError):
                validate_config(bad)

    def test_large_values_keep_cents_through_project_totals_and_allocations(self):
        huge = Decimal("9" * 70 + ".99")
        raw = replace_input(scenario(True), row_id="project-regio-fixed-assets", month="2026-01", value=huge.copy_negate())
        total = report_view(project(raw), Decimal("5.25")).activity_groups[1].children[0].subtotal.cells[0].source
        self.assertEqual(total, "-" + str(huge))
        raw = import_expense_movements(raw, movement("project-payment", str(huge.copy_negate()), "fixed-assets"))
        raw = tag(raw, category="fixed-assets")
        raw = import_actual_month(raw, "2026-01", actual_record(raw, **{"fixed-assets": huge.copy_negate()}))
        self.assertEqual(cells(review(raw), "project-regio-fixed-assets")[0].value, huge.copy_negate())

    def test_reported_history_allocation_is_estimated_and_preserves_existing_vat(self):
        raw = zero_config("2025-08")
        raw["taxes"]["checkpoints"]["2025-09"] = raw["taxes"]["checkpoints"].pop("2025-08")
        record = actual_record(raw, suppliers=-121)
        record["basis"] = "reported"
        raw["actuals"]["2025-08"] = record
        raw = initialize_expense_project(raw)
        before = project(raw)
        raw = import_expense_movements(raw, movement("project-payment", "-121", date="2025-08-10"))
        raw = review(tag(raw, vat="standard"), month="2025-08")
        self.assertEqual(cells(raw, "project-regio-suppliers")[0].value, -100)
        self.assertIn("presentation estimate", cells(raw, "project-regio-suppliers")[0].note)
        self.assertEqual(cells(raw, "vat")[0], next(r.cells[0] for r in before.rows if r.id == "vat"))
        self.assertEqual(cells(raw, "suppliers")[1].value, 0)

    def test_nested_snapshot_escapes_labels_and_preserves_collapse_relationships(self):
        raw = scenario()
        raw["expense_projects"]["regio"]["name"] = "Regio <test>"
        rendered = render_dashboard(project(raw), ROOT / "template.html", Decimal("5.25"))
        self.assertIn("Regio &lt;test&gt;", rendered)
        self.assertIn('data-groups="expenses project-regio"', rendered)
        self.assertIn('data-groups="expenses"', rendered)
        self.assertLess(rendered.index('id="group-project-regio-children"'), rendered.index('<tr class="subtotal-row expenses-subtotal"'))

    def test_real_pdf_parser_retains_all_movements_and_duplicate_references(self):
        config = load_config(ROOT / "cashflow.yaml")
        data = (ROOT / "keez-exports/movements-2026-todate.pdf").read_bytes()
        records = parse_movements_pdf(data, "Keez source", {r.id for r in config.rows}, config.settings.registration_number)
        self.assertEqual(len(records), 450)
        self.assertGreater(sum(m["reference"] == "9003" for m in records.values()), 1)
        self.assertEqual(records, parse_movements_pdf(data, "Keez source", {r.id for r in config.rows}, config.settings.registration_number))
        with self.assertRaisesRegex(ConfigError, "registration"):
            parse_movements_pdf(data, "Keez source", {r.id for r in config.rows}, "wrong")
        from server import _round_trip_yaml
        _, raw = _round_trip_yaml((ROOT / "cashflow.yaml").read_bytes(), ROOT / "cashflow.yaml")
        before = project(raw)
        imported = import_expense_movements(raw, records)
        key = next(key for key, m in records.items() if m["row_id"] == "fixed-assets")
        assigned = tag(imported, key=key, category="fixed-assets")
        self.assertEqual(import_expense_movements(assigned, records), assigned)
        self.assertEqual(project(assigned), before)


if __name__ == "__main__":
    unittest.main()
