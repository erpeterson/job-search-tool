"use strict";

const rubric = [
  "interesting_technical_problems",
  "organizational_influence",
  "cross_functional_work",
  "opportunity_to_mentor",
  "work_life_balance",
  "low_operational_burden",
  "compensation",
  "mission",
];
const statusOptions = ["researching","interested","applied","interviewing","offer","rejected","declined","paused"];
const defaultVisibleStatuses = statusOptions.filter(s => !["rejected", "declined"].includes(s));
const companyStatuses = ["watching","target","active_conversation","paused","not_interested"];
let state = { jobs: [], settings: {}, pipelines: [], rubric_fields: rubric, application_packets: [] };
let selectedId = null;
let selectedJob = null;
let selectedCompanyId = null;
let jobTableFilters = loadJobTableFilters();
let jobFilterRollupOpen = localStorage.getItem("jobFilterRollupOpen") === "1";
let currentPage = "jobs";
let searchRunning = false;
let splitInitialized = false;
let pendingCallCount = 0;
let pendingCallLabels = [];
let bulkSelectedJobIds = new Set();
let activeBulkTaskId = localStorage.getItem("activeBulkTaskId") || "";
let bulkTaskPollTimer = null;

const pretty = s => s.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
const scoreClass = n => n == null ? "" : n >= 70 ? "score-good" : n >= 40 ? "score-warn" : "score-bad";
const levelStatus = item => item.level_assessment || "Unknown - level not assessed";
const levelPreview = item => {
  const value = levelStatus(item);
  return value.length > 100 ? `${value.slice(0, 97).trim()}...` : value;
};
const scoreText = value => value == null ? "n/a" : value;
const scoreLabel = n => n >= 70 ? "strong" : n >= 40 ? "borderline" : "weak";
// Colour plus a text label, so fit is never conveyed by colour alone.
const scoreBadge = n => n == null
  ? "n/a"
  : `<b class="${scoreClass(n)}">${escapeHtml(n)}</b> <span class="score-label">${scoreLabel(n)}</span>`;
const fieldValue = (object, field, fallback) => object && object[field] != null ? object[field] : fallback;
const elementValue = (id, fallback = "") => {
  const element = document.getElementById(id);
  return element ? element.value : fallback;
};
const formatTimestamp = ts => ts ? new Date(ts * 1000).toLocaleString() : "not scheduled";
const normalizeCompanyName = value => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

function findCompanyInterestByName(companyName) {
  const normalized = normalizeCompanyName(companyName);
  return (state.company_interests || []).find(company => normalizeCompanyName(company.company) === normalized);
}

function loadJobTableFilters() {
  try {
    const parsed = JSON.parse(localStorage.getItem("jobTableFilters") || "{}");
    return {
      pipeline: parsed.pipeline || "",
      source: parsed.source || "",
      visibility: parsed.visibility || "active",
      text: parsed.text || "",
      statuses: Array.isArray(parsed.statuses) && parsed.statuses.length ? parsed.statuses : defaultVisibleStatuses,
    };
  } catch (err) {
    return { pipeline: "", source: "", visibility: "active", text: "", statuses: defaultVisibleStatuses };
  }
}

function saveJobTableFilters() {
  const checkedStatuses = [...document.querySelectorAll("#status_filter_list input:checked")].map(input => input.value);
  jobTableFilters = {
    pipeline: elementValue("pipeline_view_filter"),
    source: elementValue("source_view_filter"),
    visibility: elementValue("visibility_filter", "active"),
    text: elementValue("job_text_filter"),
    statuses: checkedStatuses,
  };
  localStorage.setItem("jobTableFilters", JSON.stringify(jobTableFilters));
}

function resetJobTableFilters() {
  jobTableFilters = { pipeline: "", source: "", visibility: "active", text: "", statuses: defaultVisibleStatuses };
  localStorage.setItem("jobTableFilters", JSON.stringify(jobTableFilters));
  renderJobTableFilterControls();
  renderJobs();
}

function saveJobFilterRollupState() {
  const rollup = document.getElementById("job_filter_rollup");
  if (!rollup) return;
  jobFilterRollupOpen = rollup.open;
  localStorage.setItem("jobFilterRollupOpen", rollup.open ? "1" : "0");
}

function activityLabel(path) {
  if (path.includes("/bulk/score-gpt")) return "Starting bulk Codex scoring";
  if (path.includes("/bulk/application-packets")) return "Starting bulk packet generation";
  if (path.includes("/codex-tasks")) return "Checking Codex task";
  if (path.includes("/score-gpt")) return "Codex scoring";
  if (path.includes("/application-packet/generate")) return "Generating application packet";
  if (path.includes("/application-packet/attach")) return "Attaching application packet";
  if (path.includes("/application-packet/content")) return "Loading application content";
  if (path === "/api/search/run") return "Job search running";
  if (path === "/api/config") return "Saving configuration";
  if (path === "/api/state?include_filtered=1") return "Refreshing data";
  return "Working";
}

function renderGlobalActivity() {
  const activity = document.getElementById("global_activity");
  if (!activity) return;
  if (pendingCallCount <= 0) {
    activity.className = "activity-pill";
    activity.innerHTML = "";
    return;
  }
  const label = pendingCallLabels[pendingCallLabels.length - 1] || "Working";
  activity.className = "activity-pill visible";
  activity.innerHTML = `<span class="spinner"></span>${escapeHtml(label)}${pendingCallCount > 1 ? ` (${pendingCallCount})` : ""}`;
}

async function api(path, options = {}) {
  const { activityLabel: explicitActivityLabel, ...fetchOptions } = options;
  const label = explicitActivityLabel || activityLabel(path);
  pendingCallCount += 1;
  pendingCallLabels.push(label);
  renderGlobalActivity();
  try {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...fetchOptions,
    });
    const isJson = (response.headers.get("content-type") || "").includes("application/json");
    const data = isJson ? await response.json() : {};
    if (!response.ok) {
      const error = new Error(data.error || `Request failed (HTTP ${response.status})`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    if (!isJson) throw new Error(`Unexpected response type from ${path}`);
    return data;
  } finally {
    pendingCallCount = Math.max(0, pendingCallCount - 1);
    const labelIndex = pendingCallLabels.lastIndexOf(label);
    if (labelIndex >= 0) pendingCallLabels.splice(labelIndex, 1);
    renderGlobalActivity();
  }
}

async function load() {
  state = await api("/api/state?include_filtered=1");
  document.getElementById("profile_banner").hidden = !state.profile_is_example;
  document.getElementById("gpt_threshold").value = state.settings.gpt_threshold;
  document.getElementById("user_threshold").value = state.settings.user_threshold;
  document.getElementById("codex_model").value = state.settings.codex_model || "";
  const pipeline = document.getElementById("pipeline");
  pipeline.innerHTML = '<option value=""></option>' + state.pipelines.map(p => `<option>${escapeHtml(p)}</option>`).join("");
  document.getElementById("search_pipeline").innerHTML = state.pipelines.map(p => `<option>${escapeHtml(p)}</option>`).join("");
  const pipelineView = document.getElementById("pipeline_view_filter");
  pipelineView.innerHTML = '<option value="">All pipelines</option>' + state.pipelines.map(p => `<option>${escapeHtml(p)}</option>`).join("");
  const filterRollup = document.getElementById("job_filter_rollup");
  if (filterRollup) filterRollup.open = jobFilterRollupOpen;
  renderJobTableFilterControls();
  renderSearchState();
  renderConfigStatus();
  renderJobs();
  renderBulkControls();
  renderCompanyTable();
  renderQueryTable();
  initializeJobSplit();
  if (selectedId) await selectJob(selectedId, false);
  if (selectedCompanyId) await selectCompany(selectedCompanyId, false);
}

function renderSearchState() {
  const enabled = (state.search_queries || []).filter(q => q.enabled);
  const latest = (state.search_runs || [])[0];
  const schedule = state.search_schedule || {};
  const nextSearchText = schedule.autorun_enabled
    ? `Next scheduled search: ${formatTimestamp(schedule.next_run_at)}`
    : "Scheduled search disabled";
  const status = document.getElementById("search_status");
  const runButton = document.getElementById("run_search_button");
  if (searchRunning) {
    status.className = "status-pill running";
    status.innerHTML = '<span class="spinner"></span> Search running';
    runButton.disabled = true;
  } else if (latest) {
    status.className = `status-pill ${latest.status}`;
    status.textContent = `${latest.status}: found ${latest.found_count}, tracked ${latest.tracked_count}, rejected ${latest.rejected_count}`;
    runButton.disabled = false;
  } else {
    status.className = "status-pill";
    status.textContent = "Idle";
    runButton.disabled = false;
  }
  const enabledHtml = enabled.map(q => `
    <div class="enabled-query">
      <b>${escapeHtml(q.pipeline || "Custom")}</b>
      <br>${escapeHtml(q.board)} · ${escapeHtml(q.location || "")}
      <br>${escapeHtml(q.keywords)}
    </div>
  `).join("") || '<div class="enabled-query">No enabled queries.</div>';
  document.getElementById("enabled_queries").innerHTML = `${enabledHtml}<div class="enabled-query"><b>${escapeHtml(nextSearchText)}</b></div>`;
  document.getElementById("search_runs").innerHTML = (state.search_runs || []).slice(0, 5).map(r => `
    <div class="note">
      <b>${escapeHtml(r.trigger)}</b> · ${escapeHtml(r.status)}
      <br>found ${r.found_count}, tracked ${r.tracked_count}, rejected ${r.rejected_count}
      <br>${new Date(r.started_at * 1000).toLocaleString()}
      ${r.message ? `<br>${escapeHtml(r.message)}` : ""}
    </div>
  `).join("") || "No search runs yet.";
}

function renderConfigStatus() {
  const config = state.config || {};
  const keyLine = key => {
    const item = config[key] || {};
    return escapeHtml(`${key}: ${item.configured ? item.masked : "not set"}`);
  };
  document.getElementById("config_status").innerHTML = `
    ${["CODEX_CLI_PATH","CODEX_MODEL"].map(keyLine).join("<br>")}
    <br>API log: ${escapeHtml(state.api_log_path || "")}
    <br>Decision log: ${escapeHtml(state.event_log_path || "")}
    <br>Captures: ${escapeHtml(state.capture_dir || "")}
    <br>Codex scoring: ${state.gpt_scoring_enabled ? "enabled" : "disabled"}
    <br>Replay cache: ${state.capture_cache_enabled ? "enabled" : "disabled"}
  `;
  if (!document.getElementById("config_CODEX_CLI_PATH").value) {
    document.getElementById("config_CODEX_CLI_PATH").value = (state.config.CODEX_CLI_PATH || {}).configured ? "" : "codex";
  }
  if (!document.getElementById("config_CODEX_MODEL").value) {
    document.getElementById("config_CODEX_MODEL").value = state.settings.codex_model || "";
  }
  document.getElementById("config_JOB_SEARCH_ENABLE_GPT_SCORING").value = state.gpt_scoring_enabled ? "1" : "0";
  document.getElementById("config_JOB_SEARCH_USE_CAPTURE_CACHE").value = state.capture_cache_enabled ? "1" : "0";
}

function renderJobTableFilterControls() {
  const pipelineView = document.getElementById("pipeline_view_filter");
  const sourceView = document.getElementById("source_view_filter");
  const visibility = document.getElementById("visibility_filter");
  const text = document.getElementById("job_text_filter");
  const statusList = document.getElementById("status_filter_list");
  if (!pipelineView || !sourceView || !visibility || !text || !statusList) return;

  pipelineView.value = state.pipelines.includes(jobTableFilters.pipeline) ? jobTableFilters.pipeline : "";
  const sources = [...new Set((state.jobs || []).map(job => job.source_board || "manual"))].sort();
  sourceView.innerHTML = '<option value="">All sources</option>' + sources.map(source => `<option>${escapeHtml(source)}</option>`).join("");
  sourceView.value = sources.includes(jobTableFilters.source) ? jobTableFilters.source : "";
  visibility.value = ["active", "all", "filtered", "downlevel"].includes(jobTableFilters.visibility) ? jobTableFilters.visibility : "active";
  text.value = jobTableFilters.text || "";
  const statuses = [...new Set([...statusOptions, ...(state.jobs || []).map(job => job.status || "").filter(Boolean)])];
  statusList.innerHTML = statuses.map(status => `
    <label><input type="checkbox" value="${escapeAttr(status)}" ${jobTableFilters.statuses.includes(status) ? "checked" : ""} data-filter-control> ${escapeHtml(status)}</label>
  `).join("");
}

function jobMatchesTableFilters(job) {
  if (jobTableFilters.pipeline && (job.pipeline || "") !== jobTableFilters.pipeline) return false;
  if (jobTableFilters.source && (job.source_board || "manual") !== jobTableFilters.source) return false;
  if (!jobTableFilters.statuses.includes(job.status || "")) return false;
  if (jobTableFilters.visibility === "active" && (job.filtered || job.downlevel)) return false;
  if (jobTableFilters.visibility === "filtered" && !job.filtered) return false;
  if (jobTableFilters.visibility === "downlevel" && !job.downlevel) return false;
  const text = (jobTableFilters.text || "").trim().toLowerCase();
  if (text) {
    const haystack = [job.company, job.title, job.location, job.pipeline, job.status, job.source_board].join(" ").toLowerCase();
    if (!haystack.includes(text)) return false;
  }
  return true;
}

function visibleJobIds() {
  return state.jobs.filter(jobMatchesTableFilters).map(job => job.id);
}

function selectedJobIds() {
  return [...bulkSelectedJobIds].filter(id => state.jobs.some(job => job.id === id));
}

function toggleBulkJobSelection(id, checked) {
  if (checked) bulkSelectedJobIds.add(id);
  else bulkSelectedJobIds.delete(id);
  renderBulkControls();
}

function selectVisibleJobs() {
  visibleJobIds().forEach(id => bulkSelectedJobIds.add(id));
  renderJobs();
  renderBulkControls();
}

function clearBulkSelection() {
  bulkSelectedJobIds.clear();
  renderJobs();
  renderBulkControls();
}

function renderBulkControls(task = null) {
  const selected = selectedJobIds();
  bulkSelectedJobIds = new Set(selected);
  const summary = document.getElementById("bulk_selection_summary");
  if (summary) summary.textContent = `${selected.length} selected`;
  const scoreButton = document.getElementById("bulk_score_button");
  if (scoreButton) scoreButton.disabled = !state.gpt_scoring_enabled || selected.length === 0;
  const packetButton = document.getElementById("bulk_packet_button");
  if (packetButton) packetButton.disabled = selected.length === 0;
  const status = document.getElementById("bulk_task_status");
  if (!status) return;
  const currentTask = task || (state.codex_tasks || []).find(t => t.id === activeBulkTaskId);
  if (!currentTask) {
    status.textContent = "No bulk Codex task running.";
    return;
  }
  const parts = [
    `${currentTask.operation}: ${currentTask.status}`,
    `${currentTask.completed || 0}/${currentTask.total || 0} complete`,
    `${currentTask.skipped || 0} skipped`,
    `${currentTask.failed || 0} failed`,
  ];
  if (currentTask.message) parts.push(currentTask.message);
  status.innerHTML = `${["running", "queued"].includes(currentTask.status) ? '<span class="spinner"></span> ' : ""}${escapeHtml(parts.join(" · "))}`;
}

// Long-running work (search, scoring, packets) runs as a server task; poll until it finishes.
async function waitForTask(task, label) {
  let current = task;
  while (["queued", "running"].includes(current.status)) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    ({ task: current } = await api(`/api/codex-tasks/${current.id}`, { activityLabel: label }));
  }
  if (current.status === "error") throw new Error(current.message || "Task failed");
  return current;
}

function startBulkTaskPolling(taskId) {
  activeBulkTaskId = taskId;
  localStorage.setItem("activeBulkTaskId", taskId);
  if (bulkTaskPollTimer) clearInterval(bulkTaskPollTimer);
  bulkTaskPollTimer = setInterval(() => pollBulkTask(taskId), 3000);
  pollBulkTask(taskId);
}

async function pollBulkTask(taskId) {
  try {
    const { task } = await api(`/api/codex-tasks/${taskId}`, { activityLabel: "Checking Codex task" });
    renderBulkControls(task);
    if (!["queued", "running"].includes(task.status)) {
      clearInterval(bulkTaskPollTimer);
      bulkTaskPollTimer = null;
      await load();
    }
  } catch (err) {
    if (bulkTaskPollTimer) clearInterval(bulkTaskPollTimer);
    bulkTaskPollTimer = null;
    renderBulkControls({ operation: "bulk", status: "error", completed: 0, total: 0, skipped: 0, failed: 1, message: err.message });
  }
}

function renderJobs() {
  const jobs = document.getElementById("jobs");
  const visibleJobs = state.jobs.filter(jobMatchesTableFilters);
  const hiddenCount = state.jobs.length - visibleJobs.length;
  document.getElementById("job_filter_summary").textContent = `${visibleJobs.length} visible, ${hiddenCount} hidden by table filters. Select a row to edit CRM details below.`;
  if (!visibleJobs.length) {
    jobs.innerHTML = '<div class="small">No jobs match the current filter.</div>';
    return;
  }
  jobs.innerHTML = `
    <table>
      <thead>
        <tr>
          <th class="select-cell">Pick</th>
          <th>Company</th>
          <th>Role</th>
          <th>Pipeline</th>
          <th>Status</th>
          <th>Codex</th>
          <th>Mine</th>
          <th>Level</th>
          <th>Source</th>
        </tr>
      </thead>
      <tbody>
        ${visibleJobs.map(job => `
          <tr class="${job.filtered ? "filtered" : ""} ${job.id === selectedId ? "active" : ""}" data-action="selectJob" data-id="${job.id}" ${job.id === selectedId ? 'aria-current="true"' : ""}>
            <td class="select-cell"><input type="checkbox" aria-label="Select ${escapeAttr(job.company)} ${escapeAttr(job.title)}" ${bulkSelectedJobIds.has(job.id) ? "checked" : ""} data-bulk-select="${job.id}"></td>
            <td><button type="button" class="link-button" data-action="selectJob" data-id="${job.id}"><b>${escapeHtml(job.company)}</b></button><div class="small">${escapeHtml(job.location || "")}</div></td>
            <td><span class="job-title">${escapeHtml(job.title)}</span>${job.url ? `<div class="small"><a href="${safeHref(job.url)}" target="_blank" rel="noopener noreferrer">posting</a></div>` : ""}</td>
            <td>${escapeHtml(job.pipeline || "Unassigned")}</td>
            <td>${escapeHtml(job.status || "")}${job.application_packet_path ? '<div class="small">packet attached</div>' : ""}${job.filtered ? '<div class="small">filtered/downlevel hidden by default</div>' : ""}</td>
            <td>${scoreBadge(job.gpt_score)}</td>
            <td>${scoreBadge(job.user_score)}</td>
            <td class="level-cell" title="${escapeAttr(levelStatus(job))}"><span class="level-preview">${escapeHtml(levelPreview(job))}</span>${job.downlevel ? '<div class="small">downlevel</div>' : ""}</td>
            <td>${escapeHtml(job.source_board || "manual")}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function renderQueryTable() {
  const el = document.getElementById("query_table");
  const queries = state.search_queries || [];
  if (!queries.length) {
    el.innerHTML = '<div class="small">No saved queries.</div>';
    return;
  }
  el.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Enabled</th>
          <th>Board</th>
          <th>Pipeline</th>
          <th>Keywords</th>
          <th>Location</th>
          <th>Refinement</th>
        </tr>
      </thead>
      <tbody>
        ${queries.map(q => `
          <tr>
            <td><button class="secondary" data-action="toggleQuery" data-id="${q.id}" data-enabled="${q.enabled ? "false" : "true"}">${q.enabled ? "Disable" : "Enable"}</button></td>
            <td>${escapeHtml(q.board)}</td>
            <td>${escapeHtml(q.pipeline || "Custom")}<div class="small">${q.seeded ? "seeded" : "custom"}</div></td>
            <td>${escapeHtml(q.keywords)}<div class="small">${escapeHtml(q.criteria || "")}</div></td>
            <td>${escapeHtml(q.location || "")}</td>
            <td>${q.last_run_at ? new Date(q.last_run_at * 1000).toLocaleString() : "never"}<div class="small">${escapeHtml(q.refinement_notes || "")}</div></td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function renderCompanyTable() {
  const el = document.getElementById("company_table");
  const companies = state.company_interests || [];
  if (!el) return;
  if (!companies.length) {
    el.innerHTML = '<div class="small">No company interests tracked yet.</div>';
    return;
  }
  el.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Company</th>
          <th>Status</th>
          <th>Interest</th>
          <th>Tracked Jobs</th>
          <th>Next Step</th>
        </tr>
      </thead>
      <tbody>
        ${companies.map(company => `
          <tr class="${company.id === selectedCompanyId ? "active" : ""}" data-action="selectCompany" data-id="${company.id}" ${company.id === selectedCompanyId ? 'aria-current="true"' : ""}>
            <td><button type="button" class="link-button" data-action="selectCompany" data-id="${company.id}"><b>${escapeHtml(company.company)}</b></button><div class="small">${escapeHtml(company.contacts || "")}</div></td>
            <td>${escapeHtml(company.status || "")}</td>
            <td>${scoreBadge(company.interest_score)}</td>
            <td>${company.tracked_job_count || 0}</td>
            <td>${escapeHtml(company.next_step || "")}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

async function selectCompany(id, rerender = true) {
  selectedCompanyId = id;
  const { company } = await api(`/api/companies/${id}`);
  renderCompanyDetail(company);
  if (rerender) renderCompanyTable();
}

function renderCompanyDetail(company) {
  const detail = document.getElementById("company_detail");
  detail.innerHTML = `
    <div class="panel">
      <div class="toolbar">
        <div>
          <h2>${escapeHtml(company.company)}</h2>
          <div class="meta">${company.jobs.length} tracked role${company.jobs.length === 1 ? "" : "s"} for this company.</div>
        </div>
        <button data-action="saveCompanyInterest" data-id="${company.id}">Save company</button>
      </div>
      <div class="grid2">
        <div><label for="edit_company_name">Company</label><input id="edit_company_name" value="${escapeAttr(company.company)}"></div>
        <div><label for="edit_company_status">Status</label><select id="edit_company_status">${companyStatuses.map(s => `<option ${company.status === s ? "selected" : ""}>${escapeHtml(s)}</option>`).join("")}</select></div>
      </div>
      <label for="edit_company_interest_score">Interest score</label><input id="edit_company_interest_score" type="number" min="0" max="100" value="${company.interest_score == null ? "" : company.interest_score}">
      <label for="edit_company_rationale">Rationale</label><textarea id="edit_company_rationale">${escapeHtml(company.rationale || "")}</textarea>
      <label for="edit_company_contacts">Contacts</label><textarea id="edit_company_contacts">${escapeHtml(company.contacts || "")}</textarea>
      <label for="edit_company_next_step">Next step</label><input id="edit_company_next_step" value="${escapeAttr(company.next_step || "")}">
      <label for="edit_company_notes">Notes</label><textarea id="edit_company_notes">${escapeHtml(company.notes || "")}</textarea>
    </div>
    <div class="panel">
      <h2>Tracked Roles At ${escapeHtml(company.company)}</h2>
      ${company.jobs.length ? `
        <table>
          <thead><tr><th>Role</th><th>Status</th><th>Pipeline</th><th>Scores</th></tr></thead>
          <tbody>${company.jobs.map(job => `
            <tr data-action="openJob" data-id="${job.id}">
              <td><button type="button" class="link-button" data-action="openJob" data-id="${job.id}">${escapeHtml(job.title)}</button>${job.url ? `<div class="small"><a href="${safeHref(job.url)}" target="_blank" rel="noopener noreferrer">posting</a></div>` : ""}</td>
              <td>${escapeHtml(job.status || "")}${job.downlevel ? '<div class="small">downlevel</div>' : ""}${job.filtered ? '<div class="small">filtered</div>' : ""}</td>
              <td>${escapeHtml(job.pipeline || "Unassigned")}</td>
              <td>Codex ${escapeHtml(scoreText(job.gpt_score))} · Mine ${escapeHtml(scoreText(job.user_score))}</td>
            </tr>
          `).join("")}</tbody>
        </table>
      ` : '<div class="small">No tracked jobs for this company yet.</div>'}
    </div>
  `;
}

async function selectJob(id, rerender = true) {
  selectedId = id;
  const { job } = await api(`/api/jobs/${id}`);
  selectedJob = job;
  renderDetail(job);
  if (rerender) renderJobs();
}

function renderDetail(job) {
  const detail = document.getElementById("detail");
  const companyInterest = findCompanyInterestByName(job.company);
  const packet = applicationPacketForJob(job);
  const unassociatedPackets = (state.application_packets || []).filter(packet => packet.unassociated);
  const markdownFiles = packet ? packet.markdown_files || [] : [];
  detail.innerHTML = `
    <div class="panel">
      <div class="toolbar">
        <div class="grow">
          <h2>${escapeHtml(job.company)} - ${escapeHtml(job.title)}</h2>
          <div class="meta">${escapeHtml(job.location || "")} ${job.url ? `· <a href="${safeHref(job.url)}" target="_blank" rel="noopener noreferrer">posting</a>` : ""}</div>
        </div>
        <div>
          <label for="status">Status</label>
          <select id="status">${statusOptions.map(s => `<option ${job.status === s ? "selected" : ""}>${escapeHtml(s)}</option>`).join("")}</select>
        </div>
        <div class="action-cluster">
          <div>
            <label for="job_action">Action</label>
            <select id="job_action">
              <option value="save_status">Save status</option>
              <option value="track_company">${companyInterest ? "View company interest" : "Track company interest"}</option>
              <option value="generate_packet" ${job.application_packet_path ? "disabled" : ""}>Generate packet with Codex</option>
              <option value="rescrape" ${job.url ? "" : "disabled"}>Re-scrape posting</option>
              <option value="delete">Delete job</option>
            </select>
          </div>
          <button data-action="runJobAction" data-id="${job.id}">Go</button>
        </div>
      </div>
      <div class="chips">
        <span class="chip">Pipeline: ${escapeHtml(job.pipeline || "Unassigned")}</span>
        <span class="chip">Codex: ${scoreBadge(job.gpt_score)}</span>
        <span class="chip">Mine: ${scoreBadge(job.user_score)}</span>
        <span class="chip">Level: ${escapeHtml(levelStatus(job))}</span>
        ${job.application_packet_path ? `<span class="chip">Packet: ${escapeHtml(job.application_packet_path)}</span>` : '<span class="chip">No packet</span>'}
        ${job.downlevel ? '<span class="chip">Downlevel</span>' : ""}
        ${job.filtered ? '<span class="chip">Filtered</span>' : ""}
      </div>
      <p>${escapeHtml(job.gpt_rationale || "No Codex rationale yet.")}</p>
      <button class="warn" data-action="scoreGpt" data-id="${job.id}" ${state.gpt_scoring_enabled ? "" : "disabled"}>${state.gpt_scoring_enabled ? "Populate Codex scorecard" : "Codex scoring disabled"}</button>
    </div>

    <div class="panel">
      <h2>Application Packet</h2>
      ${packet ? `
        <div class="meta">${escapeHtml(packet.path)}</div>
        <div class="packet-row">
          <div>
            <label for="application_markdown_file">Markdown file</label>
            <select id="application_markdown_file">${markdownFiles.map(file => `<option>${escapeHtml(file)}</option>`).join("")}</select>
          </div>
          <button class="secondary" data-action="openApplicationMarkdown" data-id="${job.id}">Open rendered view</button>
        </div>
        <p class="small">Opens the selected Markdown file in a new rendered browser window.</p>
      ` : `
        <p class="small">No application packet is associated with this job.</p>
        <div class="row">
          <button class="secondary" data-action="generateApplicationPacket" data-id="${job.id}">Generate packet with Codex</button>
        </div>
        <div class="packet-row">
          <div>
            <label for="application_packet_attach">Attach existing unassociated packet</label>
            <select id="application_packet_attach">
              <option value="">Select packet...</option>
              ${unassociatedPackets.map(packet => `<option value="${escapeAttr(packet.path)}">${escapeHtml(packet.name)}</option>`).join("")}
            </select>
          </div>
          <button class="secondary" data-action="attachApplicationPacket" data-id="${job.id}" ${unassociatedPackets.length ? "" : "disabled"}>Attach</button>
        </div>
      `}
    </div>

    <div class="panel">
      <h2>My Scorecard</h2>
      <div class="score-grid">${rubric.map(field => `
        <label>${escapeHtml(pretty(field))}<input id="user_${escapeAttr(field)}" type="number" list="score_options" min="0" max="10" step="1" value="${escapeAttr(fieldValue(job.user_scorecard, field, ""))}"></label>
      `).join("")}</div>
      <label for="user_rationale">Rationale</label><textarea id="user_rationale">${escapeHtml(job.user_rationale || "")}</textarea>
      <button data-action="saveUserScore" data-id="${job.id}">Save my score</button>
    </div>

    <div class="panel">
      <h2>Codex Scorecard</h2>
      <div class="score-grid">${rubric.map(field => `
        <div><span class="small">${escapeHtml(pretty(field))}</span><br><b>${escapeHtml(fieldValue(job.gpt_scorecard, field, "n/a"))}</b></div>
      `).join("")}</div>
    </div>

    <div class="panel">
      <h2>Interactions</h2>
      <div class="grid2">
        <div><label for="occurred_on">Date</label><input id="occurred_on" type="date"></div>
        <div><label for="channel">Channel</label><input id="channel" placeholder="intro, email, call, interview"></div>
      </div>
      <div class="grid2">
        <div><label for="person_name">Person</label><input id="person_name"></div>
        <div><label for="person_role">Role</label><input id="person_role"></div>
      </div>
      <label for="summary">What we talked about</label><textarea id="summary"></textarea>
      <label for="notes_to_self">Notes to self</label><textarea id="notes_to_self"></textarea>
      <label for="next_step">Next step</label><input id="next_step">
      <button data-action="addInteraction" data-id="${job.id}">Add interaction</button>
      <div>${job.interactions.map(i => `
        <div class="interaction">
          <b>${escapeHtml(i.occurred_on)}</b> · ${escapeHtml(i.person_name || "Unknown")} ${i.person_role ? `(${escapeHtml(i.person_role)})` : ""} · ${escapeHtml(i.channel || "")}
          <p>${escapeHtml(i.summary || "")}</p>
          <p class="small">${escapeHtml(i.notes_to_self || "")}</p>
          <p class="small">Next: ${escapeHtml(i.next_step || "")}</p>
        </div>
      `).join("")}</div>
    </div>

    <div class="panel">
      <h2>Notes</h2>
      <textarea id="new_note" placeholder="Learning journal, concerns, outreach ideas, reminders."></textarea>
      <button data-action="addNote" data-id="${job.id}">Add note</button>
      <div>${job.notes_list.map(n => `<div class="note">${escapeHtml(n.note)}</div>`).join("")}</div>
    </div>

    <div class="panel">
      <h2>Recent Discoveries</h2>
      <div>${(state.discoveries || []).slice(0, 12).map(d => `
        <div class="interaction">
          <b>${escapeHtml(d.company || "Unknown")}</b> - ${escapeHtml(d.title || "Unknown")}
          <div class="meta">${escapeHtml(d.board)} · ${escapeHtml(d.location || "")} · ${d.url ? `<a href="${safeHref(d.url)}" target="_blank" rel="noopener noreferrer">posting</a>` : ""}</div>
          <div class="chips">
            <span class="chip">Decision: ${escapeHtml(d.decision)}</span>
            <span class="chip">Codex: ${scoreBadge(d.gpt_score)}</span>
            <span class="chip">Level: ${escapeHtml(levelStatus(d))}</span>
            ${d.downlevel ? '<span class="chip">Downlevel</span>' : ""}
          </div>
          <p class="small">${escapeHtml(d.rejection_reason || d.gpt_rationale || "")}</p>
        </div>
      `).join("") || '<div class="small">No discoveries yet.</div>'}</div>
    </div>
  `;
}

function applicationPacketForJob(job) {
  if (!job.application_packet_path) return null;
  return (state.application_packets || []).find(packet => packet.path === job.application_packet_path) || {
    path: job.application_packet_path,
    name: job.application_packet_path.split("/").pop(),
    markdown_files: ["Job-Brief.md", "CV.md"],
  };
}

async function runJobAction(id) {
  const action = document.getElementById("job_action").value;
  if (action === "save_status") return saveStatus(id);
  if (action === "track_company") return trackCompanyFromSelectedJob();
  if (action === "generate_packet") return generateApplicationPacket(id);
  if (action === "rescrape") return rescrapeJob(id);
  if (action === "delete") return deleteJob(id);
}

async function generateApplicationPacket(id) {
  try {
    const { task } = await api(`/api/jobs/${id}/application-packet/generate`, {
      method: "POST",
      body: "{}",
      activityLabel: "Generating application packet",
    });
    const finished = await waitForTask(task, "Generating application packet");
    const packet = finished.result && finished.result.packet;
    if (packet && packet.warning) notify(packet.warning);
    selectedId = id;
    await load();
  } catch (err) {
    notify(err.message);
  }
}

async function attachApplicationPacket(id) {
  const selector = document.getElementById("application_packet_attach");
  const path = selector ? selector.value : "";
  if (!path) return;
  try {
    const { job } = await api(`/api/jobs/${id}/application-packet/attach`, {
      method: "POST",
      body: JSON.stringify({ path }),
      activityLabel: "Attaching application packet",
    });
    selectedId = job.id;
    selectedJob = job;
    await load();
  } catch (err) {
    notify(err.message);
  }
}

function openApplicationMarkdown(id) {
  const selector = document.getElementById("application_markdown_file");
  if (!selector || !selector.value) return;
  window.open(`/api/jobs/${id}/application-packet/render?file=${encodeURIComponent(selector.value)}`, "_blank", "noopener,noreferrer");
}

async function createJob() {
  const payload = {
    url: document.getElementById("url").value,
    pipeline: document.getElementById("pipeline").value,
    force_refresh: document.getElementById("manual_force_refresh").checked,
  };
  const { job, score_error: scoreError, score_task: scoreTask } = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload), activityLabel: "Adding job" });
  selectedId = job.id;
  document.getElementById("url").value = "";
  document.getElementById("manual_force_refresh").checked = false;
  await load();
  if (scoreError) notify(scoreError);
  if (scoreTask) {
    try {
      await waitForTask(scoreTask, "Scoring new job with Codex");
    } catch (err) {
      notify(`Automatic Codex scoring failed: ${err.message}`);
    }
    await load();
  }
}

async function createCompanyInterest() {
  const payload = {
    company: document.getElementById("company_interest_name").value,
    status: document.getElementById("company_interest_status").value,
    interest_score: nullableNumber(document.getElementById("company_interest_score").value),
    rationale: document.getElementById("company_interest_rationale").value,
    contacts: document.getElementById("company_interest_contacts").value,
    next_step: document.getElementById("company_interest_next_step").value,
    notes: document.getElementById("company_interest_notes").value,
  };
  const { company } = await api("/api/companies", { method: "POST", body: JSON.stringify(payload) });
  selectedCompanyId = company.id;
  ["company_interest_name","company_interest_score","company_interest_rationale","company_interest_contacts","company_interest_next_step","company_interest_notes"].forEach(id => document.getElementById(id).value = "");
  await load();
}

async function trackCompanyFromSelectedJob() {
  if (!selectedJob) return;
  await trackCompanyFromJob(selectedJob.company, selectedJob.title);
}

async function trackCompanyFromJob(companyName, title) {
  const existing = findCompanyInterestByName(companyName);
  if (existing) {
    selectedCompanyId = existing.id;
    showPage("companies");
    await selectCompany(existing.id);
    return;
  }
  const payload = {
    company: companyName,
    status: "watching",
    interest_score: null,
    rationale: `Interested via tracked role: ${title}`,
    contacts: "",
    next_step: "",
    notes: "",
  };
  const { company } = await api("/api/companies", { method: "POST", body: JSON.stringify(payload) });
  selectedCompanyId = company.id;
  await load();
  showPage("companies");
  await selectCompany(company.id);
}

async function saveCompanyInterest(id) {
  const payload = {
    company: document.getElementById("edit_company_name").value,
    status: document.getElementById("edit_company_status").value,
    interest_score: nullableNumber(document.getElementById("edit_company_interest_score").value),
    rationale: document.getElementById("edit_company_rationale").value,
    contacts: document.getElementById("edit_company_contacts").value,
    next_step: document.getElementById("edit_company_next_step").value,
    notes: document.getElementById("edit_company_notes").value,
  };
  await api(`/api/companies/${id}`, { method: "POST", body: JSON.stringify(payload) });
  await load();
}

async function scoreGpt(id) {
  try {
    const { task } = await api(`/api/jobs/${id}/score-gpt`, { method: "POST", body: "{}" });
    await waitForTask(task, "Scoring with Codex");
    await load();
  } catch (err) {
    notify(err.message);
  }
}

async function startBulkScorecards() {
  const ids = selectedJobIds();
  if (!ids.length) return notify("Select at least one job first.");
  try {
    const { task } = await api("/api/jobs/bulk/score-gpt", {
      method: "POST",
      body: JSON.stringify({ job_ids: ids }),
      activityLabel: "Starting bulk Codex scoring",
    });
    startBulkTaskPolling(task.id);
  } catch (err) {
    notify(err.message);
  }
}

async function startBulkApplicationPackets() {
  const ids = selectedJobIds();
  if (!ids.length) return notify("Select at least one job first.");
  try {
    const { task } = await api("/api/jobs/bulk/application-packets/generate", {
      method: "POST",
      body: JSON.stringify({ job_ids: ids }),
      activityLabel: "Starting bulk packet generation",
    });
    startBulkTaskPolling(task.id);
  } catch (err) {
    notify(err.message);
  }
}

async function rescrapeJob(id) {
  try {
    const { job } = await api(`/api/jobs/${id}/scrape`, {
      method: "POST",
      body: JSON.stringify({ force_refresh: true }),
      activityLabel: "Re-scraping job",
    });
    selectedId = job.id;
    selectedJob = job;
    await load();
  } catch (err) {
    notify(err.message);
  }
}

async function deleteJob(id) {
  if (!(await confirmTyped("DELETE", "This removes the tracked job and its CRM notes and interactions."))) return;
  await api(`/api/jobs/${id}`, {
    method: "DELETE",
    body: JSON.stringify({ confirm: "DELETE" }),
    activityLabel: "Deleting job",
  });
  selectedId = null;
  selectedJob = null;
  document.getElementById("detail").innerHTML = '<div class="empty">Select a job from the table.</div>';
  await load();
}

async function saveUserScore(id) {
  const scorecard = {};
  rubric.forEach(field => scorecard[field] = normalizedScore(document.getElementById(`user_${field}`).value));
  await api(`/api/jobs/${id}/score-user`, {
    method: "POST",
    body: JSON.stringify({ scorecard, user_rationale: document.getElementById("user_rationale").value })
  });
  await load();
}

function normalizedScore(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(0, Math.min(10, Math.round(parsed)));
}

function nullableNumber(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return null;
  return parsed;
}

async function addInteraction(id) {
  const payload = {};
  ["occurred_on","person_name","person_role","channel","summary","notes_to_self","next_step"].forEach(k => payload[k] = document.getElementById(k).value);
  await api(`/api/jobs/${id}/interactions`, { method: "POST", body: JSON.stringify(payload) });
  await load();
}

async function addNote(id) {
  await api(`/api/jobs/${id}/notes`, { method: "POST", body: JSON.stringify({ note: document.getElementById("new_note").value }) });
  await load();
}

async function saveStatus(id) {
  await api(`/api/jobs/${id}/status`, { method: "POST", body: JSON.stringify({ status: document.getElementById("status").value }) });
  await load();
}

async function saveSettings() {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      gpt_threshold: document.getElementById("gpt_threshold").value,
      user_threshold: document.getElementById("user_threshold").value,
      codex_model: document.getElementById("codex_model").value,
    })
  });
  await load();
}

async function saveConfig() {
  const payload = {};
  ["CODEX_CLI_PATH","CODEX_MODEL","JOB_SEARCH_ENABLE_GPT_SCORING","JOB_SEARCH_USE_CAPTURE_CACHE"].forEach(key => {
    const value = document.getElementById(`config_${key}`).value.trim();
    if (value || key === "CODEX_MODEL") payload[key] = value;
  });
  await api("/api/config", { method: "POST", body: JSON.stringify(payload) });
  await load();
}

async function purgeTrackedJobs() {
  if (!(await confirmTyped("PURGE", "This deletes all tracked jobs and their CRM notes and interactions."))) return;
  await api("/api/admin/purge-jobs", {
    method: "POST",
    body: JSON.stringify({ confirm: "PURGE" })
  });
  selectedId = null;
  selectedJob = null;
  document.getElementById("detail").innerHTML = '<div class="empty">Select a job from the table.</div>';
  await load();
}

async function runSearch() {
  searchRunning = true;
  renderSearchState();
  try {
    let task;
    try {
      ({ task } = await api("/api/search/run", {
        method: "POST",
        body: JSON.stringify({ force_refresh: document.getElementById("force_refresh").checked })
      }));
    } catch (err) {
      // A search is already running: follow that task instead of starting another.
      if (err.status !== 409 || !err.data || !err.data.task) throw err;
      task = err.data.task;
    }
    await waitForTask(task, "Job search running");
    searchRunning = false;
    await load();
  } catch (err) {
    searchRunning = false;
    renderSearchState();
    notify(err.message);
  }
}

async function addSearchQuery() {
  await api("/api/search/queries", {
    method: "POST",
    body: JSON.stringify({
      board: document.getElementById("search_board").value,
      pipeline: document.getElementById("search_pipeline").value,
      keywords: document.getElementById("search_keywords").value,
      location: document.getElementById("search_location").value,
      criteria: document.getElementById("search_criteria").value,
    })
  });
  document.getElementById("search_keywords").value = "";
  document.getElementById("search_criteria").value = "";
  await load();
}

async function toggleQuery(id, enabled) {
  await api(`/api/search/queries/${id}`, {
    method: "POST",
    body: JSON.stringify({ enabled })
  });
  await load();
}

function showPage(page) {
  currentPage = page;
  setPageVisible("jobs", page === "jobs");
  setPageVisible("companies", page === "companies");
  setPageVisible("queries", page === "queries");
  document.getElementById("jobs_nav").classList.toggle("secondary", page !== "jobs");
  document.getElementById("companies_nav").classList.toggle("secondary", page !== "companies");
  document.getElementById("queries_nav").classList.toggle("secondary", page !== "queries");
  ["jobs", "companies", "queries"].forEach(name => {
    const button = document.getElementById(`${name}_nav`);
    if (name === page) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (page === "jobs") requestAnimationFrame(() => {
    syncJobsPageHeight();
    initializeJobSplit();
  });
}

function setPageVisible(pageName, visible) {
  const element = document.getElementById(`${pageName}_page`);
  if (!element) return;
  element.classList.toggle("hidden", !visible);
  element.style.display = visible ? "" : "none";
}

function syncJobsPageHeight() {
  if (window.matchMedia("(max-width: 980px)").matches) return;
  const sidebar = document.querySelector("main > aside");
  const page = document.getElementById("jobs_page");
  if (!sidebar || !page || page.classList.contains("hidden")) return;
  const sidebarHeight = Math.ceil(sidebar.getBoundingClientRect().height);
  if (sidebarHeight > 0) {
    page.style.setProperty("--jobs-page-height", `${sidebarHeight}px`);
  }
}

function initializeJobSplit() {
  if (splitInitialized || window.matchMedia("(max-width: 980px)").matches) return;
  syncJobsPageHeight();
  const page = document.getElementById("jobs_page");
  const detail = document.getElementById("detail");
  const divider = document.getElementById("jobs_split_divider");
  if (!page || !detail || !divider || page.classList.contains("hidden")) return;
  const height = page.getBoundingClientRect().height;
  if (height > 0) {
    detail.style.setProperty("--detail-height", `${Math.round(height * 0.5)}px`);
    splitInitialized = true;
  }
}

function configureJobSplitDrag() {
  const page = document.getElementById("jobs_page");
  const detail = document.getElementById("detail");
  const divider = document.getElementById("jobs_split_divider");
  if (!page || !detail || !divider) return;
  const resize = event => {
    const rect = page.getBoundingClientRect();
    const minPane = 180;
    const dividerHeight = divider.getBoundingClientRect().height || 12;
    const rawDetailHeight = rect.bottom - event.clientY - dividerHeight / 2;
    const maxDetailHeight = Math.max(minPane, rect.height - minPane - dividerHeight);
    const nextHeight = Math.max(minPane, Math.min(maxDetailHeight, rawDetailHeight));
    detail.style.setProperty("--detail-height", `${Math.round(nextHeight)}px`);
    splitInitialized = true;
  };
  divider.addEventListener("pointerdown", event => {
    if (window.matchMedia("(max-width: 980px)").matches) return;
    event.preventDefault();
    divider.classList.add("dragging");
    divider.setPointerCapture(event.pointerId);
    resize(event);
  });
  divider.addEventListener("pointermove", event => {
    if (!divider.classList.contains("dragging")) return;
    resize(event);
  });
  const stop = event => {
    if (!divider.classList.contains("dragging")) return;
    divider.classList.remove("dragging");
    if (divider.hasPointerCapture(event.pointerId)) divider.releasePointerCapture(event.pointerId);
  };
  divider.addEventListener("pointerup", stop);
  divider.addEventListener("pointercancel", stop);
  window.addEventListener("resize", () => {
    splitInitialized = false;
    syncJobsPageHeight();
    initializeJobSplit();
  });
  const sidebar = document.querySelector("main > aside");
  if (sidebar && "ResizeObserver" in window) {
    new ResizeObserver(() => {
      syncJobsPageHeight();
      if (!splitInitialized) initializeJobSplit();
    }).observe(sidebar);
  }
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
}
// Accessible replacement for alert(): announced assertively and dismissible.
function notify(message) {
  document.getElementById("error_text").textContent = String(message || "");
  document.getElementById("error_region").hidden = false;
}
function dismissNotice() {
  document.getElementById("error_region").hidden = true;
}

// Accessible replacement for prompt(): resolves true only when the typed word matches.
function confirmTyped(word, message) {
  const dialog = document.getElementById("confirm_dialog");
  const input = document.getElementById("confirm_input");
  document.getElementById("confirm_word").textContent = word;
  document.getElementById("confirm_message").textContent = message;
  input.value = "";
  dialog.returnValue = "";
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm" && input.value === word), { once: true });
    dialog.showModal();
    input.focus();
  });
}

function escapeAttr(s) { return escapeHtml(s).replace(/`/g, "&#96;"); }
// Only absolute http(s) URLs become links; anything else (e.g. javascript:) renders as "#".
function safeHref(url) {
  try {
    const parsed = new URL(String(url || ""));
    if (parsed.protocol === "http:" || parsed.protocol === "https:") return escapeAttr(parsed.href);
  } catch (err) {
    // Not an absolute URL; fall through to the inert placeholder.
  }
  return "#";
}
// Event wiring: one delegated listener per event type instead of inline on* attributes,
// so the Content-Security-Policy can forbid inline script. Only allowlisted actions run.
const idOf = element => Number(element.dataset.id);
const ACTIONS = {
  showPage: element => showPage(element.dataset.page),
  cancelConfirm: () => document.getElementById("confirm_dialog").close("cancel"),
  dismissNotice: () => dismissNotice(),
  saveSettings: () => saveSettings(),
  runSearch: () => runSearch(),
  saveConfig: () => saveConfig(),
  purgeTrackedJobs: () => purgeTrackedJobs(),
  createJob: () => createJob(),
  resetJobTableFilters: () => resetJobTableFilters(),
  selectVisibleJobs: () => selectVisibleJobs(),
  clearBulkSelection: () => clearBulkSelection(),
  startBulkScorecards: () => startBulkScorecards(),
  startBulkApplicationPackets: () => startBulkApplicationPackets(),
  createCompanyInterest: () => createCompanyInterest(),
  addSearchQuery: () => addSearchQuery(),
  selectJob: element => selectJob(idOf(element)),
  selectCompany: element => selectCompany(idOf(element)),
  openJob: element => { showPage("jobs"); return selectJob(idOf(element)); },
  toggleQuery: element => toggleQuery(idOf(element), element.dataset.enabled === "true"),
  saveCompanyInterest: element => saveCompanyInterest(idOf(element)),
  runJobAction: element => runJobAction(idOf(element)),
  scoreGpt: element => scoreGpt(idOf(element)),
  openApplicationMarkdown: element => openApplicationMarkdown(idOf(element)),
  generateApplicationPacket: element => generateApplicationPacket(idOf(element)),
  attachApplicationPacket: element => attachApplicationPacket(idOf(element)),
  saveUserScore: element => saveUserScore(idOf(element)),
  addInteraction: element => addInteraction(idOf(element)),
  addNote: element => addNote(idOf(element)),
};
const FORM_CONTROLS = "input, select, textarea, label";

document.addEventListener("click", event => {
  const element = event.target.closest("[data-action]");
  if (!element) return;
  // Clicks on form controls inside a clickable row (e.g. the bulk-select checkbox) must not select the row.
  if (element.tagName === "TR" && event.target.closest(FORM_CONTROLS)) return;
  const action = ACTIONS[element.dataset.action];
  if (!action) return;
  Promise.resolve()
    .then(() => action(element, event))
    .catch(err => notify(err.message));
});

const onFilterChange = event => {
  if (!event.target.matches("[data-filter-control]")) return;
  saveJobTableFilters();
  renderJobs();
};
document.addEventListener("change", onFilterChange);
document.addEventListener("input", onFilterChange);
document.addEventListener("change", event => {
  if (event.target.matches("[data-bulk-select]")) {
    toggleBulkJobSelection(Number(event.target.dataset.bulkSelect), event.target.checked);
  }
});
document.getElementById("job_filter_rollup").addEventListener("toggle", saveJobFilterRollupState);

configureJobSplitDrag();
load().then(() => {
  if (activeBulkTaskId) startBulkTaskPolling(activeBulkTaskId);
});
