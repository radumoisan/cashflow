from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend/index.html").read_text()
JS = (ROOT / "frontend/assets/app.js").read_text()
CSS = (ROOT / "frontend/assets/styles.css").read_text()


class FrontendContractTests(unittest.TestCase):
    def test_local_table_shell_and_legacy_geometry(self):
        self.assertIn('<main class="report"', HTML)
        for text in ("min-width: 1840px", "height: 39px", "col.category-column { width: 300px; }",
                     "@media (max-width: 720px)", "table { min-width: 1740px; }", "@media print",
                     ".activity-children[hidden] { display: table-row-group; }"):
            self.assertIn(text, CSS)
        for text in ("activity-heading", "activity-children", "activity-subtotal", "balance-section"):
            self.assertIn(text, JS)

    def test_engine_precomputed_currency_and_editability_are_consumed(self):
        self.assertIn('state.currency !== "RON" || !value.editable', JS)
        self.assertIn('value.eur : value.ron', JS)
        self.assertIn('cell.dataset.provenance = value.provenance', JS)
        self.assertNotIn('rowId !== "taxes"', JS)
        self.assertNotIn("parseFloat", JS)
        self.assertNotIn("Math.", JS)

    def test_dom_output_is_text_and_editors_have_context(self):
        self.assertIn('node.textContent = value', JS)
        self.assertNotIn("innerHTML", JS)
        self.assertIn('${rowName}, ${cell.dataset.month} cash flow amount, ${value.provenance}', JS)
        self.assertIn('"aria-label": "First month of the twelve-month window"', JS)

    def test_period_navigation_and_clearing_have_explicit_transport(self):
        self.assertIn('new URLSearchParams({ start })', JS)
        self.assertIn('target.value === "" ? null : target.value', JS)
        self.assertIn('"If-None-Match": etag', JS)
        self.assertIn('"If-Match": `"${state.snapshot.revision}"`', JS)

    def test_drafts_are_independent_of_dom_and_serialize_saves(self):
        self.assertIn('drafts: new Map()', JS)
        self.assertIn('state.queue = state.queue.then', JS)
        self.assertIn('current.override !== target.baseOverride', JS)
        self.assertIn('state.sequence === sequence', JS)
        self.assertIn('state.pending || state.loading || state.drafts.size', JS)


if __name__ == "__main__":
    unittest.main()
