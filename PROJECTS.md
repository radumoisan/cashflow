# Dedicated project cash flow

The project table tracks **net cash receipts and payments**, by payment month.
The company projection includes those plans and calculates VAT, taxes, and the
bank balance globally. `engine.py` performs every financial calculation.

## Agreed ADR assumptions

- Resources and costs belong entirely to this project. There are currently no
  internal staff allocated. Payroll remains an available category.
- ADR receipts are grants: no output VAT and excluded from the profit-tax proxy.
- Ordinary operating costs receive normal expense treatment in that proxy.
  Fixed-asset purchases are capital cash outflows, not immediate deductions.
- All charged project purchase VAT is assumed recoverable. Each expense can
  use **standard VAT** (currently 21%) or **no VAT**. Actuals also allow an
  explicitly confirmed signed VAT component.
- Purchase VAT is outside the project budget. The company pays it and recovers
  it through its VAT position; a credit does not invent an immediate cash refund.

These are user-confirmed forecasting assumptions, recorded in the project's
`source`. The existing global model is a cash-flow profit-tax proxy rather than
a statutory accrual/depreciation model. Exact supplied tax payments/checkpoints
retain their precedence.

## Plan the project

1. Select **ADR Project** in the scope selector.
2. Open **Plan items → Add item**. Enter a name, category, signed net RON amount,
   payment dates, and VAT treatment.
   - Positive adds cash; negative spends cash. Refunds retain their source sign.
   - Equal first/last months create a one-off purchase or grant instalment.
   - A range repeats that net amount monthly, stopping at the explicit end.
   - ADR receipts and payroll use **No VAT**.
3. Edit an item's monthly cells for exceptions. An override changes only that
   month. Clear it to restore the item's scheduled amount; `0` cancels the cash
   movement for that month explicitly.
4. Use **Categories** to add readable subcategories, rename, or archive them.
   Parent/category totals are calculated; each item retains its financial mapping.
5. When the original dated plan is ready, **Capture budget**. This is a one-time
   snapshot; subsequent plan edits do not overwrite it.

The initialized project has no placeholder dates, amounts, or funding. An
approved grant budget alone never creates a company receipt: schedule each
expected bank receipt. A purchase and its reimbursement can have different dates.

The views are:

- **Actuals + current plan:** tagged actuals in closed company months, current
  scheduled items afterwards. Matching closed-month forecasts are inactive.
- **Original budget:** the captured original schedule, read-only.
- **Variance to budget:** current/actual minus original, by month and category.
  Positive variance means a better net cash contribution, including underspending.
- **Company impact:** company closing balances with the project and with its
  future plans excluded. Both share the actual history and tax starting state;
  the engine recalculates global VAT/taxes for each comparison.

The project cumulative net contribution starts at zero at its dated beginning.
Moving the 12-month window carries earlier activity into the opening value.
Its funding gap excludes VAT/taxes; use Company impact for the bank-cash effect.
Displayed lowest balances refer to the selected 12-month window.

## How the company table adds up

Forecast direct rows combine regular-company inputs and mapped project plans.
The new **Grants / Project Funding** row is generated from project receipts.
Project VAT is calculated per item, so no-VAT purchases do not inherit the
standard supplier rate accidentally. Project equipment VAT also enters the
global VAT calculation.

Select **Company breakdown / Regular activity** to see editable regular-company
inputs and read-only project contributions together. The subtotals and closing
balances still reconcile to the full company. The regular asset input retains
its existing VAT-inclusive convention; project asset inputs are net and their
VAT is accounted for globally. Cell notes distinguish the contributions.

When a project-active accounting month is imported, the engine waits for its
allocation review before reseeding regular recurring amounts. Until then it
retains the previous regular run rate with an explicit note. Once reviewed,
it subtracts the source-basis project allocations before carrying the regular
portion forward. This prevents actual project costs from being added a second
time to subsequent project plans.

## Import and assign actuals

1. Supply complete company accounting months as before, through files/chat and
   `engine.import_actual_month`. The monthly source totals remain authoritative.
2. Open **Transactions → Import movement PDF** with the company's Keez
   **Receipts and Payments / Incasari si plati** export.
   - The parser checks the company registration.
   - Original PDFs are archived by content hash without overwriting evidence.
   - IDs distinguish movement lines even when statement reference numbers repeat.
   - Identical/overlapping complete exports preserve existing assignments.
     A changed/partial previously imported statement batch is rejected for
     explicit source reconciliation instead of silently duplicating a correction.
3. Filter by month, partner/description/reference, or assignment. Choose
   **Assign / VAT** for a project transaction.
4. Select its project/category, optionally match a planned item, verify the
   source company category, and select its VAT treatment.
   - `Standard` derives a net amount from the source gross cash using the dated
     configured rate and retains the cent residual as VAT.
   - `No VAT` uses the original cash as net.
   - `Confirmed signed VAT amount` uses a supplied VAT component with the same
     sign as the receipt/payment.
   - Unmatched actuals still contribute to their project/category totals.
5. After identifying every project transaction for a closed month, select that
   **Allocation review month → Mark month reviewed**. The imported movement cash
   must reconcile to the company month within the existing source-rounding
   tolerance. This confirmation concerns completeness of project identification,
   not a new accounting balance or a replacement tax checkpoint.

Source date, cash amount, reference and description are immutable through the
tagging UI. Changing ownership changes the allocation, not the source payment.
A payment belongs wholly to one project or remains regular/unassigned.
Actual project tags can be corrected; changing them reopens the affected review.
A sourced company-actual correction also reopens that month's project reviews;
an identical actual reimport preserves them.
Open-month tags are provisional and do not add another actual on top of its plan.
Incomplete project history is marked explicitly; zero unreviewed item cells show
an em dash. Cumulative totals then describe known amounts only.

### Net/source-basis reconciliation

The stored accounting actual is retained. Engine presentation moves the owned
source component to its project-mapped net category and transfers the difference
to global VAT, preserving total cash and the reported closing balance. This
also supports source grant cash classified under a broader company category.

For net Clients/Suppliers source rows the default source component is the item's
net cash. For source rows retaining VAT, such as Fixed Assets, it is gross cash.
January–August 2026 Supplier actuals use **recognized deductible VAT**, which can
differ from invoice cash timing. Where that matters, enter an explicit
**Source-basis allocation** and a **sourced allocation explanation**. Such
differences must be documented rather than forced into invented payments.
Project tagging does not promote the estimated VAT splits of 2025 reported
months into project facts; it requires net-basis accounting months.

## Persistence and structure

Version 3 extends the existing YAML with:

- `projects.<id>`: name, sourced assumptions, archived state, categories, items,
  an optional original `budget`, and `completed_months`.
- `categories[]`: stable `id`, name, group, company row, archived state.
- `items.<id>`: name, category, start/end, signed net `amount`, VAT treatment,
  and sparse monthly overrides. Each item has an independent VAT treatment.
- `movements.<id>`: source facts plus assignment, VAT selection, optional exact
  VAT/source-basis components and allocation explanation.

Financial mappings already used by a plan, budget or actual cannot be silently
changed; create a new category for a different mapping. Renaming and archiving
preserve history. Archiving a project preserves its existing plans as well as
history; end/delete future plan items explicitly to cancel future activity.

All writes validate and evaluate the complete scenario, use the quoted YAML
revision, and publish atomically. Item cells use the same draft-preserving
editing workflow as company cells. Scope/view navigation waits for pending
saves. Compound-operation conflicts retain the form and display the latest
saved source; the user can review it and submit the retained draft.

To inspect a PDF without modifying the scenario:

```bash
python keez.py keez-exports/movements-2026-todate.pdf
```

The static `dashboard.html` snapshot contains the company projection. The live
server provides project planning, comparisons, and assignment controls.
