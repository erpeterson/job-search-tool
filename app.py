#!/usr/bin/env python3
import json
import os
import sqlite3
import textwrap
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "job_search.sqlite3"
GUIDANCE_PATH = ROOT / "supporting-documents" / "20260731-job-search-guidance.md"
CAREER_MANUAL_PATH = ROOT / "career-manual" / "Career-Manual.md"

load_dotenv(APP_DIR / ".env")

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")
HOST = os.environ.get("JOB_SEARCH_HOST", "127.0.0.1")
PORT = int(os.environ.get("JOB_SEARCH_PORT", "5050"))
DEBUG = os.environ.get("JOB_SEARCH_DEBUG", "0") == "1"

app = Flask(__name__)

RUBRIC_FIELDS = [
    "interesting_technical_problems",
    "organizational_influence",
    "cross_functional_work",
    "opportunity_to_mentor",
    "work_life_balance",
    "low_operational_burden",
    "compensation",
    "mission",
]

PIPELINES = [
    "Executive IC",
    "Office of the CTO",
    "Adjacent industries",
    "Wildcards",
]


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                company TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT,
                location TEXT,
                pipeline TEXT,
                status TEXT NOT NULL DEFAULT 'researching',
                posting_text TEXT,
                notes TEXT,
                gpt_score INTEGER,
                gpt_rationale TEXT,
                gpt_scorecard_json TEXT,
                user_score INTEGER,
                user_scorecard_json TEXT,
                user_rationale TEXT,
                filtered INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                occurred_on TEXT NOT NULL,
                person_name TEXT,
                person_role TEXT,
                channel TEXT,
                summary TEXT,
                notes_to_self TEXT,
                next_step TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                note TEXT NOT NULL
            );
            """
        )
        defaults = {
            "gpt_threshold": "40",
            "user_threshold": "60",
            "model": DEFAULT_MODEL,
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )


def now():
    return int(time.time())


def row_to_dict(row):
    return dict(row) if row else None


def parse_json_field(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def settings(conn):
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def apply_filter(conn, job_id):
    cfg = settings(conn)
    gpt_threshold = int(cfg.get("gpt_threshold", "40"))
    user_threshold = int(cfg.get("user_threshold", "60"))
    job = conn.execute("SELECT gpt_score, user_score FROM jobs WHERE id = ?", (job_id,)).fetchone()
    filtered = 0
    if job:
        if job["gpt_score"] is not None and job["gpt_score"] < gpt_threshold:
            filtered = 1
        if job["user_score"] is not None and job["user_score"] < user_threshold:
            filtered = 1
    conn.execute("UPDATE jobs SET filtered = ?, updated_at = ? WHERE id = ?", (filtered, now(), job_id))


def list_jobs(conn, include_filtered=False):
    query = "SELECT * FROM jobs"
    params = []
    if not include_filtered:
        query += " WHERE filtered = 0"
    query += " ORDER BY updated_at DESC, created_at DESC"
    jobs = []
    for row in conn.execute(query, params):
        item = row_to_dict(row)
        item["gpt_scorecard"] = parse_json_field(item.pop("gpt_scorecard_json"), {})
        item["user_scorecard"] = parse_json_field(item.pop("user_scorecard_json"), {})
        jobs.append(item)
    return jobs


def get_job(conn, job_id):
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    job = row_to_dict(row)
    job["gpt_scorecard"] = parse_json_field(job.pop("gpt_scorecard_json"), {})
    job["user_scorecard"] = parse_json_field(job.pop("user_scorecard_json"), {})
    job["interactions"] = [
        row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM interactions WHERE job_id = ? ORDER BY occurred_on DESC, id DESC",
            (job_id,),
        )
    ]
    job["notes_list"] = [
        row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM notes WHERE job_id = ? ORDER BY created_at DESC, id DESC",
            (job_id,),
        )
    ]
    return job


def career_context():
    manual = CAREER_MANUAL_PATH.read_text(encoding="utf-8") if CAREER_MANUAL_PATH.exists() else ""
    guidance = GUIDANCE_PATH.read_text(encoding="utf-8") if GUIDANCE_PATH.exists() else ""
    return textwrap.shorten(manual, width=9000, placeholder="\n[manual truncated]\n") + "\n\n" + guidance


def calibration_examples(conn):
    rows = conn.execute(
        """
        SELECT company, title, pipeline, gpt_score, user_score, user_rationale, posting_text
        FROM jobs
        WHERE user_score IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 8
        """
    ).fetchall()
    examples = []
    for row in rows:
        examples.append(
            {
                "company": row["company"],
                "title": row["title"],
                "pipeline": row["pipeline"],
                "gpt_score": row["gpt_score"],
                "user_score": row["user_score"],
                "user_rationale": row["user_rationale"],
                "posting_excerpt": textwrap.shorten(row["posting_text"] or "", width=800, placeholder="..."),
            }
        )
    return examples


def score_with_openai(conn, job):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set. Add the job manually or export OPENAI_API_KEY before scoring.")

    cfg = settings(conn)
    model = cfg.get("model") or DEFAULT_MODEL
    client = OpenAI(api_key=api_key)
    prompt = {
        "task": "Score this job for Eric Peterson's job search.",
        "instructions": [
            "Return JSON only.",
            "Use a 0-100 total fit score.",
            "Score each rubric item from 0-10.",
            "Reward cross-cutting architecture, organizational scaling, engineering effectiveness, developer experience, AI-enabled development, technical strategy, and technical decision quality.",
            "Penalize line management, heavy operational ownership, firefighting, incremental feature ownership, narrow service ownership, and roles that only value hands-on coding.",
            "Use the calibration examples to adjust future scoring toward Eric's own scores.",
            "Do not invent facts missing from the posting.",
        ],
        "expected_json_schema": {
            "total_score": "integer 0-100",
            "pipeline": PIPELINES,
            "scorecard": {field: "integer 0-10" for field in RUBRIC_FIELDS},
            "rationale": "short paragraph",
            "strengths": ["short bullets"],
            "risks": ["short bullets"],
            "recommended_next_step": "short sentence",
        },
        "career_context": career_context(),
        "calibration_examples": calibration_examples(conn),
        "job": {
            "company": job["company"],
            "title": job["title"],
            "url": job["url"],
            "location": job["location"],
            "pipeline": job["pipeline"],
            "posting_text": job["posting_text"],
            "notes": job["notes"],
        },
    }
    response = client.responses.create(
        model=model,
        input=json.dumps(prompt),
        text={"format": {"type": "json_object"}},
    )
    output_text = response.output_text
    if not output_text:
        output_text = ""
    if not output_text:
        raise RuntimeError("OpenAI response did not include text output.")
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI response was not valid JSON: {output_text[:1000]}") from exc
    return parsed


@app.get("/")
def index():
    return INDEX_HTML


@app.get("/api/state")
def api_state():
    include_filtered = request.args.get("include_filtered") == "1"
    with connect() as conn:
        return jsonify(
            {
                "settings": settings(conn),
                "jobs": list_jobs(conn, include_filtered=include_filtered),
                "pipelines": PIPELINES,
                "rubric_fields": RUBRIC_FIELDS,
            }
        )


@app.get("/api/jobs/<int:job_id>")
def api_job(job_id):
    with connect() as conn:
        job = get_job(conn, job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"job": job})


@app.post("/api/jobs")
def api_create_job():
    payload = request.get_json(silent=True) or {}
    ts = now()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO jobs(created_at, updated_at, company, title, url, location, pipeline, status, posting_text, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                ts,
                payload.get("company", "").strip() or "Unknown company",
                payload.get("title", "").strip() or "Unknown title",
                payload.get("url", "").strip(),
                payload.get("location", "").strip(),
                payload.get("pipeline", "").strip(),
                payload.get("status", "researching"),
                payload.get("posting_text", "").strip(),
                payload.get("notes", "").strip(),
            ),
        )
        job_id = cur.lastrowid
        apply_filter(conn, job_id)
        return jsonify({"job": get_job(conn, job_id)}), 201


@app.post("/api/jobs/<int:job_id>/score-gpt")
def api_score_gpt(job_id):
    with connect() as conn:
        job = get_job(conn, job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        score = score_with_openai(conn, job)
        total = int(score.get("total_score", 0))
        scorecard = score.get("scorecard", {})
        conn.execute(
            """
            UPDATE jobs
            SET gpt_score = ?, gpt_rationale = ?, gpt_scorecard_json = ?, pipeline = COALESCE(NULLIF(?, ''), pipeline), updated_at = ?
            WHERE id = ?
            """,
            (
                total,
                score.get("rationale", ""),
                json.dumps(scorecard),
                score.get("pipeline", ""),
                now(),
                job_id,
            ),
        )
        apply_filter(conn, job_id)
        return jsonify({"job": get_job(conn, job_id), "raw_score": score})


@app.post("/api/jobs/<int:job_id>/score-user")
def api_score_user(job_id):
    payload = request.get_json(silent=True) or {}
    scorecard = {field: int(payload.get("scorecard", {}).get(field, 0)) for field in RUBRIC_FIELDS}
    total = int(payload.get("total_score") or round(sum(scorecard.values()) * 100 / (len(RUBRIC_FIELDS) * 10)))
    with connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET user_score = ?, user_scorecard_json = ?, user_rationale = ?, updated_at = ?
            WHERE id = ?
            """,
            (total, json.dumps(scorecard), payload.get("user_rationale", ""), now(), job_id),
        )
        apply_filter(conn, job_id)
        return jsonify({"job": get_job(conn, job_id)})


@app.post("/api/jobs/<int:job_id>/interactions")
def api_add_interaction(job_id):
    payload = request.get_json(silent=True) or {}
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO interactions(job_id, occurred_on, person_name, person_role, channel, summary, notes_to_self, next_step, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                payload.get("occurred_on", ""),
                payload.get("person_name", ""),
                payload.get("person_role", ""),
                payload.get("channel", ""),
                payload.get("summary", ""),
                payload.get("notes_to_self", ""),
                payload.get("next_step", ""),
                now(),
            ),
        )
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now(), job_id))
        return jsonify({"job": get_job(conn, job_id)}), 201


@app.post("/api/jobs/<int:job_id>/notes")
def api_add_note(job_id):
    payload = request.get_json(silent=True) or {}
    with connect() as conn:
        conn.execute(
            "INSERT INTO notes(job_id, created_at, note) VALUES (?, ?, ?)",
            (job_id, now(), payload.get("note", "")),
        )
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now(), job_id))
        return jsonify({"job": get_job(conn, job_id)}), 201


@app.post("/api/jobs/<int:job_id>/status")
def api_update_status(job_id):
    payload = request.get_json(silent=True) or {}
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (payload.get("status", "researching"), now(), job_id),
        )
        return jsonify({"job": get_job(conn, job_id)})


@app.post("/api/settings")
def api_update_settings():
    payload = request.get_json(silent=True) or {}
    with connect() as conn:
        for key in ("gpt_threshold", "user_threshold", "model"):
            if key in payload:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(payload[key])),
                )
        for row in conn.execute("SELECT id FROM jobs"):
            apply_filter(conn, row["id"])
        return jsonify({"settings": settings(conn), "jobs": list_jobs(conn, include_filtered=True)})


@app.errorhandler(Exception)
def api_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    return jsonify({"error": str(exc)}), 500


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Job Search Console</title>
  <style>
    :root {
      --bg: #eef2ef;
      --ink: #15201a;
      --muted: #5e6a61;
      --line: #c6d0c8;
      --panel: #fbfcfa;
      --accent: #0f766e;
      --accent-2: #8a4b20;
      --danger: #9f2436;
      --ok: #2f7d32;
      --warn: #b26a00;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(135deg, rgba(15,118,110,.12), transparent 34%),
        linear-gradient(225deg, rgba(138,75,32,.10), transparent 40%),
        var(--bg);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
    }
    header {
      padding: 22px 28px 12px;
      border-bottom: 1px solid var(--line);
      background: rgba(251,252,250,.82);
      position: sticky;
      top: 0;
      z-index: 5;
      backdrop-filter: blur(14px);
    }
    h1 { margin: 0 0 6px; font-size: 28px; }
    .subtitle { color: var(--muted); max-width: 950px; }
    main {
      display: grid;
      grid-template-columns: 390px 1fr;
      gap: 18px;
      padding: 18px;
    }
    section, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    h2 { margin: 0 0 12px; font-size: 18px; }
    h3 { margin: 16px 0 8px; font-size: 15px; }
    label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 4px; }
    input, textarea, select, button {
      width: 100%;
      font: inherit;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      background: white;
      color: var(--ink);
    }
    textarea { min-height: 100px; resize: vertical; }
    button {
      cursor: pointer;
      background: var(--accent);
      color: white;
      border-color: var(--accent);
      font-weight: 650;
    }
    button.secondary { background: white; color: var(--ink); border-color: var(--line); }
    button.warn { background: var(--accent-2); border-color: var(--accent-2); }
    .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .row { display: flex; gap: 8px; align-items: center; }
    .row > * { flex: 1; }
    .jobs { display: grid; gap: 10px; }
    .job {
      text-align: left;
      background: white;
      color: var(--ink);
      border: 1px solid var(--line);
      border-left: 5px solid var(--accent);
      padding: 12px;
      border-radius: 7px;
    }
    .job.filtered { border-left-color: var(--danger); opacity: .72; }
    .job.active { outline: 2px solid var(--accent); }
    .job-title { font-weight: 750; }
    .meta { color: var(--muted); font-size: 13px; margin-top: 3px; }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .chip {
      border: 1px solid var(--line);
      border-radius: 99px;
      padding: 3px 8px;
      font-size: 12px;
      background: #f6f8f5;
    }
    .score-good { color: var(--ok); font-weight: 750; }
    .score-warn { color: var(--warn); font-weight: 750; }
    .score-bad { color: var(--danger); font-weight: 750; }
    .detail { display: grid; gap: 14px; }
    .score-grid { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 8px; }
    .score-grid input { text-align: right; }
    .empty {
      min-height: 420px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
    }
    .note, .interaction {
      border-top: 1px solid var(--line);
      padding-top: 10px;
      margin-top: 10px;
    }
    .toolbar { display: flex; gap: 10px; align-items: end; margin-bottom: 12px; }
    .toolbar label { margin-top: 0; }
    .small { font-size: 12px; color: var(--muted); }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .score-grid { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Job Search Console</h1>
    <div class="subtitle">Score opportunities against the ideal problem set, track applications like a CRM, and preserve learning from every conversation.</div>
  </header>
  <main>
    <aside>
      <section>
        <h2>Add Job</h2>
        <label>Company</label><input id="company">
        <label>Title</label><input id="title">
        <label>URL</label><input id="url">
        <label>Location</label><input id="location">
        <label>Pipeline</label><select id="pipeline"></select>
        <label>Posting Text</label><textarea id="posting_text" placeholder="Paste the job description here for GPT scoring."></textarea>
        <label>Initial Notes</label><textarea id="notes" placeholder="Why this is interesting, concerns, people to contact."></textarea>
        <button onclick="createJob()">Add job</button>
      </section>
      <section style="margin-top: 14px;">
        <h2>Filters</h2>
        <div class="grid2">
          <div><label>GPT threshold</label><input id="gpt_threshold" type="number" min="0" max="100"></div>
          <div><label>User threshold</label><input id="user_threshold" type="number" min="0" max="100"></div>
        </div>
        <label>Model</label><input id="model">
        <div class="row" style="margin-top: 10px;">
          <button onclick="saveSettings()">Save</button>
          <button class="secondary" onclick="toggleFiltered()">Show/Hide filtered</button>
        </div>
        <p class="small">Jobs are filtered when GPT score is below threshold or user score is below threshold.</p>
      </section>
      <section style="margin-top: 14px;">
        <h2>Jobs</h2>
        <div id="jobs" class="jobs"></div>
      </section>
    </aside>
    <section id="detail" class="detail">
      <div class="empty">Select or add a job.</div>
    </section>
  </main>
  <script>
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
    let state = { jobs: [], settings: {}, pipelines: [], rubric_fields: rubric };
    let selectedId = null;
    let includeFiltered = false;

    const pretty = s => s.replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
    const scoreClass = n => n == null ? "" : n >= 70 ? "score-good" : n >= 40 ? "score-warn" : "score-bad";

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Request failed");
      return data;
    }

    async function load() {
      state = await api(`/api/state?include_filtered=${includeFiltered ? "1" : "0"}`);
      document.getElementById("gpt_threshold").value = state.settings.gpt_threshold;
      document.getElementById("user_threshold").value = state.settings.user_threshold;
      document.getElementById("model").value = state.settings.model;
      const pipeline = document.getElementById("pipeline");
      pipeline.innerHTML = '<option value=""></option>' + state.pipelines.map(p => `<option>${p}</option>`).join("");
      renderJobs();
      if (selectedId) await selectJob(selectedId, false);
    }

    function renderJobs() {
      const jobs = document.getElementById("jobs");
      jobs.innerHTML = state.jobs.map(job => `
        <button class="job ${job.filtered ? "filtered" : ""} ${job.id === selectedId ? "active" : ""}" onclick="selectJob(${job.id})">
          <div class="job-title">${escapeHtml(job.company)} - ${escapeHtml(job.title)}</div>
          <div class="meta">${escapeHtml(job.pipeline || "Unassigned")} · ${escapeHtml(job.status || "")}</div>
          <div class="chips">
            <span class="chip">GPT: <b class="${scoreClass(job.gpt_score)}">${job.gpt_score ?? "n/a"}</b></span>
            <span class="chip">Mine: <b class="${scoreClass(job.user_score)}">${job.user_score ?? "n/a"}</b></span>
            ${job.filtered ? '<span class="chip">Filtered</span>' : ""}
          </div>
        </button>
      `).join("") || '<div class="small">No jobs match the current filter.</div>';
    }

    async function selectJob(id, rerender = true) {
      selectedId = id;
      const { job } = await api(`/api/jobs/${id}`);
      renderDetail(job);
      if (rerender) renderJobs();
    }

    function renderDetail(job) {
      const detail = document.getElementById("detail");
      detail.innerHTML = `
        <div class="panel">
          <div class="toolbar">
            <div>
              <h2>${escapeHtml(job.company)} - ${escapeHtml(job.title)}</h2>
              <div class="meta">${escapeHtml(job.location || "")} ${job.url ? `· <a href="${escapeAttr(job.url)}" target="_blank">posting</a>` : ""}</div>
            </div>
            <div>
              <label>Status</label>
              <select id="status">${["researching","interested","applied","interviewing","offer","rejected","declined","paused"].map(s => `<option ${job.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
            </div>
            <button onclick="saveStatus(${job.id})">Save status</button>
          </div>
          <div class="chips">
            <span class="chip">Pipeline: ${escapeHtml(job.pipeline || "Unassigned")}</span>
            <span class="chip">GPT: <b class="${scoreClass(job.gpt_score)}">${job.gpt_score ?? "n/a"}</b></span>
            <span class="chip">Mine: <b class="${scoreClass(job.user_score)}">${job.user_score ?? "n/a"}</b></span>
            ${job.filtered ? '<span class="chip">Filtered</span>' : ""}
          </div>
          <p>${escapeHtml(job.gpt_rationale || "No GPT rationale yet.")}</p>
          <button class="warn" onclick="scoreGpt(${job.id})">Populate GPT scorecard</button>
        </div>

        <div class="panel">
          <h2>My Scorecard</h2>
          <div class="score-grid">${rubric.map(field => `
            <label>${pretty(field)}<input id="user_${field}" type="number" min="0" max="10" value="${job.user_scorecard?.[field] ?? ""}"></label>
          `).join("")}</div>
          <label>Rationale</label><textarea id="user_rationale">${escapeHtml(job.user_rationale || "")}</textarea>
          <button onclick="saveUserScore(${job.id})">Save my score</button>
        </div>

        <div class="panel">
          <h2>GPT Scorecard</h2>
          <div class="score-grid">${rubric.map(field => `
            <div><span class="small">${pretty(field)}</span><br><b>${job.gpt_scorecard?.[field] ?? "n/a"}</b></div>
          `).join("")}</div>
        </div>

        <div class="panel">
          <h2>Interactions</h2>
          <div class="grid2">
            <div><label>Date</label><input id="occurred_on" type="date"></div>
            <div><label>Channel</label><input id="channel" placeholder="intro, email, call, interview"></div>
          </div>
          <div class="grid2">
            <div><label>Person</label><input id="person_name"></div>
            <div><label>Role</label><input id="person_role"></div>
          </div>
          <label>What we talked about</label><textarea id="summary"></textarea>
          <label>Notes to self</label><textarea id="notes_to_self"></textarea>
          <label>Next step</label><input id="next_step">
          <button onclick="addInteraction(${job.id})">Add interaction</button>
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
          <button onclick="addNote(${job.id})">Add note</button>
          <div>${job.notes_list.map(n => `<div class="note">${escapeHtml(n.note)}</div>`).join("")}</div>
        </div>
      `;
    }

    async function createJob() {
      const payload = {
        company: company.value,
        title: title.value,
        url: url.value,
        location: location.value,
        pipeline: pipeline.value,
        posting_text: posting_text.value,
        notes: notes.value,
      };
      const { job } = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
      selectedId = job.id;
      ["company","title","url","location","posting_text","notes"].forEach(id => document.getElementById(id).value = "");
      await load();
    }

    async function scoreGpt(id) {
      try {
        await api(`/api/jobs/${id}/score-gpt`, { method: "POST", body: "{}" });
        await load();
      } catch (err) {
        alert(err.message);
      }
    }

    async function saveUserScore(id) {
      const scorecard = {};
      rubric.forEach(field => scorecard[field] = Number(document.getElementById(`user_${field}`).value || 0));
      await api(`/api/jobs/${id}/score-user`, {
        method: "POST",
        body: JSON.stringify({ scorecard, user_rationale: document.getElementById("user_rationale").value })
      });
      await load();
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
          model: document.getElementById("model").value,
        })
      });
      await load();
    }

    function toggleFiltered() {
      includeFiltered = !includeFiltered;
      load();
    }

    function escapeHtml(s) {
      return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
    }
    function escapeAttr(s) { return escapeHtml(s).replace(/`/g, "&#96;"); }
    load();
  </script>
</body>
</html>
"""


def main():
    init_db()
    print(f"Job Search Console running at http://{HOST}:{PORT}")
    print(f"Database: {DB_PATH}")
    app.run(host=HOST, port=PORT, debug=DEBUG, use_reloader=False)


if __name__ == "__main__":
    main()
