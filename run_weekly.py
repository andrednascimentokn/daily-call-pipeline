#!/usr/bin/env python3
"""Weekly overview report pipeline — Friday 07:00 Europe/Lisbon (cron entry point).

Produces a per-commercial WEEKLY OVERVIEW (not call-by-call) for the Real Estate
agents only, aggregating the daily artifacts already stored in GCS (calls_meta.json
for metrics + analysis.json for coaching) over the previous Monday–Friday, and emails
one PDF per commercial to the management distribution (Sandra + Karma) — NOT to the
commercials themselves.

CLI:
    python3 run_weekly.py                                   # previous Mon–Fri, all RE agents, prod
    python3 run_weekly.py --week-start 2026-06-08           # specific week (Monday date)
    python3 run_weekly.py --agent david                     # restrict to one agent
    python3 run_weekly.py --no-email                        # build + upload only
    python3 run_weekly.py --email-override me@x.com         # redirect all emails (smoke-test)
    python3 run_weekly.py --no-gcs                          # local only (dev)
    python3 run_weekly.py --dry-run                         # skip DeepSeek + PDF + email + upload
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

# Importing run_pipeline loads /etc/daily-calls/.env (env side effect) and gives us
# the SMTP sender + logging helpers + healthcheck — reuse them directly.
from run_pipeline import _smtp_send, hc_ping, log, LOCAL_LOGS_DIR
from agents_config import AGENTS, _CC_KARMA
from gcs_storage import gcs_read_json, gcs_upload_file, gcs_write_json
from weekly_analysis import analyze_weekly
from build_weekly_report import generate_weekly_pdf

TZ_LISBON = ZoneInfo("Europe/Lisbon")
PIPELINE_DIR = Path(__file__).resolve().parent

# Weekly overview is management-only (NOT the commercials). Recipients depend on the
# agent's business line.
WEEKLY_BUSINESSES = {"real_estate", "funeral_em_vida"}


def weekly_recipients(agent):
    """(to, cc) for an agent's weekly overview — routed by business line."""
    business = agent.get("business", "")
    if business == "real_estate":
        return "sandra@realestateinvest.pt", list(_CC_KARMA)
    if business == "funeral_em_vida":
        return agent["email_to"], list(_CC_KARMA)
    return agent["email_to"], list(agent.get("email_cc", []))


def setup_weekly_logging(run_date: str) -> Path:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    log_path = LOCAL_LOGS_DIR / f"weekly_{run_date}.log"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log_path


def previous_week_monday(today) -> "datetime":
    """Monday of the previous (completed) week relative to `today`."""
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7)


def gcs_weekly_prefix(folder: str, week_label: str, gcs_prefix: str) -> str:
    base = gcs_prefix.strip("/")
    parts = [p for p in [base, "agents", folder, "weekly", week_label] if p]
    return "/".join(parts)


def aggregate_week(folder: str, days: List[datetime], gcs_prefix: str) -> Dict[str, Any]:
    """Pull per-day calls_meta.json + analysis.json from GCS and aggregate metrics."""
    base = gcs_prefix.strip("/")
    prefix = (base + "/") if base else ""
    total = completed = transcribed = 0
    days_active = 0
    per_day: List[Dict[str, Any]] = []
    daily_analyses: List[Dict[str, Any]] = []

    for d in days:
        label = d.strftime("%d-%m-%Y")
        meta = gcs_read_json(f"{prefix}agents/{folder}/data/{label}/calls_meta.json")
        d_total = d_completed = d_transcribed = 0
        if meta:
            d_total = len(meta)
            d_completed = sum(1 for c in meta if c.get("status") == "COMPLETED")
            d_transcribed = sum(1 for c in meta if c.get("has_transcript"))
            if d_total > 0:
                days_active += 1
        total += d_total
        completed += d_completed
        transcribed += d_transcribed
        per_day.append({"date": label, "total": d_total, "completed": d_completed, "transcribed": d_transcribed})

        analysis = gcs_read_json(f"{prefix}agents/{folder}/data/{label}/analysis.json")
        if analysis:
            daily_analyses.append({"date": label, "analysis": analysis})

    metrics = {
        "total_calls": total,
        "completed": completed,
        "transcribed": transcribed,
        "days_active": days_active,
        "num_days": len(days),
        "per_day": per_day,
    }
    return {"metrics": metrics, "daily_analyses": daily_analyses}


def process_agent(agent: Dict[str, Any], week_start: datetime, days: List[datetime],
                  workdir: Path, *, no_email: bool, no_gcs: bool, dry_run: bool,
                  gcs_prefix: str, email_override: Optional[str],
                  reuse_stored: bool = False) -> Dict[str, Any]:
    display = agent["display"]
    folder = agent["folder"]
    week_end = days[-1]
    week_label = week_start.strftime("%d-%m-%Y")
    range_label = f"{week_start.strftime('%d-%m')} a {week_end.strftime('%d-%m-%Y')}"

    log.info(f"--- [{display}] weekly start ({range_label}) ---")

    agg = aggregate_week(folder, days, gcs_prefix if not no_gcs else "")
    metrics = agg["metrics"]
    daily_analyses = agg["daily_analyses"]
    log.info(f"  [{display}] week metrics — total={metrics['total_calls']} "
             f"completed={metrics['completed']} transcribed={metrics['transcribed']} "
             f"days_active={metrics['days_active']} daily_analyses={len(daily_analyses)}")

    result: Dict[str, Any] = {"agent": display, **{k: metrics[k] for k in
                              ("total_calls", "completed", "transcribed", "days_active")},
                              "daily_analyses": len(daily_analyses)}

    if metrics["total_calls"] == 0:
        log.info(f"  [{display}] no calls this week — skipping PDF/email")
        result["skipped"] = "no_calls"
        return result

    if dry_run:
        log.info(f"  [{display}] dry-run — stop before analysis/PDF/email")
        return result

    # Weekly synthesis (metrics-only fallback handled inside analyze_weekly)
    weekly_json_path = workdir / "weekly_analysis.json"
    if reuse_stored and weekly_json_path.exists():
        weekly = json.loads(weekly_json_path.read_text(encoding="utf-8"))
        log.info(f"  [{display}] reusing stored weekly_analysis.json (no DeepSeek call)")
    else:
        context = gcs_read_json(f"agents/{folder}/context.json") or {} if not no_gcs else {}
        weekly = analyze_weekly(agent, range_label, daily_analyses, metrics, context)
        weekly_json_path.write_text(json.dumps(weekly, indent=2, ensure_ascii=False), encoding="utf-8")

    pdf_path = workdir / f"{folder}_Weekly_{week_label}.pdf"
    generate_weekly_pdf(
        weekly, week_start, week_end, metrics,
        agent_display=agent["report_display"],
        agent_name=agent["display"],
        output_path=str(pdf_path),
    )
    log.info(f"  [{display}] weekly PDF written: {pdf_path.name}")

    # Email — management distribution only
    if not no_email:
        subject = f"Resumo Semanal - {display} - {range_label}"
        body = (f"Em anexo o resumo semanal (overview) das chamadas de {display} "
                f"referente à semana de {range_label}.")
        wk_to, wk_cc = weekly_recipients(agent)
        if email_override:
            to_list = [email_override]
            cc_list: List[str] = []
            subject = f"[SMOKE-TEST] {subject}"
        else:
            to_list = [wk_to]
            cc_list = wk_cc
        _smtp_send(to_list, cc_list, subject, body, attachment=pdf_path)
        log.info(f"  [{display}] weekly email sent to {to_list[0]}"
                 + (f" (override; orig={wk_to})" if email_override else f" cc={cc_list}"))
    else:
        log.info(f"  [{display}] --no-email: skipping send")

    # Upload artifacts to GCS
    if not no_gcs:
        remote_base = gcs_weekly_prefix(folder, week_label, gcs_prefix)
        gcs_upload_file(pdf_path, f"{remote_base}/{pdf_path.name}", content_type="application/pdf")
        gcs_write_json(f"{remote_base}/weekly_analysis.json", weekly)
        log.info(f"  [{display}] uploaded weekly artifacts to gs://.../{remote_base}/")
    else:
        log.info(f"  [{display}] --no-gcs: skipping upload")

    result["sent"] = not no_email
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly overview report pipeline")
    ap.add_argument("--week-start", help="Monday YYYY-MM-DD (default: previous week's Monday)")
    ap.add_argument("--agent", help="Restrict to one agent (name); must be real_estate")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-gcs", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--gcs-prefix", default="")
    ap.add_argument("--email-override", help="Redirect ALL emails to this address (smoke-test)")
    ap.add_argument("--reuse-stored", action="store_true",
                    help="Reuse weekly_analysis.json from the workdir instead of calling DeepSeek")
    args = ap.parse_args()

    run_date = datetime.now(TZ_LISBON).strftime("%Y-%m-%d")
    log_path = setup_weekly_logging(run_date)

    if args.week_start:
        ws = datetime.strptime(args.week_start, "%Y-%m-%d")
        ws = ws - timedelta(days=ws.weekday())  # snap to Monday
    else:
        today = datetime.now(TZ_LISBON).date()
        pm = previous_week_monday(today)
        ws = datetime(pm.year, pm.month, pm.day)
    days = [ws + timedelta(days=i) for i in range(5)]  # Mon–Fri

    agents = [a for a in AGENTS if a.get("business") in WEEKLY_BUSINESSES]
    if args.agent:
        agents = [a for a in agents if a["name"] == args.agent]
        if not agents:
            log.error(f"Unknown weekly agent: {args.agent}")
            return 2
    else:
        agents = [a for a in agents if a.get("enabled", True)]

    log.info(f"=== Weekly start | week={ws.strftime('%d-%m-%Y')}..{days[-1].strftime('%d-%m-%Y')} "
             f"| agents={len(agents)} | gcs_prefix='{args.gcs_prefix}' ===")
    hc_ping("/start")

    workdir_root = Path("/tmp/weekly") / ws.strftime("%Y-%m-%d")
    workdir_root.mkdir(parents=True, exist_ok=True)

    results = []
    pipeline_exc = None
    try:
        for agent in agents:
            workdir = workdir_root / agent["folder"]
            workdir.mkdir(parents=True, exist_ok=True)
            try:
                results.append(process_agent(
                    agent, ws, days, workdir,
                    no_email=args.no_email, no_gcs=args.no_gcs, dry_run=args.dry_run,
                    gcs_prefix=args.gcs_prefix, email_override=args.email_override,
                    reuse_stored=args.reuse_stored,
                ))
            except Exception as e:
                log.error(f"  [{agent['display']}] FAILED: {e}\n{traceback.format_exc()}")
                results.append({"agent": agent["display"], "error": str(e)})
    except Exception as e:
        pipeline_exc = e
        log.error(f"Global failure: {e}\n{traceback.format_exc()}")

    log.info("=== Weekly summary ===")
    for r in results:
        log.info(f"  {r}")

    if not args.no_gcs:
        try:
            base = args.gcs_prefix.strip("/")
            parts = [p for p in [base, "logs", f"weekly_{run_date}.log"] if p]
            gcs_upload_file(log_path, "/".join(parts), content_type="text/plain")
        except Exception as e:
            log.warning(f"weekly log upload failed: {e}")

    if pipeline_exc is None:
        hc_ping("")
        return 0
    hc_ping("/fail")
    return 1


if __name__ == "__main__":
    sys.exit(main())
