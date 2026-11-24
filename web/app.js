"use strict";

const $ = (id) => document.getElementById(id);
const terminal = new Set(["completed", "partial", "failed"]);
const labels = {ok: "Ready", unavailable: "Unavailable", not_writable: "Not writable", pending: "Not checked"};
const operations = {download: "Download", scan: "Library scan", tidy: "Tidy preview"};
const sources = {api: "On demand", sync_manual: "Saved list", sync_schedule: "Scheduled sync", retry: "Retry", tidy_apply: "Approved tidy"};
const timer = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short", timeZone: "UTC"});
const utc = (value) => value ? `${timer.format(new Date(value))} UTC` : "—";
const nameOf = (job) => job.apply ? "Apply renames" : operations[job.operation] || job.operation;
const jobUrl = (id) => `/jobs/${encodeURIComponent(id)}`;
const jobHash = (id) => `#jobs/${encodeURIComponent(id)}`;

function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}

function badge(state, text) {
  const item = node("span", text || labels[state] || state, "badge");
  item.dataset.state = state;
  return item;
}

function button(text, action) {
  const item = node("button", text);
  item.type = "button";
  item.addEventListener("click", action);
  return item;
}

function link(text, href) {
  const item = node("a", text);
  item.href = href;
  return item;
}

function stat(label, value) {
  const item = node("div");
  const data = node("dd");
  data.append(value);
  item.append(node("dt", label), data);
  return item;
}

function feedback(message = "", error = false) {
  $("notice").hidden = !message;
  $("notice").textContent = message;
  $("notice").dataset.state = error ? "error" : "info";
}

let token = "";
let connected = false;
let view = "sync";
let jobId = null;
let jobDetails = null;
let syncSaved = null;
let syncBaseline = "";
const pendingKeys = new Map();

class RequestError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, {method = "GET", body, key, timeout = 30_000} = {}) {
  const headers = {Accept: "application/json"};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  const controller = new AbortController();
  const expiry = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {method, headers, credentials: "omit", redirect: "error",
      signal: controller.signal, body: body === undefined ? undefined : JSON.stringify(body)});
    let data;
    try { data = await response.json(); } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new RequestError(`The downloader returned HTTP ${response.status} without a JSON response.`, response.status);
    }
    if (!response.ok) throw new RequestError(data.error || `Request failed (HTTP ${response.status}).`, response.status);
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Request timed out. Retry the same action to recover its receipt.");
    throw error;
  } finally {
    clearTimeout(expiry);
  }
}

async function post(path, body = {}) {
  const signature = path + JSON.stringify(body);
  if (!pendingKeys.has(signature)) {
    const random = crypto.getRandomValues(new Uint8Array(16));
    pendingKeys.set(signature, "console:" + Array.from(random, (value) => value.toString(16).padStart(2, "0")).join(""));
  }
  try {
    const data = await request(path, {method: "POST", body, key: pendingKeys.get(signature)});
    pendingKeys.delete(signature);
    return data;
  } catch (error) {
    if (error.status >= 400 && error.status < 500) pendingKeys.delete(signature);
    throw error;
  }
}

function accessRequired(message = "") {
  token = "";
  connected = false;
  $("workspace").hidden = true;
  $("navigation").hidden = true;
  $("reconnect").hidden = true;
  $("sign-out").hidden = true;
  $("access").hidden = false;
  $("connection").textContent = "Locked";
  $("connection").dataset.state = "pending";
  if (message) feedback(message, true);
  $("api-token").focus();
}

function showError(error) {
  if (error.status === 401) accessRequired("The console needs a valid API token.");
  else feedback(error.message, true);
}

async function act(control, action) {
  if (control.dataset.busy) return;
  control.dataset.busy = "true";
  control.disabled = true;
  feedback();
  try { await action(); } catch (error) { showError(error); }
  finally {
    delete control.dataset.busy;
    control.disabled = false;
    updateSyncButtons();
    updateApplyButton();
  }
}

class SetPicker {
  constructor(prefix, limit, changed) {
    this.limit = limit;
    this.changed = changed;
    this.codes = [];
    this.rows = [];
    this.count = 12;
    this.root = $(`${prefix}-picker`);
    this.selected = node("ul", undefined, "selected-sets");
    this.selected.setAttribute("aria-label", "Selected sets");
    this.query = node("input");
    this.query.type = "search";
    this.query.id = `${prefix}-search`;
    this.query.placeholder = "Name or code, e.g. MSH";
    this.query.autocomplete = "off";
    this.filter = node("select");
    this.filter.id = `${prefix}-filter`;
    for (const [value, text] of [["all", "All types"], ["paper", "Paper sets"], ["token", "Token sets"], ["digital", "Digital sets"]]) {
      const option = node("option", text);
      option.value = value;
      this.filter.append(option);
    }
    const controls = node("div", undefined, "picker-controls");
    const searchLabel = node("label", "Find a set"); searchLabel.append(this.query);
    const filterLabel = node("label", "Type"); filterLabel.append(this.filter);
    controls.append(searchLabel, filterLabel);
    this.selection = node("p", undefined, "caption");
    this.results = node("ul", undefined, "set-results");
    this.results.setAttribute("aria-label", "Matching sets");
    this.info = node("p", "Loading the catalogue…", "caption");
    this.more = button("Show more sets", () => { this.count += 24; this.drawMatches(); });
    this.more.hidden = true;
    this.retry = button("Reload catalogue", () => loadCatalogue());
    this.retry.hidden = true;
    this.manual = button("", () => this.add(this.query.value.trim().toLowerCase()));
    this.manual.hidden = true;
    this.root.append(node("h3", "Select sets"), this.selection, this.selected, controls, this.info,
      this.results, this.more, this.manual, this.retry);
    this.query.addEventListener("input", () => { this.count = 12; this.drawMatches(); });
    this.filter.addEventListener("change", () => { this.count = 12; this.drawMatches(); });
    this.query.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const code = this.query.value.trim().toLowerCase();
      if (/^[a-z0-9]{1,12}$/.test(code)) this.add(code);
    });
    this.drawSelection();
  }

  setCodes(codes) {
    this.codes = [...codes];
    this.drawSelection();
    this.drawMatches();
  }

  catalogue(rows, error = "") {
    this.rows = rows;
    this.failure = error;
    this.drawSelection();
    this.drawMatches();
  }

  add(code) {
    if (this.codes.includes(code) || this.codes.length >= this.limit) return;
    this.setCodes([...this.codes, code]);
    this.changed();
    this.query.focus();
  }

  drawSelection() {
    this.selection.textContent = `${this.codes.length} / ${this.limit} selected`;
    this.selected.replaceChildren(...this.codes.map((code) => {
      const item = node("li");
      const control = button(`${code.toUpperCase()} ×`, () => {
        this.setCodes(this.codes.filter((selected) => selected !== code));
        this.changed();
        this.query.focus();
      });
      const name = this.rows.find((row) => row.code === code)?.name;
      if (name) control.title = name;
      control.setAttribute("aria-label", `Remove ${code.toUpperCase()}`);
      item.append(control);
      return item;
    }));
  }

  drawMatches() {
    const query = this.query.value.trim().toLowerCase();
    const filter = this.filter.value;
    const matched = this.rows.filter((row) => {
      if (filter === "paper" && (row.digital || row.set_type === "token")) return false;
      if (filter === "token" && row.set_type !== "token") return false;
      if (filter === "digital" && !row.digital) return false;
      return !query || row.code.includes(query) || row.name.toLowerCase().includes(query) || row.parent_set_code === query;
    });
    const exact = matched.findIndex((row) => row.code === query);
    if (exact > 0) matched.unshift(...matched.splice(exact, 1));
    this.info.textContent = this.failure || (this.rows.length ? (matched.length ?
      `Showing ${Math.min(matched.length, this.count)} of ${matched.length} matching sets` : "No matching sets") : "Loading the catalogue…");
    this.more.hidden = matched.length <= this.count;
    this.retry.hidden = !this.failure;
    this.manual.hidden = !/^[a-z0-9]{1,12}$/.test(query) || this.rows.some((row) => row.code === query);
    this.manual.textContent = `Add exact code ${query.toUpperCase()}`;
    this.manual.disabled = this.codes.includes(query) || this.codes.length >= this.limit;
    this.results.replaceChildren(...matched.slice(0, this.count).map((row) => {
      const description = node("div");
      const meta = [row.code.toUpperCase(), row.set_type.replaceAll("_", " "), row.released_at];
      if (row.parent_set_code) meta.push(`child of ${row.parent_set_code.toUpperCase()}`);
      description.append(node("span", row.name, "set-name"), node("span", meta.filter(Boolean).join(" · "), "set-metadata"));
      const selected = this.codes.includes(row.code);
      const add = button(selected ? "Added" : "Add", () => this.add(row.code));
      add.setAttribute("aria-label", `${selected ? "Added" : "Add"} ${row.code.toUpperCase()}`);
      add.disabled = selected || this.codes.length >= this.limit;
      const item = node("li");
      item.append(description, add);
      return item;
    }));
  }
}

function policyControls(prefix) {
  const quality = node("select");
  quality.id = `${prefix}-quality`;
  const qualities = [["", "No minimum · keep valid files"], ["small", "Small · 146 × 204"], ["normal", "Normal · 488 × 680"],
    ["large", "Large · 672 × 936"], ["png", "Native PNG · 744 × 1040"]];
  for (const [value, text] of qualities) { const option = node("option", text); option.value = value; quality.append(option); }
  const languages = node("input");
  languages.id = `${prefix}-languages`;
  languages.type = "text";
  languages.value = "zhs, en";
  languages.required = true;
  languages.spellcheck = false;
  const langLabel = node("label", "Languages in priority order");
  langLabel.append(languages, node("small", "Comma-separated codes. zhs = Simplified Chinese; en = English."));
  const qualityLabel = node("label", "Minimum quality"); qualityLabel.append(quality);
  const flags = [["refresh_language", "Replace with a verified preferred language"], ["refresh_scan", "Refresh when Scryfall has a newer scan"]];
  const controls = {};
  const root = $(`${prefix}-policy`);
  root.append(langLabel, qualityLabel);
  for (const [name, text] of flags) {
    const label = node("label", undefined, "check");
    controls[name] = node("input");
    controls[name].type = "checkbox";
    controls[name].id = `${prefix}-${name.replace("_", "-")}`;
    label.append(controls[name], text);
    root.append(label);
  }
  return {
    read: () => ({languages: languages.value.split(",").map((language) => language.trim()).filter(Boolean),
      min_quality: quality.value || null, refresh_language: controls.refresh_language.checked, refresh_scan: controls.refresh_scan.checked}),
    fill: (policy) => {
      languages.value = policy.languages.join(", ");
      quality.value = policy.min_quality || "";
      for (const [name] of flags) controls[name].checked = policy[name];
    },
  };
}

const syncPolicy = policyControls("sync");
const libraryPolicy = policyControls("library");
const syncPicker = new SetPicker("sync", 100, updateSyncButtons);
const libraryPicker = new SetPicker("library", 10, () => {});

let loadingCatalogue = false;
async function loadCatalogue() {
  if (loadingCatalogue || !connected) return;
  loadingCatalogue = true;
  try {
    const {sets} = await request("/sets");
    sets.sort((a, b) => (b.released_at || "").localeCompare(a.released_at || "") || a.code.localeCompare(b.code));
    for (const picker of [syncPicker, libraryPicker]) picker.catalogue(sets);
  } catch (error) {
    if (error.status === 401) showError(error);
    else for (const picker of [syncPicker, libraryPicker]) picker.catalogue(picker.rows, `Catalogue unavailable: ${error.message}`);
  } finally { loadingCatalogue = false; }
}

function syncConfig() {
  return {sets: syncPicker.codes, ...syncPolicy.read(), include_tokens: $("sync-tokens").checked,
    new_releases_days: $("sync-recent").valueAsNumber || null, interval_hours: $("sync-interval").valueAsNumber || null};
}

function fillSync(saved) {
  syncPicker.setCodes(saved.sets);
  syncPolicy.fill(saved);
  $("sync-tokens").checked = saved.include_tokens;
  $("sync-recent").value = saved.new_releases_days ?? "";
  $("sync-interval").value = saved.interval_hours ?? "";
  syncBaseline = JSON.stringify(syncConfig());
  drawSync(saved);
}

function updateSyncButtons() {
  const config = syncConfig();
  const dirty = syncBaseline !== JSON.stringify(config);
  const active = syncSaved?.last_job_status && !terminal.has(syncSaved.last_job_status);
  $("save-sync").disabled = !dirty || Boolean($("save-sync").dataset.busy);
  $("run-sync").disabled = dirty || (!config.sets.length && !config.new_releases_days) || Boolean(active || $("run-sync").dataset.busy);
  $("sync-draft").textContent = dirty ? "Unsaved changes" : active ? "Wait for the last sync job to finish." : "Saved policy";
}

let syncStatusSignature = "";
function drawSync(saved) {
  syncSaved = saved;
  updateSyncButtons();
  const signature = JSON.stringify([saved.next_run_at, saved.last_run_at, saved.last_job_id, saved.last_job_status, saved.last_error]);
  if (signature === syncStatusSignature) return;
  syncStatusSignature = signature;
  const last = saved.last_job_id ? link(saved.last_job_status || "Receipt expired", jobHash(saved.last_job_id)) :
    saved.last_run_at ? "No matching sets" : "Not run yet";
  $("sync-status").replaceChildren(stat("Next scheduled", saved.next_run_at ? utc(saved.next_run_at) : "Manual only"),
    stat("Last run", utc(saved.last_run_at)), stat("Last receipt", last));
  $("sync-error").hidden = !saved.last_error;
  $("sync-error").textContent = saved.last_error ? `Schedule deferred: ${saved.last_error}` : "";
}

$("sync-form").addEventListener("input", updateSyncButtons);
$("sync-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act($("save-sync"), async () => {
    fillSync(await request("/sync-list", {method: "PUT", body: syncConfig()}));
    feedback("Sync list saved.");
  });
});
$("run-sync").addEventListener("click", () => act($("run-sync"), async () => goToJob(await post("/sync-list/run"))));
$("library-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const operation = event.submitter?.value || "download";
  if (!libraryPicker.codes.length) { feedback("Select at least one set.", true); libraryPicker.query.focus(); return; }
  act(event.submitter || $("library-form").querySelector("button[name=operation]"), async () => {
    const options = libraryPolicy.read();
    if (operation !== "download") { delete options.refresh_language; delete options.refresh_scan; }
    goToJob(await post("/jobs", {sets: libraryPicker.codes, operation, ...options}));
  });
});

function goToJob(job) {
  feedback(`${nameOf(job)} accepted. Follow the receipt for its result.`);
  if (location.hash === jobHash(job.id)) drawJob(job);
  else location.hash = jobHash(job.id);
}

let historySignature = "";
function drawHistory(jobs) {
  const signature = JSON.stringify([jobId, jobs.map((job) => [job.id, job.status, job.sets_finished])]);
  if (signature === historySignature) return;
  historySignature = signature;
  const focused = $("job-list").contains(document.activeElement) ? document.activeElement.hash : null;
  const items = jobs.map((job) => {
    const item = node("li");
    const receipt = link("", jobHash(job.id));
    receipt.className = "job-link";
    if (job.id === jobId) receipt.setAttribute("aria-current", "true");
    const codes = job.set_codes;
    const title = codes.slice(0, 4).map((code) => code.toUpperCase()).join(", ");
    receipt.append(node("strong", nameOf(job)), badge(job.status), node("small",
      `${title}${codes.length > 4 ? ` +${codes.length - 4}` : ""} · ${job.sets_finished}/${codes.length} sets · ${utc(job.created_at)}`));
    item.append(receipt);
    return item;
  });
  $("job-list").replaceChildren(...(items.length ? items : [node("li", "No jobs yet.", "muted")]));
  if (focused) Array.from($("job-list").querySelectorAll("a")).find((item) => item.hash === focused)?.focus({preventScroll: true});
}

let actionSignature = "";
let detailSignature = "";
function fullDetail(report, code, open) {
  const detail = node("details", undefined, "set-report");
  detail.dataset.code = code;
  detail.open = open;
  const heading = node("summary");
  const amount = report.total_images === null ? "Image total pending" : `${report.processed_images} / ${report.total_images} images`;
  heading.append(node("strong", code.toUpperCase()), badge(report.status), node("span", amount, "caption"));
  detail.append(heading);
  if (report.total_images > 0) {
    const progress = node("progress");
    progress.max = report.total_images;
    progress.value = report.processed_images;
    progress.setAttribute("aria-label", `${code.toUpperCase()} image progress`);
    detail.append(progress);
  }
  const counts = [["downloaded", "Downloaded"], ["cached", "Reused"], ["unavailable", "Unavailable"], ["failed", "Failed"],
    ["missing", "Missing"], ["corrupt", "Corrupt"], ["below_quality", "Below quality"], ["legacy", "Legacy"],
    ["renames", "Proposed renames"], ["tidied", "Renamed"]];
  const visible = counts.filter(([field]) => report[field]);
  if (visible.length) {
    const stats = node("dl", undefined, "stats");
    stats.append(...visible.map(([field, name]) => stat(name, String(report[field]))));
    detail.append(stats);
  }
  const languages = Object.entries(report.languages).filter(([, count]) => count).map(([lang, count]) => `${lang} ${count}`);
  if (languages.length) detail.append(node("p", `Languages: ${languages.join(" · ")}`, "caption"));
  for (const [field, label] of [["warnings", "Warning"], ["errors", "Error"], ["issues", "Finding"]]) {
    if (!report[field].length) continue;
    const list = node("ul", undefined, `messages ${field}`);
    list.append(...report[field].map((text) => node("li", `${label}: ${text}`)));
    detail.append(list);
  }
  return detail;
}

function drawJob(job) {
  const previous = jobDetails;
  const signature = JSON.stringify(job);
  if (detailSignature === signature) return;
  detailSignature = signature;
  jobDetails = job;
  const heading = node("h3", nameOf(job)); heading.id = "job-title";
  const meta = node("p", undefined, "muted");
  meta.append(badge(job.status), ` ${sources[job.source] || "On demand"} · ${job.sets_finished}/${Object.keys(job.sets).length} sets finished`);
  const stamps = node("dl", undefined, "stats");
  stamps.append(stat("Created", utc(job.created_at)), stat("Last progress", utc(job.updated_at)), stat("Finished", utc(job.finished_at)));
  const receipt = node("p", `Job ${job.id}`, "caption mono");
  $("job-meta").replaceChildren(heading, meta, stamps, receipt);
  if (job.preview_id) $("job-meta").append(link("Open source preview", jobHash(job.preview_id)));
  const actionKey = `${job.id}/${job.status}`;
  if (actionSignature !== actionKey) {
    actionSignature = actionKey;
    const actions = [];
    const control = (label, command) => button(label, (event) => act(event.currentTarget, async () => {
      const result = await post(`${jobUrl(job.id)}/${command}`);
      if (command === "retry") goToJob(result);
      else drawJob(result);
    }));
    if (["running", "queued"].includes(job.status)) actions.push(control("Pause job", "pause"));
    if (job.status === "paused") actions.push(control("Resume job", "resume"));
    if (["partial", "failed"].includes(job.status) && ["scan", "download"].includes(job.operation)) {
      actions.push(control("Retry unfinished sets", "retry"));
      if (job.operation === "scan") actions.push(button("Repair from scan", () => {
        libraryPicker.setCodes(Object.keys(job.sets).filter((code) => job.sets[code].status !== "completed"));
        libraryPolicy.fill(job);
        location.hash = "#library";
      }));
    }
    $("job-actions").replaceChildren(...actions);
  }
  const reports = $("job-sets");
  const oldOpen = previous?.id === job.id ? new Map(Array.from(reports.children, (item) => [item.dataset.code, item.open])) : new Map();
  const focused = reports.contains(document.activeElement) ? document.activeElement.closest("details")?.dataset.code : null;
  reports.replaceChildren(...Object.entries(job.sets).map(([code, report], index) =>
    fullDetail(report, code, oldOpen.get(code) ?? (job.current_set === code || index === 0))));
  if (focused) Array.from(reports.children).find((item) => item.dataset.code === focused)?.querySelector("summary").focus({preventScroll: true});
  drawRename(job);
}

let loadedPlan = null;
function resetPlan() {
  loadedPlan = null;
  $("rename-rows").replaceChildren();
  $("rename-table").hidden = true;
  $("rename-confirmation").hidden = true;
  $("rename-reviewed").checked = false;
}

function drawRename(job) {
  const preview = job.operation === "tidy" && !job.apply && ["completed", "partial"].includes(job.status);
  $("rename-section").hidden = !preview;
  if (!preview) { resetPlan(); return; }
  const count = Object.values(job.sets).reduce((sum, report) => sum + report.renames, 0);
  $("rename-summary").textContent = count ? `${count} proposed ${count === 1 ? "rename" : "renames"}. Files are rechecked before applying.` :
    "No unambiguous renames found. Review the per-set findings above.";
  $("review-renames").hidden = !count || loadedPlan === job.id;
}

function updateApplyButton() {
  $("apply-renames").disabled = !(loadedPlan && $("rename-reviewed").checked) || Boolean($("apply-renames").dataset.busy);
}

$("review-renames").addEventListener("click", () => act($("review-renames"), async () => {
  const id = jobDetails?.id;
  if (!id) return;
  const rows = document.createDocumentFragment();
  let offset = 0;
  let entries = 0;
  do {
    const page = await request(`${jobUrl(id)}/renames?offset=${offset}`);
    if (jobId !== id) return;
    for (const row of page.renames) {
      const tr = node("tr");
      tr.append(node("td", row.set.toUpperCase()), node("td", row.source, "mono"), node("td", row.target, "mono"));
      rows.append(tr);
    }
    entries += page.renames.length;
    if ((page.next_offset !== null && page.next_offset <= offset) || (page.next_offset === null && entries !== page.total)) {
      throw new Error("The rename plan did not load completely. Reload the preview.");
    }
    offset = page.next_offset;
  } while (offset !== null);
  if (!entries) return;
  $("rename-rows").replaceChildren(rows);
  $("rename-table").hidden = false;
  $("rename-confirmation").hidden = false;
  $("review-renames").hidden = true;
  loadedPlan = id;
  $("rename-table").focus();
  updateApplyButton();
}));
$("rename-reviewed").addEventListener("change", updateApplyButton);
$("apply-renames").addEventListener("click", () => act($("apply-renames"), async () => {
  if (loadedPlan === jobId && $("rename-reviewed").checked) goToJob(await post(`${jobUrl(jobId)}/apply`));
}));

function drawDiagnostics(data) {
  $("runtime-status").replaceChildren(stat("Queue worker", badge(data.worker)), stat("Scheduler", badge(data.scheduler)),
    stat("Service UID:GID", data.uid === null ? "Not available" : `${data.uid}:${data.gid}`), stat("WebP", data.webp ? "Enabled" : "Disabled"));
  $("upstream-status").replaceChildren(...["scryfall", "cdn"].map((key) => {
    const result = data.probe?.[key];
    const state = result?.ok === true ? "ok" : result?.ok === false ? "unavailable" : "pending";
    const content = node("div");
    content.append(badge(state), node("div", result?.detail || "Use the check action above.", "caption"));
    return stat(key === "cdn" ? "Image CDN" : "Scryfall API", content);
  }));
  $("probe-date").textContent = data.probe ? `Sampled ${utc(data.probe.checked_at)}` : "";
  $("path-status").replaceChildren(...data.paths.map((path) => {
    const row = node("li");
    const info = node("div");
    info.append(node("strong", path.label), node("small", path.path, "mono"), node("small", path.detail));
    row.append(info, badge(path.status));
    return row;
  }));
}

$("probe").addEventListener("click", () => act($("probe"), async () => {
  const data = await request("/diagnostics?probe=1", {timeout: 55_000});
  drawDiagnostics(data);
}));

let loadingView = "";
async function refresh() {
  if (!connected) return;
  const target = view;
  const id = jobId;
  const signature = `${target}/${id || ""}`;
  if (loadingView === signature) return;
  loadingView = signature;
  try {
    if (target === "sync") {
      const data = await request("/sync-list");
      if (target === view && connected) {
        // Poll status without discarding the operator's unsaved policy.
        const draft = syncConfig();
        const changed = Object.keys(draft).some((key) => JSON.stringify(data[key]) !== JSON.stringify(draft[key]));
        if (changed && syncBaseline === JSON.stringify(draft) && !$("save-sync").dataset.busy) fillSync(data);
        else drawSync(data);
      }
    } else if (target === "jobs") {
      const {jobs} = await request("/jobs?summary=1");
      if (target !== view || id !== jobId || !connected) return;
      drawHistory(jobs);
      if (id) {
        const job = await request(jobUrl(id));
        if (target === view && id === jobId && connected) drawJob(job);
      }
    } else if (target === "health") {
      const data = await request("/diagnostics");
      if (target === view && connected) drawDiagnostics(data);
    }
  } catch (error) {
    if (target === view && id === jobId) showError(error);
  } finally {
    if (loadingView === signature) loadingView = "";
  }
}

function navigate() {
  if (!connected) return;
  const [selected, rawId] = location.hash.slice(1).split("/");
  view = ["sync", "library", "jobs", "health"].includes(selected) ? selected : "sync";
  const id = view === "jobs" && /^[a-z0-9]{32}$/.test(rawId || "") ? rawId : null;
  if (id !== jobId) {
    jobId = id;
    jobDetails = null;
    detailSignature = actionSignature = "";
    $("job-meta").replaceChildren(node("h3", id ? "Loading job…" : "Select a job"));
    $("job-meta").firstElementChild.id = "job-title";
    $("job-actions").replaceChildren();
    $("job-sets").replaceChildren();
    $("rename-section").hidden = true;
    resetPlan();
  }
  for (const item of $("navigation").querySelectorAll("[data-view]")) {
    const active = item.dataset.view === view;
    if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current");
    $(`view-${item.dataset.view}`).hidden = !active;
  }
  refresh();
}

async function connect() {
  try {
    const saved = await request("/sync-list");
    connected = true;
    $("access").hidden = true;
    $("workspace").hidden = false;
    $("navigation").hidden = false;
    $("sign-out").hidden = !token;
    $("reconnect").hidden = true;
    $("connection").textContent = "Connected";
    $("connection").dataset.state = "ok";
    fillSync(saved);
    navigate();
    loadCatalogue();
  } catch (error) {
    if (error.status === 401) accessRequired(token ? "The API token was not accepted." : "");
    else {
      $("connection").textContent = "Disconnected";
      $("connection").dataset.state = "unavailable";
      $("reconnect").hidden = false;
      showError(error);
    }
  }
}

$("access-form").addEventListener("submit", (event) => {
  event.preventDefault();
  token = $("api-token").value.trim();
  $("api-token").value = "";
  act(event.submitter || $("access-form").querySelector("button"), connect);
});
$("sign-out").addEventListener("click", () => location.reload());
$("reconnect").addEventListener("click", () => act($("reconnect"), connect));
$("refresh-jobs").addEventListener("click", () => act($("refresh-jobs"), refresh));
window.addEventListener("hashchange", navigate);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => {
  if (!document.hidden && ["sync", "jobs"].includes(view)) refresh();
}, 3500);
connect();
