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
        self.wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "td.amount input")) == 120)

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
        self.wait.until(lambda d: d.find_element(By.CSS_SELECTOR, "thead th:nth-child(2)").text == month)

    def external_override(self, value, id="clients", month="2026-01"):
        yaml, document = _round_trip_yaml(self.path.read_bytes(), self.path)
        document = replace_input(document, row_id=id, month=month, value=Decimal(value))
        with self.path.open("w") as stream:
            yaml.dump(document, stream)

    def test_layout_provenance_read_only_actuals_and_mobile_period_navigation(self):
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, "thead th")), 13)
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, "tbody.activity-subtotal")), 3)
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, "h1,h2,aside,form")), 0)
        for id in ("vat", "taxes", "dividends-paid", "opening-balance", "closing-balance"):
            self.assertEqual(self.cell(id).find_elements(By.TAG_NAME, "input"), [])
        self.assertIn("estimated net conversion", self.input().get_attribute("title"))
        self.assertIn("clients", self.input().get_attribute("aria-label").lower())
        before = self.path.read_bytes()
        self.set_period("2025-01")
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, "td.amount input")), 0)
        self.assertEqual(self.source("clients", "2025-01"), "105649.58")
        self.assertIn("RON 125,723.00", self.cell("clients", "2025-01").get_attribute("title"))
        self.assertIn("Source reconciliation", self.cell("opening-balance", "2025-04").get_attribute("title"))
        self.set_period("2026-03")
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, "thead th:last-child").text, "2027-02")
        self.assertEqual(self.path.read_bytes(), before)
        self.driver.set_window_size(390, 844)
        geometry = self.driver.execute_script("const t=document.querySelector('.table-scroll'); return [getComputedStyle(document.querySelector('table')).minWidth, t.scrollWidth > t.clientWidth, document.documentElement.scrollWidth <= innerWidth];")
        self.assertEqual(geometry, ["1320px", True, True])
        self.driver.find_element(By.CSS_SELECTOR, ".group-toggle").click()
        self.assertFalse(self.driver.find_element(By.ID, "group-operating-children").is_displayed())

    def test_net_history_mixed_window_and_currency_display(self):
        before = self.path.read_bytes()
        self.set_period("2025-12")
        for id, label, source, ron in (
            ("clients", "Clients (net)", "73304.96", "73,304.96"),
            ("suppliers", "Suppliers (net)", "-109561.98", "-109,561.98"),
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
            self.assertEqual(self.cell("suppliers", month).text, "-20,868.95")
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

    def test_invalid_edit_blocks_currency_and_period_then_escape_discards(self):
        before = self.path.read_bytes()
        self.enter("invalid", save=False)
        self.driver.find_element(By.CSS_SELECTOR, '[data-currency="EUR"]').click()
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, "main").get_attribute("data-display-currency"), "RON")
        self.driver.find_element(By.CSS_SELECTOR, '[aria-label="Next month"]').click()
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, "thead th:nth-child(2)").text, "2026-01")
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


if __name__ == "__main__":
    unittest.main()
