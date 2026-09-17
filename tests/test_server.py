from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from decimal import Decimal
from engine import evaluate_config, import_actual_month, load_config, parse_month, report_view
import server
from server import API_VERSION, MAX_REQUEST_BYTES, create_app
from tests.fixtures import actual_record, legacy_forecast_bytes, legacy_forecast_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def json_report(value: object) -> dict:
    return json.loads(json.dumps(asdict(value)))


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config_path = self.root / "cashflow.yaml"
        self.config_path.write_bytes(legacy_forecast_bytes())
        self.static_path = self.root / "frontend"
        (self.static_path / "assets").mkdir(parents=True)
        (self.static_path / "index.html").write_text("frontend", encoding="utf-8")
        (self.static_path / "assets" / "app.css").write_text("body{}", encoding="utf-8")
        self.app = create_app(self.config_path, self.static_path)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def state(self) -> tuple[dict, str]:
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        return response.get_json(), response.headers["ETag"]

    def patch_cell(self, revision: str, start=None, **cell):
        return self.client.patch(
            "/api/cell" + (f"?start={start}" if start else ""), headers={"If-Match": f'"{revision}"'}, json=cell
        )

    def test_health_and_state_match_engine_report(self) -> None:
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.get_json(), {"api_version": API_VERSION, "status": "ok"})

        state, etag = self.state()
        config = load_config(self.config_path)
        expected = json_report(
            report_view(evaluate_config(config, self.config_path), config.settings.ron_per_eur)
        )
        self.assertEqual(set(state), {"api_version", "revision", "report"})
        self.assertEqual(state["api_version"], API_VERSION)
        self.assertEqual(state["report"], expected)
        self.assertNotEqual(etag, f'"{state["revision"]}"')
        self.assertEqual(state["report"]["mode"], "forecast")
        self.assertEqual(len(state["report"]["months"]), 12)
        cell = state["report"]["opening_balance"]["cells"][0]
        self.assertIsInstance(cell["source"], str)
        self.assertRegex(cell["ron"], r"^\d{1,3}(?:,\d{3})*\.\d{2}$")
        self.assertRegex(cell["eur"], r"^\d{1,3}(?:,\d{3})*\.\d{2}$")
        negative = state["report"]["activity_groups"][1]["rows"][0]["cells"][0]
        self.assertTrue(negative["source"].startswith("-"))
        for currency in ("ron", "eur"):
            self.assertRegex(negative[currency], r"^\(\d{1,3}(?:,\d{3})*\.\d{2}\)$")
        response = self.client.get("/api/state")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_etag_returns_not_modified_only_for_valid_current_state(self) -> None:
        state, etag = self.state()
        cached = self.client.get("/api/state", headers={"If-None-Match": etag})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.headers["ETag"], etag)

        self.config_path.write_text("settings: invalid\n", encoding="utf-8")
        invalid = self.client.get("/api/state", headers={"If-None-Match": etag})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.get_json()["error"]["code"], "config_validation")
        self.assertNotEqual(state["revision"], hashlib.sha256(self.config_path.read_bytes()).hexdigest())

    def test_month_classification_updates_with_imports_and_selected_window(self):
        from tests.test_regio_api import scenario_bytes
        raw = legacy_forecast_config()
        before, etag = self.state()
        self.assertEqual(before["report"]["month_kinds"], ["forecast"] * 12)
        updated = import_actual_month(raw, "2026-01", actual_record(raw, clients=500))
        self.config_path.write_bytes(scenario_bytes(updated))
        response = self.client.get("/api/state", headers={"If-None-Match": etag})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["report"]["month_kinds"], ["actual"] + ["forecast"] * 11)
        shifted = self.client.get("/api/state?start=2025-12").get_json()
        self.assertEqual(shifted["report"]["month_kinds"], ["actual"] * 2 + ["forecast"] * 10)
        self.assertEqual(shifted["revision"], response.json["revision"])

    def test_row_update_recalculates_exactly_as_engine(self) -> None:
        before, etag = self.state()
        month = before["report"]["months"][3]
        response = self.patch_cell(
            before["revision"], row_id="clients", month=month, value="123.45", currency="RON"
        )
        self.assertEqual(response.status_code, 200)
        persisted = response.get_json()
        config = load_config(self.config_path)
        expected = json_report(
            report_view(evaluate_config(config, self.config_path), config.settings.ron_per_eur)
        )
        self.assertEqual(persisted["report"], expected)
        self.assertNotEqual(persisted["revision"], before["revision"])
        self.assertNotEqual(response.headers["ETag"], etag)
        self.assertEqual(response.headers["ETag"], f'"{server._view_etag(persisted)}"')

    def test_first_visible_opening_balance_is_read_only(self) -> None:
        before, _ = self.state()
        months = before["report"]["months"]
        response = self.patch_cell(
            before["revision"],
            row_id="opening-balance",
            month=months[0],
            value="100.00",
            currency="RON",
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(before["report"]["opening_balance"]["cells"][0]["editable"])

    def test_negative_and_zero_values_are_absolute_targets(self) -> None:
        state, _ = self.state()
        month = state["report"]["months"][0]
        negative = self.patch_cell(
            state["revision"], row_id="clients", month=month, value="-10.00", currency="RON"
        )
        self.assertEqual(negative.status_code, 200)
        negative_state = negative.get_json()
        self.assertEqual(
            negative_state["report"]["activity_groups"][0]["rows"][0]["cells"][0]["source"],
            "-10.00",
        )
        zero = self.patch_cell(
            negative_state["revision"], row_id="clients", month=month, value="0", currency="RON"
        )
        self.assertEqual(zero.status_code, 200)
        self.assertEqual(
            zero.get_json()["report"]["activity_groups"][0]["rows"][0]["cells"][0]["source"], "0.00"
        )

    def test_only_ron_requests_and_ron_source_configs_are_editable(self) -> None:
        state, _ = self.state()
        cell = {"row_id": "clients", "month": state["report"]["months"][0], "value": "1", "currency": "EUR"}
        wrong_request = self.patch_cell(state["revision"], **cell)
        self.assertEqual(wrong_request.status_code, 400)

        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace('currency: "RON"', 'currency: "EUR"'),
            encoding="utf-8",
        )
        self.assertEqual(self.client.get("/api/state").status_code, 422)

    def test_unknown_derived_and_invalid_month_targets_are_rejected(self) -> None:
        state, _ = self.state()
        before = self.config_path.read_bytes()
        month = state["report"]["months"][0]
        for row_id, target_month in (
            ("unknown", month),
            ("operating", month),
            ("subtotal", month),
            ("subtotal-inflows", month),
            ("subtotal-expenses", month),
            ("vat", month),
            ("taxes", month),
            ("dividends-paid", month),
            ("closing-balance", month),
            ("clients", "2027-01"),
            ("opening-balance", state["report"]["months"][1]),
        ):
            response = self.patch_cell(
                state["revision"], row_id=row_id, month=target_month, value="1", currency="RON"
            )
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_malformed_payloads_and_oversized_body_are_rejected(self) -> None:
        state, _ = self.state()
        valid = {"row_id": "clients", "month": state["report"]["months"][0], "value": "1", "currency": "RON"}
        no_quote = self.client.patch("/api/cell", headers={"If-Match": state["revision"]}, json=valid)
        self.assertEqual(no_quote.status_code, 400)
        malformed_json = self.client.patch(
            "/api/cell", headers={"If-Match": f'"{state["revision"]}"'}, data="not json", content_type="application/json"
        )
        self.assertEqual(malformed_json.status_code, 400)
        for payload in (
            {**valid, "value": "NaN"},
            {**valid, "value": "not-money"},
            {**valid, "value": "1e2"},
            {**valid, "value": 1},
            {key: value for key, value in valid.items() if key != "month"},
            {**valid, "unexpected": "field"},
        ):
            response = self.client.patch(
                "/api/cell", headers={"If-Match": f'"{state["revision"]}"'}, json=payload
            )
            self.assertEqual(response.status_code, 400)
        precision = self.patch_cell(
            state["revision"], row_id="clients", month=valid["month"], value="1.001", currency="RON"
        )
        self.assertEqual(precision.status_code, 422)
        oversized = self.client.patch(
            "/api/cell", headers={"If-Match": f'"{state["revision"]}"'},
            data=b"x" * (MAX_REQUEST_BYTES + 1), content_type="application/json"
        )
        self.assertEqual(oversized.status_code, 413)

    def test_stale_and_final_recheck_conflicts_preserve_external_bytes(self) -> None:
        state, _ = self.state()
        cell = {"row_id": "clients", "month": state["report"]["months"][0], "value": "1", "currency": "RON"}
        stale = self.patch_cell("0" * 64, **cell)
        self.assertEqual(stale.status_code, 409)

        external = self.config_path.read_bytes().replace(b"initial_balance: 1590.00", b"initial_balance: 1591.00")
        original_read_bytes = Path.read_bytes
        reads = 0

        def read_bytes(path: Path) -> bytes:
            nonlocal reads
            reads += 1
            if path == self.config_path and reads == 2:
                self.config_path.write_bytes(external)
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", autospec=True, side_effect=read_bytes):
            response = self.patch_cell(state["revision"], **cell)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "stale_revision")
        self.assertEqual(self.config_path.read_bytes(), external)

    def test_invalid_external_yaml_blocks_update_before_stale_check(self) -> None:
        state, _ = self.state()
        self.config_path.write_text("settings: invalid\n", encoding="utf-8")
        response = self.patch_cell(
            state["revision"], row_id="clients", month="2025-01", value="1", currency="RON"
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"]["code"], "config_validation")

    def test_round_trip_preserves_comments_quotes_and_file_mode(self) -> None:
        source = self.config_path.read_text(encoding="utf-8").replace(
            "overrides: {}", 'overrides:\n      "2026-01": 7.00 # client target', 1
        )
        self.config_path.write_text("# preserved document comment\n" + source, encoding="utf-8")
        os.chmod(self.config_path, 0o640)
        state, _ = self.state()
        response = self.patch_cell(
            state["revision"], row_id="clients", month="2026-01", value="7.50", currency="RON"
        )
        self.assertEqual(response.status_code, 200)
        persisted = self.config_path.read_text(encoding="utf-8")
        self.assertIn("# preserved document comment", persisted)
        self.assertRegex(persisted, r'"2026-01": 7\.50\s+# client target')
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o640)

    def test_failed_replace_preserves_original_bytes_and_mode(self) -> None:
        os.chmod(self.config_path, 0o640)
        before = self.config_path.read_bytes()
        state, _ = self.state()
        with patch("server.os.replace", side_effect=OSError("replace failed")):
            response = self.patch_cell(
                state["revision"], row_id="clients", month="2026-01", value="1", currency="RON"
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o640)

    def test_removed_scenario_endpoints_and_fixed_static_routes(self) -> None:
        self.assertEqual(self.client.post("/api/simulate").status_code, 404)
        self.assertEqual(self.client.put("/api/scenario").status_code, 404)
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertEqual(index.get_data(as_text=True), "frontend")
        index.close()
        asset = self.client.get("/assets/app.css")
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.headers["X-Frame-Options"], "DENY")
        asset.close()
        (self.root / "secret.txt").write_text("secret", encoding="utf-8")
        self.assertEqual(self.client.get("/assets/%2e%2e/secret.txt").status_code, 404)
        self.assertEqual(self.client.get("/cashflow.yaml").status_code, 404)
        self.assertEqual(self.client.get("/favicon.ico").status_code, 204)

    def test_main_uses_port_8000_by_default(self) -> None:
        with patch.object(server, "create_app") as create_app, patch("sys.argv", ["server.py"]):
            self.assertEqual(server.main(), 0)
        create_app.return_value.run.assert_called_once_with(host="127.0.0.1", port=8000)

    def test_clear_override_restores_estimate_and_persists_only_sparse_inputs(self):
        before, _ = self.state()
        changed = self.patch_cell(before["revision"], row_id="clients", month="2026-01", value="0", currency="RON").get_json()
        restored = self.patch_cell(changed["revision"], row_id="clients", month="2026-01", value=None, currency="RON")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.get_json()["report"], before["report"])
        c = load_config(self.config_path)
        self.assertEqual(c.rows[0].overrides, {})
        self.assertEqual(c.actual_through, parse_month("2025-12", "month"))

    def test_period_specific_etags_and_read_only_navigation(self):
        before = self.config_path.read_bytes()
        state, etag = self.state()
        shifted = self.client.get("/api/state?start=2026-03", headers={"If-None-Match": etag})
        self.assertEqual(shifted.status_code, 200)
        self.assertNotEqual(shifted.headers["ETag"], etag)
        self.assertEqual(shifted.get_json()["revision"], state["revision"])
        self.assertEqual(shifted.get_json()["report"]["opening_balance"]["cells"][0], state["report"]["opening_balance"]["cells"][2])
        cached = self.client.get("/api/state?start=2026-03", headers={"If-None-Match": shifted.headers["ETag"]})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_shifted_window_patch_returns_same_window_and_uses_source_revision(self):
        state = self.client.get("/api/state?start=2026-03").get_json()
        saved = self.patch_cell(state["revision"], start="2026-03", row_id="clients", month="2027-02", value="500", currency="RON")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["report"]["months"][0], "2026-03")
        self.assertEqual(saved.get_json()["report"]["activity_groups"][0]["rows"][0]["cells"][-1]["source"], "500.00")

    def test_historical_actuals_show_net_and_reject_patch_in_historical_view(self):
        before = self.config_path.read_bytes()
        state = self.client.get("/api/state?start=2025-01").get_json()
        groups = state["report"]["activity_groups"]
        rows = {row["id"]: row for group in groups for row in group["rows"]}
        self.assertEqual(rows["clients"]["name"], "Clients (net)")
        self.assertEqual(rows["suppliers"]["name"], "Suppliers (net)")
        for id, amount in (("clients", "105649.58"), ("suppliers", "-79570.59"), ("vat", "-2187.99")):
            with self.subTest(row=id):
                cell = rows[id]["cells"][0]
                self.assertEqual(cell["source"], amount)
                self.assertEqual(cell["provenance"], "actual-net-estimate")
                self.assertFalse(cell["editable"])
                saved = self.patch_cell(state["revision"], start="2025-01", row_id=id, month="2025-01", value="500", currency="RON")
                self.assertEqual(saved.status_code, 422)
        self.assertEqual(rows["clients"]["cells"][0]["eur"], "20,123.73")
        self.assertIn("RON 125,723.00", rows["clients"]["cells"][0]["note"])
        vat = next(group for group in groups if group["id"] == "vat")
        self.assertIsNone(vat["subtotal"])
        self.assertEqual(sum(Decimal(rows[id]["cells"][0]["source"]) for id in (
            "clients", "suppliers", "advances", "net-salaries-and-taxes", "vat", "taxes", "miscelaneous",
        )), Decimal("16420.00"))
        self.assertEqual(state["report"]["closing_balance"]["cells"][0]["source"], "9582.00")
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_invalid_window_or_query_is_rejected(self):
        for query in ("start=bad", "start=2024-12", "start=2125-01", "start=2026-01&start=2026-02", "currency=EUR"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/api/state?" + query).status_code, 422)

    def test_round_trip_does_not_reduce_large_decimal_precision(self):
        huge = "9" * 100 + ".99"
        source = self.config_path.read_text().replace("initial_balance: 1590.00", "initial_balance: " + huge)
        self.config_path.write_text(source)
        state, _ = self.state()
        saved = self.patch_cell(state["revision"], row_id="clients", month="2026-01", value="0.01", currency="RON")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(load_config(self.config_path).settings.initial_balance, Decimal(huge))


if __name__ == "__main__":
    unittest.main()
