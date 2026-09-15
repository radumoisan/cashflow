from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import unittest

from engine import (ConfigError, calculate_projection, company_breakdown_view, import_actual_month,
                    movement_breakdown, movement_import_summary, new_project, project_command,
                    project_report_view, project_workspace, replace_project_input, validate_config)
from keez import parse_movements_pdf
from tests.fixtures import actual_record, base_config, zero_config

ROOT = Path(__file__).resolve().parents[1]


def scenario(zero=False):
    raw = zero_config() if zero else base_config()
    raw["schema_version"] = 3
    raw["projects"] = {"adr": new_project(validate_config(raw), "ADR", "User assumptions: non-taxable grant; normal costs; recoverable VAT")}
    raw["movements"] = {}
    return raw


def add_item(raw, id="laptop", category="assets", amount="-1000", vat="standard", start="2026-01", end=None):
    return project_command(raw, {"action": "save_item", "project_id": "adr", "id": id,
                                "item": {"name": id.title(), "category_id": category, "amount": amount,
                                         "vat": vat, "start": start, "end": end or start, "overrides": {}}})


def import_movement(raw, id="payment", amount="-1210", row="fixed-assets", date="2026-01-12"):
    return project_command(raw, {"action": "import_movements", "movements": {id: {
        "date": date, "source": "Sourced statement page 1", "reference": "statement-1", "description": id,
        "partner": "Supplier", "row_id": row, "amount": amount}}})


def assign(raw, id="payment", category="assets", item="laptop", row="fixed-assets", vat="standard", component=None):
    return project_command(raw, {"action": "assign_movement", "id": id, "project_id": "adr",
                                "category_id": category, "item_id": item, "row_id": row,
                                "vat": vat, "vat_amount": None, "company_amount": component,
                                "allocation_note": "Sourced monthly VAT recognition reconciliation" if component is not None else ""})


def cells(projection, id):
    return next(row.cells for row in projection.rows if row.id == id)


class ProjectEngineTests(unittest.TestCase):
    def test_empty_project_has_no_financial_effect(self):
        raw = base_config()
        before = calculate_projection(validate_config(raw))
        raw["projects"] = {"adr": new_project(validate_config(raw), "ADR", "Assumptions")}
        after = calculate_projection(validate_config(raw))
        self.assertEqual(before.months, after.months)
        self.assertEqual(before.tax_details, after.tax_details)
        self.assertEqual(before.rows, after.rows[:-1])

    def test_grant_no_tax_expenses_normally_deducted_and_asset_vat_global(self):
        raw = add_item(scenario(), "grant", "receipts", "2000", "none")
        raw = add_item(raw, "operating", "suppliers", "-100", "standard")
        raw = add_item(raw)
        p = calculate_projection(validate_config(raw))
        self.assertEqual(cells(p, "project-grants")[0].value, 2000)
        self.assertEqual(cells(p, "suppliers")[0].value, -700)
        self.assertEqual(cells(p, "fixed-assets")[0].value, -1000)
        self.assertEqual(p.tax_details[0]["profit_proxy"], "200.00")
        self.assertEqual(p.tax_details[0]["vat_activity"], "-147.00")
        self.assertEqual(p.tax_details[0]["vat_credit"], "147.00")
        self.assertEqual(p.tax_details[1]["vat_payment"], "0.00")
        self.assertEqual(p.tax_details[1]["profit_payment"], "32.00")
        self.assertEqual(cells(p, "project-grants")[1].value, 0)
        self.assertFalse(cells(p, "suppliers")[0].editable)
        self.assertEqual(p.regular_rows[1].cells[0].value, -600)

    def test_mixed_vat_in_same_category_and_finite_schedules(self):
        raw = add_item(scenario(True), "with-vat", "suppliers", "-100", "standard", end="2026-03")
        raw = add_item(raw, "without-vat", "suppliers", "-100", "none", end="2026-03")
        raw = replace_project_input(raw, "adr", "with-vat", "2026-02", Decimal("0"))
        p = calculate_projection(validate_config(raw))
        self.assertEqual([c.value for c in cells(p, "suppliers")[:4]], [-200, -100, -200, 0])
        self.assertEqual([c.value for c in cells(p, "vat")[:4]], [-21, 0, -21, 0])
        cleared = replace_project_input(raw, "adr", "with-vat", "2026-02", None)
        self.assertNotIn("2026-02", cleared["projects"]["adr"]["items"]["with-vat"]["overrides"])
        self.assertEqual(cells(calculate_projection(validate_config(cleared)), "vat")[1].value, -21)
        with self.assertRaisesRegex(ConfigError, "dates"):
            replace_project_input(raw, "adr", "with-vat", "2026-04", Decimal("1"))

    def test_original_budget_is_immutable_and_variance_survives_changes(self):
        raw = add_item(scenario(True))
        raw = project_command(raw, {"action": "capture_budget", "project_id": "adr", "name": "Approved"})
        raw = replace_project_input(raw, "adr", "laptop", "2026-01", Decimal("-1200"))
        config = validate_config(raw)
        self.assertEqual(project_report_view(config, "adr", mode="budget").closing_balance.cells[0].source, "-1000.00")
        self.assertEqual(project_report_view(config, "adr", mode="variance").closing_balance.cells[0].source, "-200.00")
        with self.assertRaisesRegex(ConfigError, "cannot be overwritten"):
            project_command(raw, {"action": "capture_budget", "project_id": "adr", "name": "Overwrite"})
        removed = project_command(raw, {"action": "delete_item", "project_id": "adr", "id": "laptop"})
        self.assertEqual(project_report_view(validate_config(removed), "adr", mode="variance").closing_balance.cells[0].source, "1000.00")

    def test_overlapping_project_views_keep_lifetime_net_and_company_impact(self):
        raw = add_item(scenario(True), end="2026-06")
        config = validate_config(raw)
        first = project_report_view(config, "adr")
        shifted = project_report_view(config, "adr", "2026-03")
        self.assertEqual(first.closing_balance.cells[2:], shifted.closing_balance.cells[:10])
        impact = project_report_view(config, "adr", mode="impact")
        self.assertEqual(impact.activity_groups[0].subtotal.cells[0].source, "-1210.00")
        self.assertEqual(first.closing_balance.cells[0].source, "-1000.00")

    def test_actual_asset_net_reclassification_preserves_reported_cash(self):
        raw = add_item(scenario(True))
        record = actual_record(raw, **{"fixed-assets": -1210})
        raw = import_actual_month(raw, "2026-01", record)
        raw = import_movement(raw)
        raw = assign(raw)
        config = validate_config(raw)
        p = calculate_projection(config)
        self.assertEqual(config.actuals[2026 * 12].values["fixed-assets"], -1210)
        self.assertEqual(cells(p, "fixed-assets")[0].value, -1000)
        self.assertEqual(cells(p, "vat")[0].value, -210)
        self.assertEqual(p.months[0].net, -1210)
        self.assertEqual(p.months[0].closing_balance, record["closing_balance"])
        self.assertEqual(project_report_view(config, "adr").summary["allocation_status"], "Incomplete")
        raw = project_command(raw, {"action": "complete_month", "project_id": "adr", "month": "2026-01", "complete": True})
        view = project_report_view(validate_config(raw), "adr")
        self.assertEqual(view.closing_balance.cells[0].source, "-1000.00")
        self.assertNotEqual(view.closing_balance.cells[0].provenance, "incomplete")
        with self.assertRaisesRegex(ConfigError, "forecast"):
            replace_project_input(raw, "adr", "laptop", "2026-01", Decimal("1"))

    def test_partial_import_cannot_complete_and_tags_do_not_publish_open_actuals(self):
        raw = add_item(scenario())
        raw = import_movement(raw)
        raw = assign(raw)
        before = calculate_projection(validate_config(add_item(scenario())))
        self.assertEqual(calculate_projection(validate_config(raw)).months, before.months)
        raw = import_actual_month(raw, "2026-01", actual_record(raw, **{"fixed-assets": -1210, "clients": 100}))
        with self.assertRaisesRegex(ConfigError, "reconcile"):
            project_command(raw, {"action": "complete_month", "project_id": "adr", "month": "2026-01", "complete": True})

    def test_project_actual_removed_before_regular_supplier_carry(self):
        raw = add_item(scenario(), "services", "suppliers", "-100", "none", end="2026-03")
        record = actual_record(raw, clients=0, suppliers=-700, payroll=0)
        raw = import_actual_month(raw, "2026-01", record)
        raw = import_movement(raw, "services-paid", "-100", "suppliers")
        raw = import_movement(raw, "regular-paid", "-600", "suppliers")
        raw = assign(raw, "services-paid", "suppliers", "services", "suppliers", "none")
        partial = calculate_projection(validate_config(raw))
        self.assertEqual(cells(partial, "suppliers")[1].value, -700)
        self.assertIn("incomplete", partial.regular_rows[1].cells[1].note)
        raw = project_command(raw, {"action": "complete_month", "project_id": "adr", "month": "2026-01", "complete": True})
        p = calculate_projection(validate_config(raw))
        self.assertEqual(cells(p, "suppliers")[1].value, -700)
        self.assertEqual(cells(p, "suppliers")[3].value, -600)
        self.assertEqual(p.tax_details[0]["vat_activity"], "-126.00")
        view = company_breakdown_view(p, Decimal("5.25"))
        self.assertTrue(next(row for group in view.activity_groups for row in group.rows if row.id == "suppliers").cells[1].editable)

    def test_import_is_idempotent_preserves_tags_and_rejects_source_changes(self):
        raw = assign(import_movement(add_item(scenario(True))))
        again = import_movement(raw)
        self.assertEqual(again["movements"], raw["movements"])
        with self.assertRaisesRegex(ConfigError, "conflicts"):
            import_movement(raw, amount="-2000")
        with self.assertRaisesRegex(ConfigError, "linked to actuals"):
            project_command(raw, {"action": "delete_item", "project_id": "adr", "id": "laptop"})

    def test_categories_expand_rename_archive_without_rewriting_mapping(self):
        raw = scenario()
        cat = {"id": "hosting", "name": "Hosting", "group": "suppliers", "company_row": "suppliers", "archived": False}
        raw = project_command(raw, {"action": "save_category", "project_id": "adr", "category": cat})
        raw = add_item(raw, "hosting-plan", "hosting", "-50", "none")
        before = calculate_projection(validate_config(raw)).months
        cat["name"], cat["archived"] = "Cloud hosting", True
        raw = project_command(raw, {"action": "save_category", "project_id": "adr", "category": cat})
        self.assertEqual(calculate_projection(validate_config(raw)).months, before)
        cat["company_row"] = "fixed-assets"
        with self.assertRaisesRegex(ConfigError, "mapping"):
            project_command(raw, {"action": "save_category", "project_id": "adr", "category": cat})

    def test_monetary_validation_refunds_confirmed_vat_and_large_precision(self):
        raw = add_item(scenario(True))
        raw = import_movement(raw, amount="1210")
        raw = assign(raw)
        config = validate_config(raw)
        self.assertEqual(movement_breakdown(config, config.movements["payment"]), (Decimal("1000.00"), Decimal("210.00"), Decimal("1210")))
        for value in ("NaN", "-1.234", "1e5"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                add_item(raw, amount=value)
        for id in ("opening-balance", "closing-balance", "monthly-net", "subtotal-assets"):
            with self.subTest(id=id), self.assertRaisesRegex(ConfigError, "reserved"):
                add_item(raw, id=id)
        raw["movements"]["payment"]["vat"] = "confirmed"
        raw["movements"]["payment"]["vat_amount"] = Decimal("-210")
        with self.assertRaisesRegex(ConfigError, "component"):
            validate_config(raw)
        huge = "9" * 70 + ".99"
        config = validate_config(add_item(scenario(True), amount="-" + huge, vat="none"))
        self.assertEqual(project_report_view(config, "adr").closing_balance.cells[0].source, "-" + huge)

    def test_real_keez_import_handles_repeated_references_and_reconciles_cash(self):
        config = validate_config(scenario())
        path = ROOT / "keez-exports/movements-2026-todate.pdf"
        records = parse_movements_pdf(path.read_bytes(), path.name, {row.id for row in config.rows})
        self.assertEqual(len(records), 450)
        self.assertEqual(movement_import_summary(records), "450 movements; signed source cash total -RON 3,580.09")
        self.assertGreater(sum(m["reference"] == "9003" for m in records.values()), 1)
        self.assertEqual(records, parse_movements_pdf(path.read_bytes(), path.name, {row.id for row in config.rows}))
        with self.assertRaisesRegex(ConfigError, "registration"):
            parse_movements_pdf(path.read_bytes(), path.name, {row.id for row in config.rows}, "wrong-company")

    def test_grant_actual_is_reclassified_without_cash_or_tax_duplication(self):
        raw = add_item(scenario(True), "grant", "receipts", "1000", "none")
        raw = add_item(raw, "cost", "suppliers", "-100", "none")
        raw = import_actual_month(raw, "2026-01", actual_record(raw, clients=1000, suppliers=-100))
        raw = import_movement(raw, "grant-paid", "1000", "clients")
        raw = import_movement(raw, "cost-paid", "-100", "suppliers")
        raw = assign(raw, "grant-paid", "receipts", "grant", "clients", "none")
        raw = assign(raw, "cost-paid", "suppliers", "cost", "suppliers", "none")
        raw = project_command(raw, {"action": "complete_month", "project_id": "adr", "month": "2026-01", "complete": True})
        config = validate_config(raw)
        p = calculate_projection(config)
        self.assertEqual(cells(p, "clients")[0].value, 0)
        self.assertEqual(cells(p, "project-grants")[0].value, 1000)
        self.assertEqual(p.months[0].net, 900)
        self.assertEqual(p.tax_details[0]["profit_proxy"], "-100.00")
        self.assertEqual(p.tax_details[0]["vat_activity"], "0.00")
        self.assertEqual(p.tax_details[1]["profit_loss"], "100.00")
        self.assertEqual(cells(p, "project-grants")[1].value, 0)
        self.assertEqual(cells(p, "clients")[1].value, 0)
        impact = project_report_view(config, "adr", mode="impact")
        self.assertEqual(impact.activity_groups[0].subtotal.cells[0].source, "0.00")

    def test_recognition_basis_bridge_is_explained_and_cash_conserving(self):
        raw = add_item(scenario(True), "cost", "suppliers", "-100", "standard")
        raw = import_actual_month(raw, "2026-01", actual_record(raw, suppliers=-79, vat=-42))
        raw = import_movement(raw, "cost-paid", "-121", "suppliers")
        raw = assign(raw, "cost-paid", "suppliers", "cost", "suppliers", "standard", "-79")
        p = calculate_projection(validate_config(raw))
        self.assertEqual(cells(p, "suppliers")[0].value, -100)
        self.assertEqual(cells(p, "vat")[0].value, -21)
        self.assertEqual(p.months[0].net, -121)
        self.assertIn("source-basis reclassification", cells(p, "suppliers")[0].note)
        raw["movements"]["cost-paid"]["allocation_note"] = ""
        with self.assertRaisesRegex(ConfigError, "sourced allocation note"):
            validate_config(raw)

    def test_budget_variance_retains_large_value_cents(self):
        huge = "9" * 70 + ".99"
        raw = add_item(scenario(True), amount="-" + huge, vat="none")
        raw = project_command(raw, {"action": "capture_budget", "project_id": "adr", "name": "Large original"})
        raw = replace_project_input(raw, "adr", "laptop", "2026-01", Decimal("0"))
        view = project_report_view(validate_config(raw), "adr", mode="variance")
        self.assertEqual(view.closing_balance.cells[0].source, huge)

    def test_source_batch_replacement_cannot_silently_duplicate_corrected_cash(self):
        raw = import_movement(scenario(True))
        correction = {"payment-corrected": {"date": "2026-01-12", "source": "Corrected PDF", "reference": "statement-1",
                                             "description": "payment", "partner": "Supplier", "row_id": "fixed-assets", "amount": "-1220"}}
        with self.assertRaisesRegex(ConfigError, "batch.*changed"):
            project_command(raw, {"action": "import_movements", "movements": correction, "complete_batches": True})

    def test_project_vat_effective_rates_and_confirmed_payment_replacement(self):
        raw = zero_config("2025-07")
        raw["projects"] = {"adr": new_project(validate_config(raw), "ADR", "Assumptions")}
        raw = add_item(raw, start="2025-07", end="2025-08")
        raw["tax_payments"] = {"2025-08": {"source": "Confirmed remittance", "vat": 50}}
        p = calculate_projection(validate_config(raw))
        self.assertEqual(p.tax_details[0]["vat_activity"], "-190.00")
        self.assertEqual(p.tax_details[1]["vat_activity"], "-210.00")
        self.assertEqual(cells(p, "vat")[1].value, -260)
        self.assertEqual(p.tax_details[1]["vat_payment"], "50.00")

    def test_sourced_actual_correction_reopens_project_allocation_review(self):
        raw = add_item(scenario(True))
        original = actual_record(raw, **{"fixed-assets": -1210})
        raw = assign(import_movement(import_actual_month(raw, "2026-01", original)))
        raw = project_command(raw, {"action": "complete_month", "project_id": "adr", "month": "2026-01", "complete": True})
        unchanged = import_actual_month(raw, "2026-01", original)
        self.assertEqual(unchanged["projects"]["adr"]["completed_months"], ["2026-01"])
        corrected = deepcopy(original)
        corrected["source"] = "Sourced accounting correction: additional bank charge"
        corrected["values"]["misc"] = -100
        updated = import_actual_month(raw, "2026-01", corrected)
        self.assertEqual(updated["projects"]["adr"]["completed_months"], [])
        self.assertEqual(raw["projects"]["adr"]["completed_months"], ["2026-01"])
        self.assertEqual(updated["actuals"]["2026-01"], corrected)


if __name__ == "__main__":
    unittest.main()
