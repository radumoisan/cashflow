from io import BytesIO, StringIO
from pathlib import Path
import tempfile
import unittest
from engine import load_config
from server import _round_trip_yaml, create_app
from tests.test_regio import ROOT, closed_scenario, review, tag


def scenario_bytes(raw):
    yaml, _ = _round_trip_yaml(b"schema_version: 3", Path("fixture.yaml"))
    stream = StringIO()
    yaml.dump(raw, stream)
    return stream.getvalue().encode()


class RegioAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cashflow.yaml"
        self.path.write_bytes(scenario_bytes(closed_scenario()))
        self.client = create_app(self.path, ROOT / "frontend").test_client()

    def state(self, query=""):
        result = self.client.get("/api/state" + query)
        self.assertEqual(result.status_code, 200, result.get_json())
        return result.get_json()

    def test_project_cells_use_existing_atomic_patch_and_actuals_remain_locked(self):
        revision = self.state()["revision"]
        command = dict(row_id="project-regio-suppliers", month="2026-02", value="-200", currency="RON")
        saved = self.client.patch("/api/cell", json=command, headers={"If-Match": f'"{revision}"'})
        self.assertEqual(saved.status_code, 200, saved.get_json())
        config = load_config(self.path)
        self.assertEqual(config.expense_projects["regio"].categories[0].overrides[2026 * 12 + 1], -200)
        command["month"] = "2026-01"
        before = self.path.read_bytes()
        failed = self.client.patch("/api/cell", json=command, headers={"If-Match": f'"{saved.get_json()["revision"]}"'})
        self.assertEqual(failed.status_code, 422)
        self.assertEqual(self.path.read_bytes(), before)

    def test_actual_import_and_assignment_are_not_http_endpoints(self):
        before = self.path.read_bytes()
        headers = {"If-Match": f'"{self.state()["revision"]}"'}
        self.assertEqual(self.client.get("/api/expenses").status_code, 404)
        self.assertEqual(self.client.post("/api/expenses/actions", json={"action": "review"}, headers=headers).status_code, 404)
        self.assertEqual(self.client.post("/api/expenses/pdf", data={"file": (BytesIO(b"%PDF-test"), "movements.pdf"), "currency": "RON"}, headers=headers).status_code, 404)
        self.assertEqual(self.path.read_bytes(), before)

    def test_backend_allocations_are_returned_as_locked_report_cells(self):
        original = load_config(self.path).actuals
        self.path.write_bytes(scenario_bytes(review(tag(closed_scenario()))))
        state = self.state()
        expenses = state["report"]["activity_groups"][1]
        regular = next(r for r in expenses["rows"] if r["id"] == "suppliers")
        project = expenses["children"][0]["rows"][0]
        self.assertEqual(regular["cells"][0]["source"], "-600.00")
        self.assertEqual(project["cells"][0]["source"], "-100.00")
        self.assertFalse(project["cells"][0]["editable"])
        self.assertEqual(project["cells"][0]["provenance"], "actual-allocation")
        self.assertEqual(load_config(self.path).actuals, original)
        self.assertEqual(self.state("?start=2026-03")["report"]["months"][0], "2026-03")


if __name__ == "__main__":
    unittest.main()
