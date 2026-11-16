/* -------------------------------------------------------------------
 * Device Port Reporter — single-page UI.
 * Vanilla JS, no frameworks, no CDN calls. All DOM text uses
 * textContent or safely-constructed elements to avoid XSS even though
 * the server never injects user data into the HTML template.
 * ------------------------------------------------------------------- */

(() => {
  "use strict";

  // Cached DOM handles -----------------------------------------------------
  const el = (id) => document.getElementById(id);
  const dropzone      = el("dropzone");
  const fileInput     = el("file-input");
  const uploadView    = el("upload-view");
  const resultsView   = el("results-view");
  const uploadError   = el("upload-error");
  const uploadProgress= el("upload-progress");
  const sourceBanner  = el("source-banner");
  const kpiGrid       = el("kpi-grid");
  const vlanChart     = el("vlan-chart");
  const vlanCaption   = el("vlan-caption");
  const tabs          = document.querySelectorAll(".tab");
  const searchInput   = el("search-input");
  const tableHead     = document.querySelector("#iface-table thead");
  const tableBody     = document.querySelector("#iface-table tbody");
  const emptyState    = el("empty-state");
  const btnDownload   = el("btn-download");
  const btnReset      = el("btn-reset");

  // State ------------------------------------------------------------------
  let currentPayload = null;   // whole /api/parse response
  let currentConfigText = null;// uploaded raw text, re-sent on download
  let currentFileName = null;
  let activeFilter = "all";
  let searchText = "";

  // ---------------- Upload wiring ----------------
  ["dragenter", "dragover"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove("is-dragover");
    });
  });
  dropzone.addEventListener("drop", (e) => {
    const files = e.dataTransfer?.files;
    if (files && files.length > 0) handleFile(files[0]);
  });
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files[0]) handleFile(fileInput.files[0]);
  });

  // ---------------- Reset & download ----------------
  btnReset.addEventListener("click", () => {
    currentPayload = null;
    currentConfigText = null;
    currentFileName = null;
    activeFilter = "all";
    searchText = "";
    fileInput.value = "";
    searchInput.value = "";
    tabs.forEach((t) => t.classList.toggle("is-active", t.dataset.filter === "all"));
    resultsView.hidden = true;
    uploadView.hidden = false;
    btnDownload.hidden = true;
    btnReset.hidden = true;
    uploadError.hidden = true;
  });

  btnDownload.addEventListener("click", async () => {
    if (!currentConfigText) return;
    btnDownload.disabled = true;
    const originalText = btnDownload.textContent;
    btnDownload.textContent = "Preparing…";
    try {
      const fd = new FormData();
      fd.append("config_text", currentConfigText);
      const resp = await fetch("/api/download", { method: "POST", body: fd });
      if (!resp.ok) {
        const msg = await safeError(resp);
        throw new Error(msg);
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = buildDownloadName(currentFileName);
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert("Download failed: " + err.message);
    } finally {
      btnDownload.disabled = false;
      btnDownload.textContent = originalText;
    }
  });

  // ---------------- Tabs & search ----------------
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("is-active"));
      tab.classList.add("is-active");
      activeFilter = tab.dataset.filter;
      renderTable();
    });
  });
  searchInput.addEventListener("input", () => {
    searchText = searchInput.value.trim().toLowerCase();
    renderTable();
  });

  // ---------------- Core upload handler ----------------
  async function handleFile(file) {
    uploadError.hidden = true;
    if (!file) return;

    currentFileName = file.name || "config.txt";
    try {
      currentConfigText = await file.text();
    } catch (err) {
      showUploadError("Could not read file: " + err.message);
      return;
    }

    uploadProgress.hidden = false;
    try {
      const fd = new FormData();
      fd.append("config", file);
      const resp = await fetch("/api/parse", { method: "POST", body: fd });
      if (!resp.ok) {
        const msg = await safeError(resp);
        throw new Error(msg);
      }
      const payload = await resp.json();
      currentPayload = payload;
      renderResults(payload, file);
    } catch (err) {
      showUploadError(err.message);
      currentPayload = null;
      currentConfigText = null;
    } finally {
      uploadProgress.hidden = true;
    }
  }

  async function safeError(resp) {
    try {
      const data = await resp.json();
      return data.error || `HTTP ${resp.status}`;
    } catch {
      return `HTTP ${resp.status} ${resp.statusText}`;
    }
  }

  function showUploadError(msg) {
    uploadError.textContent = "✖ " + msg;
    uploadError.hidden = false;
  }

  // ---------------- Render ----------------
  function renderResults(payload, file) {
    uploadView.hidden = true;
    resultsView.hidden = false;
    btnDownload.hidden = false;
    btnReset.hidden = false;

    renderSourceBanner(file, payload);
    renderKpis(payload.kpis);
    renderVlanChart(payload);
    renderTableHead(payload.columns);
    renderTable();
  }

  function renderSourceBanner(file, payload) {
    sourceBanner.textContent = "";
    const parts = [
      textSpan("Parsed "),
      strongSpan(String(payload.kpis.total)),
      textSpan(" interface(s) from "),
      strongSpan(file.name),
      textSpan(` (${formatBytes(file.size)})`),
    ];
    parts.forEach((p) => sourceBanner.appendChild(p));
  }

  function renderKpis(kpis) {
    kpiGrid.textContent = "";
    const kpiDefs = [
      { key: "total",         label: "Total Interfaces",  cls: "" },
      { key: "trunks",        label: "Trunk Ports",       cls: "trunk" },
      { key: "access",        label: "Access Ports",      cls: "access" },
      { key: "routed",        label: "Routed / L3",       cls: "routed" },
      { key: "shutdown",      label: "Shutdown",          cls: "shutdown" },
      { key: "port_channels", label: "Port-Channels",     cls: "" },
      { key: "fex_fabric",    label: "FEX Fabric Links",  cls: "other" },
      { key: "distinct_vlans",label: "Distinct VLANs",    cls: "" },
    ];
    kpiDefs.forEach((def) => {
      const card = document.createElement("div");
      card.className = "kpi" + (def.cls ? " " + def.cls : "");
      const lbl = document.createElement("div");
      lbl.className = "kpi-label";
      lbl.textContent = def.label;
      const val = document.createElement("div");
      val.className = "kpi-value";
      val.textContent = String(kpis[def.key] ?? 0);
      card.appendChild(lbl);
      card.appendChild(val);
      kpiGrid.appendChild(card);
    });
  }

  function renderVlanChart(payload) {
    vlanChart.textContent = "";
    const data = payload.vlan_usage_top || [];
    const total = payload.vlan_usage_total || 0;
    vlanCaption.textContent = total
      ? `${data.length} of ${total} VLAN${total === 1 ? "" : "s"} referenced`
      : "";
    if (!data.length) {
      const empty = document.createElement("div");
      empty.className = "vlan-empty";
      empty.textContent = "No VLANs referenced in any interface configuration.";
      vlanChart.appendChild(empty);
      return;
    }
    const max = Math.max(...data.map((d) => d.count));
    data.forEach((d) => {
      const row = document.createElement("div");
      row.className = "vlan-row";

      const id = document.createElement("div");
      id.className = "vlan-id";
      id.textContent = "VLAN " + d.vlan;

      const track = document.createElement("div");
      track.className = "vlan-bar-track";
      const fill = document.createElement("div");
      fill.className = "vlan-bar-fill";
      const pct = max > 0 ? Math.max(4, (d.count / max) * 100) : 0;
      fill.style.width = pct + "%";
      track.appendChild(fill);

      const count = document.createElement("div");
      count.className = "vlan-count";
      count.textContent = String(d.count);

      row.appendChild(id);
      row.appendChild(track);
      row.appendChild(count);
      vlanChart.appendChild(row);
    });
  }

  function renderTableHead(columns) {
    tableHead.textContent = "";
    const tr = document.createElement("tr");
    columns.forEach((col) => {
      const th = document.createElement("th");
      th.textContent = col.label;
      tr.appendChild(th);
    });
    tableHead.appendChild(tr);
  }

  function renderTable() {
    tableBody.textContent = "";
    if (!currentPayload) return;

    const rows = currentPayload.rows.filter((r) => rowMatchesFilter(r));
    updateTabCounts(currentPayload.rows);

    if (rows.length === 0) {
      emptyState.hidden = false;
      return;
    }
    emptyState.hidden = true;

    const columns = currentPayload.columns;
    const frag = document.createDocumentFragment();
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      columns.forEach((col) => {
        const td = document.createElement("td");
        const value = r[col.key];
        if (col.key === "interface") {
          td.className = "iface-name";
          td.textContent = String(value ?? "");
        } else if (col.key === "mode") {
          td.appendChild(modeBadge(String(value ?? "")));
        } else if (col.key === "admin_status") {
          if (String(value) === "shutdown") {
            const b = document.createElement("span");
            b.className = "badge badge-shutdown";
            b.textContent = "shutdown";
            td.appendChild(b);
          } else {
            td.textContent = String(value ?? "");
          }
        } else if (col.key === "description" || col.key === "allowed_vlans") {
          td.className = "wrap";
          td.textContent = String(value ?? "");
        } else {
          td.textContent = value === 0 || value ? String(value) : "";
        }
        tr.appendChild(td);
      });
      frag.appendChild(tr);
    });
    tableBody.appendChild(frag);
  }

  function modeBadge(mode) {
    const span = document.createElement("span");
    span.className = "badge";
    let cls = "badge-other";
    if (mode === "trunk")        cls = "badge-trunk";
    else if (mode === "access")  cls = "badge-access";
    else if (mode === "routed")  cls = "badge-routed";
    else if (mode === "fex-fabric") cls = "badge-fex";
    span.classList.add(cls);
    span.textContent = mode || "—";
    return span;
  }

  function rowMatchesFilter(r) {
    // Tab filter
    if (activeFilter !== "all") {
      if (activeFilter === "trunk"   && r.mode !== "trunk")   return false;
      if (activeFilter === "access"  && r.mode !== "access")  return false;
      if (activeFilter === "routed"  && !(r.mode === "routed" || r.mode === "n/a")) return false;
      if (activeFilter === "shutdown"&& r.admin_status !== "shutdown") return false;
    }
    // Search filter
    if (searchText) {
      const haystack = [
        r.interface, r.description, r.allowed_vlans,
        r.access_vlan, r.native_vlan, r.vrf, r.port_channel, r.vpc,
        r.ip_address, r.ipv6_address, r.mode,
      ].map((v) => String(v ?? "").toLowerCase()).join(" | ");
      if (!haystack.includes(searchText)) return false;
    }
    return true;
  }

  function updateTabCounts(allRows) {
    el("count-all").textContent      = String(allRows.length);
    el("count-trunk").textContent    = String(allRows.filter(r => r.mode === "trunk").length);
    el("count-access").textContent   = String(allRows.filter(r => r.mode === "access").length);
    el("count-routed").textContent   = String(allRows.filter(r => r.mode === "routed" || r.mode === "n/a").length);
    el("count-shutdown").textContent = String(allRows.filter(r => r.admin_status === "shutdown").length);
  }

  // ---------------- Helpers ----------------
  function formatBytes(n) {
    if (!Number.isFinite(n)) return "";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(n < 10 && i > 0 ? 1 : 0) + " " + units[i];
  }

  function buildDownloadName(filename) {
    if (!filename) return "interfaces.xlsx";
    const stem = filename.replace(/\.[^.]+$/, "") || "interfaces";
    return stem + "-interfaces.xlsx";
  }

  function textSpan(text) { const s = document.createElement("span"); s.textContent = text; return s; }
  function strongSpan(text) { const s = document.createElement("strong"); s.textContent = text; return s; }
})();
