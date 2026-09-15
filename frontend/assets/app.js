(() => {
  "use strict";

  const api = { state: "/api/state", cell: "/api/cell" };
  const report = document.querySelector(".report");
  const status = document.querySelector("#status");
  const state = {
    snapshot: null, currency: "RON", drafts: new Map(), pending: 0,
    queue: Promise.resolve(), requested: null, loading: false,
    pollInFlight: false, sequence: 0,
  };
  const decimal = /^-?\d+(?:\.\d{1,2})?$/;
  const valid = (value) => value === "" || decimal.test(value);
  const keyOf = (rowId, month) => `${rowId}:${month}`;
  const errorMessage = (payload) => payload?.error?.message || "The request could not be completed.";
  const element = (tag, properties = {}, children = []) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(properties)) {
      if (key === "className") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value !== null && value !== undefined) node.setAttribute(key, String(value));
    }
    node.append(...children);
    return node;
  };

  function announce(message, visible = false, draftKey = null) {
    status.classList.toggle("visually-hidden", !visible);
    status.replaceChildren(document.createTextNode(message));
    if (draftKey) status.append(element("button", {
      type: "button", text: "Discard unsaved edit", onclick: () => cancelDraft(draftKey),
    }));
  }

  function rows(view) {
    const flatten = (group) => [...group.rows, ...(group.children || []).flatMap(flatten), ...(group.subtotal ? [group.subtotal] : [])];
    return [view.opening_balance, ...view.activity_groups.flatMap(flatten), view.closing_balance];
  }

  function modelCell(rowId, month) {
    const view = state.snapshot?.report;
    if (!view) return null;
    const index = view.months.indexOf(month);
    return rows(view).find((row) => row.id === rowId)?.cells[index] || null;
  }

  const targetInput = (key) => [...document.querySelectorAll("td.amount input")].find((input) => input.dataset.key === key);
  const collapsedGroups = () => new Set([...document.querySelectorAll('.group-toggle[aria-expanded="false"]')].map((button) => button.getAttribute("aria-controls")));
  const groupSignature = (group) => [group.id, group.name, Boolean(group.subtotal), (group.children || []).map(groupSignature)];
  const signature = (view) => JSON.stringify({
    months: view.months, monthLabels: view.month_labels, currency: view.currency,
    rows: rows(view).map((row) => [row.id, row.name]),
    groups: view.activity_groups.map(groupSignature),
  });

  function cancelDraft(key) {
    const draft = state.drafts.get(key);
    if (!draft) return;
    if (draft.pending) {
      draft.value = draft.submitted.value;
      draft.sequence = draft.submitted.sequence;
      draft.error = null;
      announce("Save in progress; newer unsent typing cancelled.");
    } else {
      state.drafts.delete(key);
      announce("Unsaved edit discarded.");
    }
    apply(state.snapshot.report);
    finishNavigation();
  }

  function paintCell(cell, value, rowName) {
    const key = keyOf(cell.dataset.rowId, cell.dataset.month);
    const draft = state.drafts.get(key);
    cell.dataset.ron = value.ron;
    cell.dataset.eur = value.eur;
    cell.dataset.source = value.source;
    cell.dataset.provenance = value.provenance;
    cell.classList.toggle("negative", value.source.startsWith("-"));
    cell.classList.toggle("invalid", Boolean(draft?.error));
    cell.classList.toggle("pending", Boolean(draft?.pending));
    const description = `${value.provenance}: ${value.note}`;
    cell.title = description;
    if (state.currency !== "RON" || !value.editable) {
      cell.textContent = state.currency === "EUR" ? value.eur : value.ron;
      cell.classList.toggle("accounting-negative", cell.textContent.startsWith("("));
      return;
    }
    let input = cell.querySelector("input");
    if (!input) {
      input = element("input", { type: "text", inputmode: "decimal", "data-key": key });
      input.addEventListener("focus", () => {
        if (!state.drafts.has(key)) input.value = cell.dataset.source;
        cell.classList.remove("accounting-negative");
        input.select();
      });
      input.addEventListener("input", () => {
        const current = state.drafts.get(key) || { row_id: cell.dataset.rowId, month: cell.dataset.month, pending: false };
        current.value = input.value;
        current.sequence = ++state.sequence;
        current.error = null;
        state.drafts.set(key, current);
        input.removeAttribute("aria-invalid");
        cell.classList.remove("invalid");
        cell.classList.remove("accounting-negative");
      });
      input.addEventListener("blur", () => {
        commit(key);
        if (!state.drafts.has(key)) apply(state.snapshot.report);
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          cancelDraft(key);
        } else if (event.key === "Enter") {
          event.preventDefault();
          if (commit(key)) {
            const inputs = [...document.querySelectorAll(".activity-children:not([hidden]) td.amount input")];
            inputs[inputs.indexOf(input) + 1]?.focus();
          }
        }
      });
      cell.replaceChildren(input);
    }
    input.setAttribute("aria-label", `${rowName}, ${cell.dataset.month} cash flow amount, ${value.provenance}`);
    input.title = draft?.error || `${description}. Signed RON assumption. Clear to restore automatic estimation.`;
    input.disabled = state.loading;
    if (draft?.error) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
    const text = draft ? draft.value : document.activeElement === input ? value.source : value.ron;
    cell.classList.toggle("accounting-negative", text.startsWith("("));
    if (input.value !== text) input.value = text;
  }

  function tableRow(data, view, className) {
    const cells = data.cells.map((value, index) => {
      const cell = element("td", { className: "amount", "data-row-id": data.id, "data-month": view.months[index] });
      paintCell(cell, value, data.name);
      return cell;
    });
    return element("tr", { className }, [element("th", { scope: "row", text: data.name }), ...cells]);
  }

  function sectionGap(view) {
    return element("tbody", { className: "section-gap", "aria-hidden": "true" }, [
      element("tr", {}, [element("td", { colspan: String(view.months.length + 1) })]),
    ]);
  }

  function updateGroupVisibility() {
    const collapsed = collapsedGroups();
    document.querySelectorAll("tbody[data-groups]").forEach((body) => {
      body.hidden = body.dataset.groups.split(" ").some((id) => collapsed.has(`group-${id}-children`));
    });
  }

  function groupBodies(group, view, collapsed, ancestors = []) {
    if (!group.subtotal) {
      return [element("tbody", { className: "standalone-section" }, group.rows.map((row) => tableRow(row, view, `standalone-row ${group.id}-row`)))];
    }
    const id = `group-${group.id}-children`;
    const nested = ancestors.length ? " project-section" : "";
    const children = element("tbody", { className: `activity-children${nested}`, id, "data-groups": [...ancestors, group.id].join(" ") }, group.rows.map((row) => tableRow(row, view, `subcategory-row ${group.id}-row`)));
    children.hidden = collapsed.has(id);
    const toggle = element("button", { className: "group-toggle", type: "button", "aria-expanded": String(!children.hidden), "aria-controls": id });
    const mark = element("span", { className: "toggle-mark", "aria-hidden": "true", text: children.hidden ? "+" : "-" });
    toggle.append(mark, element("span", { text: group.name }));
    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!expanded));
      mark.textContent = expanded ? "+" : "-";
      updateGroupVisibility();
    });
    return [
      element("tbody", { className: `activity-heading${nested}`, "data-groups": ancestors.join(" ") }, [element("tr", { className: `activity-heading-row ${group.id}-heading` }, [
        element("th", { scope: "row" }, [toggle]), ...view.months.map(() => element("td", { className: "activity-fill", "aria-hidden": "true" })),
      ])]),
      children,
      ...(group.children || []).flatMap((child) => groupBodies(child, view, collapsed, [...ancestors, group.id])),
      element("tbody", { className: `activity-subtotal${nested}`, "data-groups": ancestors.join(" ") }, [tableRow(group.subtotal, view, `subtotal-row ${group.id}-subtotal`)]),
    ];
  }

  function toolbar(view) {
    const nav = view.navigation;
    const range = `${nav.start} – ${nav.end}`;
    const picker = element("input", { type: "month", value: nav.start, min: nav.min_start, max: nav.max_start,
      "aria-label": "First month of the twelve-month window", title: range,
      onclick: (event) => event.currentTarget.showPicker?.(),
      onchange: (event) => {
        const start = event.target.value;
        event.target.value = state.snapshot.report.months[0];
        navigate({ start });
      },
    });
    const previous = element("button", { type: "button", text: "‹", "aria-label": "Previous month", onclick: () => navigate({ start: nav.previous }) });
    const next = element("button", { type: "button", text: "›", "aria-label": "Next month", onclick: () => navigate({ start: nav.next }) });
    previous.disabled = !nav.previous;
    next.disabled = !nav.next;
    const period = element("div", { className: "period-control" }, [previous, element("label", { className: "period-range" }, [element("span", { text: range }), picker]), next]);
    const toggle = element("div", { className: "currency-toggle", role: "group", "aria-label": "Display currency" });
    ["RON", "EUR"].forEach((currency) => toggle.append(element("button", {
      className: "currency-option", type: "button", "data-currency": currency,
      "aria-pressed": String(state.currency === currency), text: currency, onclick: () => navigate({ currency }),
    })));
    const controls = [period];
    if (view.activity_groups.some((group) => group.children?.length)) {
      controls.push(element("span", { className: "project-hint", text: "Root expenses: regular activity · Regio: monthly amounts, default zero" }));
    }
    controls.push(toggle);
    return element("div", { className: "toolbar" }, controls);
  }

  function render(view) {
    const collapsed = collapsedGroups();
    const previousScroll = document.querySelector(".table-scroll");
    const scroll = { left: previousScroll?.scrollLeft || 0, top: previousScroll?.scrollTop || 0 };
    const table = element("table", { "aria-label": "Twelve-month cash flow" }, [
      element("colgroup", {}, [element("col", { className: "category-column" }), element("col", { span: "12" })]),
      element("thead", {}, [element("tr", {}, [element("th", { scope: "col", text: "Category" }), ...view.month_labels.map((label) => element("th", { scope: "col", text: label }))])]),
      element("tbody", { className: "balance-section" }, [tableRow(view.opening_balance, view, "balance-row opening-row")]),
      ...view.activity_groups.flatMap((group) => [sectionGap(view), ...groupBodies(group, view, collapsed)]),
      sectionGap(view),
      element("tbody", { className: "balance-section" }, [tableRow(view.closing_balance, view, "balance-row closing-row")]),
    ]);
    report.replaceChildren(toolbar(view), element("div", { className: "table-scroll" }, [table]), status);
    updateGroupVisibility();
    const replacement = document.querySelector(".table-scroll");
    replacement.scrollLeft = scroll.left;
    replacement.scrollTop = scroll.top;
    report.dataset.displayCurrency = state.currency;
    document.title = `Cash Flow | ${view.months[0]} to ${view.months[11]}`;
  }

  function apply(view) {
    const lookup = new Map(rows(view).map((row) => [row.id, row]));
    document.querySelectorAll("td.amount").forEach((cell) => {
      const row = lookup.get(cell.dataset.rowId);
      const index = view.months.indexOf(cell.dataset.month);
      if (row && index >= 0) paintCell(cell, row.cells[index], row.name);
    });
  }

  function adopt(payload) {
    const previous = state.snapshot;
    state.snapshot = payload;
    if (!previous || signature(previous.report) !== signature(payload.report)) render(payload.report);
    else apply(payload.report);
  }

  function endpoint(path, start) {
    return start ? `${path}?${new URLSearchParams({ start })}` : path;
  }

  async function getState(start, conditional = false) {
    const etag = state.snapshot?.etag;
    const response = await fetch(endpoint(api.state, start), {
      cache: "no-store", headers: conditional && etag ? { "If-None-Match": etag } : {},
    });
    if (response.status === 304) return null;
    const payload = await response.json();
    if (!response.ok) throw new Error(errorMessage(payload));
    payload.etag = response.headers.get("ETag");
    return payload;
  }

  function draftError(key, message) {
    const draft = state.drafts.get(key);
    if (draft) draft.error = message;
    apply(state.snapshot.report);
    announce(`Not saved: ${draft?.row_id}, ${draft?.month}, ${draft?.value || "clear override"}. ${message}`, true, key);
  }

  function commit(key) {
    const draft = state.drafts.get(key);
    if (!draft) return true;
    if (draft.pending) return true;
    if (!valid(draft.value)) {
      draftError(key, "Enter a signed decimal with at most two places, or clear the cell to restore estimation.");
      return false;
    }
    const cell = modelCell(draft.row_id, draft.month);
    if (!cell?.editable) {
      draftError(key, "This cell is now read-only. The unsaved edit has been retained for review.");
      return false;
    }
    draft.error = null;
    draft.pending = true;
    draft.submitted = { value: draft.value, sequence: draft.sequence };
    const target = { ...draft.submitted, row_id: draft.row_id, month: draft.month, baseOverride: cell.override };
    state.pending += 1;
    apply(state.snapshot.report);
    state.queue = state.queue.then(() => persist(key, target));
    return true;
  }

  async function patch(target) {
    const { row_id, month } = target;
    return fetch(endpoint(api.cell, state.snapshot.report.months[0]), {
      method: "PATCH", headers: { "Content-Type": "application/json", "If-Match": `"${state.snapshot.revision}"` },
      body: JSON.stringify({ row_id, month, value: target.value === "" ? null : target.value, currency: "RON" }),
    });
  }

  function assertTargetUnchanged(target) {
    const current = modelCell(target.row_id, target.month);
    if (!current?.editable || current.override !== target.baseOverride) {
      throw new Error(`The same cell changed externally (current RON amount: ${current?.ron || "unavailable"}). Review before retrying or discard the draft.`);
    }
  }

  async function persist(key, target) {
    let saved = false;
    try {
      assertTargetUnchanged(target);
      let response = await patch(target);
      if (response.status === 409) {
        adopt(await getState(state.snapshot.report.months[0]));
        assertTargetUnchanged(target);
        response = await patch(target);
      }
      const payload = await response.json();
      if (!response.ok) throw new Error(errorMessage(payload));
      payload.etag = response.headers.get("ETag");
      const draft = state.drafts.get(key);
      if (draft?.sequence === target.sequence) state.drafts.delete(key);
      adopt(payload);
      saved = true;
      announce("Saved.");
    } catch (error) {
      draftError(key, error.message);
      state.requested = null;
    } finally {
      state.pending -= 1;
      const latest = state.drafts.get(key);
      if (latest) latest.pending = false;
      if (saved && latest) commit(key);
      apply(state.snapshot.report);
      finishNavigation();
    }
  }

  function navigate(change) {
    if (!state.snapshot || state.loading || ("start" in change && !change.start)) return;
    for (const [key, draft] of state.drafts) {
      if (draft.error || !valid(draft.value)) {
        draftError(key, draft.error || "Correct this amount before changing the view.");
        targetInput(key)?.focus();
        return;
      }
    }
    state.requested = { start: state.snapshot.report.months[0], currency: state.currency, ...state.requested, ...change };
    for (const key of state.drafts.keys()) {
      if (!commit(key)) { state.requested = null; return; }
    }
    finishNavigation();
  }

  async function finishNavigation() {
    if (!state.requested || state.pending || state.loading || state.drafts.size) return;
    const requested = state.requested;
    state.requested = null;
    if (requested.start !== state.snapshot.report.months[0]) {
      await loadWindow(requested.start, requested.currency);
    } else if (requested.currency !== state.currency) {
      state.currency = requested.currency;
      render(state.snapshot.report);
      announce(state.currency === "EUR" ? "EUR is display-only. Switch to RON to edit assumptions." : "RON forecast assumptions are editable.");
    }
  }

  async function loadWindow(start, currency = state.currency) {
    state.loading = true;
    state.sequence += 1;
    document.querySelectorAll(".toolbar button, .toolbar input, td.amount input").forEach((input) => { input.disabled = true; });
    try {
      const payload = await getState(start);
      state.currency = currency;
      state.snapshot = payload;
      state.loading = false;
      render(payload.report);
      window.history.replaceState(null, "", `?${new URLSearchParams({ start: payload.report.months[0] })}`);
      announce("");
    } catch (error) {
      state.loading = false;
      if (state.snapshot) render(state.snapshot.report);
      announce(`Source unavailable or invalid: ${error.message}`, true);
    }
  }

  async function poll() {
    if (!state.snapshot) {
      if (!state.loading) await loadWindow(new URLSearchParams(window.location.search).get("start"));
      return;
    }
    if (state.loading || state.pollInFlight || state.pending || state.drafts.size) return;
    const sequence = state.sequence;
    const revision = state.snapshot.revision;
    const start = state.snapshot.report.months[0];
    state.pollInFlight = true;
    try {
      const payload = await getState(start, true);
      if (state.sequence === sequence && state.snapshot.revision === revision && !state.loading && !state.pending && !state.drafts.size) {
        if (payload) adopt(payload);
        announce("");
      }
    } catch (error) {
      announce(`Source unavailable or invalid: ${error.message}`, true);
    } finally {
      state.pollInFlight = false;
    }
  }

  loadWindow(new URLSearchParams(window.location.search).get("start"));
  window.setInterval(poll, 2000);
})();
