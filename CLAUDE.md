# Daily Call Pipeline — Claude CLI Instructions

This project fetches HubSpot sales calls, transcribes them with Whisper, and generates coaching reports.

## When invoked automatically (via launchd)

You are called after `daily_fetch.py` has finished. Your job:

1. **Determine which date was processed** — read the latest log in `logs/` to find the target date and which agents had transcriptions.

2. **For each agent that has transcripts** (transcribed > 0), do the following **one agent at a time**:

   a. Read the transcripts from `agents/<Agent>_Call_Report/data/<dd-mm-yyyy>/transcripts/*.txt`
   b. Read the agent's `context.json` at `agents/<Agent>_Call_Report/context.json` for historical context
   c. Read `agents/<Agent>_Call_Report/data/<dd-mm-yyyy>/calls_meta.json` for call metadata
   d. Generate `analysis.json` in the same data folder with coaching analysis (use the Anthropic API via the `agents/<Agent>_Call_Report/generate_report.py` analysis format as reference)
   e. Update `coaching_insights` in `context.json` with new patterns, strengths, weaknesses, and evolution notes
   f. Run: `python3 build_report.py --agent <name> --date <YYYY-MM-DD> --total-calls <N> --completed-calls <N>`
   g. Send the generated PDF by email using the agent's `email_to` and `email_cc` from `agents_config.py`:
      ```
      python3 send_email.py \
        --to <agent.email_to> \
        --cc <agent.email_cc joined by commas> \
        --subject "Call Report - <Agent Display Name> - <dd-mm-yyyy>" \
        --body "Em anexo o report diário de chamadas de <Agent Display Name>." \
        --attachment agents/<Agent>_Call_Report/reports/<Agent>_Call_Report_<dd-mm-yyyy>.pdf
      ```
      Read the email routing from `agents_config.py` — each agent has `email_to` and `email_cc` fields.

3. **Skip agents** with 0 transcriptions — no report to generate.

## analysis.json format

The analysis.json must contain these keys:
- `overall_assessment`: 2-3 sentences, direct and specific
- `what_agent_does_well`: list of `{"title": "...", "description": "..."}`
- `critical_weaknesses`: list of `{"title": "...", "description": "...", "key_missed_moments": [...], "action": "..."}`
- `call_summary`: list of `{"contact": "...", "duration": "...", "outcome": "...", "key_issue": "..."}`
- `priority_coaching_actions`: list of `{"title": "...", "description": "...", "example_phrase": "..."}`

## Key paths

- Agent config: `agents_config.py` — lists all agents with name, display, owner_id, data_dir, report_dir, email_to, email_cc
- Data: `agents/<Agent>_Call_Report/data/<dd-mm-yyyy>/`
- Reports: `agents/<Agent>_Call_Report/reports/`
- Context: `agents/<Agent>_Call_Report/context.json`
- Email sender: `send_email.py`
- PDF builder: `build_report.py`
- Weekly report: `run_weekly.py`, `weekly_analysis.py`, `build_weekly_report.py`; artifacts in `agents/<Agent>_Call_Report/weekly/<dd-mm-yyyy>/`
- Logs: `logs/<YYYY-MM-DD>.log`

## Date format

Folders use `dd-mm-yyyy` format (e.g., `08-05-2026`). The `--date` CLI argument uses `YYYY-MM-DD`.

## Weekly overview report (run_weekly.py)

Separate from the daily reports: a **weekly overview** (high-level, NOT call-by-call) for
**Real Estate agents only**, emailed every **Friday 07:00 Europe/Lisbon** (cron `0 8 * * 5` —
system time is Lisbon+1h) to the **management distribution only** (`sandra@realestateinvest.pt` + `_CC_KARMA`), never to the commercials themselves.

- `run_weekly.py` — orchestrator. Defaults to the previous Mon–Fri; flags: `--week-start YYYY-MM-DD`,
  `--agent`, `--no-email`, `--no-gcs`, `--dry-run`, `--email-override`, `--reuse-stored`.
- It does NOT re-fetch HubSpot — it aggregates the already-stored daily `calls_meta.json` (metrics)
  and `analysis.json` (coaching) from GCS over the week.
- `weekly_analysis.py::analyze_weekly` — DeepSeek weekly synthesis. Agents with no daily analyses
  fall back to a deterministic metrics-only overview (no model call).
- `build_weekly_report.py::generate_weekly_pdf` — PDF reusing the daily report's styles/helpers.
- Artifacts upload to GCS under `agents/<folder>/weekly/<dd-mm-yyyy>/`.

Weekly analysis schema keys: `week_overview`, `metrics_commentary`, `recurring_strengths`,
`recurring_weaknesses` (each with an `action`), `weekly_trend`, `focus_next_week`.

## Important

- Process one agent at a time, not in parallel
- Be brutally honest in coaching analysis — reference specific calls, moments, and quotes
- Write all analysis content in Portuguese (Portugal) — overall assessment, strengths, weaknesses, call summaries, coaching actions, and example phrases
- The coaching insights in context.json should evolve over time — note improvements and recurring issues

## Real Estate agents (David, Sandra, Zé, Joana, Emanuel Martins)

These agents sell real estate (Conde da Ribeira, Copa, etc.). When analysing their calls:

- **Do NOT suggest cross-selling other developments** (e.g., Santo Amaro 154). Cross-sell between developments is not available — never flag its absence as a weakness or coaching action.
- **Think outside the box**: focus on negotiation techniques, objection handling, visit scheduling, lead qualification, follow-up discipline, emotional intelligence, and creative solutions to close deals within the products the agent actually sells.
- When a prospect's budget doesn't match, analyse how the agent handles the mismatch — redirect to available typologies within the same development, negotiate terms, or qualify out gracefully. Do not suggest redirecting to other developments.
