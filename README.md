# Declarative Cash Flow

A local cash-flow tool with two deliberately separate data layers:

- `keez-exports/` contains immutable reports received from accounting.
- `accounting/actuals-2025.yaml` is a normalized, testable transcription of those reports.
- `cashflow.yaml` contains editable settings, the future category catalog, and scenario assumptions.
- `engine.py` performs all calculations and writes the standalone `dashboard.html` report.

The current dashboard is in historical `actuals` mode so the implementation can be
verified against real 2025 figures before forward assumptions are added.

## Requirements

- Python 3.10 or newer
- PyYAML 6.x

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

```bash
python engine.py
```

The command validates and reconciles the selected data, prints an engine-generated
summary, and writes `dashboard.html`. The HTML opens directly from disk and has no
network dependencies.

```bash
xdg-open dashboard.html
```

The report is a fixed 13-column matrix: `Activity / Category` followed by `Month 1`
through `Month 12`. Operating, Investing, and Financing are expanded by default and
can be collapsed independently; each section keeps its heading and engine-calculated
subtotal visible while its category rows are hidden. Printing expands every section.
The RON/EUR control switches between values precomputed by the engine using the fixed
configured exchange rate. The report uses two decimal places and intentionally has no
graph, KPI cards, drill-down rows, or final total column.

## Current Configuration

```yaml
settings:
  company_name: "PLANEMO SOFTWARE LABS S.R.L."
  registration_number: "42131419"
  currency: "RON"
  ron_per_eur: 5.25
  start_date: "2025-01"
  projection_months: 12
  initial_balance: 1590.00
  dashboard_mode: "actuals"
  actuals_file: "accounting/actuals-2025.yaml"

categories:
  - id: "software"
    name: "Software"
    activity: "operating"

recurring: []
events: []
```

`dashboard_mode` can be `actuals` or `projection`. Actuals mode renders the normalized
accounting cash rows. Projection mode renders the category catalog and rolls only the
recurring and one-off assumptions declared in `cashflow.yaml`. In both modes, section
subtotals are calculated from the displayed category rows rather than copied from a
source-reported total.

`ron_per_eur` is the fixed number of RON represented by one EUR. It is used only to
prepare the alternate dashboard display; source accounting values and projection
calculations remain in the configured `currency`.

## Accounting References

`accounting/actuals-2025.yaml` preserves both Keez reports:

- Cash flow: reported opening balances, activity sections, category rows, closing balances, and source totals.
- Profit: revenue and expense categories, EBITDA, amortization, financial costs, taxes, net profit, and source totals.

The engine checks monthly category sums, annual totals, profit formulas, cash closing
formulas, and opening-balance rollforwards. Keez displays whole RON while aggregating
more precise source values, so differences up to 6 RON are classified as report
rounding. Larger differences are reported as material source discrepancies.

The supplied cash report contains two material opening-balance rollforward differences.
They remain unchanged in the normalized fixture and are detected by the engine.

Profit amounts are never silently treated as cash amounts. The profit report supplies
the future operating category catalog; cash-only categories come from the cash report.
`Amortizare` is retained for profit reconciliation but excluded from cash because it is
non-cash. `Altele` is excluded from the future catalog because it has no activity.

## Projection Entries

Every projection entry references a category ID from the catalog:

```yaml
recurring:
  - id: "example-contract"
    name: "Example Contract"
    type: "inflow"
    amount: 5000.00
    start_date: "2026-01"
    end_date: null
    category: "software"
```

- Dates use `YYYY-MM`; recurring bounds are inclusive.
- Amounts are positive; `type` determines direction.
- IDs are unique across recurring and one-off entries.
- Recurring cancellations preserve the entry by setting `end_date` to the preceding month.
- A future one-off event cancelled before it occurs may be removed; historical actuals are not modified.
- Monetary inputs support up to two decimal places and 256 integer digits.
- The projection window is fixed at twelve months.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests cover Decimal precision, projection boundaries, strict schemas, normalized
accounting totals, source reconciliation, category mapping, HTML escaping, and the
13-column grouped report and currency-toggle contracts.
