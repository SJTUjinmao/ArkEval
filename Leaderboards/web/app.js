const els = {
  lockState: document.querySelector("#lockState"),
  passRate: document.querySelector("#passRate"),
  passedCount: document.querySelector("#passedCount"),
  runCount: document.querySelector("#runCount"),
  patchInput: document.querySelector("#patchInput"),
  dropzone: document.querySelector("#dropzone"),
  fileLabel: document.querySelector("#fileLabel"),
  selectedCount: document.querySelector("#selectedCount"),
  patchGrid: document.querySelector("#patchGrid"),
  clearBtn: document.querySelector("#clearBtn"),
  refreshBtn: document.querySelector("#refreshBtn"),
  stopBtn: document.querySelector("#stopBtn"),
  submitBtn: document.querySelector("#submitBtn"),
  submitMessage: document.querySelector("#submitMessage"),
  parallelSlots: document.querySelector("#parallelSlots"),
  hdcTargets: document.querySelector("#hdcTargets"),
  runId: document.querySelector("#runId"),
  fullRegression: document.querySelector("#fullRegression"),
  activeRun: document.querySelector("#activeRun"),
  runMode: document.querySelector("#runMode"),
  progressText: document.querySelector("#progressText"),
  progressPercent: document.querySelector("#progressPercent"),
  progressBar: document.querySelector("#progressBar"),
  runPass: document.querySelector("#runPass"),
  runFail: document.querySelector("#runFail"),
  runPending: document.querySelector("#runPending"),
  statusGrid: document.querySelector("#statusGrid"),
  rowsTable: document.querySelector("#rowsTable"),
  logHint: document.querySelector("#logHint"),
  logTail: document.querySelector("#logTail"),
};

const selectedPatches = new Map();
const TOTAL_ROWS = 502;
let activeRunId = null;
let pollHandle = null;
let runIsActive = false;

function resetRunMetrics() {
  els.passRate.textContent = "--";
  els.passedCount.textContent = "--";
}

function rowLabel(row) {
  return String(row).padStart(String(TOTAL_ROWS).length, "0");
}

function parseRowNumber(name) {
  const baseName = name.split(/[\\/]/).pop() || name;
  const match = baseName.match(/_([^_.]+)\.(?:patch|diff|txt)$/i);
  if (!match) return null;
  const rowMatch = match[1].match(/(\d+)$/);
  if (!rowMatch) return null;
  const row = Number(rowMatch[1]);
  if (row < 1 || row > TOTAL_ROWS) return null;
  return row;
}

function isPatchFileName(name) {
  return /\.(patch|diff|txt)$/i.test(name);
}

function renderPatchGrid() {
  els.patchGrid.innerHTML = "";
  for (let row = 1; row <= TOTAL_ROWS; row += 1) {
    const cell = document.createElement("div");
    cell.className = `row-cell ${selectedPatches.has(row) ? "selected" : ""}`;
    cell.textContent = rowLabel(row);
    cell.title = selectedPatches.get(row)?.filename || `row ${row}`;
    els.patchGrid.appendChild(cell);
  }
  els.selectedCount.textContent = selectedPatches.size;
  els.submitBtn.disabled = selectedPatches.size === 0 || runIsActive;
  els.fileLabel.textContent = selectedPatches.size
    ? `${selectedPatches.size} patch files ready`
    : `Select model patches (1-${TOTAL_ROWS})`;
}

function renderStatusGrid(rows = []) {
  const byRow = new Map(rows.map((row) => [Number(row.row), row]));
  const rowNumbers = rows.length
    ? [...byRow.keys()].sort((a, b) => a - b)
    : Array.from({ length: TOTAL_ROWS }, (_, index) => index + 1);
  els.statusGrid.innerHTML = "";
  for (const row of rowNumbers) {
    const item = byRow.get(row);
    const status = item?.status || "pending";
    const cell = document.createElement("div");
    cell.className = `row-cell ${status}`;
    cell.textContent = rowLabel(row);
    cell.title = item ? `${item.verdict || status} :: ${item.title || ""}` : `row ${row}`;
    els.statusGrid.appendChild(cell);
  }
}

function stageBadge(label, tone, title = "") {
  const safeTitle = title.replace(/"/g, "&quot;");
  return `<span class="stage-badge ${tone}" title="${safeTitle}">${label}</span>`;
}

function exitCodeStage(code, label) {
  if (code === null || code === undefined || code === "") {
    return stageBadge("-", "muted", `${label}: not run yet`);
  }
  const numeric = Number(code);
  if (numeric === 0) {
    return stageBadge("PASS", "pass", `${label}: exit 0`);
  }
  return stageBadge("FAIL", "fail", `${label}: exit ${code}`);
}

function testStage(row) {
  const local = row.local_test_exit_code;
  const instrument = row.instrument_test_exit_code;
  if (
    (local === null || local === undefined || local === "") &&
    (instrument === null || instrument === undefined || instrument === "")
  ) {
    return stageBadge("-", "muted", "tests: not run yet");
  }
  const localOk = local === null || local === undefined || Number(local) === 0;
  const instrumentOk = instrument === null || instrument === undefined || Number(instrument) === 0;
  const title = `local: ${local ?? "-"}, instrument: ${instrument ?? "-"}`;
  return localOk && instrumentOk ? stageBadge("PASS", "pass", title) : stageBadge("FAIL", "fail", title);
}

function renderRowsTable(rows = []) {
  els.rowsTable.innerHTML = "";
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${rowLabel(Number(row.row))}</td>
      <td>${row.repo || ""}</td>
      <td>${row.verdict || row.status || ""}</td>
      <td>${exitCodeStage(row.build_exit_code, "build")}</td>
      <td>${exitCodeStage(row.install_exit_code, "install")}</td>
      <td>${testStage(row)}</td>
    `;
    els.rowsTable.appendChild(tr);
  });
}

function setMessage(text, tone = "muted") {
  els.submitMessage.textContent = text;
  els.submitMessage.style.color =
    tone === "error" ? "var(--red)" : tone === "ok" ? "var(--green)" : "var(--muted)";
}

function ensureAlertDialog() {
  let overlay = document.querySelector("#alertDialog");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "alertDialog";
  overlay.className = "dialog-overlay hidden";
  overlay.innerHTML = `
    <section class="dialog-panel" role="alertdialog" aria-modal="true" aria-labelledby="alertDialogTitle">
      <div class="dialog-head">
        <h2 id="alertDialogTitle"></h2>
        <button class="icon-button dialog-close" type="button" title="Close">
          <span aria-hidden="true">×</span>
        </button>
      </div>
      <div class="dialog-body" id="alertDialogBody"></div>
      <div class="dialog-actions">
        <button class="primary-button dialog-ok" type="button">Close</button>
      </div>
    </section>
  `;
  document.body.appendChild(overlay);
  const close = () => overlay.classList.add("hidden");
  overlay.querySelector(".dialog-close").addEventListener("click", close);
  overlay.querySelector(".dialog-ok").addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });
  return overlay;
}

function showAlertDialog(title, message) {
  const overlay = ensureAlertDialog();
  overlay.querySelector("#alertDialogTitle").textContent = title;
  const body = overlay.querySelector("#alertDialogBody");
  const lines = String(message || "")
    .split(/\r?\n/)
    .map((line) => line.replace(/^- /, "").trim())
    .filter(Boolean);
  const detailLines = lines[0]?.toLowerCase().includes("environment preflight failed")
    ? lines.slice(1)
    : lines;
  if (detailLines.length > 1) {
    const list = document.createElement("ul");
    detailLines.forEach((line) => {
      const item = document.createElement("li");
      item.textContent = line;
      list.appendChild(item);
    });
    body.replaceChildren(list);
  } else {
    const paragraph = document.createElement("p");
    paragraph.textContent = detailLines[0] || message || "submit failed";
    body.replaceChildren(paragraph);
  }
  overlay.classList.remove("hidden");
  overlay.querySelector(".dialog-ok").focus();
}

async function decodePatchFile(file) {
  const buffer = await file.arrayBuffer();
  const attempts = [
    ["utf-8", "UTF-8"],
    ["gbk", "GBK"],
  ];
  const errors = [];
  for (const [label, display] of attempts) {
    try {
      const text = new TextDecoder(label, { fatal: true }).decode(buffer);
      return { text, encoding: display };
    } catch (error) {
      errors.push(`${display}: ${error.message}`);
    }
  }
  throw new Error(`${file.name} cannot be decoded losslessly (${errors.join("; ")})`);
}

async function readFiles(files) {
  selectedPatches.clear();
  const errors = [];
  const ignored = [];
  const duplicates = [];
  for (const file of files) {
    if (!isPatchFileName(file.name)) {
      ignored.push(file.name);
      continue;
    }
    const row = parseRowNumber(file.name);
    if (!row) {
      errors.push(`cannot determine row for ${file.name}`);
      continue;
    }
    try {
      const decoded = await decodePatchFile(file);
      if (selectedPatches.has(row)) {
        duplicates.push(`${rowLabel(row)}: ${selectedPatches.get(row).filename} -> ${file.name}`);
      }
      selectedPatches.set(row, {
        row,
        filename: file.name,
        content: decoded.text,
        encoding: decoded.encoding,
      });
    } catch (error) {
      errors.push(error.message);
    }
  }
  renderPatchGrid();
  if (errors.length) {
    setMessage(errors.slice(0, 3).join(" | "), "error");
    return;
  }
  if (duplicates.length) {
    setMessage(`duplicate rows: ${duplicates.slice(0, 3).join(" | ")}`, "error");
    return;
  }
  const ignoredHint = ignored.length ? `; ignored ${ignored.length} non-patch files` : "";
  setMessage(`${selectedPatches.size} patches accepted${ignoredHint}`, "ok");
}

async function loadSummary() {
  const res = await fetch("/api/summary");
  const data = await res.json();
  els.lockState.textContent = data.lock_ok ? "test patch lock verified" : "test patch lock failed";
  els.lockState.style.color = data.lock_ok ? "var(--green)" : "var(--red)";
  els.runCount.textContent = String(data.runs?.length || 0);
  if (data.runs?.length && !activeRunId) {
    activeRunId = data.runs[0].run_id;
    pollRun(activeRunId);
  } else if (!activeRunId) {
    resetRunMetrics();
  }
}

async function submitRun() {
  if (selectedPatches.size === 0) return;
  setMessage("submitting run");
  const patches = [...selectedPatches.values()].sort((a, b) => a.row - b.row);
  const payload = {
    run_id: els.runId.value.trim(),
    parallel_slots: Number(els.parallelSlots.value || 1),
    hdc_targets: els.hdcTargets.value.trim(),
    full_regression: els.fullRegression.checked,
    client_version: "arkeval-502-subset-v1",
    patches,
  };
  const res = await fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    const error = data.error || "submit failed";
    setMessage(error.split(/\r?\n/)[0], "error");
    if (error.toLowerCase().includes("environment preflight failed")) {
      showAlertDialog("Environment Preflight Failed", error);
    }
    return;
  }
  activeRunId = data.run_id;
  const archiveDir = data.manifest?.model_patch_archive_dir;
  setMessage(archiveDir ? `run ${activeRunId} started; saved to ${archiveDir}` : `run ${activeRunId} started`, "ok");
  if (pollHandle) clearInterval(pollHandle);
  pollRun(activeRunId);
  pollHandle = setInterval(() => pollRun(activeRunId), 2500);
}

async function cancelRun() {
  if (!activeRunId) return;
  els.stopBtn.disabled = true;
  setMessage(`stopping run ${activeRunId}`);
  const res = await fetch(`/api/runs/${encodeURIComponent(activeRunId)}/cancel`, {
    method: "POST",
  });
  const data = await res.json();
  if (!res.ok) {
    setMessage(data.error || "stop failed", "error");
    return;
  }
  renderRun(data);
  setMessage(`run ${activeRunId} stopped`, "ok");
}

function renderRun(data) {
  const summary = data.summary || {};
  const completed = summary.completed || 0;
  const total = summary.total || selectedPatches.size || TOTAL_ROWS;
  const percent = Math.round((summary.progress || 0) * 100);
  const passed = summary.passed || 0;
  const status = data.state?.status || "idle";
  const fullRegression = data.manifest?.full_regression === true;
  runIsActive = status === "running" || status === "starting";
  els.activeRun.textContent = status;
  if (els.runMode) {
    els.runMode.textContent = fullRegression ? "full regression" : "new-test-only";
    els.runMode.className = `mode-pill ${fullRegression ? "full" : "isolated"}`;
  }
  if (els.stopBtn) {
    els.stopBtn.disabled = !runIsActive;
  }
  renderPatchGrid();
  els.progressText.textContent = `${completed} / ${total}`;
  els.progressPercent.textContent = `${percent}%`;
  els.progressBar.style.width = `${percent}%`;
  els.runPass.textContent = passed;
  els.runFail.textContent = summary.failed || 0;
  els.runPending.textContent = total - completed;
  els.passRate.textContent = completed ? `${Math.round((passed / total) * 100)}%` : "--";
  els.passedCount.textContent = completed ? `${passed}/${total}` : "--";
  renderStatusGrid(data.rows || []);
  renderRowsTable(data.rows || []);
  if (els.logHint) {
    els.logHint.textContent = status === "running" ? "live tail" : status;
  }
  if (status === "canceled") {
    const canceledAt = data.state?.canceled_at || data.state?.finished_at || "";
    els.logTail.textContent = `run canceled${canceledAt ? ` at ${canceledAt}` : ""}\nworker logs and partial score files were cleared`;
  } else {
    els.logTail.textContent = data.log_tail || "";
  }
  if (["completed", "failed", "canceled"].includes(status) && pollHandle) {
    clearInterval(pollHandle);
    pollHandle = null;
    loadSummary();
  }
}

async function pollRun(runId) {
  if (!runId) return;
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
  const data = await res.json();
  if (res.ok) {
    renderRun(data);
  }
}

els.patchInput.addEventListener("change", (event) => readFiles(event.target.files));
els.clearBtn.addEventListener("click", () => {
  selectedPatches.clear();
  els.patchInput.value = "";
  renderPatchGrid();
  setMessage("");
});
els.refreshBtn.addEventListener("click", () => loadSummary());
els.submitBtn.addEventListener("click", () => submitRun());
els.stopBtn?.addEventListener("click", () => cancelRun());

["dragenter", "dragover"].forEach((name) => {
  els.dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    els.dropzone.classList.add("dragging");
  });
});
["dragleave", "drop"].forEach((name) => {
  els.dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    els.dropzone.classList.remove("dragging");
  });
});
els.dropzone.addEventListener("drop", (event) => readFiles(event.dataTransfer.files));

renderPatchGrid();
renderStatusGrid();
resetRunMetrics();
loadSummary();
