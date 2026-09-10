const statusEl = document.getElementById("status");

function el(tag, props, children) {
  const node = document.createElement(tag);
  Object.assign(node, props || {});
  for (const child of children || []) node.append(child);
  return node;
}

function emptyRow(message) {
  return el("div", { className: "empty", textContent: message });
}

// ---- JSON download -------------------------------------------------------

function downloadJSON(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

// ---- Confirm modal -------------------------------------------------------

function showConfirmModal({ title, bodyNodes, confirmText = "Confirm", danger = true }) {
  return new Promise((resolve) => {
    const overlay = document.getElementById("modal-overlay");
    const titleEl = document.getElementById("modal-title");
    const bodyEl = document.getElementById("modal-body");
    const confirmBtn = document.getElementById("modal-confirm");
    const cancelBtn = document.getElementById("modal-cancel");

    titleEl.textContent = title;
    bodyEl.innerHTML = "";
    bodyEl.append(...bodyNodes);
    confirmBtn.textContent = confirmText;
    confirmBtn.className = danger ? "danger" : "primary";

    function cleanup(result) {
      overlay.hidden = true;
      confirmBtn.removeEventListener("click", onConfirm);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onOverlayClick);
      document.removeEventListener("keydown", onKeydown);
      resolve(result);
    }
    function onConfirm() {
      cleanup(true);
    }
    function onCancel() {
      cleanup(false);
    }
    function onOverlayClick(event) {
      if (event.target === overlay) cleanup(false);
    }
    function onKeydown(event) {
      if (event.key === "Escape") cleanup(false);
    }

    confirmBtn.addEventListener("click", onConfirm);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onOverlayClick);
    document.addEventListener("keydown", onKeydown);
    overlay.hidden = false;
    confirmBtn.focus();
  });
}

// Builds a "why is this safe/unsafe" verdict from the same used_in/integration_status/
// yaml_defined signals the tables already show, plus one extra caller-supplied signal
// (current state for duplicates, or the flagged reason for inactive entities).
function assessSafety({ usedIn, integrationStatus, yamlDefined, extraSignal }) {
  const reasons = [];
  let riskCount = 0;

  if (!usedIn || usedIn.length === 0) {
    reasons.push({ ok: true, text: "Not referenced in any automation, script, dashboard, or template file." });
  } else {
    riskCount += 1;
    reasons.push({ ok: false, text: `Referenced in ${usedIn.length} YAML file(s):`, files: usedIn });
  }

  if (integrationStatus === "active") {
    riskCount += 1;
    reasons.push({ ok: false, text: "Owning integration is currently active and loaded." });
  } else {
    reasons.push({ ok: true, text: `Integration status: ${integrationStatus}` });
  }

  // yamlDefined is only meaningful for hand-authored domains (automation, script,
  // input_*, ...) -- null/undefined means it doesn't apply (e.g. integration-backed).
  if (yamlDefined === false) {
    reasons.push({ ok: true, text: "No longer defined in any YAML file — this looks like an orphaned registry entry from a since-edited/deleted YAML block." });
  } else if (yamlDefined === true) {
    riskCount += 1;
    reasons.push({ ok: false, text: "Still defined in YAML — removing here won't update that file, so the definition will try to recreate this entity." });
  }

  if (extraSignal) {
    if (!extraSignal.ok) riskCount += 1;
    reasons.push(extraSignal);
  }

  let verdict;
  let verdictClass;
  if (riskCount === 0) {
    verdict = "Looks safe to remove";
    verdictClass = "verdict-ok";
  } else if (riskCount >= 2) {
    verdict = "This entity looks actively used — think twice";
    verdictClass = "verdict-warn";
  } else {
    verdict = "Mixed signals — review before proceeding";
    verdictClass = "verdict-muted";
  }
  return { verdict, verdictClass, reasons };
}

function safetyModalBody(entityId, kind, assessment) {
  const nodes = [el("code", { textContent: entityId })];
  nodes.push(el("div", { className: `verdict ${assessment.verdictClass}`, textContent: assessment.verdict }));

  const list = el("ul", { className: "reason-list" });
  for (const reason of assessment.reasons) {
    list.append(el("li", { className: reason.ok ? "reason-ok" : "reason-warn", textContent: reason.text }));
    if (reason.files && reason.files.length) {
      for (const file of reason.files) {
        list.append(el("div", { className: "reason-files", textContent: file }));
      }
    }
  }
  nodes.push(list);

  const followUp =
    kind === "disable"
      ? "Disabling is reversible any time by re-enabling the entity in Home Assistant."
      : "Removing deletes the registry entry. This tool snapshots it first so you can attempt Restore afterward, but a full restore isn't guaranteed — see the Removed entities section.";
  nodes.push(el("div", { textContent: followUp, style: "font-size:0.8rem;color:var(--muted);" }));
  return nodes;
}

async function confirmAndApply(kind, context, button) {
  const verb = kind === "disable" ? "Disable" : "Remove";
  const assessment = assessSafety(context);
  const confirmed = await showConfirmModal({
    title: `${verb} ${context.entityId}?`,
    bodyNodes: safetyModalBody(context.entityId, kind, assessment),
    confirmText: verb,
    danger: kind === "remove",
  });
  if (!confirmed) return;

  button.disabled = true;
  button.textContent = "Working...";
  try {
    const response = await fetch(`/api/actions/${kind}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entity_id: context.entityId, confirm: true }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "action failed");
    button.textContent = "Done";
    await scan();
    await loadDeleted();
  } catch (error) {
    button.disabled = false;
    button.textContent = verb;
    window.alert(`Failed: ${error.message}`);
  }
}

// context: { entityId, usedIn, integrationStatus, extraSignal }
function actionButtons(context) {
  const disable = el("button", { textContent: "Disable" });
  disable.addEventListener("click", () => confirmAndApply("disable", context, disable));
  const remove = el("button", { className: "danger", textContent: "Remove" });
  remove.addEventListener("click", () => confirmAndApply("remove", context, remove));
  return el("div", { className: "action-group" }, [disable, remove]);
}

function stateSignal(entity) {
  const state = entity.state;
  if (!state || state === "unavailable" || state === "unknown") {
    return { ok: true, text: `Current state is ${state || "unknown"}.` };
  }
  return { ok: false, text: `Current state is "${state}" — looks live.` };
}

function tableWrap(table) {
  return el("div", { className: "table-wrap" }, [table]);
}

function usedInCell(usedIn) {
  if (!usedIn || !usedIn.length) {
    return el("span", { className: "badge badge-warn", textContent: "not referenced in YAML" });
  }
  return el("div", { className: "used-in-list" }, usedIn.map((path) => el("div", { textContent: path })));
}

function integrationBadge(status) {
  let className = "badge badge-muted";
  if (status === "active") className = "badge badge-ok";
  else if (status.startsWith("integration") || status.startsWith("unknown")) className = "badge badge-warn";
  return el("span", { className, textContent: status });
}

// yamlDefined is only meaningful for hand-authored domains (automation, script,
// input_*, ...); null/undefined means "n/a" (e.g. this entity is integration-backed).
function yamlDefinedBadge(yamlDefined) {
  if (yamlDefined === true) return el("span", { className: "badge badge-warn", textContent: "still defined" });
  if (yamlDefined === false) return el("span", { className: "badge badge-ok", textContent: "orphaned — not in YAML" });
  return el("span", { className: "badge badge-muted", textContent: "n/a" });
}

// ---- Column resizing -------------------------------------------------------
// Widths persist (in memory) across re-renders the same way sort state does.
const columnWidths = { duplicates: {}, inactive: {}, good: {}, drift: {}, deleted: {} };

function makeResizable(table, tableKey) {
  const ths = Array.from(table.querySelectorAll("thead th"));
  ths.forEach((th, index) => {
    const saved = columnWidths[tableKey][index];
    if (saved) {
      table.style.tableLayout = "fixed";
      th.style.width = `${saved}px`;
    }
    const resizer = el("div", { className: "col-resizer" });
    resizer.addEventListener("click", (event) => event.stopPropagation());
    resizer.addEventListener("mousedown", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (table.style.tableLayout !== "fixed") {
        // Freeze every column's current rendered width before switching to fixed
        // layout, so columns without an explicit width don't collapse/overlap.
        ths.forEach((otherTh, otherIndex) => {
          const width = columnWidths[tableKey][otherIndex] || otherTh.getBoundingClientRect().width;
          otherTh.style.width = `${width}px`;
        });
        table.style.tableLayout = "fixed";
      }
      const startX = event.clientX;
      const startWidth = th.getBoundingClientRect().width;
      resizer.classList.add("active");
      function onMove(moveEvent) {
        const newWidth = Math.max(50, startWidth + (moveEvent.clientX - startX));
        th.style.width = `${newWidth}px`;
      }
      function onUp() {
        columnWidths[tableKey][index] = th.getBoundingClientRect().width;
        resizer.classList.remove("active");
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
    th.append(resizer);
  });
}

// ---- Sorting -------------------------------------------------------
// Sort state persists across re-renders (e.g. after a Disable/Remove triggers a re-scan)
// so clicking a column header once keeps that order until you change it.
const sortState = {
  duplicates: { key: "device_name", dir: 1 },
  inactive: { key: "entity_id", dir: 1 },
  good: { key: "entity_id", dir: 1 },
  drift: { key: "kind", dir: 1 },
};

function compareValues(a, b) {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { sensitivity: "base" });
}

function sortRows(rows, keyFns, table) {
  const state = sortState[table];
  const keyFn = keyFns[state.key];
  if (!keyFn) return rows;
  return [...rows].sort((a, b) => compareValues(keyFn(a), keyFn(b)) * state.dir);
}

function sortableHeader(text, table, key, className) {
  const state = sortState[table];
  const indicator = state.key === key ? (state.dir === 1 ? " ▲" : " ▼") : "";
  const th = el("th", {
    className: className || "",
    textContent: text + indicator,
    title: "Click to sort",
    style: "cursor:pointer;user-select:none;",
  });
  th.addEventListener("click", () => {
    if (state.key === key) state.dir *= -1;
    else {
      state.key = key;
      state.dir = 1;
    }
    if (table === "duplicates") renderDuplicates(lastScan.duplicates);
    else if (table === "inactive") renderInactive(lastScan.inactive);
    else if (table === "good") renderGood(lastScan.good);
    else if (table === "drift") renderDrift(lastScan.drift);
  });
  return th;
}

let lastScan = { duplicates: [], inactive: [], good: [], drift: [] };

const duplicateSortKeys = {
  device_name: (g) => g.device_name.toLowerCase(),
  reason: (g) => g.reason.toLowerCase(),
  count: (g) => g.entities.length,
};

function renderDuplicates(groups) {
  const container = document.getElementById("duplicates");
  container.innerHTML = "";
  if (!groups.length) {
    container.append(emptyRow("No duplicate entities found."));
    return;
  }
  const sorted = sortRows(groups, duplicateSortKeys, "duplicates");
  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, [
      sortableHeader("Device", "duplicates", "device_name"),
      sortableHeader("Why flagged", "duplicates", "reason"),
      el("th", { className: "nowrap", textContent: "Entity" }),
      el("th", { className: "nowrap", textContent: "State" }),
      el("th", { className: "nowrap", textContent: "Last changed" }),
      el("th", { textContent: "Integration" }),
      el("th", { textContent: "YAML def." }),
      el("th", { textContent: "Used in YAML" }),
      el("th", { className: "nowrap", textContent: "Actions" }),
    ])]),
  ]);
  const tbody = el("tbody");
  for (const group of sorted) {
    group.entities.forEach((entity, index) => {
      const row = el("tr");
      if (index === 0) {
        row.append(
          el("td", { textContent: group.device_name, rowSpan: group.entities.length }),
          el("td", { textContent: group.reason, rowSpan: group.entities.length }),
        );
      }
      const context = {
        entityId: entity.entity_id,
        usedIn: entity.used_in,
        integrationStatus: entity.integration_status,
        yamlDefined: entity.yaml_defined,
        extraSignal: stateSignal(entity),
      };
      row.append(
        el("td", { className: "nowrap" }, [el("code", { textContent: entity.entity_id })]),
        el("td", { className: "nowrap", textContent: entity.state || "-" }),
        el("td", { className: "nowrap", textContent: entity.last_changed ? new Date(entity.last_changed).toLocaleString() : "-" }),
        el("td", {}, [integrationBadge(entity.integration_status)]),
        el("td", {}, [yamlDefinedBadge(entity.yaml_defined)]),
        el("td", {}, [usedInCell(entity.used_in)]),
        el("td", { className: "nowrap" }, [actionButtons(context)]),
      );
      tbody.append(row);
    });
  }
  table.append(tbody);
  container.append(tableWrap(table));
  makeResizable(table, "duplicates");
}

const inactiveSortKeys = {
  entity_id: (i) => i.entity_id,
  friendly_name: (i) => i.friendly_name.toLowerCase(),
  reason: (i) => i.reason.toLowerCase(),
  integration_status: (i) => i.integration_status.toLowerCase(),
  used_in: (i) => i.used_in.length,
};

function renderInactive(items) {
  const container = document.getElementById("inactive");
  container.innerHTML = "";
  if (!items.length) {
    container.append(emptyRow("No disabled or inactive entities found."));
    return;
  }
  const sorted = sortRows(items, inactiveSortKeys, "inactive");
  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, [
      sortableHeader("Entity", "inactive", "entity_id", "nowrap"),
      sortableHeader("Name", "inactive", "friendly_name"),
      sortableHeader("Reason", "inactive", "reason"),
      sortableHeader("Integration", "inactive", "integration_status"),
      el("th", { textContent: "YAML def." }),
      sortableHeader("Used in YAML", "inactive", "used_in"),
      el("th", { className: "nowrap", textContent: "Actions" }),
    ])]),
  ]);
  const tbody = el("tbody");
  for (const item of sorted) {
    const context = {
      entityId: item.entity_id,
      usedIn: item.used_in,
      integrationStatus: item.integration_status,
      yamlDefined: item.yaml_defined,
      extraSignal: { ok: true, text: `Flagged because: ${item.reason}` },
    };
    tbody.append(el("tr", {}, [
      el("td", { className: "nowrap" }, [el("code", { textContent: item.entity_id })]),
      el("td", { textContent: item.friendly_name }),
      el("td", { textContent: item.reason }),
      el("td", {}, [integrationBadge(item.integration_status)]),
      el("td", {}, [yamlDefinedBadge(item.yaml_defined)]),
      el("td", {}, [usedInCell(item.used_in)]),
      el("td", { className: "nowrap" }, [actionButtons(context)]),
    ]));
  }
  table.append(tbody);
  container.append(tableWrap(table));
  makeResizable(table, "inactive");
}

const goodSortKeys = {
  entity_id: (i) => i.entity_id,
  friendly_name: (i) => i.friendly_name.toLowerCase(),
  state: (i) => (i.state || "").toLowerCase(),
  integration_status: (i) => i.integration_status.toLowerCase(),
  used_in: (i) => i.used_in.length,
};

function renderGood(items) {
  const container = document.getElementById("good");
  container.innerHTML = "";
  if (!items.length) {
    container.append(emptyRow("No entities to show — everything is flagged above, or the registry is empty."));
    return;
  }
  const sorted = sortRows(items, goodSortKeys, "good");
  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, [
      sortableHeader("Entity", "good", "entity_id", "nowrap"),
      sortableHeader("Name", "good", "friendly_name"),
      sortableHeader("State", "good", "state", "nowrap"),
      sortableHeader("Integration", "good", "integration_status"),
      el("th", { textContent: "YAML def." }),
      sortableHeader("Used in YAML", "good", "used_in"),
      el("th", { className: "nowrap", textContent: "Actions" }),
    ])]),
  ]);
  const tbody = el("tbody");
  for (const item of sorted) {
    const context = {
      entityId: item.entity_id,
      usedIn: item.used_in,
      integrationStatus: item.integration_status,
      yamlDefined: item.yaml_defined,
      extraSignal: stateSignal(item),
    };
    tbody.append(el("tr", {}, [
      el("td", { className: "nowrap" }, [el("code", { textContent: item.entity_id })]),
      el("td", { textContent: item.friendly_name }),
      el("td", { className: "nowrap", textContent: item.state || "-" }),
      el("td", {}, [integrationBadge(item.integration_status)]),
      el("td", {}, [yamlDefinedBadge(item.yaml_defined)]),
      el("td", {}, [usedInCell(item.used_in)]),
      el("td", { className: "nowrap" }, [actionButtons(context)]),
    ]));
  }
  table.append(tbody);
  container.append(tableWrap(table));
  makeResizable(table, "good");
}

const driftSortKeys = {
  kind: (i) => i.kind.toLowerCase(),
  detail: (i) => i.detail.toLowerCase(),
  location: (i) => i.location.toLowerCase(),
};

function renderDrift(issues) {
  const container = document.getElementById("drift");
  container.innerHTML = "";
  if (!issues.length) {
    container.append(emptyRow("No YAML drift found."));
    return;
  }
  const sorted = sortRows(issues, driftSortKeys, "drift");
  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, [
      sortableHeader("Type", "drift", "kind", "nowrap"),
      sortableHeader("Detail", "drift", "detail"),
      sortableHeader("Location", "drift", "location"),
    ])]),
  ]);
  const tbody = el("tbody");
  for (const issue of sorted) {
    tbody.append(el("tr", {}, [
      el("td", { className: "nowrap", textContent: issue.kind }),
      el("td", {}, [el("code", { textContent: issue.detail })]),
      el("td", { textContent: issue.location }),
    ]));
  }
  table.append(tbody);
  container.append(tableWrap(table));
  makeResizable(table, "drift");
}

function renderDeleted(records) {
  const container = document.getElementById("deleted");
  container.innerHTML = "";
  if (!records.length) {
    container.append(emptyRow("No entities removed through this tool yet."));
    return;
  }
  const sorted = [...records].sort((a, b) => b.removed_at.localeCompare(a.removed_at));
  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, [
      el("th", { className: "nowrap", textContent: "Entity" }),
      el("th", { textContent: "Name" }),
      el("th", { className: "nowrap", textContent: "Removed at" }),
      el("th", { textContent: "Actions" }),
    ])]),
  ]);
  const tbody = el("tbody");
  for (const record of sorted) {
    const snapshot = record.registry_snapshot || {};
    const name = snapshot.name || snapshot.original_name || record.entity_id;
    const restoreBtn = el("button", { className: "primary", textContent: "Restore" });
    restoreBtn.addEventListener("click", async () => {
      if (!window.confirm(`Attempt to restore ${record.entity_id}? This reloads its integration and reapplies saved settings on your live Home Assistant instance.`)) {
        return;
      }
      restoreBtn.disabled = true;
      restoreBtn.textContent = "Restoring...";
      try {
        const response = await fetch("/api/actions/restore", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ entity_id: record.entity_id, confirm: true }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "restore failed");
        window.alert(payload.message || payload.status || "Restore attempted.");
        await loadDeleted();
        await scan();
      } catch (error) {
        restoreBtn.disabled = false;
        restoreBtn.textContent = "Restore";
        window.alert(`Failed: ${error.message}`);
      }
    });
    tbody.append(el("tr", {}, [
      el("td", { className: "nowrap" }, [el("code", { textContent: record.entity_id })]),
      el("td", { textContent: name }),
      el("td", { className: "nowrap", textContent: new Date(record.removed_at).toLocaleString() }),
      el("td", {}, [restoreBtn]),
    ]));
  }
  table.append(tbody);
  container.append(tableWrap(table));
  makeResizable(table, "deleted");
}

let lastDeleted = [];

async function loadDeleted() {
  try {
    const response = await fetch("/api/deleted");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "failed to load removed entities");
    lastDeleted = payload.deleted;
    renderDeleted(payload.deleted);
  } catch (error) {
    document.getElementById("deleted").innerHTML = "";
    document.getElementById("deleted").append(emptyRow(`Failed to load: ${error.message}`));
  }
}

async function scan() {
  statusEl.textContent = "scanning...";
  try {
    const response = await fetch("/api/scan");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "scan failed");
    lastScan = payload;
    renderDuplicates(payload.duplicates);
    renderInactive(payload.inactive);
    renderGood(payload.good);
    renderDrift(payload.drift);
    statusEl.textContent = `last scan: ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    statusEl.textContent = `scan failed: ${error.message}`;
  }
}

async function runBackup() {
  if (!window.confirm("Start a Home Assistant backup now (core config + database, no add-ons/media)? This writes a backup file on your live instance.")) {
    return;
  }
  const button = document.getElementById("backup");
  button.disabled = true;
  button.textContent = "Backing up...";
  try {
    const response = await fetch("/api/actions/backup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "backup failed to start");
    window.alert(`Backup "${payload.name}" started. Check Settings > System > Backups in Home Assistant for progress.`);
  } catch (error) {
    window.alert(`Failed: ${error.message}`);
  } finally {
    button.disabled = false;
    button.textContent = "Back up HA";
  }
}

function initCollapsibleSections() {
  document.querySelectorAll("main > section > h2").forEach((h2) => {
    h2.addEventListener("click", () => {
      h2.parentElement.classList.toggle("collapsed");
    });
  });
}

function sectionData(name) {
  if (name === "deleted") return lastDeleted;
  return lastScan[name] || [];
}

function initDownloadButtons() {
  document.querySelectorAll(".section-download").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation(); // don't also toggle the section's collapse
      const section = button.dataset.download;
      const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
      downloadJSON(sectionData(section), `ha-entity-manager-${section}-${timestamp}.json`);
    });
  });
}

function downloadAll() {
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  downloadJSON({ ...lastScan, deleted: lastDeleted }, `ha-entity-manager-export-${timestamp}.json`);
}

document.getElementById("rescan").addEventListener("click", () => {
  scan();
  loadDeleted();
});
document.getElementById("backup").addEventListener("click", runBackup);
document.getElementById("download-all").addEventListener("click", downloadAll);
initCollapsibleSections();
initDownloadButtons();
scan();
loadDeleted();
