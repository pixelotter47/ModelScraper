const state = {
  data: null,
  logs: [],
  blockedQuery: "",
  masterQuery: "",
  pinned: JSON.parse(localStorage.getItem("modelscraper.pinned") || "[]"),
  // The user's own hide-browser choice. Stripchat displays the toggle
  // forced off, so this remembers what to put back for other platforms.
  headlessPreference: null,
};

let countries = [["All", "All Countries"], ["", "Unknown"]];

const MAX_MASTER_ROWS = 500;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  let payload = null;
  const text = await response.text();
  if (text) payload = JSON.parse(text);
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : null;
    // The API returns a structured envelope; rendering the object itself
    // showed the user "[object Object]" instead of the reason.
    let message = response.statusText;
    if (detail && typeof detail === "object") {
      message = detail.message || detail.code || message;
    } else if (detail) {
      message = detail;
    }
    const error = new Error(message);
    if (detail && typeof detail === "object") error.code = detail.code;
    throw error;
  }
  if (payload && payload.platforms) applyState(payload);
  return payload;
}

function taskPayload(task) {
  const toggle = $("#headlessToggle");
  const isStripchat = state.data && state.data.platform === "Stripchat";
  // Send null when the user never touched the toggle so the service can
  // resolve the platform-safe default instead of us guessing true.
  const headless = isStripchat
    ? false
    : (toggle.indeterminate ? null : toggle.checked);
  const startMinimized = $("#minimizeToggle").checked;
  // The labeled step button declares that the user prepared its network.
  const manualNetworkConfirmed = Boolean(state.settings && state.settings.vpnProvider === "manual");
  if (task === "full-auto") return { headless, startMinimized };
  if (task === "resume") {
    return { headless, startMinimized, runSelection: "resume" };
  }
  if (task === "step1" || task === "step2") return { headless, manualNetworkConfirmed };
  if (task === "step4" || task === "verify-master") return { startMinimized, manualNetworkConfirmed };
  return null;
}

function automaticVpnEnabled() {
  return Boolean(state.settings && state.settings.vpnProvider === "mullvad");
}

async function startTask(task) {
  if (!state.data) {
    throw new Error("Application state has not loaded yet.");
  }
  if ((task === "step1" || task === "step2" || task === "step4" || task === "verify-master") && !state.settings) {
    throw new Error("VPN preferences have not loaded yet.");
  }
  if ((task === "full-auto" || task === "resume") && !automaticVpnEnabled()) {
    throw new Error("Full Auto requires Mullvad (automatic). Use individual steps with Manual / any VPN.");
  }
  const map = {
    "full-auto": "/api/tasks/full-auto",
    resume: "/api/tasks/resume",
    step1: "/api/tasks/step1",
    step2: "/api/tasks/step2",
    step3: "/api/tasks/step3",
    step4: "/api/tasks/step4",
    stop: "/api/tasks/stop",
    "compile-master": "/api/master-list/compile",
    "verify-master": "/api/master-list/verify",
  };
  const body = taskPayload(task);
  await api(map[task], {
    method: "POST",
    body: body ? JSON.stringify(body) : undefined,
  });
}

function applyState(nextState) {
  state.data = nextState;
  state.logs = Array.isArray(nextState.logs) ? nextState.logs.slice() : state.logs;
  render();
}

function appendLog(message) {
  if (!message) return;
  if (state.logs[state.logs.length - 1] !== message) {
    state.logs.push(message);
    state.logs = state.logs.slice(-500);
  }
  renderLogs();
}

function render() {
  if (!state.data) return;
  const data = state.data;
  const isStripchat = data.platform === "Stripchat";
  const headlessToggle = $("#headlessToggle");
  if (isStripchat) {
    if (state.headlessPreference === null) {
      state.headlessPreference = headlessToggle.checked;
    }
    headlessToggle.checked = false;
    headlessToggle.indeterminate = false;
  } else if (state.headlessPreference !== null) {
    // Leaving Stripchat: restore the user's choice instead of leaving
    // every other platform silently running with the browser visible.
    headlessToggle.checked = state.headlessPreference;
    state.headlessPreference = null;
  }
  headlessToggle.disabled = data.busy || isStripchat;
  $("#locationSettingsFields").disabled = data.busy || !state.settings;
  $("#vpnRelayLocation").disabled = $("#vpnProvider").value !== "mullvad";
  $("#vpnModeInstructions").textContent = automaticVpnEnabled()
    ? "Mullvad (automatic): Full Auto controls the selected relay. For individual steps, connect VPN before Step 1; disconnect or bypass it before Steps 2 and 4. Ensure the app and browser use that network. Step 3 uses saved lists only."
    : "Manual / any VPN: connect your VPN and route the app and browser through it before Step 1. Disconnect or bypass it before Steps 2 and 4. Step 3 uses saved lists only. Clicking a step confirms you prepared its network. Full Auto is unavailable.";

  $("#platformTitle").textContent = data.platform;
  $("#statusBadge").textContent = data.status;
  $("#statusBadge").classList.toggle("busy", data.busy);
  const progress = data.progress || {};
  const lastOutcome = data.lastOutcome || {};
  $("#progressText").textContent = data.busy
    ? (progress.requiresUser
      ? `Waiting for user: ${progress.waitingReason || "browser challenge"}`
      : `${progress.phase || "Starting"}${progress.total ? ` · ${progress.completed || 0}/${progress.total}` : ""}`)
    : (lastOutcome.status ? `Last run: ${lastOutcome.status}` : "No active workflow");
  $("#resumeButton").hidden = !data.resumable;
  $("#sessionPath").textContent = data.sessionPath || "No session selected";
  $("#connectionDot").classList.add("online");
  $("#connectionDot").classList.remove("offline");
  $("#connectionLabel").textContent = "Connected";
  $("#sortSelect").value = data.masterListSortMode || "name";
  renderCountryFilter(data.masterListCountryFilter || "All");

  $$(".platform-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.platform === data.platform);
    button.disabled = data.busy;
  });

  $$("[data-task]").forEach((button) => {
    if (button.dataset.task === "stop") {
      button.disabled = !data.busy;
    } else {
      button.disabled = data.busy || ((button.dataset.task === "full-auto" || button.dataset.task === "resume") && !automaticVpnEnabled());
    }
    const manualLabels = {step1: "Step 1 — VPN ready: collect list", step2: "Step 2 — local ready: collect list", step4: "Step 4 — local ready: verify", "verify-master": "Local ready: verify master"};
    const standardLabels = {step1: "Run Step 1 - VPN List", step2: "Run Step 2 - Local List", step4: "Run Step 4 - Verify", "verify-master": "Verify Master"};
    if (manualLabels[button.dataset.task]) button.textContent = (automaticVpnEnabled() ? standardLabels : manualLabels)[button.dataset.task];
  });

  $$("[data-action]").forEach((button) => {
    button.disabled = data.busy && button.dataset.action !== "upload-blocked";
  });

  renderLogs();
  renderBlocked();
  renderMaster();
}

function renderLogs() {
  const output = $("#logOutput");
  output.textContent = state.logs.join("\n");
  output.scrollTop = output.scrollHeight;
}

function modelUsername(rawModel) {
  const trimmed = String(rawModel || "").trim().replace(/\/$/, "");
  const lastPart = trimmed.split("/").pop() || trimmed;
  return lastPart.replace(/^#/, "");
}

function copyModelName(rawModel) {
  const username = modelUsername(rawModel);
  if (username && navigator.clipboard) {
    navigator.clipboard.writeText(username).catch(() => {});
  }
}

function renderBlocked() {
  const blocked = state.data ? state.data.blockedModels || [] : [];
  const query = state.blockedQuery.trim().toLowerCase();
  const filtered = blocked.filter((model) => model.toLowerCase().includes(query));
  $("#blockedCount").textContent = blocked.length;
  $("#blockedList").innerHTML = "";
  filtered.forEach((model) => {
    const row = document.createElement("li");
    const link = document.createElement("a");
    link.href = modelUrl(model);
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = model;
    link.addEventListener("click", () => copyModelName(model));
    const pin = document.createElement("button");
    pin.type = "button";
    pin.className = "small-button";
    pin.textContent = state.pinned.includes(model) ? "Pinned" : "Pin";
    pin.addEventListener("click", () => togglePinned(model));
    row.append(link, pin);
    $("#blockedList").append(row);
  });
}

function renderMaster() {
  const models = state.data ? state.data.masterListModels || [] : [];
  const query = state.masterQuery.trim().toLowerCase();
  const filtered = models.filter((model) => {
    const name = String(model.name || "").toLowerCase();
    return name.includes(query);
  });
  const visible = filtered.slice(0, MAX_MASTER_ROWS);
  $("#masterCount").textContent = models.length;
  $("#visibleMasterCount").textContent =
    filtered.length > visible.length
      ? `Showing ${visible.length} of ${filtered.length}`
      : `Showing ${visible.length}`;
  $("#masterTableBody").innerHTML = "";
  visible.forEach((model) => {
    const row = document.createElement("tr");
    if (model.vpn_location_profile_match) {
      row.classList.add("restricted-location-profile-row");
    } else if (model.location_profile_match) {
      row.classList.add("location-profile-row");
    }
    const nameCell = document.createElement("td");
    const link = document.createElement("a");
    link.href = modelUrl(model.name || "");
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = model.name || "";
    link.addEventListener("click", () => copyModelName(model.name));
    nameCell.append(link);
    if (model.is_new) {
      const flag = document.createElement("span");
      flag.className = "new-flag";
      flag.textContent = "new";
      nameCell.append(flag);
    }
    if (model.vpn_location_profile_match) {
      const evidence = model.vpn_profile_match || {};
      const badge = document.createElement("span");
      badge.className = "restricted-location-profile-flag";
      badge.textContent = "RESTRICTED PROFILE";
      badge.title = [
        "Matching profile with an observed access restriction",
        evidence.location ? `Location: ${evidence.location}` : "",
        evidence.country ? `Country: ${evidence.country}` : "",
        Array.isArray(evidence.match_reasons) ? `Matched by: ${evidence.match_reasons.join(", ")}` : "",
        model.verification_reason ? `Observed reason: ${model.verification_reason}` : "",
      ]
        .filter(Boolean)
        .join("\n");
      nameCell.append(badge);
    } else if (model.location_profile_match) {
      const evidence = model.profile_match || {};
      const badge = document.createElement("span");
      badge.className = "location-profile-flag";
      badge.textContent = "PROFILE";
      badge.title = [
        "Profile location matches your preferences",
        evidence.location ? `Location: ${evidence.location}` : "",
        evidence.country ? `Country: ${evidence.country}` : "",
        Array.isArray(evidence.match_reasons) ? `Matched by: ${evidence.match_reasons.join(", ")}` : "",
        model.verification_reason ? `Observed reason: ${model.verification_reason}` : "",
      ]
        .filter(Boolean)
        .join("\n");
      nameCell.append(badge);
    }

    const dateCell = document.createElement("td");
    const timestamp = String(model.timestamp || "");
    dateCell.textContent =
      timestamp.length >= 16
        ? `${timestamp.slice(0, 10)} ${timestamp.slice(11, 16)}`
        : model.date || "Unknown";

    const countryCell = document.createElement("td");
    const countrySelect = document.createElement("select");
    countries
      .filter(([code]) => code !== "All")
      .forEach(([code, label]) => {
        const option = document.createElement("option");
        option.value = code;
        option.textContent = label;
        countrySelect.append(option);
      });
    countrySelect.value = model.country || "";
    countrySelect.addEventListener("change", async () => {
      await runAction(() =>
        api("/api/master-list/country", {
          method: "PATCH",
          body: JSON.stringify({ model: model.name, country: countrySelect.value }),
        })
      );
    });
    countryCell.append(countrySelect);

    const actionCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    actions.append(
      actionButton("Delete", () => deleteModel(model.name, false)),
      actionButton("Block", () => deleteModel(model.name, true))
    );
    actionCell.append(actions);

    row.append(nameCell, dateCell, countryCell, actionCell);
    $("#masterTableBody").append(row);
  });
}

function renderCountryFilter(selected) {
  const filter = $("#countryFilter");
  if (filter.dataset.catalog !== String(countries.length)) {
    filter.innerHTML = "";
    countries.forEach(([code, label]) => {
      const option = document.createElement("option");
      option.value = code;
      option.textContent = label;
      filter.append(option);
    });
    filter.dataset.catalog = String(countries.length);
  }
  filter.value = selected;
}

function actionButton(label, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "small-button";
  button.textContent = label;
  button.addEventListener("click", () => runAction(handler));
  return button;
}

async function deleteModel(model, block) {
  await api("/api/master-list", {
    method: "DELETE",
    body: JSON.stringify({ model, block }),
  });
}

function modelUrl(raw) {
  const value = String(raw || "").trim();
  if (!value) return "#";
  if (/^https?:\/\//i.test(value)) return value;
  const platform = state.data ? state.data.platform : "Chaturbate";
  if (platform === "MyFreeCams") return `https://www.myfreecams.com/#${encodeURIComponent(value)}`;
  if (platform === "Stripchat") return `https://stripchat.com/${encodeURIComponent(value)}`;
  if (platform === "XHamsterLive") return `https://xhamsterlive.com/${encodeURIComponent(value)}`;
  return `https://chaturbate.com/${encodeURIComponent(value)}/`;
}

function togglePinned(model) {
  if (state.pinned.includes(model)) {
    state.pinned = state.pinned.filter((item) => item !== model);
  } else {
    state.pinned = [model, ...state.pinned];
  }
  localStorage.setItem("modelscraper.pinned", JSON.stringify(state.pinned));
  renderBlocked();
}

async function runAction(action) {
  try {
    await action();
  } catch (error) {
    showToast(error.message || String(error));
  }
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("visible"), 3200);
}

function connectEvents() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/events`);
  socket.addEventListener("open", () => {
    $("#connectionDot").classList.add("online");
    $("#connectionDot").classList.remove("offline");
    $("#connectionLabel").textContent = "Connected";
  });
  socket.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (payload.type === "state") applyState(payload.state);
    if (payload.type === "log") appendLog(payload.message);
    if (payload.type === "progress" && state.data) {
      state.data.progress = payload.progress || {};
      render();
    }
    if (payload.type === "waitingForUser") {
      showToast("Browser challenge needs user action.");
    }
    if (payload.type === "taskFinished") {
      const status = payload.outcome && payload.outcome.status;
      showToast(status ? `${payload.task}: ${status}` : (payload.error ? `${payload.task} failed` : `${payload.task} finished`));
      refreshState();
    }
    if (payload.type === "masterListCompiled") {
      showToast(`Master list: ${payload.totalCount} models`);
    }
  });
  socket.addEventListener("close", () => {
    $("#connectionDot").classList.remove("online");
    $("#connectionDot").classList.add("offline");
    $("#connectionLabel").textContent = "Reconnecting";
    setTimeout(connectEvents, 1600);
  });
}

async function refreshState() {
  await runAction(() => api("/api/state"));
}

function wireEvents() {
  $$(".platform-button").forEach((button) => {
    button.addEventListener("click", () =>
      runAction(() =>
        api("/api/platform", {
          method: "POST",
          body: JSON.stringify({ platform: button.dataset.platform }),
        })
      )
    );
  });

  $$("[data-task]").forEach((button) => {
    button.addEventListener("click", () => runAction(() => startTask(button.dataset.task)));
  });

  $("[data-action='create-session']").addEventListener("click", () =>
    runAction(() => api("/api/session/create", { method: "POST" }))
  );
  $("[data-action='latest-session']").addEventListener("click", () =>
    runAction(() => api("/api/session/latest", { method: "POST" }))
  );
  $("[data-action='open-session']").addEventListener("click", () =>
    runAction(() => api("/api/session/open-folder", { method: "POST" }))
  );
  $("[data-action='upload-blocked']").addEventListener("click", () => {
    $("#blockedFileInput").click();
  });
  $("#blockedFileInput").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const text = await file.text();
    await runAction(() =>
      api("/api/blocked-list/load-text", {
        method: "POST",
        body: JSON.stringify({ text }),
      })
    );
    event.target.value = "";
  });
  $("[data-action='add-manual']").addEventListener("click", () => {
    const username = $("#manualModelInput").value.trim();
    if (!username) return;
    runAction(async () => {
      await api("/api/master-list/manual", {
        method: "POST",
        body: JSON.stringify({ username }),
      });
      $("#manualModelInput").value = "";
    });
  });

  $("#blockedSearch").addEventListener("input", (event) => {
    state.blockedQuery = event.target.value;
    renderBlocked();
  });
  $("#masterSearch").addEventListener("input", (event) => {
    state.masterQuery = event.target.value;
    renderMaster();
  });
  $("#sortSelect").addEventListener("change", (event) =>
    runAction(() =>
      api("/api/master-list/sort", {
        method: "POST",
        body: JSON.stringify({ mode: event.target.value }),
      })
    )
  );
  $("#countryFilter").addEventListener("change", (event) =>
    runAction(() =>
      api("/api/master-list/filter-country", {
        method: "POST",
        body: JSON.stringify({ country: event.target.value }),
      })
    )
  );
  $("#vpnProvider").addEventListener("change", () => {
    $("#vpnRelayLocation").disabled = $("#vpnProvider").value !== "mullvad";
  });
  $("#locationSettingsForm").addEventListener("submit", (event) => {
    event.preventDefault();
    runAction(async () => {
      const payload = await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          targetCountries: $("#targetCountries").value,
          targetLocationTerms: $("#targetLocationTerms").value,
          vpnRelayLocation: $("#vpnRelayLocation").value,
          vpnProvider: $("#vpnProvider").value,
        }),
      });
      applySettings(payload);
      showToast("Preferences saved. Use a new session after changing location preferences.");
    });
  });
  $("#clearLogsButton").addEventListener("click", () => {
    state.logs = [];
    renderLogs();
  });
  $("#saveLayoutButton").addEventListener("click", () => showToast("Layout saved in browser"));
}

function applySettings(payload) {
  state.settings = payload.settings || {};
  countries = [["All", "All Countries"], ["", "Unknown"],
    ...(payload.countryChoices || []).map((item) => [item.code, `${item.name} (${item.code})`])];
  $("#targetCountries").value = state.settings.targetCountries || "";
  $("#targetLocationTerms").value = state.settings.targetLocationTerms || "";
  $("#vpnProvider").value = state.settings.vpnProvider || "manual";
  const relays = $("#vpnRelayLocation");
  relays.innerHTML = "";
  (payload.vpnLocations || []).forEach((item) => {
    const option = document.createElement("option");
    option.value = item.code;
    option.textContent = item.name;
    relays.append(option);
  });
  relays.value = state.settings.vpnRelayLocation || "";
  const codes = $("#countryCodes");
  codes.innerHTML = "";
  (payload.countryChoices || []).forEach((item) => {
    const option = document.createElement("option");
    option.value = item.code;
    option.label = item.name;
    codes.append(option);
  });
  if (state.data) render();
}

wireEvents();
runAction(async () => applySettings(await api("/api/settings")));
refreshState();
connectEvents();
