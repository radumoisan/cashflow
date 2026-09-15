# Regio: expense forecasts and accounting actuals

Regio sits inside **Expenses**, after the regular company categories. Its category
rows are Suppliers (net), Payroll, Fixed Assets, Advances, and Miscellaneous.
**Total Regio** is included once in **Total Expenses**. VAT and Taxes are global.

## Forecast now

1. Expand Regio in the table and enter signed RON amounts in forecast months.
2. Each cell is a monthly category total. Negative spends cash; positive records
   a refund. Clearing a cell restores zero; entering zero saves an explicit zero.
3. A project amount applies only to its month. It never establishes a run rate.
4. Enter regular-company expenses in the root rows and project expenses in Regio.
   The root rows exclude project costs.

**Input basis matters:** Suppliers excludes VAT and participates in the company's
standard-rate VAT estimate. Payroll, Fixed Assets, Advances, and Miscellaneous
use the same cash basis as their root counterparts, including any VAT already in
the payment. They do not generate an additional VAT cash movement. Project
expenses inherit the corresponding profit-proxy weights; fixed assets and advances
are not immediate profit-proxy deductions.

Regio is available across the whole timeline. It starts at zero. Existing 2025
history has explicit, sourced zero-allocation assumptions so initialization has
no effect on balances or forecast run rates. Those assumptions are distinguishable
from reviewed accounting allocations in cell notes.

Regio can collapse independently of Expenses. Its total remains visible while
Regio is collapsed; collapsing Expenses hides its contents and keeps Total
Expenses visible. EUR is read-only. The generated snapshot includes this layout.

## Move forward with accounting

1. Update the accounting exports in **`keez-exports/`**.
2. Tell the assistant to import them and identify which expenses belong to Regio
   (for example by date, supplier, amount, or statement reference).
3. The assistant inspects and reconciles the sources, imports complete company
   months and detailed movements through the backend, and applies your Regio
   assignments. Each movement belongs wholly to Regio or regular activity.
4. Once your identification of project expenses is complete and the source cash
   reconciles, the assistant records the month's completed review, saves the
   validated data, and runs `python engine.py`. The table displays the result.

The frontend is for forecast editing and read-only actuals display. Imports,
assignments, and review completion are handled through files/chat, not browser
upload or tagging controls. The assistant asks for clarification only when an
expense, accounting basis, or completeness of the supplied information is ambiguous.

Assigning a closed-month movement moves its category-basis amount from the
regular row into Regio. It does not change the authoritative accounting record,
the existing actual VAT or Taxes row, monthly net cash, or reported closing cash.
For reported/gross history, Suppliers uses the same estimated standard-rate net
presentation as the company; source amounts remain unchanged and cell notes flag
the estimate. Exact supplier accounting-basis bridges require net actual months.

While review is incomplete, the table displays known allocations with dotted
provenance. Zero cells show **—**, meaning no allocated amount is yet established,
not confirmed zero project activity. The total covers known allocations only.
The previous usable regular run rate remains in place. Once reviewed, recurring
regular expenses carry forward from **accounting actual minus Regio allocations**.
Future Regio months continue to use their own monthly forecasts.

Tags in open months are provisional and never add cash on top of a forecast.
Importing a complete actual month supersedes its forecast and activates its
source movement allocations. Later-month overrides retain their dates.

## Source basis and corrections

Supplier allocations may use the agreed standard-rate net estimate, a no-VAT
amount, or a supplied signed VAT component. A documented `source_amount` and
explanation bridge differences between invoice cash and the authoritative
monthly supplier row's VAT recognition basis. This is the whole movement's
accounting-basis component, never a partial project share. Other project
categories retain their corresponding company row's cash basis.

- Tell the assistant when an assignment needs correction. Removing project
  ownership restores the amount
  to the regular row. Changed assignments reopen that month's review.
- Accounting corrections use `engine.import_actual_month` with source evidence
  and reopen review; an identical reimport retains it.
- Review fingerprints also detect external changes to accounting facts, source
  movements, category mappings, or VAT assumptions. Stale review fingerprints
  cannot silently seed future regular run rates.
- The assistant treats supplied PDFs as read-only source evidence.
  Identical or overlapping complete imports preserve assignments. A changed or
  partial previously imported statement/reference batch is rejected for explicit
  reconciliation, avoiding duplicate corrected cash.

## Backend support and stored inputs

- `keez.parse_movements_pdf` reads detailed Keez **Receipts and Payments /
  Incasari si plati** exports. It requires Poppler's `pdftotext` executable.
- `engine.import_actual_month` validates complete company accounting months.
- `engine.import_expense_movements` validates and merges sourced movements.
- `engine.expense_command` assigns/unassigns whole movements and records completed
  or reopened reviews. Review validates cash reconciliation and category bounds.
- `engine.allocation_status` reports whether a closed month's review is complete.

These helpers return validated candidate data; they do not independently publish
it. The assistant uses them during the requested backend import, retains source
explanations, and saves only after validation. The source accounting records remain
authoritative; project rows are not additional accounting cash.

Schema version 3 adds:

- `expense_projects.<id>`: name, initialization source, categories.
- `categories[]`: stable `id`, name, mapped `row_id`, sparse `overrides`.
- `movements.<id>`: immutable date, reference, source, description, partner, source
  row, cash amount; optional project/category ownership, VAT treatment, signed
  VAT/source-basis components, and sourced allocation explanation.
- `allocation_reviews.<YYYY-MM>`: `kind` (`assumption` or `reviewed`), source, and
  engine-generated source fingerprint. Do not hand-edit fingerprints.

`PATCH /api/cell` accepts namespaced forecast IDs such as
`project-regio-suppliers`, using the existing `{row_id, month, value, currency}`
body and quoted YAML revision. Direct historical cell editing stays locked.

The earlier `PROJECTS.md`, `frontend/assets/projects.js`, and project test drafts
describe an unintegrated broader system. This document describes the active
Regio workflow.
