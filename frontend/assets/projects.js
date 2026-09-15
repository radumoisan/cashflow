/* Project controls use engine-produced amounts. No financial arithmetic lives here. */
(() => {
  "use strict";

  function controls(ctx) {
    const { state, element: el, navigate } = ctx;
    const workspace = state.snapshot.workspace;
    const selector = el("select", { "aria-label": "Cash flow scope", onchange: (event) => navigate({ scope: event.target.value, view: "current" }) });
    for (const [value, label] of [["company", "Company"], ["regular", "Company breakdown / Regular activity"],
      ...Object.entries(workspace.projects).map(([id, project]) => [id, project.name + (project.archived ? " (archived)" : "")])]) {
      const option = el("option", { value, text: label });
      option.selected = state.scope === value;
      selector.append(option);
    }
    const result = [selector];
    const project = workspace.projects[state.scope];
    if (project) {
      const view = el("select", { "aria-label": "Project view", onchange: (event) => navigate({ view: event.target.value }) });
      for (const [value, label] of [["current", "Actuals + current plan"], ["budget", "Original budget"], ["variance", "Variance to budget"], ["impact", "Company impact"]]) {
        const option = el("option", { value, text: label });
        option.selected = state.view === value;
        view.append(option);
      }
      result.push(view);
      result.push(button(ctx, "Plan items", () => plans(ctx), project.archived));
      result.push(button(ctx, "Categories", () => categories(ctx), project.archived));
      result.push(button(ctx, "Capture budget", () => budget(ctx), project.archived || Boolean(project.budget)));
      result.push(button(ctx, "Project settings", () => settings(ctx)));
    }
    result.push(button(ctx, "Transactions", () => transactions(ctx)));
    result.push(button(ctx, "New project", () => createProject(ctx)));
    return result;
  }

  function button(ctx, text, onclick, disabled = false) {
    const result = ctx.element("button", { type: "button", className: "project-control", text, onclick });
    result.disabled = disabled || ctx.state.currency !== "RON";
    return result;
  }

  function summary(ctx) {
    const { state, element: el } = ctx;
    const view = state.snapshot.report;
    const project = state.snapshot.workspace.projects[state.scope];
    const panel = el("div", { className: "project-summary" });
    if (!project && state.scope === "company") {
      if (Object.keys(state.snapshot.workspace.projects).length) panel.append(el("span", { text: "Project plans feed company totals. Use Company breakdown to edit regular activity; select a project to plan its net receipts and costs." }));
      return panel;
    }
    if (!project) {
      panel.append(el("span", { text: "Editable regular-company inputs plus read-only project contributions. VAT, taxes and balances are company-wide." }));
      return panel;
    }
    const money = (value) => value?.[state.currency === "EUR" ? "eur" : "ron"] || "—";
    panel.append(el("strong", { text: project.name }));
    panel.append(el("span", { text: state.view === "impact" ? "Company closing balances; actual history shared, future project commitments excluded in the comparison." : "Project net amounts · purchase VAT recoverable and financed globally · grant non-taxable" }));
    panel.append(el("span", { text: `Ending: ${money(view.summary.ending_balance)} ${state.currency} · Lowest: ${money(view.summary.lowest_balance)} (${view.summary.lowest_month})` }));
    if (state.view === "impact") panel.append(el("span", { text: `Without future project — ending: ${money(view.summary.without_ending_balance)} · lowest: ${money(view.summary.without_lowest_balance)} (${view.summary.without_lowest_month})` }));
    else {
      panel.append(el("span", { text: view.summary.budget }));
      if (view.summary.allocation_status === "Incomplete" || view.summary.ending_balance.provenance === "incomplete") panel.append(el("strong", { className: "incomplete-note", text: "Allocation review incomplete: totals contain known tagged amounts only." }));
      if (!Object.keys(project.items).length) panel.append(el("span", { text: "Use Plan items to add a grant instalment, a purchase, or a bounded recurring expense." }));
    }
    return panel;
  }

  function panel(ctx, title) {
    if (ctx.state.pending || ctx.state.drafts.size) {
      ctx.announce("Finish the current cell save before opening project details.", true);
      return null;
    }
    document.querySelector("dialog.project-dialog")?.close();
    const el = ctx.element;
    const dialog = el("dialog", { className: "project-dialog", "aria-label": title });
    const heading = el("h2", { text: title });
    const close = button(ctx, "Close", () => dialog.close());
    const content = el("div", { className: "dialog-content" });
    const error = el("p", { className: "dialog-error", role: "alert" });
    dialog.append(el("div", { className: "dialog-heading" }, [heading, close]), content, error);
    let revision = ctx.state.snapshot.revision;
    let busy = false;
    dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
    dialog.addEventListener("close", () => { dialog.remove(); ctx.state.modal = Boolean(document.querySelector("dialog.project-dialog[open]")); });
    document.body.append(dialog);
    ctx.state.modal = true;
    dialog.showModal();
    async function run(command, success = () => dialog.close(), file = null) {
      if (busy) return;
      busy = true;
      const buttons = [...dialog.querySelectorAll("button")].map((button) => [button, button.disabled]);
      buttons.forEach(([button]) => { button.disabled = true; });
      error.textContent = "Saving…";
      try {
        await ctx.mutate(command, revision, file);
        revision = ctx.state.snapshot.revision;
        success();
      } catch (failure) {
        error.textContent = failure.message;
        if (ctx.state.snapshot.revision !== revision && !dialog.querySelector(".conflict-review")) {
          const project = ctx.state.snapshot.workspace.projects[command?.project_id || ctx.state.scope];
          const current = command?.id && command.action === "assign_movement"
            ? ctx.state.snapshot.workspace.movements.find((m) => m.id === command.id)
            : command?.id ? project?.items[command.id] : project;
          const review = el("details", { className: "conflict-review", open: "" }, [
            el("summary", { text: "Latest saved source — your draft above is retained" }),
            el("pre", { text: JSON.stringify(current || ctx.state.snapshot.workspace.projects, null, 2) }),
            button(ctx, "Use reviewed source revision", () => {
              revision = ctx.state.snapshot.revision;
              review.remove();
              error.textContent = "Source reviewed. Submit your retained draft when ready.";
            }),
          ]);
          dialog.append(review);
        }
      } finally {
        busy = false;
        buttons.forEach(([button, disabled]) => { button.disabled = disabled; });
      }
    }
    return { dialog, content, error, run };
  }

  function field(ctx, form, name, label, value = "", type = "text", options = null, required = true) {
    const el = ctx.element;
    const input = options ? el("select", { name, "aria-label": label }) : el("input", { name, type, "aria-label": label });
    if (options) for (const [id, text] of options) input.append(el("option", { value: id, text }));
    if (type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";
    input.required = required && type !== "checkbox";
    form.append(el("label", { className: "editor-field" }, [el("span", { text: label }), input]));
    return input;
  }

  function editor(ctx, title, build) {
    const p = panel(ctx, title);
    if (!p) return;
    const form = ctx.element("form", { className: "project-editor" });
    p.content.append(form);
    const command = build(form, p);
    const save = ctx.element("button", { type: "submit", className: "primary", text: "Save" });
    form.append(save);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      p.run(command());
    });
    form.querySelector("input,select")?.focus();
  }

  function plans(ctx) {
    const p = panel(ctx, "Project plan items");
    if (!p) return;
    p.content.append(ctx.element("p", { text: "Signed net RON: positive receipts, negative costs. Equal start/end dates create a one-off; a date range repeats monthly. Edit month-specific amounts in the table." }));
    p.content.append(button(ctx, "Add item", () => editItem(ctx)));
    const project = ctx.state.snapshot.workspace.projects[ctx.state.scope];
    const list = ctx.element("div", { className: "management-list" });
    for (const [id, item] of Object.entries(project.items)) {
      const category = project.categories.find((cat) => cat.id === item.category_id);
      list.append(ctx.element("div", { className: "management-row" }, [
        ctx.element("span", { text: `${item.name} · ${category.name} · ${item.start}–${item.end} · ${item.amount} net RON · VAT ${item.vat}` }),
        button(ctx, "Edit " + item.name, () => editItem(ctx, id)),
      ]));
    }
    if (!Object.keys(project.items).length) list.append(ctx.element("p", { text: "No planned items yet." }));
    p.content.append(list);
  }

  function editItem(ctx, id = null) {
    const project = ctx.state.snapshot.workspace.projects[ctx.state.scope];
    const old = project.items[id];
    const key = id || `item-${crypto.randomUUID()}`;
    editor(ctx, old ? "Edit plan item" : "Add plan item", (form, p) => {
      const name = field(ctx, form, "name", "Item name", old?.name);
      const cats = project.categories.filter((cat) => !cat.archived || cat.id === old?.category_id);
      const category = field(ctx, form, "category", "Category", old?.category_id || "suppliers", "text", cats.map((cat) => [cat.id, cat.name]));
      const amount = field(ctx, form, "amount", "Monthly net amount (signed RON)", old?.amount);
      amount.inputMode = "decimal";
      const first = field(ctx, form, "start", "First payment / receipt month", old?.start || ctx.state.snapshot.workspace.forecast_start, "month");
      const last = field(ctx, form, "end", "Last payment / receipt month", old?.end || first.value, "month");
      const vat = field(ctx, form, "vat", "VAT treatment", old?.vat || "standard", "text", [["standard", `Standard (${ctx.state.snapshot.workspace.standard_vat})`], ["none", "No VAT"]]);
      const updateVAT = () => {
        const cat = cats.find((cat) => cat.id === category.value);
        vat.disabled = cat?.group === "receipts" || cat?.group === "payroll";
        if (vat.disabled) vat.value = "none";
      };
      category.addEventListener("change", updateVAT);
      updateVAT();
      if (old) form.append(button(ctx, "Delete planned item", () => p.run({ action: "delete_item", project_id: ctx.state.scope, id })));
      return () => ({ action: "save_item", project_id: ctx.state.scope, id: key,
        item: { name: name.value, category_id: category.value, amount: amount.value, start: first.value, end: last.value,
          vat: vat.value, overrides: old?.overrides || {} } });
    });
  }

  function categories(ctx) {
    const p = panel(ctx, "Project categories");
    if (!p) return;
    p.content.append(button(ctx, "Add category", () => editCategory(ctx)));
    for (const category of ctx.state.snapshot.workspace.projects[ctx.state.scope].categories) {
      p.content.append(ctx.element("div", { className: "management-row" }, [
        ctx.element("span", { text: `${category.name} · ${category.company_row}${category.archived ? " · archived" : ""}` }),
        button(ctx, "Edit " + category.name, () => editCategory(ctx, category)),
      ]));
    }
  }

  function editCategory(ctx, old = null) {
    editor(ctx, old ? "Edit category" : "Add category", (form) => {
      const workspace = ctx.state.snapshot.workspace;
      const project = workspace.projects[ctx.state.scope];
      const name = field(ctx, form, "name", "Category name", old?.name);
      const group = field(ctx, form, "group", "Project group", old?.group || "suppliers", "text", Object.entries(workspace.groups));
      const row = field(ctx, form, "company-row", "Company category", old?.company_row || "suppliers", "text", [
        ["project-grants", "Grants / Project Funding"], ...workspace.company_rows.filter((row) => row.id !== "clients").map((row) => [row.id, row.name]),
      ]);
      group.addEventListener("change", () => { row.value = project.categories.find((cat) => cat.group === group.value)?.company_row || "suppliers"; });
      const archived = field(ctx, form, "archived", "Archive category", old?.archived || false, "checkbox");
      return () => ({ action: "save_category", project_id: ctx.state.scope,
        category: { id: old?.id || `category-${crypto.randomUUID()}`, name: name.value, group: group.value, company_row: row.value, archived: archived.checked } });
    });
  }

  function budget(ctx) {
    editor(ctx, "Capture original budget", (form) => {
      form.append(ctx.element("p", { text: "Capture the current dated plan once as the original budget. Later plan edits and actuals will be compared with this snapshot." }));
      const name = field(ctx, form, "name", "Budget name", "Original approved budget");
      return () => ({ action: "capture_budget", project_id: ctx.state.scope, name: name.value });
    });
  }

  function settings(ctx) {
    const project = ctx.state.snapshot.workspace.projects[ctx.state.scope];
    editor(ctx, "Project settings", (form) => {
      const name = field(ctx, form, "name", "Project name", project.name);
      const archived = field(ctx, form, "archived", "Archive project (preserve plans and history)", project.archived, "checkbox");
      form.append(ctx.element("p", { text: project.source }));
      return () => ({ action: "update_project", project_id: ctx.state.scope, name: name.value, archived: archived.checked });
    });
  }

  function createProject(ctx) {
    editor(ctx, "New dedicated grant project", (form) => {
      const id = field(ctx, form, "id", "Project ID (lowercase, hyphenated)");
      const name = field(ctx, form, "name", "Project name");
      const source = field(ctx, form, "source", "Source / confirmed modelling assumptions", "User assumption: non-taxable grant; normal operating costs; fully recoverable purchase VAT; dedicated resources.");
      return () => ({ action: "create_project", id: id.value, name: name.value, source: source.value });
    });
  }

  function transactions(ctx) {
    const p = panel(ctx, "Accounting transactions");
    if (!p) return;
    const el = ctx.element;
    const workspace = ctx.state.snapshot.workspace;
    const filters = el("div", { className: "transaction-filters" });
    const search = field(ctx, filters, "search", "Search partner / details / reference", "", "search", null, false);
    const month = field(ctx, filters, "month", "Transaction month", "", "month", null, false);
    const owner = field(ctx, filters, "owner", "Assignment", "all", "text", [["all", "All transactions"], ["unassigned", "Unassigned"], ...Object.entries(workspace.projects).map(([key, project]) => [key, project.name])]);
    const upload = el("input", { type: "file", accept: "application/pdf,.pdf", "aria-label": "Keez movement PDF" });
    const importButton = button(ctx, "Import movement PDF", () => {
      if (!upload.files[0]) { p.error.textContent = "Select a Keez Receipts and Payments PDF."; return; }
      p.run(null, () => transactions(ctx), upload.files[0]);
    });
    p.content.append(el("p", { text: "Import Receipts and Payments, then assign project transactions and confirm VAT. Open-month tags stay provisional until the full company accounting month is imported. Reference numbers can contain multiple movements." }), filters,
      el("div", { className: "import-controls" }, [upload, importButton]));
    const list = el("div", { className: "transaction-list" });
    p.content.append(list);
    function render() {
      const needle = search.value.toLowerCase();
      const matches = workspace.movements.filter((m) => (!month.value || m.date.startsWith(month.value)) &&
        (owner.value === "all" || (owner.value === "unassigned" ? !m.project_id : m.project_id === owner.value)) &&
        `${m.partner} ${m.description} ${m.reference}`.toLowerCase().includes(needle));
      list.replaceChildren(el("p", { text: `${matches.length} matching transactions` }));
      for (const movement of matches) {
        list.append(el("div", { className: "transaction-row", "data-movement-id": movement.id }, [
          el("span", { text: `${movement.date} · ${movement.partner || movement.reference} · ${movement.cash.ron} RON cash` }),
          el("span", { className: "transaction-description", text: movement.description, title: movement.source }),
          el("span", { text: `${workspace.projects[movement.project_id]?.name || "Unassigned"}${movement.net ? ` · ${movement.net.ron} net` : ""}${movement.closed ? "" : " · provisional"}` }),
          button(ctx, "Assign / VAT", () => assignMovement(ctx, movement)),
        ]));
      }
    }
    [search, month, owner].forEach((input) => input.addEventListener("input", render));
    render();
    const project = workspace.projects[ctx.state.scope];
    if (project) {
      const review = el("div", { className: "allocation-review" });
      const date = field(ctx, review, "review-month", "Allocation review month", "", "month");
      review.append(button(ctx, "Mark month reviewed", () => p.run({ action: "complete_month", project_id: ctx.state.scope, month: date.value, complete: true }, () => transactions(ctx))));
      review.append(button(ctx, "Reopen month review", () => p.run({ action: "complete_month", project_id: ctx.state.scope, month: date.value, complete: false }, () => transactions(ctx))));
      review.append(el("p", { text: `Reviewed months: ${project.completed_months.join(", ") || "none"}. Mark reviewed after all project transactions are assigned. Imported cash must reconcile with the company month.` }));
      p.content.append(review);
    }
    const reconciliation = el("details", {}, [el("summary", { text: "Company movement reconciliation" })]);
    for (const item of workspace.reconciliation) reconciliation.append(el("p", { text: `${item.month}: ${item.count} movements · difference ${item.difference.ron} RON · ${item.reconciled ? "within source-rounding tolerance" : "incomplete or mismatched"}` }));
    p.content.append(reconciliation);
  }

  function assignMovement(ctx, movement) {
    const workspace = ctx.state.snapshot.workspace;
    editor(ctx, "Assign transaction and confirm VAT", (form) => {
      form.append(ctx.element("p", { text: `${movement.date} · ${movement.partner} · ${movement.cash.ron} RON cash. ${movement.description}` }));
      form.append(ctx.element("small", { text: movement.source }));
      const initialOwner = movement.project_id || (workspace.projects[ctx.state.scope] ? ctx.state.scope : "");
      const owner = field(ctx, form, "project", "Project assignment", initialOwner, "text", [["", "Regular / unassigned"], ...Object.entries(workspace.projects).filter(([, project]) => !project.archived).map(([key, project]) => [key, project.name])], false);
      const category = field(ctx, form, "category", "Project category", "", "text", [], false);
      const item = field(ctx, form, "item", "Match plan item (optional)", "", "text", [], false);
      const sourceRow = field(ctx, form, "source-row", "Source company category", movement.row_id || "", "text", [["", "Select source category"], ...workspace.company_rows.map((row) => [row.id, row.name])], false);
      const vat = field(ctx, form, "vat", "VAT treatment", movement.vat || "standard", "text", [["standard", `Standard (${workspace.standard_vat})`], ["none", "No VAT"], ["confirmed", "Confirmed signed VAT amount"]]);
      const vatAmount = field(ctx, form, "vat-amount", "Confirmed VAT component (signed RON)", movement.vat_amount, "text", null, false);
      const component = field(ctx, form, "company-amount", "Source-basis allocation (optional signed RON)", movement.company_amount, "text", null, false);
      const note = field(ctx, form, "allocation-note", "Allocation explanation / source", movement.allocation_note, "text", null, false);
      form.append(ctx.element("small", { text: "Leave source-basis allocation empty for the normal split. Supply it only to reconcile documented VAT recognition/payment differences in an accounting net row. Source cash is preserved." }));
      function updateItems() {
        item.replaceChildren(ctx.element("option", { value: "", text: "Unmatched actuals" }));
        for (const [id, plan] of Object.entries(workspace.projects[owner.value]?.items || {})) if (plan.category_id === category.value) item.append(ctx.element("option", { value: id, text: plan.name }));
        if ([...item.options].some((o) => o.value === movement.item_id)) item.value = movement.item_id;
        const group = workspace.projects[owner.value]?.categories.find((cat) => cat.id === category.value)?.group;
        vat.disabled = group === "receipts" || group === "payroll";
        if (vat.disabled) vat.value = "none";
        vatAmount.closest("label").hidden = vat.value !== "confirmed";
      }
      function updateCategories() {
        category.replaceChildren();
        const cats = workspace.projects[owner.value]?.categories || [];
        for (const cat of cats) category.append(ctx.element("option", { value: cat.id, text: cat.name }));
        const match = cats.find((cat) => cat.id === movement.category_id) || cats.find((cat) => cat.company_row === movement.row_id) || cats.find((cat) => cat.group === "suppliers");
        if (match) category.value = match.id;
        category.disabled = item.disabled = !owner.value;
        sourceRow.required = Boolean(owner.value);
        updateItems();
      }
      owner.addEventListener("change", updateCategories);
      category.addEventListener("change", updateItems);
      vat.addEventListener("change", () => { vatAmount.closest("label").hidden = vat.value !== "confirmed"; });
      updateCategories();
      return () => ({ action: "assign_movement", id: movement.id, project_id: owner.value || null,
        category_id: owner.value ? category.value : null, item_id: owner.value && item.value ? item.value : null,
        row_id: sourceRow.value || null, vat: vat.value, vat_amount: vat.value === "confirmed" ? vatAmount.value : null,
        company_amount: component.value || null, allocation_note: note.value });
    });
  }

  window.ProjectUI = { controls, summary };
})();
