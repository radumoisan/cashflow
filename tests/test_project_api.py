import hashlib
from io import BytesIO, StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine import load_config
from server import _round_trip_yaml, create_app
from tests.fixtures import forecast_scenario_bytes
from tests.test_projects import add_item, scenario

ROOT = Path(__file__).resolve().parents[1]


class ProjectAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cashflow.yaml"
        yaml, _ = _round_trip_yaml(forecast_scenario_bytes(), self.path)
        stream = StringIO()
        raw = add_item(scenario(), end="2026-03")
        raw["settings"]["registration_number"] = "42131419"
        yaml.dump(raw, stream)
        self.path.write_text(stream.getvalue())
        self.client = create_app(self.path, ROOT / "frontend").test_client()

    def state(self, query=""):
        response = self.client.get("/api/state" + query)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response

    def command(self, command, revision=None, query=""):
        revision = revision or self.state().get_json()["revision"]
        return self.client.post("/api/projects/action" + query, json=command, headers={"If-Match": f'"{revision}"'})

    def test_scope_view_etags_and_read_only_modes(self):
        views = [self.state(query) for query in ("", "?scope=regular", "?scope=adr", "?scope=adr&view=budget", "?scope=adr&view=variance", "?scope=adr&view=impact")]
        self.assertEqual(len({view.headers["ETag"] for view in views}), 6)
        self.assertEqual(len({view.get_json()["revision"] for view in views}), 1)
        response = self.client.get("/api/state?scope=adr", headers={"If-None-Match": views[0].headers["ETag"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/state?scope=adr", headers={"If-None-Match": views[2].headers["ETag"]}).status_code, 304)
        before = self.path.read_bytes()
        for view in ("budget", "variance", "impact"):
            result = self.client.patch(f"/api/cell?scope=adr&view={view}", json={"row_id": "laptop", "month": "2026-01", "value": "1", "currency": "RON"},
                                       headers={"If-Match": f'"{views[0].get_json()["revision"]}"'})
            self.assertEqual(result.status_code, 422)
        self.assertEqual(self.path.read_bytes(), before)
        for query in ("?scope=missing", "?scope=adr&view=missing", "?scope=adr&scope=company", "?view=budget"):
            self.assertEqual(self.client.get("/api/state" + query).status_code, 422)

    def test_project_patch_is_scoped_and_company_total_cannot_hide_project(self):
        state = self.state().get_json()
        body = {"row_id": "fixed-assets", "month": "2026-01", "value": "-2000", "currency": "RON"}
        headers = {"If-Match": f'"{state["revision"]}"'}
        self.assertEqual(self.client.patch("/api/cell", json=body, headers=headers).status_code, 422)
        result = self.client.patch("/api/cell?scope=regular", json=body, headers=headers)
        self.assertEqual(result.status_code, 200)
        revision = result.get_json()["revision"]
        body["row_id"], body["value"] = "laptop", "-1500"
        result = self.client.patch("/api/cell?scope=adr", json=body, headers={"If-Match": f'"{revision}"'})
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(result.get_json()["selection"], {"scope": "adr", "view": "current"})
        config = load_config(self.path)
        self.assertEqual(str(config.projects["adr"]["items"]["laptop"]["overrides"]["2026-01"]), "-1500")
        self.assertEqual(next(row for row in config.rows if row.id == "fixed-assets").overrides[2026 * 12], -2000)

    def test_atomic_commands_stale_revision_invalid_fields_and_write_failure(self):
        old = self.state().get_json()["revision"]
        capture = {"action": "capture_budget", "project_id": "adr", "name": "Approved"}
        self.assertEqual(self.command(capture, old).status_code, 200)
        current = self.path.read_bytes()
        self.assertEqual(self.command({"action": "delete_item", "project_id": "adr", "id": "laptop"}, old).status_code, 409)
        self.assertEqual(self.path.read_bytes(), current)
        malformed = [dict(capture, actuals={}), {"action": "save_category", "project_id": [], "category": {}},
                     {"action": "save_item", "project_id": "adr", "id": "bad", "item": {"amount": "NaN"}}]
        for command in malformed:
            with self.subTest(command=command):
                self.assertEqual(self.command(command).status_code, 422)
                self.assertEqual(self.path.read_bytes(), current)
        with patch("server.os.replace", side_effect=OSError("disk failure")):
            response = self.command({"action": "update_project", "project_id": "adr", "name": "Changed", "archived": False})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.path.read_bytes(), current)

    def test_pdf_import_archives_source_and_reimport_preserves_assignment(self):
        pdf = (ROOT / "keez-exports/movements-2026-todate.pdf").read_bytes()
        def upload(name):
            revision = self.state().get_json()["revision"]
            return self.client.post("/api/movements/pdf?scope=adr", data={"file": (BytesIO(pdf), name)},
                                    headers={"If-Match": f'"{revision}"'})
        response = upload("statement.pdf")
        self.assertEqual(response.status_code, 200, response.get_json())
        moves = response.get_json()["workspace"]["movements"]
        self.assertEqual(len(moves), 450)
        archive = self.path.parent / "keez-exports" / ("import-" + hashlib.sha256(pdf).hexdigest() + ".pdf")
        self.assertEqual(archive.read_bytes(), pdf)
        target = next(m for m in moves if m["row_id"] == "fixed-assets")
        command = {"action": "assign_movement", "id": target["id"], "project_id": "adr", "category_id": "assets", "item_id": "laptop",
                   "row_id": "fixed-assets", "vat": "standard", "vat_amount": None, "company_amount": None}
        self.assertEqual(self.command(command).status_code, 200)
        assigned = load_config(self.path).movements[target["id"]]
        self.assertEqual(upload("same-source-renamed.pdf").status_code, 200)
        self.assertEqual(load_config(self.path).movements[target["id"]], assigned)
        self.assertEqual(len(load_config(self.path).movements), 450)
        revision = self.state().get_json()["revision"]
        before = self.path.read_bytes()
        result = self.client.post("/api/movements/pdf", data={"file": (BytesIO(b"not pdf"), "bad.pdf")}, headers={"If-Match": f'"{revision}"'})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
