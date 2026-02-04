const stateEl = document.getElementById("printerState");
const progressEl = document.getElementById("progress");
const bedEl = document.getElementById("bedTemp");
const nozzleEl = document.getElementById("nozzleTemp");
const lightStateEl = document.getElementById("lightState");
const selectedAmsEl = document.getElementById("selectedAms");
const etaEl = document.getElementById("remainingTime");
const errEl = document.getElementById("lastError");
const uploadMsg = document.getElementById("uploadMsg");
const controlMsg = document.getElementById("controlMsg");
const fleetMsg = document.getElementById("fleetMsg");
const tempMsg = document.getElementById("tempMsg");
const cameraMsg = document.getElementById("cameraMsg");
const queueMsg = document.getElementById("queueMsg");
const dispatchStatusEl = document.getElementById("dispatchStatus");
const jogMsg = document.getElementById("jogMsg");
const fanMsg = document.getElementById("fanMsg");
const partFan = document.getElementById("partFan");
const auxFan = document.getElementById("auxFan");
const chamberFan = document.getElementById("chamberFan");
const partFanVal = document.getElementById("partFanVal");
const auxFanVal = document.getElementById("auxFanVal");
const chamberFanVal = document.getElementById("chamberFanVal");
const applyFansBtn = document.getElementById("applyFansBtn");
const fansOffBtn = document.getElementById("fansOffBtn");
const fleetFanMsg = document.getElementById("fleetFanMsg");
const fleetPartFan = document.getElementById("fleetPartFan");
const fleetAuxFan = document.getElementById("fleetAuxFan");
const fleetChamberFan = document.getElementById("fleetChamberFan");
const fleetPartFanVal = document.getElementById("fleetPartFanVal");
const fleetAuxFanVal = document.getElementById("fleetAuxFanVal");
const fleetChamberFanVal = document.getElementById("fleetChamberFanVal");
const fleetApplyFansBtn = document.getElementById("fleetApplyFansBtn");
const fleetFansOffBtn = document.getElementById("fleetFansOffBtn");
const amsStatusEl = document.getElementById("amsStatus");
const amsListEl = document.getElementById("amsList");
const printerSelect = document.getElementById("printerSelect");
const farmTableBody = document.querySelector("#farmTable tbody");
const cameraGrid = document.getElementById("cameraGrid");
const cameraSnapshotMode = document.getElementById("cameraSnapshotMode");
let camerasLoaded = false;
let fanUiLockUntil = 0;
let fleetFanUiLockUntil = 0;
let cameraRefreshHandle = null;
const cameraDiagLastAt = {};

function getSelectedPrinter() {
  return localStorage.getItem("selectedPrinterId");
}

function setSelectedPrinter(id) {
  localStorage.setItem("selectedPrinterId", id);
}

function isSnapshotMode() {
  return localStorage.getItem("cameraSnapshotMode") === "1";
}

function setSnapshotMode(on) {
  localStorage.setItem("cameraSnapshotMode", on ? "1" : "0");
}

function withPrinterId(path) {
  const id = getSelectedPrinter();
  if (!id) {
    throw new Error("No printer selected");
  }
  const url = new URL(path, window.location.origin);
  url.searchParams.set("printer_id", id);
  return url.toString();
}

function setNotice(el, msg, kind) {
  if (!el) return;
  el.textContent = msg || "";
  el.classList.remove("ok", "err");
  if (kind) el.classList.add(kind);
}

function lockFans(ms) {
  fanUiLockUntil = Date.now() + (ms ?? 5000);
}

function lockFleetFans(ms) {
  fleetFanUiLockUntil = Date.now() + (ms ?? 5000);
}

function setFanLabel(inputEl, labelEl) {
  if (!inputEl || !labelEl) return;
  labelEl.textContent = `${inputEl.value}%`;
}

function formatFailReason(value) {
  if (value == null) return "";
  const raw = String(value).trim();
  if (!raw) return "";
  const num = Number(raw);
  if (!Number.isFinite(num)) {
    return `fail_reason ${raw}`;
  }
  const hex = Math.trunc(num).toString(16).toUpperCase().padStart(8, "0");
  const code = `${hex.slice(0, 4)}-${hex.slice(4, 8)}`;
  return `fail_reason ${raw} (${code})`;
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function renderFarmRow(item) {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td>${item.name ?? item.id}</td>
    <td>${item.printer_state ?? "–"}</td>
    <td>${item.percentage != null ? `${item.percentage}%` : "–"}</td>
    <td>${item.bed_temp != null ? `${item.bed_temp}C` : "–"}</td>
    <td>${item.nozzle_temp != null ? `${item.nozzle_temp}C` : "–"}</td>
    <td>${item.remaining_time != null ? item.remaining_time : "–"}</td>
    <td>${item.connected ? "OK" : "OFF"}</td>
  `;
  return tr;
}

if (cameraSnapshotMode) {
  cameraSnapshotMode.checked = isSnapshotMode();
  cameraSnapshotMode.addEventListener("change", () => {
    setSnapshotMode(cameraSnapshotMode.checked);
    loadCameras(getSelectedPrinter());
  });
}

async function refreshFarm() {
  const res = await fetch("/api/printers");
  const list = await res.json();
  farmTableBody.innerHTML = "";
  list.forEach((item) => farmTableBody.appendChild(renderFarmRow(item)));
}

async function refreshSelectedStatus() {
  const id = getSelectedPrinter();
  if (!id) return;
  try {
    const res = await fetch(withPrinterId("/api/status"));
    const data = await res.json();
    const printerState = data.printer_state ?? "–";
    stateEl.textContent = printerState;
    progressEl.textContent =
      data.percentage != null ? `${data.percentage}%` : "–";
    bedEl.textContent = data.bed_temp != null ? `${data.bed_temp}C` : "–";
    nozzleEl.textContent =
      data.nozzle_temp != null ? `${data.nozzle_temp}C` : "–";
    lightStateEl.textContent = data.light_state ?? "–";
    if (selectedAmsEl) {
      const sel = data.selected_ams;
      selectedAmsEl.textContent = sel
        ? `AMS ${sel.ams_id} Tray ${sel.tray_id}`
        : "–";
    }
    etaEl.textContent = data.remaining_time != null ? data.remaining_time : "–";
    const failReason =
      data.fail_reason != null && String(data.fail_reason).trim() !== ""
        ? formatFailReason(data.fail_reason)
        : "";
    const lastErr =
      data.last_error != null && String(data.last_error).trim() !== ""
        ? String(data.last_error)
        : "";
    errEl.textContent = lastErr || failReason || "–";

    // Enable/disable print control buttons based on actual printer state so we
    // don't send confusing no-op commands (e.g., pause when FINISH).
    const pauseBtnEl = document.getElementById("pauseBtn");
    const resumeBtnEl = document.getElementById("resumeBtn");
    const stopBtnEl = document.getElementById("stopBtn");
    const s = String(printerState || "").toUpperCase();
    const canPause = s === "RUNNING" || s === "PREPARE";
    const canResume = s === "PAUSE";
    const canStop =
      s === "RUNNING" || s === "PREPARE" || s === "PAUSE" || s === "FAILED";
    const clearFaultBtnEl = document.getElementById("clearFaultBtn");
    if (pauseBtnEl) pauseBtnEl.disabled = !canPause;
    if (resumeBtnEl) resumeBtnEl.disabled = !canResume;
    if (stopBtnEl) stopBtnEl.disabled = !canStop;
    if (clearFaultBtnEl) clearFaultBtnEl.disabled = s !== "FAILED";

    // Jogging while printing can cause collisions and toolhead damage. Keep the
    // UI locked unless the printer is clearly idle.
    const canJog = s === "IDLE" || s === "FINISH";
    [
      "jogYPlus",
      "jogYMinus",
      "jogXPlus",
      "jogXMinus",
      "jogZPlus",
      "jogZMinus",
      "jogHome",
      "jogStep",
      "jogFeed",
    ].forEach((elId) => {
      const el = document.getElementById(elId);
      if (el) el.disabled = !canJog;
    });
    if (amsStatusEl) {
      const status = data.print_status ? `Print: ${data.print_status}` : "";
      const runout = data.filament_runout ? "Filament runout detected" : "";
      const sel = data.selected_ams
        ? `Selected AMS ${data.selected_ams.ams_id} Tray ${data.selected_ams.tray_id}`
        : "";
      const msg = [status, runout, sel].filter(Boolean).join(" • ");
      amsStatusEl.textContent = msg;
    }

    // Keep fan UI in sync with last-known values from the backend, but don't
    // fight the user while they're dragging sliders.
    if (Date.now() > fanUiLockUntil) {
      if (partFan && data.part_fan_percent != null) {
        partFan.value = String(data.part_fan_percent);
      }
      if (auxFan && data.aux_fan_percent != null) {
        auxFan.value = String(data.aux_fan_percent);
      }
      if (chamberFan && data.chamber_fan_percent != null) {
        chamberFan.value = String(data.chamber_fan_percent);
      }
    }
    setFanLabel(partFan, partFanVal);
    setFanLabel(auxFan, auxFanVal);
    setFanLabel(chamberFan, chamberFanVal);
  } catch (err) {
    errEl.textContent = String(err);
  }
}

async function loadPrinters() {
  const res = await fetch("/api/printers");
  const list = await res.json();
  printerSelect.innerHTML = "";

  list.forEach((item) => {
    const opt = document.createElement("option");
    opt.value = item.id;
    opt.textContent = item.name ?? item.id;
    printerSelect.appendChild(opt);
  });

  const saved = getSelectedPrinter();
  const fallback = list.length ? list[0].id : null;
  const selected = saved && list.some((p) => p.id === saved) ? saved : fallback;
  if (selected) {
    printerSelect.value = selected;
    setSelectedPrinter(selected);
  }
}

function cameraUrlFor(printerId) {
  const endpoint = isSnapshotMode() ? "/api/camera/snapshot" : "/api/camera";
  const url = new URL(endpoint, window.location.origin);
  url.searchParams.set("printer_id", printerId);
  url.searchParams.set("t", String(Date.now()));
  return url.toString();
}

function renderCameraCard(printer, src) {
  const card = document.createElement("div");
  card.className = "camera-card";
  const name = printer.name ?? printer.id;
  card.innerHTML = `
	    <div class="camera-title">${name}</div>
	    <div class="camera-feed">
	      <img src="${src}" alt="${name} camera" />
	    </div>
	  `;
  const img = card.querySelector("img");
  if (img) {
    img.dataset.printerId = printer.id;
  }
  return card;
}

async function cameraDiag(printerId, label) {
  const now = Date.now();
  const last = cameraDiagLastAt[printerId] || 0;
  if (now - last < 5000) return;
  cameraDiagLastAt[printerId] = now;

  try {
    const url = new URL("/api/camera/diag", window.location.origin);
    url.searchParams.set("printer_id", printerId);
    url.searchParams.set("t", String(now));
    const res = await fetch(url.toString());
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (res.ok && data && data.ok) {
      setNotice(cameraMsg, "", "");
      return;
    }
    const err = data && data.error ? data.error : await res.text();
    setNotice(cameraMsg, `Camera error (${label}): ${err}`, "err");
  } catch (err) {
    setNotice(cameraMsg, `Camera error (${label}): ${String(err)}`, "err");
  }
}

function refreshCameraFrames() {
  if (!cameraGrid) return;
  const imgs = cameraGrid.querySelectorAll("img");
  imgs.forEach((img) => {
    const pid = img.dataset.printerId;
    if (!pid) return;
    img.src = cameraUrlFor(pid);
  });
}

function scheduleCameraRefresh() {
  if (cameraRefreshHandle) clearTimeout(cameraRefreshHandle);
  const delay = isSnapshotMode() ? 1200 : 15000;
  cameraRefreshHandle = setTimeout(() => {
    refreshCameraFrames();
    scheduleCameraRefresh();
  }, delay);
}

function formatTrayLabel(tray) {
  const name = tray.tray_id_name || `Tray ${tray.tray_id}`;
  const type = tray.tray_type || "Unknown";
  return `${name} (${type})`;
}

function trayColorStyle(color) {
  if (!color) return "#ccc";
  let c = color.replace("#", "");
  if (c.length >= 6) return `#${c.slice(0, 6)}`;
  return "#ccc";
}

function renderAmsTray(amsId, tray, selected) {
  const row = document.createElement("div");
  row.className = "ams-tray";
  const left = document.createElement("div");
  left.className = "ams-row";
  const dot = document.createElement("span");
  dot.className = "ams-color";
  dot.style.background = trayColorStyle(tray.tray_color);
  const meta = document.createElement("div");
  meta.className = "meta";
  const line1 = document.createElement("div");
  line1.textContent = formatTrayLabel(tray);
  const line2 = document.createElement("div");
  line2.textContent = `Temp ${tray.nozzle_temp_min}-${tray.nozzle_temp_max}C`;
  meta.appendChild(line1);
  meta.appendChild(line2);
  left.appendChild(dot);
  left.appendChild(meta);

  const btn = document.createElement("button");
  const isSelected =
    selected &&
    Number(selected.ams_id) === Number(amsId) &&
    Number(selected.tray_id) === Number(tray.tray_id);
  btn.textContent = isSelected ? "Selected" : "Use";
  btn.disabled = isSelected;
  btn.onclick = async () => {
    try {
      const res = await apiPost(withPrinterId("/api/ams/select"), {
        ams_id: amsId,
        tray_id: tray.tray_id,
      });
      const result = res.result || {};
      const sel = result.selected || { ams_id: amsId, tray_id: tray.tray_id };
      const toolId = sel.tool_id != null ? ` (tool ${sel.tool_id})` : "";
      const savedNote =
        result.toolchange || result.resume ? "" : " • saved for next start";
      const toolNote =
        result.toolchange === true
          ? " • tool set"
          : result.toolchange === false && result.resume
            ? " • tool set failed"
            : "";
      const resumeNote = result.resume ? " • resumed" : "";
      const setNote = result.set === false ? " • filament meta rejected" : "";
      setNotice(
        amsStatusEl,
        res.ok
          ? `Selected AMS ${sel.ams_id} Tray ${sel.tray_id}${toolId}${savedNote}${toolNote}${resumeNote}${setNote}`
          : "AMS selection failed",
        res.ok ? "ok" : "err",
      );
      await refreshAms();
    } catch (err) {
      setNotice(amsStatusEl, String(err), "err");
    }
  };

  row.appendChild(left);
  row.appendChild(btn);
  return row;
}

function renderAmsCard(ams, selected) {
  const card = document.createElement("div");
  card.className = "ams-card";
  const title = document.createElement("h4");
  title.textContent = `AMS ${ams.ams_id} • ${ams.humidity}% RH`;
  card.appendChild(title);
  if (!ams.trays || ams.trays.length === 0) {
    const empty = document.createElement("div");
    empty.className = "notice";
    empty.textContent = "No trays detected";
    card.appendChild(empty);
    return card;
  }
  ams.trays.forEach((tray) =>
    card.appendChild(renderAmsTray(ams.ams_id, tray, selected)),
  );
  return card;
}

async function refreshAms() {
  const id = getSelectedPrinter();
  if (!id || !amsListEl) return;
  try {
    const res = await fetch(withPrinterId("/api/ams"));
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const selected = data.selected || null;
    amsListEl.innerHTML = "";
    (data.ams || []).forEach((ams) =>
      amsListEl.appendChild(renderAmsCard(ams, selected)),
    );
    if (!data.ams || data.ams.length === 0) {
      amsListEl.textContent = "No AMS detected";
    }
  } catch (err) {
    setNotice(amsStatusEl, String(err), "err");
  }
}

async function loadCameras(printerId) {
  setNotice(cameraMsg, "Loading cameras...", "");
  cameraGrid.innerHTML = "";
  try {
    const res = await fetch("/api/printers");
    if (!res.ok) {
      throw new Error(`Printers request failed: ${res.status}`);
    }
    const list = await res.json();
    if (!Array.isArray(list) || list.length === 0) {
      throw new Error("No printers returned from /api/printers");
    }
    const filtered = printerId ? list.filter((p) => p.id === printerId) : list;
    filtered.forEach((printer) => {
      const card = renderCameraCard(printer, cameraUrlFor(printer.id));
      const img = card.querySelector("img");
      if (img) {
        img.onerror = () => {
          cameraDiag(printer.id, printer.name ?? printer.id);
          img.src = cameraUrlFor(printer.id);
        };
      }
      cameraGrid.appendChild(card);
    });
    setNotice(cameraMsg, "", "");
    scheduleCameraRefresh();
  } catch (err) {
    const selectedId = printerId || getSelectedPrinter();
    if (selectedId) {
      const name =
        printerSelect?.selectedOptions?.[0]?.textContent || selectedId;
      const fallback = { id: selectedId, name };
      const card = renderCameraCard(fallback, cameraUrlFor(selectedId));
      cameraGrid.appendChild(card);
      setNotice(cameraMsg, "", "");
      console.error(err);
      scheduleCameraRefresh();
      return;
    }
    setNotice(cameraMsg, String(err), "err");
    console.error(err);
  }
}

function getJogSettings() {
  const step = Number(document.getElementById("jogStep").value || 5);
  const feed = Number(document.getElementById("jogFeed").value || 3000);
  return { step, feed };
}

async function jog(dx, dy, dz) {
  try {
    const { step, feed } = getJogSettings();
    const body = { dx: dx * step, dy: dy * step, dz: dz * step, feed };
    const res = await apiPost(withPrinterId("/api/jog"), body);
    setNotice(
      jogMsg,
      res.ok ? "Jog sent" : "Jog failed",
      res.ok ? "ok" : "err",
    );
  } catch (err) {
    setNotice(jogMsg, String(err), "err");
  }
}

function setupTabs() {
  const tabs = document.querySelectorAll(".tab");
  const panels = document.querySelectorAll(".tab-panel");
  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      tabs.forEach((b) => b.classList.remove("active"));
      panels.forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      const target = document.getElementById(`tab-${btn.dataset.tab}`);
      if (target) target.classList.add("active");
      if (btn.dataset.tab === "control" && !camerasLoaded) {
        loadCameras(getSelectedPrinter());
        camerasLoaded = true;
      }
    });
  });
}

printerSelect.addEventListener("change", () => {
  setSelectedPrinter(printerSelect.value);
  refreshSelectedStatus();
  loadCameras(printerSelect.value);
  refreshAms();
});

setupTabs();

document.getElementById("pauseBtn").onclick = async () => {
  try {
    const res = await apiPost(withPrinterId("/api/pause"));
    setNotice(
      controlMsg,
      res.ok ? "Paused" : "Pause failed",
      res.ok ? "ok" : "err",
    );
  } catch (err) {
    setNotice(controlMsg, String(err), "err");
  }
};

document.getElementById("resumeBtn").onclick = async () => {
  try {
    const res = await apiPost(withPrinterId("/api/resume"));
    setNotice(
      controlMsg,
      res.ok ? "Resumed" : "Resume failed",
      res.ok ? "ok" : "err",
    );
  } catch (err) {
    setNotice(controlMsg, String(err), "err");
  }
};

document.getElementById("stopBtn").onclick = async () => {
  try {
    const res = await apiPost(withPrinterId("/api/stop"));
    setNotice(
      controlMsg,
      res.ok ? "Stopped" : "Stop failed",
      res.ok ? "ok" : "err",
    );
  } catch (err) {
    setNotice(controlMsg, String(err), "err");
  }
};

const clearFaultBtn = document.getElementById("clearFaultBtn");
if (clearFaultBtn) {
  clearFaultBtn.onclick = async () => {
    try {
      const res = await apiPost(withPrinterId("/api/fault/clear"));
      const after = res.after ? ` • after: ${res.after}` : "";
      setNotice(
        controlMsg,
        res.ok ? `Fault clear attempted${after}` : `Fault clear failed${after}`,
        res.ok ? "ok" : "err",
      );
      await refreshSelectedStatus();
      await refreshDispatchStatus();
    } catch (err) {
      setNotice(controlMsg, String(err), "err");
    }
  };
}

const chamberOnBtn = document.getElementById("chamberOnBtn");
if (chamberOnBtn) {
  chamberOnBtn.onclick = async () => {
    try {
      const res = await apiPost(withPrinterId("/api/light/chamber/on"));
      setNotice(
        controlMsg,
        res.ok ? "Chamber light on" : "Chamber light on failed",
        res.ok ? "ok" : "err",
      );
    } catch (err) {
      setNotice(controlMsg, String(err), "err");
    }
  };
}

const chamberOffBtn = document.getElementById("chamberOffBtn");
if (chamberOffBtn) {
  chamberOffBtn.onclick = async () => {
    try {
      const res = await apiPost(withPrinterId("/api/light/chamber/off"));
      setNotice(
        controlMsg,
        res.ok ? "Chamber light off" : "Chamber light off failed",
        res.ok ? "ok" : "err",
      );
    } catch (err) {
      setNotice(controlMsg, String(err), "err");
    }
  };
}

async function applyFansForSelected(part, aux, chamber) {
  try {
    const res = await apiPost(withPrinterId("/api/fans"), {
      part,
      aux,
      chamber,
    });
    setNotice(fanMsg, "Fans updated", res.ok ? "ok" : "err");
    lockFans(0);
    await refreshSelectedStatus();
  } catch (err) {
    setNotice(fanMsg, String(err), "err");
  }
}

if (partFan) {
  partFan.addEventListener("input", () => {
    lockFans();
    setFanLabel(partFan, partFanVal);
  });
}
if (auxFan) {
  auxFan.addEventListener("input", () => {
    lockFans();
    setFanLabel(auxFan, auxFanVal);
  });
}
if (chamberFan) {
  chamberFan.addEventListener("input", () => {
    lockFans();
    setFanLabel(chamberFan, chamberFanVal);
  });
}

if (applyFansBtn) {
  applyFansBtn.onclick = async () => {
    const part = Number(partFan?.value ?? 0);
    const aux = Number(auxFan?.value ?? 0);
    const chamber = Number(chamberFan?.value ?? 0);
    await applyFansForSelected(part, aux, chamber);
  };
}

if (fansOffBtn) {
  fansOffBtn.onclick = async () => {
    if (partFan) partFan.value = "0";
    if (auxFan) auxFan.value = "0";
    if (chamberFan) chamberFan.value = "0";
    setFanLabel(partFan, partFanVal);
    setFanLabel(auxFan, auxFanVal);
    setFanLabel(chamberFan, chamberFanVal);
    await applyFansForSelected(0, 0, 0);
  };
}

const pauseAllBtn = document.getElementById("pauseAllBtn");
if (pauseAllBtn) {
  pauseAllBtn.onclick = async () => {
    try {
      await apiPost("/api/broadcast/pause");
      setNotice(fleetMsg, "Pause all sent", "ok");
    } catch (err) {
      setNotice(fleetMsg, String(err), "err");
    }
  };
}

const resumeAllBtn = document.getElementById("resumeAllBtn");
if (resumeAllBtn) {
  resumeAllBtn.onclick = async () => {
    try {
      await apiPost("/api/broadcast/resume");
      setNotice(fleetMsg, "Resume all sent", "ok");
    } catch (err) {
      setNotice(fleetMsg, String(err), "err");
    }
  };
}

const stopAllBtn = document.getElementById("stopAllBtn");
if (stopAllBtn) {
  stopAllBtn.onclick = async () => {
    try {
      await apiPost("/api/broadcast/stop");
      setNotice(fleetMsg, "Stop all sent", "ok");
    } catch (err) {
      setNotice(fleetMsg, String(err), "err");
    }
  };
}

const chamberAllOnBtn = document.getElementById("chamberAllOnBtn");
if (chamberAllOnBtn) {
  chamberAllOnBtn.onclick = async () => {
    try {
      await apiPost("/api/broadcast/light/chamber/on");
      setNotice(fleetMsg, "Chamber lights on sent", "ok");
    } catch (err) {
      setNotice(fleetMsg, String(err), "err");
    }
  };
}

const chamberAllOffBtn = document.getElementById("chamberAllOffBtn");
if (chamberAllOffBtn) {
  chamberAllOffBtn.onclick = async () => {
    try {
      await apiPost("/api/broadcast/light/chamber/off");
      setNotice(fleetMsg, "Chamber lights off sent", "ok");
    } catch (err) {
      setNotice(fleetMsg, String(err), "err");
    }
  };
}

async function applyFansForFleet(part, aux, chamber) {
  try {
    await apiPost("/api/broadcast/fans", { part, aux, chamber });
    setNotice(fleetFanMsg, "Fleet fans updated", "ok");
    lockFleetFans(0);
  } catch (err) {
    setNotice(fleetFanMsg, String(err), "err");
  }
}

if (fleetPartFan) {
  fleetPartFan.addEventListener("input", () => {
    lockFleetFans();
    setFanLabel(fleetPartFan, fleetPartFanVal);
  });
}
if (fleetAuxFan) {
  fleetAuxFan.addEventListener("input", () => {
    lockFleetFans();
    setFanLabel(fleetAuxFan, fleetAuxFanVal);
  });
}
if (fleetChamberFan) {
  fleetChamberFan.addEventListener("input", () => {
    lockFleetFans();
    setFanLabel(fleetChamberFan, fleetChamberFanVal);
  });
}

if (fleetApplyFansBtn) {
  fleetApplyFansBtn.onclick = async () => {
    const part = Number(fleetPartFan?.value ?? 0);
    const aux = Number(fleetAuxFan?.value ?? 0);
    const chamber = Number(fleetChamberFan?.value ?? 0);
    await applyFansForFleet(part, aux, chamber);
  };
}

if (fleetFansOffBtn) {
  fleetFansOffBtn.onclick = async () => {
    if (fleetPartFan) fleetPartFan.value = "0";
    if (fleetAuxFan) fleetAuxFan.value = "0";
    if (fleetChamberFan) fleetChamberFan.value = "0";
    setFanLabel(fleetPartFan, fleetPartFanVal);
    setFanLabel(fleetAuxFan, fleetAuxFanVal);
    setFanLabel(fleetChamberFan, fleetChamberFanVal);
    await applyFansForFleet(0, 0, 0);
  };
}

const jogYPlus = document.getElementById("jogYPlus");
const jogYMinus = document.getElementById("jogYMinus");
const jogXPlus = document.getElementById("jogXPlus");
const jogXMinus = document.getElementById("jogXMinus");
const jogZPlus = document.getElementById("jogZPlus");
const jogZMinus = document.getElementById("jogZMinus");
const jogHome = document.getElementById("jogHome");

if (jogYPlus) jogYPlus.onclick = () => jog(0, 1, 0);
if (jogYMinus) jogYMinus.onclick = () => jog(0, -1, 0);
if (jogXPlus) jogXPlus.onclick = () => jog(1, 0, 0);
if (jogXMinus) jogXMinus.onclick = () => jog(-1, 0, 0);
// Bambu bed kinematics make "Z up/down" unintuitive; map the UI to the physical expectation:
// Z+ raises the nozzle away from the bed, Z- lowers it closer to the bed.
if (jogZPlus) jogZPlus.onclick = () => jog(0, 0, -1);
if (jogZMinus) jogZMinus.onclick = () => jog(0, 0, 1);
if (jogHome)
  jogHome.onclick = async () => {
    try {
      const res = await apiPost(withPrinterId("/api/jog/home"));
      setNotice(
        jogMsg,
        res.ok ? "Homing" : "Home failed",
        res.ok ? "ok" : "err",
      );
    } catch (err) {
      setNotice(jogMsg, String(err), "err");
    }
  };

const setTempsBtn = document.getElementById("setTempsBtn");
const cooldownBtn = document.getElementById("cooldownBtn");
if (setTempsBtn)
  setTempsBtn.onclick = async () => {
    const bedRaw = document.getElementById("bedInput")?.value ?? "";
    const nozzleRaw = document.getElementById("nozzleInput")?.value ?? "";
    const bed = String(bedRaw).trim() === "" ? null : Number(bedRaw);
    const nozzle = String(nozzleRaw).trim() === "" ? null : Number(nozzleRaw);
    if (bed != null && !Number.isFinite(bed)) {
      setNotice(tempMsg, "Invalid bed temperature value", "err");
      return;
    }
    if (nozzle != null && !Number.isFinite(nozzle)) {
      setNotice(tempMsg, "Invalid nozzle temperature value", "err");
      return;
    }
    try {
      const res = await apiPost(withPrinterId("/api/temps"), {
        bed,
        nozzle,
      });
      setNotice(tempMsg, "Temps sent", res.ok ? "ok" : "err");
    } catch (err) {
      setNotice(tempMsg, String(err), "err");
    }
  };

if (cooldownBtn)
  cooldownBtn.onclick = async () => {
    try {
      const res = await apiPost(withPrinterId("/api/temps"), {
        bed: 0,
        nozzle: 0,
      });
      setNotice(tempMsg, "Cool down sent", res.ok ? "ok" : "err");
    } catch (err) {
      setNotice(tempMsg, String(err), "err");
    }
  };

async function upload(start) {
  const file = document.getElementById("fileInput").files[0];
  const plate = document.getElementById("plateInput").value || 1;
  if (!file) {
    uploadMsg.textContent = "Choose a file first.";
    return;
  }
  uploadMsg.textContent = "Uploading...";
  const form = new FormData();
  form.append("file", file);
  const url = new URL(withPrinterId("/api/upload"));
  url.searchParams.set("start", start ? "1" : "0");
  url.searchParams.set("plate", String(plate));
  const res = await fetch(url.toString(), {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    uploadMsg.textContent = await res.text();
    return;
  }
  const data = await res.json();
  const slicedNote = data.sliced ? " (sliced)" : "";
  uploadMsg.textContent = start
    ? `Uploaded${slicedNote}. Start: ${data.started}`
    : `Uploaded${slicedNote}.`;
}

const uploadBtn = document.getElementById("uploadBtn");
if (uploadBtn) uploadBtn.onclick = () => upload(false);
const uploadStartBtn = document.getElementById("uploadStartBtn");
if (uploadStartBtn) uploadStartBtn.onclick = () => upload(true);

async function renderJobs() {
  const res = await fetch("/api/jobs");
  const jobs = await res.json();
  const tbody = document.querySelector("#jobTable tbody");
  tbody.innerHTML = "";
  jobs.forEach((job) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${job.id}</td>
      <td>${job.filename}</td>
      <td>${job.status}</td>
      <td>${job.printer_id ?? "auto"}</td>
      <td>${job.assigned_printer_id ?? "–"}</td>
      <td>
        <button data-cancel="${job.id}">Cancel</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll("button[data-cancel]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await apiPost(`/api/jobs/${btn.dataset.cancel}/cancel`);
        await renderJobs();
      } catch (err) {
        setNotice(queueMsg, String(err), "err");
      }
    });
  });
}

async function refreshDispatchStatus() {
  if (!dispatchStatusEl) return;
  try {
    const res = await fetch("/api/dispatch/status");
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const printers = Array.isArray(data.printers) ? data.printers : [];
    const available = printers.filter((p) => p.available).length;
    const total = printers.length;
    const queued = data.jobs?.queued ?? "–";
    const alive = data.thread_alive ? "ON" : "OFF";
    const err = data.last_error ? ` • error: ${data.last_error}` : "";
    const stateSummary = printers
      .map((p) => {
        const id = p.id ?? "unknown";
        const st = p.printer_state ?? "–";
        const code =
          p.print_error_code != null ? String(p.print_error_code) : "";
        const fail =
          p.fail_reason != null ? formatFailReason(p.fail_reason) : "";
        const parts = [];
        if (code && code !== "0") parts.push(`err ${code}`);
        if (fail) parts.push(fail);
        const detail = parts.length ? ` (${parts.join(", ")})` : "";
        return `${id}: ${st}${detail}`;
      })
      .join(" • ");
    const detail = stateSummary ? ` • ${stateSummary}` : "";
    dispatchStatusEl.textContent = `Dispatcher: ${alive} • queued: ${queued} • available: ${available}/${total}${err}${detail}`;
  } catch (err) {
    dispatchStatusEl.textContent = "Dispatcher: unavailable";
    console.error(err);
  }
}

const enqueueBtn = document.getElementById("enqueueBtn");
if (enqueueBtn)
  enqueueBtn.onclick = async () => {
    const file = document.getElementById("queueFileInput").files[0];
    const plate = document.getElementById("queuePlateInput").value || 1;
    const autoAssign = document.getElementById("autoAssignInput").checked;
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const url = new URL("/api/jobs", window.location.origin);
    url.searchParams.set("plate", String(plate));
    url.searchParams.set("auto_assign", autoAssign ? "1" : "0");
    if (!autoAssign) {
      url.searchParams.set("printer_id", getSelectedPrinter() || "");
    }
    const resp = await fetch(url.toString(), { method: "POST", body: form });
    if (!resp.ok) {
      setNotice(queueMsg, await resp.text(), "err");
      return;
    }
    const data = await resp.json();
    setNotice(queueMsg, `Queued job ${data.id} (${data.filename})`, "ok");
    await renderJobs();
    await refreshDispatchStatus();
  };

const dispatchNowBtn = document.getElementById("dispatchNowBtn");
if (dispatchNowBtn)
  dispatchNowBtn.onclick = async () => {
    try {
      const res = await apiPost("/api/dispatch/once");
      const result = res.result || {};
      const started = (result.dispatched || []).length;
      const failed = (result.failed || []).length;
      const skipped = (result.skipped || []).length;
      setNotice(
        queueMsg,
        `Dispatch: ${started} started • ${failed} failed • ${skipped} skipped`,
        res.ok ? "ok" : "err",
      );
      await renderJobs();
      await refreshDispatchStatus();
    } catch (err) {
      setNotice(queueMsg, String(err), "err");
    }
  };

async function boot() {
  setupTabs();
  await loadPrinters();
  await refreshFarm();
  await loadCameras(getSelectedPrinter());
  camerasLoaded = true;
  await refreshSelectedStatus();
  await refreshAms();
  await renderJobs();
  await refreshDispatchStatus();
  setInterval(refreshFarm, 3000);
  setInterval(refreshSelectedStatus, 2000);
  setInterval(refreshAms, 5000);
  setInterval(renderJobs, 4000);
  setInterval(refreshDispatchStatus, 5000);
  scheduleCameraRefresh();
}

boot();
