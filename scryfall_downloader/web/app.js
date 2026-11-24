import { hasToken, post, request, setToken } from "./api.js";
import { SetPicker, policyControls } from "./sets.js";
import { $, badge, button, feedback, link, node, stat, utc } from "./ui.js";

const terminal = new Set(["completed", "partial", "failed"]);
const operations = {download: "Download", scan: "Library scan", tidy: "Tidy preview"};
const sources = {api: "On demand", sync_manual: "Saved list", sync_schedule: "Scheduled sync", retry: "Retry",
  tidy_apply: "Approved tidy", archive: "On demand", archive_bootstrap: "First start"};
const nameOf = (job) => job.archive ? "Full catalogue sync" : job.apply ? "Apply renames" : operations[job.operation] || job.operation;
const jobUrl = (id) => `/jobs/${encodeURIComponent(id)}`;
const jobHash = (id) => `#jobs/${encodeURIComponent(id)}`;

let connected = false;
let view = "sync";
let jobId = null;
let jobDetails = null;
let syncSaved = null;
let syncBaseline = "";

function accessRequired(message = "") {
  setToken("");
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

const syncPolicy = policyControls("sync");
const libraryPolicy = policyControls("library");
const syncPicker = new SetPicker("sync", 100, updateSyncButtons, loadCatalogue);
const libraryPicker = new SetPicker("library", 10, () => {}, loadCatalogue);

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
$("run-archive").addEventListener("click", () => act($("run-archive"), async () => goToJob(await post("/archive/run"))));

function goToJob(job) {
  feedback(`${nameOf(job)} accepted. Follow the receipt for its result.`);
  if (location.hash === jobHash(job.id)) drawJob(job);
  else location.hash = jobHash(job.id);
}

let historySignature = "";
function drawHistory(jobs) {
  const signature = JSON.stringify([jobId, jobs.map((job) => [job.id, job.status, job.sets_finished, job.set_codes.length])]);
  if (signature === historySignature) return;
  historySignature = signature;
  const focused = $("job-list").contains(document.activeElement) ? document.activeElement.hash : null;
  const items = jobs.map((job) => {
    const item = node("li");
    const receipt = link("", jobHash(job.id));
    receipt.className = "job-link";
    if (job.id === jobId) receipt.setAttribute("aria-current", "true");
    const codes = job.set_codes;
    const title = codes.length ? codes.slice(0, 4).map((code) => code.toUpperCase()).join(", ") : "Catalogue pending";
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
  if (job.error) $("job-meta").append(node("p", job.error, "callout"));
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
  const entries = Object.entries(job.sets);
  const focused = reports.contains(document.activeElement) ? document.activeElement.closest("details")?.dataset.code : null;
  if (previous?.id !== job.id || reports.children.length !== entries.length) {
    reports.replaceChildren(...entries.map(([code, report], index) =>
      fullDetail(report, code, job.current_set === code || index === 0)));
  } else {
    entries.forEach(([code, report], index) => {
      if (JSON.stringify(previous.sets[code]) !== JSON.stringify(report)) {
        const old = reports.children[index];
        old.replaceWith(fullDetail(report, code, old.open || job.current_set === code));
      }
    });
  }
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
    $("sign-out").hidden = !hasToken();
    $("reconnect").hidden = true;
    $("connection").textContent = "Connected";
    $("connection").dataset.state = "ok";
    fillSync(saved);
    navigate();
    loadCatalogue();
  } catch (error) {
    if (error.status === 401) accessRequired(hasToken() ? "The API token was not accepted." : "");
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
  setToken($("api-token").value.trim());
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
