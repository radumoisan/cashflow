# Cash Flow Agent Instructions

This project treats `cashflow.yaml` as the source of truth for editable scenarios and
`engine.py` as the only authority for financial calculations. Files in `keez-exports/`
are immutable accounting evidence. `accounting/actuals-2025.yaml` is a normalized
transcription and must only change when it is reconciled to a corresponding export.

## Delegation Rules

- Use the `explore` subagent whenever exploration or discovery is required.
- Use the `general` subagent whenever code changes are required.
- Delegate only one level: the main agent delegates to subagents, and subagents complete work without further delegation.
- The main agent splits delegated work into small, focused chunks and delegates each chunk to the appropriate subagent.

## Scenario Changes

When the user describes a business change:

1. Read `cashflow.yaml` and locate any related entries.
2. If `dashboard_mode` is `actuals`, ask whether to switch to `projection` and confirm the projection start and initial balance. Never apply scenarios to historical actuals.
3. Ask a concise question if the date, amount, direction, category, or intended entry is ambiguous.
4. Edit `cashflow.yaml`; do not edit `dashboard.html` directly.
5. Run `python engine.py` after every successful data change.
6. Report the ending and lowest balance exactly as printed by the engine for the active mode.

Never calculate or estimate financial balances in a response. Delegate all projection
math to `engine.py` and quote its output.

## Code Changes

- Run `python -m unittest discover -s tests -v` for code changes.

## Data Rules

- Dates must use `YYYY-MM`.
- Entry IDs must be stable and unique. Prefer lowercase hyphenated IDs.
- Entry `category` values must reference IDs declared under `categories`.
- Amounts must be positive numbers. Use `type` to determine direction.
- `settings.ron_per_eur` is the positive fixed exchange rate used for dashboard display conversion; one EUR equals that many RON.
- `inflow` adds cash and `outflow` removes cash.
- `settings.projection_months` must remain exactly `12`, matching current engine behavior.
- A recurring `end_date` is inclusive; `null` means indefinite.
- To stop a recurring item from a given month, set `end_date` to the preceding month.
- Do not delete an existing recurring entry to represent a cancellation. Preserve history with `end_date`.
- A future one-off event canceled before it occurs may be removed.
- Never modify historical actuals to represent a cancellation.
- One-off events apply only in their exact `date` month.
- Do not change `engine.py` or `template.html` for routine business scenario updates.
- Never copy profit values into cash assumptions without an explicit user instruction and stated cash-timing assumption.

## Examples

- "We signed a client for 3000/mo starting June 2026" means adding a recurring inflow starting `2026-06`.
- "We took a 40k loan in August 2026, paying 900/mo from September" means adding an event inflow in `2026-08` and a recurring outflow starting `2026-09`.
- "Cancel Figma from July 2026" means setting its recurring `end_date` to `2026-06`.
