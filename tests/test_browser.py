"""Opt-in Chromium integration tests: CASHFLOW_BROWSER_TESTS=1 python -m unittest tests.test_browser -v."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path

from flask import request
from werkzeug.serving import make_server

from engine import load_config, replace_input
from server import _round_trip_yaml, create_app
from tests.test_regio import closed_scenario, review, tag
from tests.test_regio_api import scenario_bytes

ENABLED = os.environ.get("CASHFLOW_BROWSER_TESTS") == "1"
if ENABLED:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(ENABLED, "set CASHFLOW_BROWSER_TESTS=1 to run Chromium integration tests")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        options = webdriver.ChromeOptions()
        options.binary_location = os.environ.get("CHROMIUM_BINARY", "/usr/bin/chromium")
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
        cls.driver = webdriver.Chrome(options=options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "cashflow.yaml"
        shutil.copy2(ROOT / "cashflow.yaml", self.path)
        self.control = {"before_patch": None, "delay": 0, "patches": 0, "fail": False}
        app = create_app(self.path, ROOT / "frontend")

        @app.before_request
        def interfere():
            if request.method == "PATCH":
                self.control["patches"] += 1
                callback = self.control["before_patch"]
                self.control["before_patch"] = None
                if callback:
                    callback()
                time.sleep(self.control["delay"])
                if self.control["fail"]:
                    self.control["fail"] = False
                    return {"error": {"message": "Simulated write failure"}}, 422

        self.server = make_server("127.0.0.1", 0, app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.driver.set_window_size(1440, 900)
        self.driver.get(f"http://127.0.0.1:{self.server.server_port}")
        self.wait = WebDriverWait(self.driver, 10)
        self.wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "td.amount input")) == 180)

    def tearDown(self):
        self.driver.get("about:blank")
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temp.cleanup()

    def cell(self, id, month="2026-01"):
        return self.driver.find_element(By.CSS_SELECTOR, f'td[data-row-id="{id}"][data-month="{month}"]')

    def input(self, id="clients", month="2026-01"):
        return self.cell(id, month).find_element(By.TAG_NAME, "input")

    def enter(self, value, id="clients", month="2026-01", save=True):
        input = self.input(id, month)
        input.click()
        input.send_keys(Keys.CONTROL, "a")
        input.send_keys(value if value else Keys.BACKSPACE)
        if save:
            input.send_keys(Keys.ENTER)
        return input

    def source(self, id, month="2026-01"):
        return self.cell(id, month).get_attribute("data-source")

    def set_period(self, month):
        self.driver.execute_script("const input = document.querySelector('.period-range input'); input.value = arguments[0]; input.dispatchEvent(new Event('change', {bubbles:true}));", month)
        self.wait.until(lambda d: d.find_element(By.CSS_SELECTOR, ".period-range input").get_attribute("value") == month)

    def assert_section_gaps(self, height=6):
        gaps = self.driver.execute_script("""
            const rows = [...document.querySelectorAll('tbody:not([aria-hidden="true"]) > tr')]
                .filter(row => row.getClientRects().length);
            return rows.slice(1).flatMap((row, index) => {
                const gap = row.getBoundingClientRect().top - rows[index].getBoundingClientRect().bottom;
                return gap > 0.5 ? [[row.className, gap]] : [];
            });
        """)
        self.assertEqual([name for name, _ in gaps], [
            "activity-heading-row inflows-heading", "activity-heading-row expenses-heading",
            "standalone-row vat-row", "activity-heading-row financing-heading", "balance-row closing-row",
        ])
        for name, actual_height in gaps:
            self.assertAlmostEqual(actual_height, height, delta=0.5, msg=name)

    def external_override(self, value, id="clients", month="2026-01"):
        yaml, document = _round_trip_yaml(self.path.read_bytes(), self.path)
        document = replace_input(document, row_id=id, month=month, value=Decimal(value))
        with self.path.open("w") as stream:
            yaml.dump(document, stream)

    def test_layout_provenance_read_only_actuals_and_mobile_period_navigation(self):
        self.assert_section_gaps()
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, "thead th")), 13)
        self.assertEqual([header.text for header in self.driver.find_elements(By.CSS_SELECTOR, "thead th")[1:]],
                         ["Jan 26", "Feb 26", "Mar 26", "Apr 26", "May 26", "Jun 26",
                          "Jul 26", "Aug 26", "Sep 26", "Oct 26", "Nov 26", "Dec 26"])
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, "tbody.activity-subtotal")), 4)
        self.assertEqual([button.text for button in self.driver.find_elements(By.CSS_SELECTOR, ".group-toggle span:last-child")],
                         ["Inflows", "Expenses", "Regio", "Financing"])
        self.assertEqual([label.text for label in self.driver.find_elements(By.CSS_SELECTOR, "#group-expenses-children th")],
                         ["Suppliers (net)", "Payroll", "Taxes", "Fixed Assets", "Advances", "Miscellaneous"])
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, ".standalone-section .vat-row")), 1)
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, "h1,h2,aside,form")), 0)
        self.assertNotIn("Project actuals", [button.text for button in self.driver.find_elements(By.CSS_SELECTOR, ".toolbar button")])
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, 'input[type="file"], dialog'), [])
        for id in ("vat", "taxes", "dividends-paid", "opening-balance", "closing-balance"):
            self.assertEqual(self.cell(id).find_elements(By.TAG_NAME, "input"), [])
        self.assertIn("estimated net conversion", self.input().get_attribute("title"))
        self.assertIn("clients", self.input().get_attribute("aria-label").lower())
        before = self.path.read_bytes()
        self.set_period("2025-01")
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, "thead th:nth-child(2)").text, "Jan 25")
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, "td.amount input")), 0)
        self.assertEqual(self.source("clients", "2025-01"), "105649.58")
        self.assertIn("RON 125,723.00", self.cell("clients", "2025-01").get_attribute("title"))
        self.assertIn("Source reconciliation", self.cell("opening-balance", "2025-04").get_attribute("title"))
        self.set_period("2026-03")
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, "thead th:nth-child(2)").text, "Mar 26")
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, "thead th:last-child").text, "Feb 27")
        self.assertEqual(self.path.read_bytes(), before)
        self.driver.set_window_size(390, 844)
        geometry = self.driver.execute_script("const t=document.querySelector('.table-scroll'); return [getComputedStyle(document.querySelector('table')).minWidth, t.scrollWidth > t.clientWidth, document.documentElement.scrollWidth <= innerWidth];")
        self.assertEqual(geometry, ["1740px", True, True])
        self.driver.find_element(By.CSS_SELECTOR, ".group-toggle").click()
        self.assertFalse(self.driver.find_element(By.ID, "group-inflows-children").is_displayed())
        self.assert_section_gaps()

    def test_expense_edit_totals_collapsing_and_standalone_vat_survive_navigation(self):
        old_total = Decimal(self.source("subtotal-expenses"))
        old_closing = Decimal(self.source("closing-balance"))
        old_financing = self.source("subtotal-financing")
        old_vat = self.source("vat")
        self.enter("-200", id="fixed-assets")
        self.wait.until(lambda d: self.source("fixed-assets") == "-200.00")
        self.assertEqual(Decimal(self.source("subtotal-expenses")), old_total - 200)
        self.assertEqual(Decimal(self.source("closing-balance")), old_closing - 200)
        self.assertEqual(self.source("subtotal-financing"), old_financing)
        self.assertEqual(self.source("vat"), old_vat)
        self.assertEqual(self.driver.switch_to.active_element.get_attribute("data-key"), "fixed-assets:2026-02")
        self.enter("40", id="advances")
        self.wait.until(lambda d: self.source("advances") == "40.00")
        self.assertEqual(Decimal(self.source("subtotal-expenses")), old_total - 160)
        self.enter("-10", id="miscelaneous", month="2026-12")
        self.wait.until(lambda d: self.source("miscelaneous", "2026-12") == "-10.00")
        self.assertEqual(self.driver.switch_to.active_element.get_attribute("data-key"), "project-regio-suppliers:2026-01")
        self.driver.find_element(By.CSS_SELECTOR, '[aria-controls="group-expenses-children"]').click()
        self.assertFalse(self.driver.find_element(By.ID, "group-expenses-children").is_displayed())
        self.assertTrue(self.cell("subtotal-expenses").is_displayed())
        self.assertTrue(self.cell("vat").is_displayed())
        self.assertEqual(self.cell("vat").find_elements(By.TAG_NAME, "input"), [])
        self.driver.find_element(By.CSS_SELECTOR, '[data-currency="EUR"]').click()
        self.wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, "td.amount input"))
        self.assertEqual(self.cell("vat").text, self.cell("vat").get_attribute("data-eur"))
        self.assertFalse(self.driver.find_element(By.ID, "group-expenses-children").is_displayed())
        self.set_period("2025-12")
        self.assertFalse(self.driver.find_element(By.ID, "group-expenses-children").is_displayed())
        self.assertTrue(self.cell("vat", "2025-12").is_displayed())
        self.assertEqual(self.cell("vat", "2025-12").get_attribute("data-provenance"), "actual-net-estimate")

    def test_net_history_mixed_window_and_currency_display(self):
        before = self.path.read_bytes()
        self.set_period("2025-12")
        for id, label, source, ron in (
            ("clients", "Clients (net)", "73304.96", "73,304.96"),
            ("suppliers", "Suppliers (net)", "-109561.98", "(109,561.98)"),
        ):
            with self.subTest(row=id):
                historical = self.cell(id, "2025-12")
                self.assertEqual(historical.find_element(By.XPATH, "../th").text, label)
                self.assertEqual(historical.find_elements(By.TAG_NAME, "input"), [])
                self.assertEqual(historical.get_attribute("data-provenance"), "actual-net-estimate")
                self.assertIn("21%", historical.get_attribute("title"))
                self.assertEqual(historical.text, ron)
                self.assertEqual(self.source(id, "2025-12"), source)
                self.assertEqual(self.source(id, "2026-01"), source)
                self.assertEqual(self.input(id).get_attribute("value"), ron)
        self.assertIn("fully deductible", self.cell("suppliers", "2025-12").get_attribute("title"))
        self.assertEqual(self.source("vat", "2025-12"), "-7613.98")
        self.assertIn("original reported VAT cash RON 0.00", self.cell("vat", "2025-12").get_attribute("title"))
        self.assertEqual(self.source("closing-balance", "2025-12"), "3758.00")
        self.driver.find_element(By.CSS_SELECTOR, '[data-currency="EUR"]').click()
        self.wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, "td.amount input"))
        for month in ("2025-12", "2026-01"):
            self.assertEqual(self.cell("clients", month).text, "13,962.85")
            self.assertEqual(self.cell("suppliers", month).text, "(20,868.95)")
        self.driver.find_element(By.CSS_SELECTOR, '[data-currency="RON"]').click()
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "td.amount input"))
        self.set_period("2025-07")
        self.assertIn("19%", self.cell("clients", "2025-07").get_attribute("title"))
        self.assertIn("21%", self.cell("clients", "2025-08").get_attribute("title"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_edit_propagation_enter_clear_and_dependent_cells(self):
        original = self.source("clients")
        old_vat = self.source("vat")
        self.enter("200000")
        self.wait.until(lambda d: self.source("clients") == "200000.00")
        self.assertEqual(self.driver.switch_to.active_element.get_attribute("data-key"), "clients:2026-02")
        self.assertEqual(self.source("clients", "2026-02"), "200000.00")
        self.assertNotEqual(self.source("vat"), old_vat)
        self.assertEqual(self.cell("clients").get_attribute("data-provenance"), "override")
        self.enter("")
        self.wait.until(lambda d: self.source("clients") == original)
        self.assertEqual(load_config(self.path).rows[0].overrides, {})

    def test_accounting_display_signed_edits_drafts_and_rounded_currency_zero(self):
        before = self.path.read_bytes()
        supplier = self.input("suppliers")
        self.assertEqual(supplier.get_attribute("value"), "(109,561.98)")
        supplier.click()
        self.assertEqual(supplier.get_attribute("value"), "-109561.98")
        self.driver.find_element(By.CSS_SELECTOR, "thead th").click()
        self.assertEqual(supplier.get_attribute("value"), "(109,561.98)")
        self.assertEqual(self.control["patches"], 0)
        self.assertEqual(self.path.read_bytes(), before)

        self.enter("-1234.50", id="fixed-assets")
        self.wait.until(lambda d: self.source("fixed-assets") == "-1234.50")
        self.assertEqual(self.input("fixed-assets").get_attribute("value"), "(1,234.50)")
        saved = next(r for r in load_config(self.path).rows if r.id == "fixed-assets")
        self.assertEqual(saved.overrides[2026 * 12], Decimal("-1234.50"))
        self.control["fail"] = True
        self.enter("-222", id="fixed-assets")
        self.wait.until(lambda d: self.input("fixed-assets").get_attribute("aria-invalid") == "true")
        self.assertEqual(self.input("fixed-assets").get_attribute("value"), "-222")
        self.assertNotIn("accounting-negative", self.cell("fixed-assets").get_attribute("class"))
        self.driver.find_element(By.CSS_SELECTOR, "#status button").click()
        self.assertEqual(self.input("fixed-assets").get_attribute("value"), "(1,234.50)")
        self.enter("", id="fixed-assets")
        self.wait.until(lambda d: self.source("fixed-assets") == "0.00")
        self.assertEqual(self.input("fixed-assets").get_attribute("value"), "0.00")
        self.enter("-0.01", id="fixed-assets")
        self.wait.until(lambda d: self.source("fixed-assets") == "-0.01")
        self.assertEqual(self.input("fixed-assets").get_attribute("value"), "(0.01)")
        self.click_text("EUR")
        self.wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, "td.amount input"))
        self.assertEqual(self.cell("fixed-assets").text, "0.00")
        self.assertNotIn("accounting-negative", self.cell("fixed-assets").get_attribute("class"))
        self.click_text("RON")
        self.wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "td.amount input"))
        self.assertEqual(self.input("fixed-assets").get_attribute("value"), "(0.01)")

    def assert_amounts_fit(self):
        overflow = self.driver.execute_script("""
            const canvas = document.createElement('canvas').getContext('2d');
            return [...document.querySelectorAll('td.amount')].flatMap(cell => {
                if (!cell.getClientRects().length) return [];
                const input = cell.querySelector('input');
                const node = input || cell;
                const style = getComputedStyle(node);
                const text = input ? input.value : cell.textContent;
                canvas.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
                const width = canvas.measureText(text).width;
                const available = node.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
                return width > available + 0.5 ? [{text, width, available}] : [];
            });
        """)
        self.assertEqual(overflow, [])

    def test_accounting_layout_live_snapshot_mobile_and_landscape_print(self):
        from engine import evaluate_config, render_dashboard
        config = load_config(self.path)
        snapshot = self.path.parent / "snapshot.html"
        snapshot.write_text(render_dashboard(evaluate_config(config), ROOT / "template.html", config.settings.ron_per_eur))
        for view in ("live", "snapshot"):
            if view == "snapshot":
                self.driver.get(snapshot.as_uri())
            for size, width, media in (("desktop", 1440, ""), ("mobile", 390, ""), ("print", 1047, "print")):
                self.driver.set_window_size(width, 900)
                self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": media})
                try:
                    for currency in ("RON", "EUR"):
                        with self.subTest(view=view, size=size, currency=currency):
                            # The print stylesheet hides the toolbar.
                            self.driver.execute_script("document.querySelector(arguments[0]).click()", f'[data-currency="{currency}"]')
                            self.wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "main").get_attribute("data-display-currency") == currency)
                            self.assert_amounts_fit()
                            digit_ends = self.driver.execute_script("""
                                const canvas = document.createElement('canvas').getContext('2d');
                                return ['.inflows-subtotal td', '.expenses-subtotal td'].map(selector => {
                                    const cell = document.querySelector(selector);
                                    const style = getComputedStyle(cell);
                                    canvas.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
                                    const suffix = cell.textContent.endsWith(')') ? canvas.measureText(')').width : 0;
                                    return cell.getBoundingClientRect().right - parseFloat(style.paddingRight) - suffix;
                                });
                            """)
                            self.assertAlmostEqual(*digit_ends, delta=0.5)
                            self.assert_section_gaps(height=4 if media else 6)
                            amount = self.driver.find_element(By.CSS_SELECTOR, ".closing-row td")
                            self.assertIn("accounting-negative", amount.get_attribute("class"))
                            self.assertTrue(amount.text.startswith("("))
                            self.assertIn("JetBrains", amount.value_of_css_property("font-family"))
                            self.assertEqual(amount.value_of_css_property("font-size"), "9.5px" if media else "16px")
                            self.assertEqual(amount.value_of_css_property("color"), "rgba(180, 35, 24, 1)")
                            self.assertTrue(self.driver.execute_script("return document.documentElement.scrollWidth <= innerWidth"))
                            if size == "mobile":
                                self.assertTrue(self.driver.execute_script("""
                                    const box = document.querySelector('.table-scroll');
                                    const label = document.querySelector('.closing-row th');
                                    const amount = document.querySelector('.closing-row td');
                                    return box.clientWidth - label.getBoundingClientRect().width >= amount.getBoundingClientRect().width;
                                """))
                finally:
                    self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": ""})

    def test_invalid_edit_blocks_currency_and_period_then_escape_discards(self):
        before = self.path.read_bytes()
        self.enter("invalid", save=False)
        self.driver.find_element(By.CSS_SELECTOR, '[data-currency="EUR"]').click()
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, "main").get_attribute("data-display-currency"), "RON")
        self.driver.find_element(By.CSS_SELECTOR, '[aria-label="Next month"]').click()
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, "thead th:nth-child(2)").text, "Jan 26")
        self.assertEqual(self.input().get_attribute("value"), "invalid")
        self.input().send_keys(Keys.ESCAPE)
        self.assertEqual(self.path.read_bytes(), before)
        self.driver.find_element(By.CSS_SELECTOR, '[data-currency="EUR"]').click()
        self.wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, "td.amount input"))

    def test_pending_newer_edit_saves_once_then_switches_currency(self):
        self.control["delay"] = 0.4
        self.enter("111")
        self.enter("222", save=False)
        self.driver.find_element(By.CSS_SELECTOR, '[data-currency="EUR"]').click()
        self.wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "main").get_attribute("data-display-currency") == "EUR")
        self.assertEqual(load_config(self.path).rows[0].overrides, {2026 * 12: Decimal("222")})
        self.assertEqual(self.control["patches"], 2)

    def test_same_cell_conflict_retains_draft_without_overwriting_external_value(self):
        self.control["before_patch"] = lambda: self.external_override("250")
        self.enter("200")
        self.wait.until(lambda d: self.input().get_attribute("aria-invalid") == "true")
        self.assertEqual(self.input().get_attribute("value"), "200")
        self.assertEqual(self.source("clients"), "250.00")
        self.assertEqual(self.control["patches"], 1)
        self.driver.find_element(By.CSS_SELECTOR, "#status button").click()
        self.assertEqual(self.input().get_attribute("value"), "250.00")

    def test_unrelated_conflict_retries_and_idle_polling_adopts_external_changes(self):
        self.control["before_patch"] = lambda: self.external_override("-500", id="suppliers")
        self.enter("300")
        self.wait.until(lambda d: self.source("clients") == "300.00")
        self.assertEqual(self.control["patches"], 2)
        self.assertEqual(self.source("suppliers"), "-500.00")
        self.external_override("700")
        self.wait.until(lambda d: self.source("clients") == "700.00")

    def test_failed_save_retains_input_and_can_be_retried(self):
        self.control["fail"] = True
        self.enter("321")
        self.wait.until(lambda d: self.input().get_attribute("aria-invalid") == "true")
        self.assertEqual(load_config(self.path).rows[0].overrides, {})
        self.assertEqual(self.input().get_attribute("value"), "321")
        self.input().send_keys(Keys.ENTER)
        self.wait.until(lambda d: self.source("clients") == "321.00")

    def click_text(self, text):
        self.driver.find_element(By.XPATH, f'//button[normalize-space()="{text}"]').click()

    def load_closed_fixture(self):
        self.path.write_bytes(scenario_bytes(closed_scenario()))
        self.driver.refresh()
        self.wait.until(lambda d: self.source("clients") == "1000.00")

    def test_regio_editing_independent_collapsing_and_snapshot(self):
        old_regular = self.source("suppliers")
        self.enter("-100", id="project-regio-suppliers")
        self.wait.until(lambda d: self.source("project-regio-suppliers") == "-100.00")
        self.assertEqual(self.source("suppliers"), old_regular)
        self.assertEqual(self.source("project-regio-suppliers", "2026-02"), "0.00")
        self.assertEqual(self.source("subtotal-project-regio"), "-100.00")
        self.driver.find_element(By.CSS_SELECTOR, '[aria-controls="group-project-regio-children"]').click()
        self.assertFalse(self.cell("project-regio-suppliers").is_displayed())
        self.assertTrue(self.cell("subtotal-project-regio").is_displayed())
        self.driver.find_element(By.CSS_SELECTOR, '[aria-controls="group-expenses-children"]').click()
        self.assertFalse(self.cell("subtotal-project-regio").is_displayed())
        self.assertTrue(self.cell("subtotal-expenses").is_displayed())
        self.driver.find_element(By.CSS_SELECTOR, '[aria-controls="group-expenses-children"]').click()
        self.assertFalse(self.cell("project-regio-suppliers").is_displayed())
        self.assertTrue(self.cell("subtotal-project-regio").is_displayed())
        self.click_text("EUR")
        self.wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, "td.amount input"))
        self.assertFalse(self.cell("project-regio-suppliers").is_displayed())
        self.assertEqual(self.cell("subtotal-project-regio").text, self.cell("subtotal-project-regio").get_attribute("data-eur"))
        self.set_period("2026-03")
        self.assertFalse(self.cell("project-regio-suppliers", "2026-03").is_displayed())
        self.assert_section_gaps()
        from engine import evaluate_config, render_dashboard
        config = load_config(self.path)
        snapshot = self.path.parent / "snapshot.html"
        snapshot.write_text(render_dashboard(evaluate_config(config), ROOT / "template.html", config.settings.ron_per_eur))
        self.driver.get(snapshot.as_uri())
        self.assert_section_gaps()
        for group in ("project-regio", "expenses", "expenses"):
            self.driver.find_element(By.CSS_SELECTOR, f'[aria-controls="group-{group}-children"]').click()
        self.assertFalse(self.driver.find_element(By.ID, "group-project-regio-children").is_displayed())
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, ".project-regio-subtotal").is_displayed())
        self.click_text("EUR")
        cell = self.driver.find_element(By.CSS_SELECTOR, ".project-regio-subtotal td")
        self.assertEqual(cell.text, cell.get_attribute("data-eur"))
        self.assert_section_gaps()
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "print"})
        try:
            self.assert_section_gaps(height=4)
        finally:
            self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": ""})

    def test_backend_allocations_refresh_as_read_only_actuals(self):
        self.load_closed_fixture()
        closing = self.source("closing-balance")
        vat = self.source("vat")
        updated = review(tag(closed_scenario()))
        self.path.write_bytes(scenario_bytes(updated))
        self.wait.until(lambda d: self.source("project-regio-suppliers") == "-100.00")
        self.assertEqual(self.source("suppliers"), "-600.00")
        self.assertEqual(self.source("closing-balance"), closing)
        self.assertEqual(self.source("vat"), vat)
        self.assertEqual(self.cell("project-regio-suppliers").get_attribute("data-provenance"), "actual-allocation")
        self.assertEqual(self.cell("project-regio-suppliers").find_elements(By.TAG_NAME, "input"), [])
        self.path.write_bytes(scenario_bytes(tag(updated, owner=None)))
        self.wait.until(lambda d: self.source("project-regio-suppliers") == "0.00")
        self.assertEqual(self.source("suppliers"), "-700.00")
        self.assertEqual(self.source("closing-balance"), closing)


if __name__ == "__main__":
    unittest.main()
