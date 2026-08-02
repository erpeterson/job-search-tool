# Job Search Tool

Local CRM and scoring tool for Eric's job search.

Run:

```bash
job-search-tool/run.sh
```

The script will:

- create `job-search-tool/.venv` if needed
- install dependencies from `job-search-tool/requirements.txt` if needed
- create `job-search-tool/.env` if needed
- prompt for `OPENAI_API_KEY` if it is not already configured
- start the local Flask app

To prepare the environment without starting the app:

```bash
job-search-tool/run.sh --setup-only
```

Then open:

```text
http://127.0.0.1:5050
```

Optional GPT scoring:

```bash
job-search-tool/run.sh --api-key YOUR_OPENAI_API_KEY
```

This writes the key to `job-search-tool/.env`. You can create an API key at
`https://platform.openai.com/api-keys`.

Data is stored locally in `job-search-tool/job_search.sqlite3`.

## Product Rules

- The rubric is based on `supporting-documents/20260731-job-search-guidance.md`.
- GPT score threshold defaults to `40`.
- User score threshold defaults to `60`.
- A job is filtered when either score is below its threshold.
- GPT scoring includes recent user-scored examples as calibration context, so the model can adapt to Eric's preferences over time.
- Thresholds are editable in the UI and persisted in SQLite.

## Current Scope

- Add and track job listings.
- Paste posting text for scoring.
- Generate GPT scorecards when `OPENAI_API_KEY` is configured.
- Record Eric's own scorecard.
- Track application status.
- Track people, conversations, notes, and next steps.
- Filter low-fit jobs.
