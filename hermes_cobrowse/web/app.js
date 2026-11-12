/**
 * Hermes CoBrowse — Spatial Canvas Studio Client
 * Manages multi-window draggable/resizable live Chromium viewports, floating
 * Inspector Studio (Activity / DOM / Console / Network), bottom Dock, and
 * real-time /ws/studio multiplexed CDP streaming.
 */

(function () {
  "use strict";

  const PRESETS = [
    { label: "1280×800", width: 1280, height: 800 },
    { label: "1440×900", width: 1440, height: 900 },
    { label: "820×1024", width: 820, height: 1024 },
    { label: "390×844", width: 390, height: 844 },
  ];

  const authToken = new URLSearchParams(window.location.search).get("token") || "";

  function withAuthQuery(path) {
    if (!authToken) return path;
    const sep = path.includes("?") ? "&" : "?";
    return `${path}${sep}token=${encodeURIComponent(authToken)}`;
  }

  function authHeaders(extra = {}) {
    return authToken ? { ...extra, "X-CoBrowse-Token": authToken } : { ...extra };
  }

  // ---------------- DOM References ----------------
  const spatialStage = document.getElementById("spatialStage");
  const canvasSurface = document.getElementById("canvasSurface");
  const emptyStageCard = document.getElementById("emptyStageCard");

  const btnNewWindow = document.getElementById("btnNewWindow");
  const newWindowPopover = document.getElementById("newWindowPopover");
  const newWinUrl = document.getElementById("newWinUrl");
  const newWinTitle = document.getElementById("newWinTitle");
  const newWinPreset = document.getElementById("newWinPreset");
  const btnCancelNewWin = document.getElementById("btnCancelNewWin");
  const btnCreateNewWin = document.getElementById("btnCreateNewWin");
  const btnEmptyLaunch = document.getElementById("btnEmptyLaunch");

  const btnTileWindows = document.getElementById("btnTileWindows");
  const btnCascadeWindows = document.getElementById("btnCascadeWindows");
  const btnFitCanvas = document.getElementById("btnFitCanvas");
  const btnZoomOut = document.getElementById("btnZoomOut");
  const btnZoomReset = document.getElementById("btnZoomReset");
  const btnZoomIn = document.getElementById("btnZoomIn");

  const globalStatusBadge = document.getElementById("globalStatusBadge");
  const globalStatusText = document.getElementById("globalStatusText");
  const btnToggleInspector = document.getElementById("btnToggleInspector");
  const btnCommandPalette = document.getElementById("btnCommandPalette");

  const inspectorWindow = document.getElementById("inspectorWindow");
  const inspectorSessionChip = document.getElementById("inspectorSessionChip");
  const segmentedThumb = document.getElementById("segmentedThumb");
  const tabBtns = document.querySelectorAll(".inspector-tabs .tab-btn");
  const tabContents = {
    telemetry: document.getElementById("tabTelemetry"),
    elements: document.getElementById("tabElements"),
    console: document.getElementById("tabConsole"),
    network: document.getElementById("tabNetwork"),
  };
  const logStream = document.getElementById("logStream");
  const elementsList = document.getElementById("elementsList");
  const consoleList = document.getElementById("consoleList");
  const networkList = document.getElementById("networkList");
  const networkCounter = document.getElementById("networkCounter");
  const btnClearLog = document.getElementById("btnClearLog");
  const btnRefreshElements = document.getElementById("btnRefreshElements");
  const btnRefreshTelemetry = document.getElementById("btnRefreshTelemetry");

  const dockWindowsList = document.getElementById("dockWindowsList");
  const dockInspectorBtn = document.getElementById("dockInspectorBtn");
  const dockInspectorDot = document.getElementById("dockInspectorDot");
  const streamDot = document.getElementById("streamDot");
  const streamStatus = document.getElementById("streamStatus");
  const streamFps = document.getElementById("streamFps");

  const commandOverlay = document.getElementById("commandOverlay");
  const commandSearch = document.getElementById("commandSearch");
  const commandResults = document.getElementById("commandResults");

  // ---------------- Application State ----------------
  let ws = null;
  let reconnectTimer = null;
  let topZ = 30;
  let activeSessionId = null;
  let frameCounter = 0;

  const canvasView = {
    panX: 0,
    panY: 0,
    zoom: 1.0,
  };

  /** @type {Map<string, object>} */
  const sessions = new Map();

  const inspectorState = {
    id: "__inspector__",
    visible: true,
    minimized: false,
    maximized: false,
    x: 910,
    y: 22,
    width: 356,
    height: 600,
    restoreRect: null,
  };

  let activeTab = "telemetry";
  let lastMouseMoveSent = 0;
  let commandSelectedIdx = 0;

  // ---------------- Utilities ----------------
  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function nowClock(tsSec) {
    const d = tsSec ? new Date(tsSec * 1000) : new Date();
    return d.toTimeString().split(" ")[0];
  }

  function formatDuration(sec) {
    const m = Math.floor(sec / 60);
    const s = Math.max(0, sec % 60);
    return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }

  function nextZ() {
    topZ += 1;
    return topZ;
  }

  function getStageRect() {
    return {
      width: spatialStage.clientWidth || window.innerWidth,
      height: spatialStage.clientHeight || window.innerHeight - 54,
    };
  }

  function applyCanvasTransform() {
    canvasSurface.style.transform =
      `translate(${canvasView.panX}px, ${canvasView.panY}px) scale(${canvasView.zoom})`;
    spatialStage.style.backgroundPosition = `${canvasView.panX}px ${canvasView.panY}px`;
    const dotSize = Math.max(12, Math.round(22 * canvasView.zoom));
    spatialStage.style.backgroundSize = `${dotSize}px ${dotSize}px`;
    btnZoomReset.textContent = `${Math.round(canvasView.zoom * 100)}%`;
  }

  function appendLog(msg, kind = "info", sessionId = null) {
    const entry = document.createElement("div");
    entry.className = `log-entry ${kind}`;
    const prefix = sessionId ? `[${sessionId}] ` : "";
    entry.innerHTML = `<span class="log-time">${nowClock()}</span><span class="log-msg">${escapeHtml(prefix + msg)}</span>`;
    logStream.appendChild(entry);
    while (logStream.children.length > 250) {
      logStream.removeChild(logStream.firstChild);
    }
    logStream.scrollTop = logStream.scrollHeight;
  }

  // ---------------- Window Layout Math ----------------
  function computeInitialInspectorBounds() {
    const st = getStageRect();
    const w = Math.min(352, Math.max(288, Math.round(st.width * 0.27)));
    const h = Math.max(400, st.height - 86);
    const x = Math.max(16, st.width - w - 16);
    inspectorState.x = x;
    inspectorState.y = 18;
    inspectorState.width = w;
    inspectorState.height = h;
    applyWindowRect(inspectorWindow, inspectorState);
  }

  function computeNewBrowserRect(index, viewportW, viewportH) {
    const st = getStageRect();
    const reservedRight = inspectorState.visible && !inspectorState.minimized ? inspectorState.width + 44 : 36;
    const availW = Math.max(420, st.width - reservedRight - 24);
    const availH = Math.max(340, st.height - 86);

    const aspect = viewportW && viewportH ? viewportW / viewportH : 1280 / 800;
    let w = Math.min(availW, Math.round(availW * 0.94));
    let h = Math.round(w / aspect + 78);
    if (h > availH) {
      h = availH;
      w = Math.min(availW, Math.max(380, Math.round((h - 78) * aspect)));
    }
    w = Math.max(380, Math.min(w, availW));
    h = Math.max(300, Math.min(h, availH));

    const cascadeStep = (index % 5) * 34;
    const x = Math.min(Math.max(20, 22 + cascadeStep), Math.max(20, st.width - w - 20));
    const y = Math.min(Math.max(16, 20 + cascadeStep), Math.max(16, st.height - h - 68));
    return { x, y, width: w, height: h };
  }

  function applyWindowRect(el, stateObj) {
    el.style.left = `${Math.round(stateObj.x)}px`;
    el.style.top = `${Math.round(stateObj.y)}px`;
    el.style.width = `${Math.round(stateObj.width)}px`;
    el.style.height = `${Math.round(stateObj.height)}px`;
  }

  // ---------------- Generic OS Window Controls (Browser + Inspector) ----------------
  function makeWindowInteractive(winEl, winObj, options = {}) {
    const titlebar = winEl.querySelector(".cb-win-titlebar");
    const resizeHandles = winEl.querySelectorAll(".cb-resize-handle");

    winEl.addEventListener("pointerdown", () => {
      raiseWindow(winEl, winObj);
      if (options.onFocus) options.onFocus();
    });

    // Drag by titlebar
    titlebar.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      if (e.target.closest("button, input, select")) return;
      if (winObj.maximized) return;
      e.preventDefault();
      raiseWindow(winEl, winObj);
      if (options.onFocus) options.onFocus();

      const startX = e.clientX;
      const startY = e.clientY;
      const origX = winObj.x;
      const origY = winObj.y;
      winEl.classList.add("dragging");

      function onMove(ev) {
        const dx = (ev.clientX - startX) / canvasView.zoom;
        const dy = (ev.clientY - startY) / canvasView.zoom;
        winObj.x = origX + dx;
        winObj.y = Math.max(-20, origY + dy);
        winEl.style.left = `${Math.round(winObj.x)}px`;
        winEl.style.top = `${Math.round(winObj.y)}px`;
      }

      function onUp() {
        winEl.classList.remove("dragging");
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      }

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    });

    // 8-direction Dynamic Resize
    resizeHandles.forEach((handle) => {
      handle.addEventListener("pointerdown", (e) => {
        if (e.button !== 0 || winObj.maximized) return;
        e.preventDefault();
        e.stopPropagation();
        raiseWindow(winEl, winObj);
        if (options.onFocus) options.onFocus();

        const dir = handle.dataset.resize;
        const startX = e.clientX;
        const startY = e.clientY;
        const orig = {
          x: winObj.x,
          y: winObj.y,
          w: winObj.width,
          h: winObj.height,
        };
        const minW = options.minWidth || 340;
        const minH = options.minHeight || 280;

        winEl.classList.add("resizing");
        const dimPill = winEl.querySelector(".cb-resize-dim-pill");
        if (dimPill) {
          dimPill.classList.remove("hidden");
          dimPill.textContent = `${Math.round(orig.w)} × ${Math.round(orig.h)}`;
        }

        function onMove(ev) {
          const dx = (ev.clientX - startX) / canvasView.zoom;
          const dy = (ev.clientY - startY) / canvasView.zoom;

          let newX = orig.x;
          let newY = orig.y;
          let newW = orig.w;
          let newH = orig.h;

          if (dir.includes("e")) {
            newW = Math.max(minW, orig.w + dx);
          }
          if (dir.includes("s")) {
            newH = Math.max(minH, orig.h + dy);
          }
          if (dir.includes("w")) {
            const candidate = orig.w - dx;
            if (candidate >= minW) {
              newW = candidate;
              newX = orig.x + dx;
            } else {
              newW = minW;
              newX = orig.x + (orig.w - minW);
            }
          }
          if (dir.includes("n")) {
            const candidate = orig.h - dy;
            if (candidate >= minH) {
              newH = candidate;
              newY = orig.y + dy;
            } else {
              newH = minH;
              newY = orig.y + (orig.h - minH);
            }
          }

          winObj.x = newX;
          winObj.y = newY;
          winObj.width = newW;
          winObj.height = newH;
          applyWindowRect(winEl, winObj);
          if (dimPill) {
            dimPill.textContent = `${Math.round(newW)} × ${Math.round(newH)}`;
          }
        }

        function onUp() {
          winEl.classList.remove("resizing");
          if (dimPill) dimPill.classList.add("hidden");
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          if (options.onResizeEnd) options.onResizeEnd(winObj);
        }

        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      });
    });

    // Traffic Lights
    winEl.querySelectorAll("[data-win-action]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const act = btn.dataset.winAction;
        if (act === "close" && options.onClose) options.onClose();
        if (act === "minimize" && options.onMinimize) options.onMinimize();
        if (act === "maximize" && options.onMaximize) options.onMaximize();
      });
    });
  }

  function raiseWindow(winEl, winObj) {
    document.querySelectorAll(".cb-window.focused").forEach((el) => el.classList.remove("focused"));
    winEl.classList.add("focused");
    winEl.style.zIndex = String(nextZ());
  }

  function toggleMaximizeWindow(winEl, winObj, onComplete) {
    if (winObj.maximized && winObj.restoreRect) {
      Object.assign(winObj, winObj.restoreRect);
      winObj.restoreRect = null;
      winObj.maximized = false;
      winEl.classList.remove("maximized");
    } else {
      winObj.restoreRect = {
        x: winObj.x,
        y: winObj.y,
        width: winObj.width,
        height: winObj.height,
      };
      const st = getStageRect();
      winObj.x = (14 - canvasView.panX) / canvasView.zoom;
      winObj.y = (14 - canvasView.panY) / canvasView.zoom;
      winObj.width = Math.max(420, (st.width - 28) / canvasView.zoom);
      winObj.height = Math.max(320, (st.height - 80) / canvasView.zoom);
      winObj.maximized = true;
      winEl.classList.add("maximized");
    }
    applyWindowRect(winEl, winObj);
    raiseWindow(winEl, winObj);
    if (onComplete) setTimeout(() => onComplete(winObj), 260);
  }

  // ---------------- Inspector Studio Setup ----------------
  makeWindowInteractive(inspectorWindow, inspectorState, {
    minWidth: 300,
    minHeight: 340,
    onClose: () => {
      inspectorState.visible = false;
      inspectorWindow.classList.add("hidden");
      updateInspectorChromeState();
    },
    onMinimize: () => {
      inspectorState.minimized = true;
      inspectorWindow.classList.add("minimized");
      updateInspectorChromeState();
    },
    onMaximize: () => toggleMaximizeWindow(inspectorWindow, inspectorState),
  });

  function showOrFocusInspector() {
    inspectorState.visible = true;
    inspectorState.minimized = false;
    inspectorWindow.classList.remove("hidden", "minimized");
    applyWindowRect(inspectorWindow, inspectorState);
    raiseWindow(inspectorWindow, inspectorState);
    updateInspectorChromeState();
  }

  function updateInspectorChromeState() {
    const activeVisible = inspectorState.visible && !inspectorState.minimized;
    btnToggleInspector.classList.toggle("active", activeVisible);
    dockInspectorBtn.classList.toggle("focused", activeVisible);
    dockInspectorBtn.classList.toggle("minimized", inspectorState.visible && inspectorState.minimized);
    dockInspectorDot.className = "dock-dot " + (inspectorState.visible ? "connected" : "disconnected");
  }

  btnToggleInspector.addEventListener("click", () => {
    if (inspectorState.visible && !inspectorState.minimized) {
      inspectorState.minimized = true;
      inspectorWindow.classList.add("minimized");
      updateInspectorChromeState();
    } else {
      showOrFocusInspector();
    }
  });

  dockInspectorBtn.addEventListener("click", () => {
    showOrFocusInspector();
  });

  tabBtns.forEach((btn, idx) => {
    btn.addEventListener("click", () => {
      activeTab = btn.dataset.tab;
      tabBtns.forEach((b) => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      Object.entries(tabContents).forEach(([k, el]) => {
        el.classList.toggle("active", k === activeTab);
      });
      if (segmentedThumb) {
        segmentedThumb.style.transform = `translateX(${idx * 100}%)`;
      }
      if (activeTab === "elements" && activeSessionId) {
        fetchSessionDomState(activeSessionId);
      } else if ((activeTab === "console" || activeTab === "network") && activeSessionId) {
        fetchSessionTelemetry(activeSessionId);
      }
    });
  });

  btnClearLog.addEventListener("click", () => {
    logStream.innerHTML = "";
    appendLog("Action log cleared.");
  });
  btnRefreshElements.addEventListener("click", () => {
    if (activeSessionId) fetchSessionDomState(activeSessionId);
  });
  btnRefreshTelemetry.addEventListener("click", () => {
    if (activeSessionId) fetchSessionTelemetry(activeSessionId);
  });

  // ---------------- Session Window Lifecycle ----------------
  function createOrUpdateBrowserWindow(snapshot) {
    const sid = snapshot.session_id;
    let sess = sessions.get(sid);

    if (!sess) {
      const vp = snapshot.viewport || { width: 1280, height: 800 };
      const initialRect = computeNewBrowserRect(sessions.size, vp.width, vp.height);

      const winEl = document.createElement("section");
      winEl.className = "cb-window cb-browser-window";
      winEl.dataset.windowId = sid;
      winEl.innerHTML = buildBrowserWindowHtml(sid, snapshot, vp);

      canvasSurface.appendChild(winEl);
      applyWindowRect(winEl, initialRect);

      const canvasEl = winEl.querySelector(".cb-stream-canvas");
      const ctx2d = canvasEl.getContext("2d");

      sess = {
        id: sid,
        snapshot,
        x: initialRect.x,
        y: initialRect.y,
        width: initialRect.width,
        height: initialRect.height,
        minimized: false,
        maximized: false,
        restoreRect: null,
        interventionActive: Boolean(snapshot.intervention_active),
        interventionSeconds: snapshot.intervention_timeout || 0,
        interventionTimerHandle: null,
        takeover: Boolean(snapshot.user_takeover_active),
        elements: [],
        consoleLogs: [],
        networkLogs: [],
        lastAgentEventTs: 0,
        el: winEl,
        canvas: canvasEl,
        ctx: ctx2d,
        titleTextEl: winEl.querySelector(".cb-win-title"),
        statusDotEl: winEl.querySelector(".cb-win-status-dot"),
        urlInputEl: winEl.querySelector(".cb-url-input"),
        presetSelectEl: winEl.querySelector(".cb-preset-select"),
        backBtnEl: winEl.querySelector('[data-nav="back"]'),
        fwdBtnEl: winEl.querySelector('[data-nav="forward"]'),
        reloadBtnEl: winEl.querySelector('[data-nav="reload"]'),
        takeoverBtnEl: winEl.querySelector('[data-action="takeover"]'),
        shotBtnEl: winEl.querySelector('[data-action="screenshot"]'),
        loadingEl: winEl.querySelector(".cb-win-loading"),
        interventionSheetEl: winEl.querySelector(".cb-win-intervention"),
        intReasonEl: winEl.querySelector(".cb-int-title"),
        intGuidanceEl: winEl.querySelector(".cb-int-guidance"),
        intTimerEl: winEl.querySelector(".cb-int-timer"),
        resumeBtnEl: winEl.querySelector(".btn-win-resume"),
        agentCursorEl: winEl.querySelector(".cb-agent-cursor"),
        agentCursorTagEl: winEl.querySelector(".cursor-tag"),
        humanCursorEl: winEl.querySelector(".cb-human-cursor"),
        ripplesEl: winEl.querySelector(".cb-ripples-layer"),
      };

      sessions.set(sid, sess);
      wireBrowserWindowInteractions(sess);
      applySessionSnapshot(sess, snapshot);

      if (sess.interventionActive) {
        startWindowInterventionCountdown(sess, snapshot.intervention_timeout || 300);
      }

      if (!activeSessionId || activeSessionId === sid) {
        focusSession(sid, false);
      } else {
        raiseWindow(winEl, sess);
      }
    } else {
      applySessionSnapshot(sess, snapshot);
    }

    updateEmptyState();
    renderDock();
    updateGlobalStatus();
    return sess;
  }

  function buildBrowserWindowHtml(sid, snapshot, vp) {
    const title = escapeHtml(snapshot.title || "New Window");
    const url = escapeHtml(snapshot.url || "about:blank");
    const presetVal = `${vp.width}x${vp.height}`;

    let presetOpts = PRESETS.map((p) => {
      const val = `${p.width}x${p.height}`;
      return `<option value="${val}"${val === presetVal ? " selected" : ""}>${p.label}</option>`;
    }).join("");
    if (!PRESETS.some((p) => `${p.width}x${p.height}` === presetVal)) {
      presetOpts += `<option value="${escapeHtml(presetVal)}" selected>${escapeHtml(presetVal)}</option>`;
    }

    return `
      <div class="cb-win-titlebar" data-drag-target="${escapeHtml(sid)}">
        <div class="cb-traffic-lights" aria-label="Window controls">
          <button class="tl-btn tl-close" data-win-action="close" title="Close Browser Session" aria-label="Close">
            <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round">
              <line x1="1.6" y1="1.6" x2="6.4" y2="6.4"/><line x1="6.4" y1="1.6" x2="1.6" y2="6.4"/>
            </svg>
          </button>
          <button class="tl-btn tl-min" data-win-action="minimize" title="Minimize to Dock" aria-label="Minimize">
            <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round">
              <line x1="1.5" y1="4" x2="6.5" y2="4"/>
            </svg>
          </button>
          <button class="tl-btn tl-max" data-win-action="maximize" title="Maximize Window" aria-label="Maximize">
            <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="2 4.8 2 2 4.8 2"/><polyline points="6 3.2 6 6 3.2 6"/>
            </svg>
          </button>
        </div>

        <div class="cb-win-title-wrap">
          <span class="cb-win-status-dot"></span>
          <span class="cb-win-title">${title}</span>
          <span class="cb-session-chip">${escapeHtml(sid)}</span>
        </div>

        <div class="cb-win-right-tools">
          <button class="win-mini-icon-btn" data-action="screenshot" title="Download PNG Snapshot" aria-label="Download PNG Snapshot">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
              <path d="M2.5 5.2C2.5 4.4 3.1 3.8 3.9 3.8H5.4L6.3 2.5H9.7L10.6 3.8H12.1C12.9 3.8 13.5 4.4 13.5 5.2V11.8C13.5 12.6 12.9 13.2 12.1 13.2H3.9C3.1 13.2 2.5 12.6 2.5 11.8V5.2Z"/>
              <circle cx="8" cy="8.4" r="2.3"/>
            </svg>
          </button>
          <button class="win-chip-btn" data-action="takeover" title="Take manual mouse & keyboard control">
            <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3.2 2.4L6.8 13.2L8.7 8.9L13.1 7.1L3.2 2.4Z"/>
            </svg>
            <span>Takeover</span>
          </button>
        </div>
      </div>

      <div class="cb-win-toolbar">
        <div class="cb-nav-btns">
          <button class="win-mini-icon-btn" data-nav="back" title="Back" aria-label="Back">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="9.8 3.5 5.3 8 9.8 12.5"/>
            </svg>
          </button>
          <button class="win-mini-icon-btn" data-nav="forward" title="Forward" aria-label="Forward">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="6.2 3.5 10.7 8 6.2 12.5"/>
            </svg>
          </button>
          <button class="win-mini-icon-btn" data-nav="reload" title="Reload" aria-label="Reload">
            <svg width="12.5" height="12.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M13.2 7.8A5.2 5.2 0 1 1 11.7 4.2"/><path d="M13.3 2.4V5.1H10.6"/>
            </svg>
          </button>
        </div>

        <div class="cb-url-pill">
          <span class="lock-glyph" aria-hidden="true">
            <svg width="11" height="11" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round">
              <rect x="2.8" y="6.2" width="8.4" height="5.8" rx="1.6"/>
              <path d="M4.6 6.2V4.5A2.4 2.4 0 0 1 9.4 4.5V6.2"/>
            </svg>
          </span>
          <input type="text" class="cb-url-input" value="${url}" spellcheck="false" autocomplete="off" aria-label="Address bar" />
        </div>

        <select class="cb-preset-select" title="Resize Chromium Viewport">${presetOpts}</select>
      </div>

      <div class="cb-win-intervention hidden" role="status">
        <div class="cb-int-info">
          <div class="cb-int-title">${escapeHtml(snapshot.intervention_reason || "Human Verification Required")}</div>
          <div class="cb-int-guidance">${escapeHtml(snapshot.intervention_guidance || "Complete action directly below, then click Resume.")}</div>
        </div>
        <div class="cb-int-actions">
          <span class="cb-int-timer">05:00</span>
          <button class="btn-win-resume" type="button">Resume Agent</button>
        </div>
      </div>

      <div class="cb-win-viewport">
        <canvas class="cb-stream-canvas" width="${vp.width}" height="${vp.height}" tabindex="0"></canvas>
        <div class="cb-ripples-layer" aria-hidden="true"></div>
        <div class="cb-agent-cursor hidden" aria-hidden="true">
          <svg width="20" height="22" viewBox="0 0 22 24" fill="none">
            <path d="M3 2.5L18.5 11.4L11.2 13.4L8.1 20.5L3 2.5Z" fill="#1D1D1F" stroke="#FFFFFF" stroke-width="1.8" stroke-linejoin="round"/>
          </svg>
          <div class="cursor-tag">Hermes</div>
        </div>
        <div class="cb-human-cursor hidden" aria-hidden="true"></div>
        <div class="cb-win-loading">
          <svg class="spinner" width="22" height="22" viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="9.5" stroke="rgba(0,0,0,0.08)" stroke-width="2.2"/>
            <path d="M12 2.5A9.5 9.5 0 0 1 21.5 12" stroke="#1D1D1F" stroke-width="2.2" stroke-linecap="round"/>
          </svg>
          <span>Attaching CDP stream...</span>
        </div>
        <div class="cb-resize-dim-pill hidden">1280 × 800</div>
      </div>

      <div class="cb-resize-handle n" data-resize="n"></div>
      <div class="cb-resize-handle s" data-resize="s"></div>
      <div class="cb-resize-handle e" data-resize="e"></div>
      <div class="cb-resize-handle w" data-resize="w"></div>
      <div class="cb-resize-handle ne" data-resize="ne"></div>
      <div class="cb-resize-handle nw" data-resize="nw"></div>
      <div class="cb-resize-handle se" data-resize="se"></div>
      <div class="cb-resize-handle sw" data-resize="sw"></div>
    `;
  }

  function applySessionSnapshot(sess, snap) {
    sess.snapshot = { ...sess.snapshot, ...snap };
    if (snap.title) {
      sess.titleTextEl.textContent = snap.title;
    }
    if (snap.url && document.activeElement !== sess.urlInputEl) {
      sess.urlInputEl.value = snap.url;
    }
    if (snap.viewport) {
      syncCanvasResolution(sess, snap.viewport.width, snap.viewport.height);
    }
    if (typeof snap.can_go_back === "boolean") {
      sess.backBtnEl.disabled = !snap.can_go_back;
    }
    if (typeof snap.can_go_forward === "boolean") {
      sess.fwdBtnEl.disabled = !snap.can_go_forward;
    }
    if (typeof snap.user_takeover_active === "boolean") {
      sess.takeover = snap.user_takeover_active;
    }
    if (typeof snap.intervention_active === "boolean") {
      sess.interventionActive = snap.intervention_active;
      sess.interventionSheetEl.classList.toggle("hidden", !sess.interventionActive);
      if (snap.intervention_reason) sess.intReasonEl.textContent = snap.intervention_reason;
      if (snap.intervention_guidance) sess.intGuidanceEl.textContent = snap.intervention_guidance;
    }
    updateSessionModeUI(sess);
  }

  function syncCanvasResolution(sess, w, h) {
    if (!w || !h) return;
    if (sess.canvas.width !== w || sess.canvas.height !== h) {
      sess.canvas.width = w;
      sess.canvas.height = h;
    }
    const val = `${w}x${h}`;
    let opt = Array.from(sess.presetSelectEl.options).find((o) => o.value === val);
    if (!opt) {
      opt = document.createElement("option");
      opt.value = val;
      opt.textContent = `${w}×${h}`;
      sess.presetSelectEl.appendChild(opt);
    }
    sess.presetSelectEl.value = val;
  }

  function updateSessionModeUI(sess) {
    const dot = sess.statusDotEl;
    dot.className = "cb-win-status-dot";
    if (sess.interventionActive) {
      dot.classList.add("intervention");
    } else if (sess.takeover) {
      dot.classList.add("takeover");
    }
    sess.takeoverBtnEl.classList.toggle("active", sess.takeover);
    sess.canvas.classList.toggle("takeover-active", sess.takeover || sess.interventionActive);
  }

  function wireBrowserWindowInteractions(sess) {
    const sid = sess.id;

    makeWindowInteractive(sess.el, sess, {
      minWidth: 340,
      minHeight: 280,
      onFocus: () => focusSession(sid, true),
      onClose: () => closeSessionRequest(sid),
      onMinimize: () => {
        sess.minimized = true;
        sess.el.classList.add("minimized");
        renderDock();
      },
      onMaximize: () =>
        toggleMaximizeWindow(sess.el, sess, () => {
          syncViewportToWindowBody(sess);
        }),
      onResizeEnd: () => {
        syncViewportToWindowBody(sess);
      },
    });

    // Address bar navigate
    sess.urlInputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        navigateSession(sid, sess.urlInputEl.value.trim());
        sess.urlInputEl.blur();
      }
    });

    // Nav buttons
    sess.backBtnEl.addEventListener("click", () => sessionPost(sid, "back"));
    sess.fwdBtnEl.addEventListener("click", () => sessionPost(sid, "forward"));
    sess.reloadBtnEl.addEventListener("click", () => sessionPost(sid, "reload"));

    // Screenshot download
    sess.shotBtnEl.addEventListener("click", () => {
      window.open(withAuthQuery(`/api/sessions/${encodeURIComponent(sid)}/screenshot`), "_blank");
      appendLog(`Captured PNG snapshot`, "success", sid);
    });

    // Viewport preset selector
    sess.presetSelectEl.addEventListener("change", () => {
      const [wStr, hStr] = sess.presetSelectEl.value.split("x");
      const vw = parseInt(wStr, 10);
      const vh = parseInt(hStr, 10);
      if (vw && vh) {
        resizeSessionViewport(sid, vw, vh);
        if (!sess.maximized) {
          const st = getStageRect();
          const newW = Math.min(Math.max(360, vw), st.width - 60);
          const newH = Math.min(Math.max(300, vh + 78), st.height - 86);
          sess.width = newW;
          sess.height = Math.round(newW * (vh / vw) + 78);
          if (sess.height > st.height - 86) {
            sess.height = st.height - 86;
            sess.width = Math.max(360, Math.round((sess.height - 78) * (vw / vh)));
          }
          applyWindowRect(sess.el, sess);
        }
      }
    });

    // Takeover toggle
    sess.takeoverBtnEl.addEventListener("click", () => {
      sess.takeover = !sess.takeover;
      updateSessionModeUI(sess);
      renderDock();
      sendWs({ type: "takeover_toggle", session_id: sid, enabled: sess.takeover });
      appendLog(sess.takeover ? "Manual takeover enabled" : "Returned control to Autonomous mode", "info", sid);
    });

    // Resume Agent from per-window intervention banner
    sess.resumeBtnEl.addEventListener("click", async () => {
      sess.interventionSheetEl.classList.add("hidden");
      sess.interventionActive = false;
      stopWindowInterventionCountdown(sess);
      updateSessionModeUI(sess);
      updateGlobalStatus();
      renderDock();

      sendWs({ type: "resume_agent", session_id: sid, note: "Resolved on Spatial Canvas" });
      await sessionPost(sid, "resume", { note: "Resolved on Spatial Canvas" });
      appendLog("Resumed autonomous agent execution", "success", sid);
    });

    // Mouse / Keyboard live input relay on this window's canvas
    const c = sess.canvas;
    const isInteractive = () => sess.takeover || sess.interventionActive;

    c.addEventListener("mousedown", (e) => {
      focusSession(sid, true);
      c.focus();
      const coords = getCanvasCoords(c, e.clientX, e.clientY);
      spawnRipple(sess, coords.relX, coords.relY);
      if (!isInteractive()) return;

      const btnMap = { 0: "left", 1: "middle", 2: "right" };
      sendWs({
        type: "mouse_down",
        session_id: sid,
        x: coords.x,
        y: coords.y,
        button: btnMap[e.button] || "left",
      });
    });

    c.addEventListener("mouseup", (e) => {
      if (!isInteractive()) return;
      const coords = getCanvasCoords(c, e.clientX, e.clientY);
      const btnMap = { 0: "left", 1: "middle", 2: "right" };
      sendWs({
        type: "mouse_up",
        session_id: sid,
        x: coords.x,
        y: coords.y,
        button: btnMap[e.button] || "left",
      });
    });

    c.addEventListener("mousemove", (e) => {
      if (!isInteractive()) {
        sess.humanCursorEl.classList.add("hidden");
        return;
      }
      const coords = getCanvasCoords(c, e.clientX, e.clientY);
      sess.humanCursorEl.classList.remove("hidden");
      sess.humanCursorEl.style.left = `${coords.relX}px`;
      sess.humanCursorEl.style.top = `${coords.relY}px`;

      const now = performance.now();
      if (now - lastMouseMoveSent > 40) {
        lastMouseMoveSent = now;
        sendWs({ type: "mouse_move", session_id: sid, x: coords.x, y: coords.y });
      }
    });

    c.addEventListener("mouseleave", () => {
      sess.humanCursorEl.classList.add("hidden");
    });

    c.addEventListener("contextmenu", (e) => {
      if (isInteractive()) e.preventDefault();
    });

    c.addEventListener(
      "wheel",
      (e) => {
        if (!isInteractive()) return;
        e.preventDefault();
        sendWs({ type: "wheel", session_id: sid, deltaX: e.deltaX, deltaY: e.deltaY });
      },
      { passive: false }
    );

    c.addEventListener("keydown", (e) => {
      if (!isInteractive()) return;
      if (document.activeElement !== c) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") return;
      e.preventDefault();
      sendWs({ type: "key_down", session_id: sid, key: e.key, code: e.code });
    });

    c.addEventListener("keyup", (e) => {
      if (!isInteractive()) return;
      if (document.activeElement !== c) return;
      e.preventDefault();
      sendWs({ type: "key_up", session_id: sid, key: e.key, code: e.code });
    });

    c.addEventListener("paste", (e) => {
      if (!isInteractive()) return;
      const text = e.clipboardData?.getData("text");
      if (text) {
        e.preventDefault();
        sendWs({ type: "insert_text", session_id: sid, text });
      }
    });
  }

  function getContainedMetrics(canvas) {
    const rect = canvas.getBoundingClientRect();
    const layoutW = canvas.clientWidth || 1;
    const layoutH = canvas.clientHeight || 1;
    const imgRatio = (canvas.width || 1) / (canvas.height || 1);
    const boxRatio = layoutW / layoutH;
    let drawW, drawH, offsetX, offsetY;
    if (boxRatio > imgRatio) {
      drawH = layoutH;
      drawW = layoutH * imgRatio;
      offsetX = (layoutW - drawW) / 2;
      offsetY = 0;
    } else {
      drawW = layoutW;
      drawH = layoutW / imgRatio;
      offsetX = 0;
      offsetY = (layoutH - drawH) / 2;
    }
    return { rect, offsetX, offsetY, drawW, drawH };
  }

  function getCanvasCoords(canvas, clientX, clientY) {
    const { rect, offsetX, offsetY, drawW, drawH } = getContainedMetrics(canvas);
    const relX = (clientX - rect.left) / (canvasView.zoom || 1);
    const relY = (clientY - rect.top) / (canvasView.zoom || 1);
    const clampedX = Math.max(0, Math.min(1, (relX - offsetX) / (drawW || 1)));
    const clampedY = Math.max(0, Math.min(1, (relY - offsetY) / (drawH || 1)));
    return {
      x: Math.round(clampedX * canvas.width),
      y: Math.round(clampedY * canvas.height),
      relX,
      relY,
    };
  }

  function spawnRipple(sess, relX, relY) {
    const r = document.createElement("div");
    r.className = "user-ripple";
    r.style.left = `${relX}px`;
    r.style.top = `${relY}px`;
    sess.ripplesEl.appendChild(r);
    setTimeout(() => r.remove(), 460);
  }

  function moveAgentCursor(sess, x, y, label) {
    if (x == null || y == null) return;
    const { offsetX, offsetY, drawW, drawH } = getContainedMetrics(sess.canvas);
    if (!drawW || !drawH) return;
    const dispX = offsetX + (x / (sess.canvas.width || 1)) * drawW;
    const dispY = offsetY + (y / (sess.canvas.height || 1)) * drawH;
    sess.agentCursorEl.classList.remove("hidden");
    sess.agentCursorEl.style.transform = `translate(${dispX}px, ${dispY}px)`;
    if (label) sess.agentCursorTagEl.textContent = label;
    sess.lastAgentEventTs = performance.now();
    setTimeout(() => {
      if (performance.now() - sess.lastAgentEventTs > 2300) {
        sess.agentCursorEl.classList.add("hidden");
      }
    }, 2400);
  }

  function startWindowInterventionCountdown(sess, seconds) {
    stopWindowInterventionCountdown(sess);
    sess.interventionSeconds = Math.max(0, seconds || 300);
    sess.intTimerEl.textContent = formatDuration(sess.interventionSeconds);
    sess.interventionTimerHandle = setInterval(() => {
      sess.interventionSeconds = Math.max(0, sess.interventionSeconds - 1);
      sess.intTimerEl.textContent = formatDuration(sess.interventionSeconds);
      if (sess.interventionSeconds <= 0) {
        stopWindowInterventionCountdown(sess);
      }
    }, 1000);
  }

  function stopWindowInterventionCountdown(sess) {
    if (sess.interventionTimerHandle) {
      clearInterval(sess.interventionTimerHandle);
      sess.interventionTimerHandle = null;
    }
  }

  function syncViewportToWindowBody(sess, force = false) {
    const now = performance.now();
    if (!force && now - (sess.lastResizeSync || 0) < 150) return;
    sess.lastResizeSync = now;
    const vpBody = sess.el.querySelector(".cb-win-viewport");
    if (!vpBody) return;
    const targetW = Math.max(320, Math.min(1920, Math.round(vpBody.clientWidth)));
    const targetH = Math.max(240, Math.min(1200, Math.round(vpBody.clientHeight)));
    if (Math.abs(targetW - sess.canvas.width) >= 14 || Math.abs(targetH - sess.canvas.height) >= 14) {
      resizeSessionViewport(sess.id, targetW, targetH);
    }
  }

  // ---------------- REST API Helpers ----------------
  async function sessionPost(sid, action, body = null) {
    try {
      const resp = await fetch(`/api/sessions/${encodeURIComponent(sid)}/${action}`, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await resp.json();
      if (!resp.ok && data?.error) {
        appendLog(`${action} blocked: ${data.error}`, "warn", sid);
      }
      return data;
    } catch (e) {
      appendLog(`${action} failed: ${e.message}`, "warn", sid);
      return null;
    }
  }

  async function navigateSession(sid, url) {
    if (!url) return;
    appendLog(`Navigating to ${url}`, "info", sid);
    await sessionPost(sid, "navigate", { url });
  }

  async function resizeSessionViewport(sid, width, height) {
    const res = await sessionPost(sid, "resize", { width, height });
    if (res?.viewport) {
      const sess = sessions.get(sid);
      if (sess) syncCanvasResolution(sess, res.viewport.width, res.viewport.height);
      appendLog(`Viewport resized to ${width}×${height}`, "info", sid);
    }
  }

  async function createNewSession(url, title, width, height) {
    try {
      appendLog(`Launching Chromium window (${width}×${height})...`, "info");
      const resp = await fetch("/api/sessions", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ url: url || "about:blank", title: title || "", width, height }),
      });
      const data = await resp.json();
      if (!resp.ok && data?.error) {
        appendLog(`Window launch blocked: ${data.error}`, "warn");
        return data;
      }
      if (data.session) {
        const sess = createOrUpdateBrowserWindow(data.session);
        focusSession(sess.id, true);
        sendWs({ type: "request_frame", session_id: sess.id });
      }
      return data;
    } catch (e) {
      appendLog(`Failed to create session: ${e.message}`, "warn");
      return null;
    }
  }

  async function closeSessionRequest(sid) {
    const sess = sessions.get(sid);
    if (sess) sess.el.classList.add("closing");
    try {
      await fetch(`/api/sessions/${encodeURIComponent(sid)}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
    } catch (e) {
      appendLog(`Close error: ${e.message}`, "warn", sid);
    }
    removeBrowserWindow(sid);
  }

  function removeBrowserWindow(sid) {
    const sess = sessions.get(sid);
    if (!sess) return;
    stopWindowInterventionCountdown(sess);
    sess.el.remove();
    sessions.delete(sid);

    if (activeSessionId === sid) {
      const next = sessions.keys().next().value || null;
      activeSessionId = next;
      if (next) focusSession(next, false);
      else updateInspectorForNoSession();
    }
    updateEmptyState();
    renderDock();
    updateGlobalStatus();
  }

  function focusSession(sid, notifyBackend = true) {
    const sess = sessions.get(sid);
    if (!sess) return;
    activeSessionId = sid;
    if (sess.minimized) {
      sess.minimized = false;
      sess.el.classList.remove("minimized");
    }
    raiseWindow(sess.el, sess);
    inspectorSessionChip.textContent = sid;
    renderDock();
    renderConsoleList();
    renderNetworkList();
    if (activeTab === "elements") fetchSessionDomState(sid);
    fetchSessionTelemetry(sid);

    if (notifyBackend) {
      sendWs({ type: "focus_session", session_id: sid });
    }
  }

  function updateInspectorForNoSession() {
    inspectorSessionChip.textContent = "No Target";
    elementsList.innerHTML = `<p class="empty-state">Launch or focus a browser window to inspect DOM elements.</p>`;
    consoleList.innerHTML = `<p class="empty-state">No Chromium console messages captured yet.</p>`;
    networkList.innerHTML = `<p class="empty-state">No HTTP responses recorded for this session yet.</p>`;
    networkCounter.textContent = "0";
  }

  async function fetchSessionDomState(sid) {
    if (!sid) return;
    try {
      const resp = await fetch(`/api/sessions/${encodeURIComponent(sid)}/state`, {
        headers: authHeaders(),
      });
      if (!resp.ok) return;
      const data = await resp.json();
      const sess = sessions.get(sid);
      if (sess) {
        sess.elements = data.elements || [];
        if (sid === activeSessionId) renderElementsList(sess.elements, sid);
      }
    } catch (_) {}
  }

  async function fetchSessionTelemetry(sid) {
    if (!sid) return;
    try {
      const resp = await fetch(`/api/sessions/${encodeURIComponent(sid)}/telemetry`, {
        headers: authHeaders(),
      });
      if (!resp.ok) return;
      const data = await resp.json();
      const sess = sessions.get(sid);
      if (sess) {
        sess.consoleLogs = data.console_logs || [];
        sess.networkLogs = data.network_logs || [];
        if (sid === activeSessionId) {
          renderConsoleList();
          renderNetworkList();
        }
      }
    } catch (_) {}
  }

  function renderElementsList(elements, sid) {
    if (!elements || elements.length === 0) {
      elementsList.innerHTML = `<p class="empty-state">No visible interactive elements on the current viewport.</p>`;
      return;
    }
    elementsList.innerHTML = "";
    elements.forEach((el) => {
      const card = document.createElement("div");
      card.className = "element-card";
      card.innerHTML = `
        <div class="element-top">
          <span class="el-tag">&lt;${escapeHtml(el.tag)}&gt;</span>
          <div class="el-actions">
            <button class="el-copy-btn" type="button">Copy</button>
          </div>
        </div>
        <div class="el-label">${escapeHtml(el.text || el.id || el.name || "(unlabeled)")}</div>
        <div class="el-selector">${escapeHtml(el.selector)}</div>
      `;
      const copyBtn = card.querySelector(".el-copy-btn");
      copyBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        navigator.clipboard?.writeText(el.selector || "").then(() => {
          copyBtn.textContent = "Copied";
          setTimeout(() => { copyBtn.textContent = "Copy"; }, 1000);
        });
      });
      card.addEventListener("click", () => {
        sessionPost(sid, "highlight", { selector: el.selector });
        appendLog(`Highlighted element ${el.selector}`, "agent", sid);
      });
      elementsList.appendChild(card);
    });
  }

  function renderConsoleList() {
    const sess = activeSessionId ? sessions.get(activeSessionId) : null;
    const logs = sess ? sess.consoleLogs : [];
    if (!logs.length) {
      consoleList.innerHTML = `<p class="empty-state">No Chromium console messages captured yet.</p>`;
      return;
    }
    consoleList.innerHTML = "";
    logs.slice(-80).reverse().forEach((entry) => {
      const row = document.createElement("div");
      const lvl = (entry.level || "log").toLowerCase();
      row.className = "telemetry-row" + (lvl === "error" ? " err" : lvl === "warning" || lvl === "warn" ? " warn" : "");
      row.innerHTML = `
        <div class="net-row-head">
          <span class="net-method">${escapeHtml(lvl.toUpperCase())}</span>
          <span class="log-time">${nowClock(entry.ts)}</span>
        </div>
        <div class="log-msg">${escapeHtml(entry.text)}</div>
      `;
      consoleList.appendChild(row);
    });
  }

  function renderNetworkList() {
    const sess = activeSessionId ? sessions.get(activeSessionId) : null;
    const logs = sess ? sess.networkLogs : [];
    networkCounter.textContent = String(logs.length);
    if (!logs.length) {
      networkList.innerHTML = `<p class="empty-state">No HTTP responses recorded for this session yet.</p>`;
      return;
    }
    networkList.innerHTML = "";
    logs.slice(-80).reverse().forEach((entry) => {
      const row = document.createElement("div");
      row.className = "telemetry-row";
      const ok = entry.ok !== false && entry.status > 0 && entry.status < 400;
      const ms = entry.duration_ms != null ? `${entry.duration_ms}ms` : "--";
      row.innerHTML = `
        <div class="net-row-head">
          <span class="net-method">${escapeHtml(entry.method || "GET")} <span style="font-weight:400;color:var(--text-muted)">${escapeHtml(entry.resource_type || "")}</span></span>
          <span><span class="net-status ${ok ? "" : "fail"}">${entry.status || "ERR"}</span> <span class="log-time">${ms}</span></span>
        </div>
        <div class="net-url" title="${escapeHtml(entry.url)}">${escapeHtml(entry.url)}</div>
      `;
      networkList.appendChild(row);
    });
  }

  // ---------------- Canvas Arrangements & Pan / Zoom ----------------
  function tileWindows() {
    const open = Array.from(sessions.values()).filter((s) => !s.minimized);
    if (!open.length) return;
    canvasView.panX = 0;
    canvasView.panY = 0;
    canvasView.zoom = 1.0;
    applyCanvasTransform();

    const st = getStageRect();
    if (inspectorState.visible && !inspectorState.minimized && !inspectorState.maximized) {
      inspectorState.x = Math.max(16, st.width - inspectorState.width - 16);
      inspectorState.y = 18;
      inspectorState.height = Math.max(400, st.height - 86);
      applyWindowRect(inspectorWindow, inspectorState);
    }

    const reserveRight = inspectorState.visible && !inspectorState.minimized ? inspectorState.width + 32 : 20;
    const usableW = Math.max(360, st.width - reserveRight - 16);
    const usableH = Math.max(300, st.height - 84);
    let cols = open.length === 1 ? 1 : Math.ceil(Math.sqrt(open.length));
    if (cols > 1 && Math.floor((usableW - 14 * (cols - 1)) / cols) < 300) {
      cols = 1;
    }
    const rows = Math.ceil(open.length / cols);
    const gap = 14;
    const cellW = Math.max(280, Math.floor((usableW - gap * (cols - 1)) / cols));
    const cellH = Math.max(240, Math.floor((usableH - gap * (rows - 1)) / rows));

    open.forEach((sess, idx) => {
      const col = idx % cols;
      const row = Math.floor(idx / cols);
      sess.maximized = false;
      sess.el.classList.remove("maximized");
      sess.x = 16 + col * (cellW + gap);
      sess.y = 18 + row * (cellH + gap);
      sess.width = cellW;
      sess.height = cellH;
      applyWindowRect(sess.el, sess);
    });
    setTimeout(() => {
      open.forEach((sess) => syncViewportToWindowBody(sess, true));
    }, 360);
    appendLog(`Tiled ${open.length} window${open.length > 1 ? "s" : ""} across canvas`);
  }

  function cascadeWindows() {
    const open = Array.from(sessions.values()).filter((s) => !s.minimized);
    if (!open.length) return;
    canvasView.panX = 0;
    canvasView.panY = 0;
    canvasView.zoom = 1.0;
    applyCanvasTransform();

    open.forEach((sess, idx) => {
      const r = computeNewBrowserRect(idx, sess.canvas.width, sess.canvas.height);
      sess.maximized = false;
      sess.el.classList.remove("maximized");
      Object.assign(sess, r);
      applyWindowRect(sess.el, sess);
      sess.el.style.zIndex = String(nextZ());
    });
    const last = open[open.length - 1];
    if (last) focusSession(last.id, true);
  }

  function fitAllWindows() {
    const wins = Array.from(sessions.values()).filter((s) => !s.minimized);
    if (inspectorState.visible && !inspectorState.minimized) wins.push(inspectorState);
    if (!wins.length) {
      canvasView.panX = 0;
      canvasView.panY = 0;
      canvasView.zoom = 1.0;
      applyCanvasTransform();
      return;
    }

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    wins.forEach((w) => {
      minX = Math.min(minX, w.x);
      minY = Math.min(minY, w.y);
      maxX = Math.max(maxX, w.x + w.width);
      maxY = Math.max(maxY, w.y + w.height);
    });
    const st = getStageRect();
    const boxW = Math.max(100, maxX - minX);
    const boxH = Math.max(100, maxY - minY);
    const zoom = Math.min(1.1, Math.max(0.45, Math.min((st.width - 64) / boxW, (st.height - 104) / boxH)));
    canvasView.zoom = zoom;
    canvasView.panX = Math.round((st.width - boxW * zoom) / 2 - minX * zoom);
    canvasView.panY = Math.round((st.height - 48 - boxH * zoom) / 2 - minY * zoom);
    applyCanvasTransform();
  }

  // Pan empty Spatial Canvas background
  spatialStage.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    if (e.target !== spatialStage && e.target !== canvasSurface) return;
    e.preventDefault();
    spatialStage.classList.add("panning");
    const sx = e.clientX;
    const sy = e.clientY;
    const origPanX = canvasView.panX;
    const origPanY = canvasView.panY;

    function onMove(ev) {
      canvasView.panX = origPanX + (ev.clientX - sx);
      canvasView.panY = origPanY + (ev.clientY - sy);
      applyCanvasTransform();
    }
    function onUp() {
      spatialStage.classList.remove("panning");
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });

  btnTileWindows.addEventListener("click", tileWindows);
  btnCascadeWindows.addEventListener("click", cascadeWindows);
  btnFitCanvas.addEventListener("click", fitAllWindows);
  btnZoomOut.addEventListener("click", () => {
    canvasView.zoom = Math.max(0.45, Math.round((canvasView.zoom - 0.1) * 100) / 100);
    applyCanvasTransform();
  });
  btnZoomIn.addEventListener("click", () => {
    canvasView.zoom = Math.min(1.5, Math.round((canvasView.zoom + 0.1) * 100) / 100);
    applyCanvasTransform();
  });
  btnZoomReset.addEventListener("click", () => {
    canvasView.zoom = 1.0;
    canvasView.panX = 0;
    canvasView.panY = 0;
    applyCanvasTransform();
  });

  // ---------------- New Window Popover ----------------
  function toggleNewWinPopover(show) {
    const shouldShow = show ?? newWindowPopover.classList.contains("hidden");
    newWindowPopover.classList.toggle("hidden", !shouldShow);
    if (shouldShow) {
      newWinUrl.focus();
      newWinUrl.select();
    }
  }

  btnNewWindow.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleNewWinPopover();
  });
  btnEmptyLaunch.addEventListener("click", () => toggleNewWinPopover(true));
  btnCancelNewWin.addEventListener("click", () => toggleNewWinPopover(false));

  btnCreateNewWin.addEventListener("click", async () => {
    const [w, h] = newWinPreset.value.split("x").map((n) => parseInt(n, 10));
    const url = newWinUrl.value.trim() || "about:blank";
    const title = newWinTitle.value.trim();
    toggleNewWinPopover(false);
    await createNewSession(url, title, w || 1280, h || 800);
    newWinTitle.value = "";
  });

  newWinUrl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      btnCreateNewWin.click();
    }
  });

  document.addEventListener("click", (e) => {
    if (!newWindowPopover.classList.contains("hidden") && !e.target.closest("#newWindowPopover, #btnNewWindow, #btnEmptyLaunch")) {
      newWindowPopover.classList.add("hidden");
    }
  });

  // ---------------- Dock & Global Status ----------------
  function updateEmptyState() {
    emptyStageCard.classList.toggle("hidden", sessions.size > 0);
  }

  function updateGlobalStatus() {
    const count = sessions.size;
    const paused = Array.from(sessions.values()).filter((s) => s.interventionActive).length;
    if (paused > 0) {
      globalStatusBadge.className = "status-badge mode-intervention";
      globalStatusText.textContent = `${paused} Need${paused === 1 ? "s" : ""} Handoff`;
    } else {
      globalStatusBadge.className = "status-badge mode-autonomous";
      globalStatusText.textContent = `${count} Window${count === 1 ? "" : "s"}`;
    }
  }

  function renderDock() {
    dockWindowsList.innerHTML = "";
    sessions.forEach((sess, sid) => {
      const pill = document.createElement("button");
      pill.type = "button";
      pill.className = "dock-pill";
      if (sid === activeSessionId && !sess.minimized) pill.classList.add("focused");
      if (sess.minimized) pill.classList.add("minimized");

      const dotCls = sess.interventionActive ? "intervention" : sess.takeover ? "takeover" : "";
      const label = (sess.snapshot.title && sess.snapshot.title !== "Untitled Session")
        ? sess.snapshot.title
        : sid;
      const shortLabel = label.length > 22 ? label.slice(0, 20) + "…" : label;

      pill.innerHTML = `<span class="dock-dot ${dotCls}"></span><span>${escapeHtml(shortLabel)}</span>`;
      pill.title = `${label} (${sid})${sess.minimized ? " — Click to restore" : ""}`;
      pill.addEventListener("click", () => {
        if (sess.minimized) {
          sess.minimized = false;
          sess.el.classList.remove("minimized");
        }
        focusSession(sid, true);
      });
      dockWindowsList.appendChild(pill);
    });
  }

  // ---------------- Command Palette (Ctrl+K / Cmd+K) ----------------
  function getCommandItems() {
    const items = [
      { label: "New Browser Window", hint: "Create session", run: () => toggleNewWinPopover(true) },
      { label: "Arrange Windows: Tile Grid", hint: "Layout", run: tileWindows },
      { label: "Arrange Windows: Cascade", hint: "Layout", run: cascadeWindows },
      { label: "Fit All Windows in View", hint: "Canvas", run: fitAllWindows },
      { label: "Reset Canvas Zoom (100%)", hint: "Canvas", run: () => btnZoomReset.click() },
      { label: "Focus or Restore Inspector Studio", hint: "Panel", run: showOrFocusInspector },
    ];
    if (activeSessionId) {
      const sid = activeSessionId;
      items.push({ label: `Reload Active Window (${sid})`, hint: "Browser", run: () => sessionPost(sid, "reload") });
      items.push({
        label: `Download PNG Snapshot (${sid})`,
        hint: "Export",
        run: () => window.open(withAuthQuery(`/api/sessions/${encodeURIComponent(sid)}/screenshot`), "_blank"),
      });
    }
    sessions.forEach((sess, sid) => {
      items.push({
        label: `Switch to Window: ${sess.snapshot.title || sid}`,
        hint: sid,
        run: () => focusSession(sid, true),
      });
    });
    return items;
  }

  function renderCommandList() {
    const q = commandSearch.value.trim().toLowerCase();
    const list = getCommandItems().filter((it) => !q || it.label.toLowerCase().includes(q) || it.hint.toLowerCase().includes(q));
    commandSelectedIdx = Math.min(commandSelectedIdx, Math.max(0, list.length - 1));
    commandResults.innerHTML = "";
    list.forEach((item, idx) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "command-item" + (idx === commandSelectedIdx ? " selected" : "");
      btn.innerHTML = `<span>${escapeHtml(item.label)}</span><span class="cmd-hint">${escapeHtml(item.hint)}</span>`;
      btn.addEventListener("click", () => {
        closeCommandPalette();
        item.run();
      });
      commandResults.appendChild(btn);
    });
  }

  function openCommandPalette() {
    commandOverlay.classList.remove("hidden");
    commandSearch.value = "";
    commandSelectedIdx = 0;
    renderCommandList();
    commandSearch.focus();
  }

  function closeCommandPalette() {
    commandOverlay.classList.add("hidden");
  }

  btnCommandPalette.addEventListener("click", openCommandPalette);
  commandOverlay.addEventListener("click", (e) => {
    if (e.target === commandOverlay) closeCommandPalette();
  });
  commandSearch.addEventListener("input", () => {
    commandSelectedIdx = 0;
    renderCommandList();
  });
  commandSearch.addEventListener("keydown", (e) => {
    const items = commandResults.querySelectorAll(".command-item");
    if (e.key === "Escape") {
      closeCommandPalette();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      commandSelectedIdx = (commandSelectedIdx + 1) % Math.max(1, items.length);
      renderCommandList();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      commandSelectedIdx = (commandSelectedIdx - 1 + Math.max(1, items.length)) % Math.max(1, items.length);
      renderCommandList();
    } else if (e.key === "Enter" && items[commandSelectedIdx]) {
      e.preventDefault();
      items[commandSelectedIdx].click();
    }
  });

  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (commandOverlay.classList.contains("hidden")) openCommandPalette();
      else closeCommandPalette();
    } else if (e.key === "Escape" && !commandOverlay.classList.contains("hidden")) {
      closeCommandPalette();
    }
  });

  // ---------------- WebSocket (/ws/studio) ----------------
  function sendWs(payload) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  }

  function connectStudioSocket() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = withAuthQuery(`${proto}//${window.location.host}/ws/studio`);

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      streamDot.classList.add("connected");
      streamStatus.textContent = "Connected";
      appendLog("Connected to Spatial Canvas Studio gateway.", "success");
    };

    ws.onclose = () => {
      streamDot.classList.remove("connected");
      streamStatus.textContent = "Reconnecting";
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connectStudioSocket, 1500);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        handleStudioMessage(msg);
      } catch (_) {}
    };
  }

  function handleStudioMessage(msg) {
    const sid = msg.session_id;
    switch (msg.type) {
      case "studio_init": {
        activeSessionId = msg.active_session_id || null;
        const incomingIds = new Set((msg.sessions || []).map((s) => s.session_id));
        for (const existingSid of Array.from(sessions.keys())) {
          if (!incomingIds.has(existingSid)) removeBrowserWindow(existingSid);
        }
        (msg.sessions || []).forEach((snap) => createOrUpdateBrowserWindow(snap));
        if (activeSessionId && sessions.has(activeSessionId)) {
          focusSession(activeSessionId, false);
        }
        updateEmptyState();
        renderDock();
        updateGlobalStatus();
        break;
      }

      case "session_created": {
        if (msg.session) {
          createOrUpdateBrowserWindow(msg.session);
          appendLog(`Opened browser window (${msg.session.url || "about:blank"})`, "agent", sid);
          if (msg.active_session_id) focusSession(msg.active_session_id, false);
        }
        break;
      }

      case "session_closed": {
        removeBrowserWindow(sid);
        appendLog(`Closed browser session`, "info", sid);
        break;
      }

      case "session_focused": {
        if (sid && sessions.has(sid) && sid !== activeSessionId) {
          focusSession(sid, false);
        }
        break;
      }

      case "frame": {
        const sess = sessions.get(sid);
        if (!sess || !msg.data) break;
        if (!sess.loadingEl.classList.contains("hidden")) {
          sess.loadingEl.classList.add("hidden");
        }
        frameCounter += 1;
        const img = new Image();
        img.onload = () => {
          if (sess.canvas.width !== img.width || sess.canvas.height !== img.height) {
            syncCanvasResolution(sess, img.width, img.height);
          }
          sess.ctx.drawImage(img, 0, 0, sess.canvas.width, sess.canvas.height);
        };
        img.src = "data:image/jpeg;base64," + msg.data;
        break;
      }

      case "page_updated": {
        const sess = sessions.get(sid);
        if (!sess) break;
        applySessionSnapshot(sess, {
          url: msg.url,
          title: msg.title,
          can_go_back: msg.can_go_back,
          can_go_forward: msg.can_go_forward,
        });
        renderDock();
        appendLog(`Loaded: ${msg.title || msg.url}`, "info", sid);
        if (sid === activeSessionId && activeTab === "elements") {
          setTimeout(() => fetchSessionDomState(sid), 220);
        }
        break;
      }

      case "viewport_updated": {
        const sess = sessions.get(sid);
        if (sess && msg.viewport) {
          syncCanvasResolution(sess, msg.viewport.width, msg.viewport.height);
        }
        break;
      }

      case "agent_action": {
        const sess = sessions.get(sid);
        if (sess && msg.x !== undefined && msg.y !== undefined) {
          const shortLbl = msg.action === "click" ? "Clicking" : msg.action === "highlight" ? "Inspect" : "Hermes";
          moveAgentCursor(sess, msg.x, msg.y, shortLbl);
        }
        appendLog(msg.label || `Hermes action: ${msg.action}`, "agent", sid);
        break;
      }

      case "intervention_required": {
        const sess = sessions.get(sid);
        if (sess) {
          sess.interventionActive = true;
          sess.intReasonEl.textContent = msg.reason || "Human Verification Required";
          sess.intGuidanceEl.textContent = msg.guidance || "Complete action directly below, then resume.";
          sess.interventionSheetEl.classList.remove("hidden");
          startWindowInterventionCountdown(sess, msg.timeout || 300);
          updateSessionModeUI(sess);
          focusSession(sid, false);
        }
        updateGlobalStatus();
        renderDock();
        appendLog(`Intervention requested: ${msg.reason}`, "warn", sid);
        break;
      }

      case "intervention_resolved": {
        const sess = sessions.get(sid);
        if (sess) {
          sess.interventionActive = false;
          sess.interventionSheetEl.classList.add("hidden");
          stopWindowInterventionCountdown(sess);
          updateSessionModeUI(sess);
        }
        updateGlobalStatus();
        renderDock();
        appendLog(msg.label || "Intervention resolved", "success", sid);
        break;
      }

      case "takeover_status": {
        const sess = sessions.get(sid);
        if (sess) {
          sess.takeover = Boolean(msg.enabled);
          updateSessionModeUI(sess);
          renderDock();
        }
        break;
      }

      case "console_log": {
        const sess = sessions.get(sid);
        if (sess && msg.entry) {
          sess.consoleLogs.push(msg.entry);
          if (sess.consoleLogs.length > 200) sess.consoleLogs.shift();
          if (sid === activeSessionId && activeTab === "console") renderConsoleList();
        }
        break;
      }

      case "network_log": {
        const sess = sessions.get(sid);
        if (sess && msg.entry) {
          sess.networkLogs.push(msg.entry);
          if (sess.networkLogs.length > 200) sess.networkLogs.shift();
          if (sid === activeSessionId && activeTab === "network") renderNetworkList();
        }
        break;
      }
    }
  }

  // FPS ticker
  setInterval(() => {
    streamFps.textContent = String(frameCounter);
    frameCounter = 0;
  }, 1000);

  // ---------------- Boot ----------------
  computeInitialInspectorBounds();
  updateInspectorChromeState();
  applyCanvasTransform();
  connectStudioSocket();
})();
