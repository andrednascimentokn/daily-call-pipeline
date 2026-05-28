#!/usr/bin/env python3
"""End-to-end daily call pipeline orchestrator (VPS/cron entry point).

Replaces the Mac + Claude Code hybrid flow (daily_fetch.py + DailyCalls.app/run
+ on-demand Claude orchestration). One deterministic Python script:

    1. fetch HubSpot calls (yesterday Europe/Lisbon)
    2. download audio → /tmp/pipeline/<date>/<agent_folder>/audio/
    3. split (ffmpeg) for files >25 MB
    4. transcribe with Whisper (parallel x5)
    5. analyze with DeepSeek v4-pro (single-shot, includes context.json history)
    6. merge coaching_insights into context.json
    7. build PDF (ReportLab)
    8. send email via Gmail SMTP
    9. upload everything to GCS (audio + transcripts + meta + analysis + pdf + context + log)
    10. cleanup /tmp/pipeline/<date>/

Per-agent failures are isolated: a crash in one agent does not block the others.
Global crashes trigger an alert email to PIPELINE_FAILURE_EMAILS with traceback.

Secrets layout:
    /etc/daily-calls/.env                — runtime env vars (chmod 600)
    /etc/daily-calls/gcs-sa.json         — GCS service account (chmod 600)

CLI:
    python3 run_pipeline.py                              # yesterday, all agents, prod
    python3 run_pipeline.py --date 2026-05-27            # specific date
    python3 run_pipeline.py --agent ruben                # restrict to one agent
    python3 run_pipeline.py --no-email                   # skip email send (staging)
    python3 run_pipeline.py --gcs-prefix=test/           # write under gs://<bucket>/test/...
    python3 run_pipeline.py --no-gcs                     # local-only (dev)
    python3 run_pipeline.py --dry-run                    # skip DeepSeek + PDF + email + upload
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import smtplib
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from openai import OpenAI

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

# ---------------------------------------------------------------------------
# Env loading — production lives in /etc/daily-calls/.env, dev falls back to repo .env
# ---------------------------------------------------------------------------
_PROD_ENV = Path("/etc/daily-calls/.env")
_DEV_ENV = Path(__file__).resolve().parent / ".env"
if _PROD_ENV.exists():
    load_dotenv(_PROD_ENV)
elif _DEV_ENV.exists():
    load_dotenv(_DEV_ENV)

from agents_config import AGENTS  # noqa: E402
from daily_fetch import (  # noqa: E402
    build_meta,
    fetch_and_transcribe,
    update_context_metrics,
)

TZ_LISBON = ZoneInfo("Europe/Lisbon")
PIPELINE_DIR = Path(__file__).resolve().parent
LOCAL_LOGS_DIR = PIPELINE_DIR / "logs"
LOCAL_LOGS_DIR.mkdir(exist_ok=True)

log = logging.getLogger("pipeline")


def hc_ping(suffix: str = "") -> None:
    """Best-effort ping to healthchecks.io. Silent on failure — never blocks pipeline."""
    url = os.getenv("HEALTHCHECK_URL", "").strip()
    if not url:
        return
    full = url.rstrip("/") + suffix
    try:
        requests.get(full, timeout=5)
        log.info(f"healthcheck ping{suffix or ' (success)'} → {full}")
    except Exception as e:
        log.warning(f"healthcheck ping failed ({full}): {e}")


def setup_logging(run_date: str) -> Path:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    log_path = LOCAL_LOGS_DIR / f"{run_date}.log"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log_path


# ---------------------------------------------------------------------------
# Email helpers — Gmail SMTP, same credentials as legacy send_email.py
# ---------------------------------------------------------------------------
def _smtp_send(to_list: List[str], cc_list: List[str], subject: str, body: str,
               attachment: Optional[Path] = None) -> None:
    user = os.getenv("GMAIL_USER", "")
    pwd = os.getenv("GMAIL_APP_PASSWORD", "")
    if not user or not pwd:
        raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD not set")

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachment is not None:
        att_path = Path(attachment)
        if not att_path.exists():
            raise FileNotFoundError(att_path)
        with open(att_path, "rb") as f:
            part = MIMEApplication(f.read(), Name=att_path.name)
        part["Content-Disposition"] = f'attachment; filename="{att_path.name}"'
        msg.attach(part)

    recipients = to_list + cc_list
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(user, pwd)
        server.sendmail(user, recipients, msg.as_string())


def send_agent_report(agent: Dict[str, Any], pdf_path: Path, date_label: str,
                      *, email_override: Optional[str] = None) -> None:
    subject = f"Call Report - {agent['display']} - {date_label}"
    body = f"Em anexo o report diário de chamadas de {agent['display']}."
    if email_override:
        to_list = [email_override]
        cc_list: List[str] = []
        subject = f"[SMOKE-TEST] {subject}"
    else:
        to_list = [agent["email_to"]]
        cc_list = agent.get("email_cc", [])
    _smtp_send(to_list, cc_list, subject, body, attachment=pdf_path)
    log.info(f"  [{agent['display']}] email sent to {to_list[0]}"
             + (f" (override; orig={agent['email_to']})" if email_override else ""))


def send_failure_alert(target_date: str, log_path: Path, tb: str) -> None:
    raw = os.getenv("PIPELINE_FAILURE_EMAILS", "")
    recipients = [e.strip() for e in raw.split(",") if e.strip()]
    if not recipients:
        log.warning("PIPELINE_FAILURE_EMAILS not set — skipping failure alert email")
        return

    tail = ""
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-200:])
        except Exception:
            tail = "(could not read log)"
    body = (
        f"Pipeline failed for target_date={target_date}\n\n"
        f"--- Traceback ---\n{tb}\n\n"
        f"--- Last 200 log lines ---\n{tail}\n"
    )
    try:
        _smtp_send(recipients, [], f"[PIPELINE FAILURE] daily calls — {target_date}", body)
        log.info(f"Failure alert email sent to {recipients}")
    except Exception as e:
        log.error(f"Could not send failure alert email: {e}")


# ---------------------------------------------------------------------------
# Per-agent flow
# ---------------------------------------------------------------------------
def gcs_data_prefix(agent: Dict[str, Any], date_label: str, gcs_prefix: str) -> str:
    base = gcs_prefix.strip("/")
    parts = [p for p in [base, "agents", agent["folder"], "data", date_label] if p]
    return "/".join(parts)


def gcs_context_path(agent: Dict[str, Any], gcs_prefix: str) -> str:
    base = gcs_prefix.strip("/")
    parts = [p for p in [base, "agents", agent["folder"], "context.json"] if p]
    return "/".join(parts)


def gcs_log_path(target_date: str, gcs_prefix: str) -> str:
    base = gcs_prefix.strip("/")
    parts = [p for p in [base, "logs", f"{target_date}.log"] if p]
    return "/".join(parts)


def process_agent(
    agent: Dict[str, Any],
    target_date: str,
    workdir_root: Path,
    openai_client: OpenAI,
    *,
    no_email: bool,
    no_gcs: bool,
    dry_run: bool,
    gcs_prefix: str,
    email_override: Optional[str] = None,
) -> Dict[str, Any]:
    display = agent["display"]
    folder = agent["folder"]
    report_date = datetime.strptime(target_date, "%Y-%m-%d")
    date_label = report_date.strftime("%d-%m-%Y")

    workdir = workdir_root / folder
    audio_dir = workdir / "audio"
    transcript_dir = workdir / "transcripts"
    workdir.mkdir(parents=True, exist_ok=True)

    log.info(f"--- [{display}] start ({target_date}) ---")

    # 1-4. fetch + download + transcribe
    calls = fetch_and_transcribe(agent, target_date, audio_dir, transcript_dir, openai_client)
    meta = build_meta(calls, target_date)
    meta_path = workdir / "calls_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    total = len(meta)
    with_rec = sum(1 for c in calls if c.get("recording_url"))
    transcribed = sum(1 for c in meta if c.get("has_transcript"))
    completed = sum(1 for c in meta if c.get("status") == "COMPLETED")
    log.info(f"  [{display}] meta — total={total} with_recording={with_rec} transcribed={transcribed} completed={completed}")

    # Load existing context from GCS (or local Mac copy as fallback in dev)
    context = load_context(agent, no_gcs=no_gcs, gcs_prefix=gcs_prefix)

    result: Dict[str, Any] = {
        "agent": display, "total": total, "with_recording": with_rec,
        "transcribed": transcribed, "completed": completed,
    }

    if transcribed == 0:
        log.info(f"  [{display}] no transcripts — skip analysis/PDF/email")
        update_context_metrics(context, agent, target_date, total, with_rec, transcribed, completed)
        if not no_gcs:
            upload_agent_artifacts(agent, workdir, date_label, context, gcs_prefix=gcs_prefix)
        result["skipped_analysis"] = True
        return result

    if dry_run:
        log.info(f"  [{display}] dry-run — stop after transcription")
        return result

    # 5. analyze with DeepSeek
    from deepseek_analysis import analyze, merge_coaching_insights  # local import to defer dep
    transcripts = {
        c["call_id"]: (transcript_dir / f"{c['call_id']}.txt").read_text(encoding="utf-8")
        for c in meta if c.get("has_transcript") and (transcript_dir / f"{c['call_id']}.txt").exists()
    }
    analysis, insights_update = analyze(agent, target_date, meta, transcripts, context)

    analysis_path = workdir / "analysis.json"
    analysis_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"  [{display}] analysis.json written ({analysis_path.stat().st_size} bytes)")

    # 6. update context.json
    update_context_metrics(context, agent, target_date, total, with_rec, transcribed, completed)
    context["coaching_insights"] = merge_coaching_insights(
        context.get("coaching_insights", {}), insights_update, date_label,
    )

    # 7. build PDF
    from build_report import generate_pdf  # local import to defer ReportLab
    pdf_path = workdir / f"{folder}_{date_label}.pdf"
    generate_pdf(
        analysis,
        report_date,
        total_calls=total,
        completed_calls=completed,
        agent_display=agent["report_display"],
        reports_dir=workdir,
        agent_name=agent["display"],
        output_path=pdf_path,
    )
    log.info(f"  [{display}] PDF written: {pdf_path.name}")

    # 8. email
    if not no_email:
        send_agent_report(agent, pdf_path, date_label, email_override=email_override)
    else:
        log.info(f"  [{display}] --no-email: skipping send")

    # 9. upload to GCS
    if not no_gcs:
        upload_agent_artifacts(agent, workdir, date_label, context, gcs_prefix=gcs_prefix)
    else:
        log.info(f"  [{display}] --no-gcs: skipping upload")

    result["analysis_ok"] = True
    return result


def load_context(agent: Dict[str, Any], *, no_gcs: bool, gcs_prefix: str) -> Dict[str, Any]:
    """Fetch context.json from GCS (or the Mac legacy path as dev fallback)."""
    if not no_gcs:
        try:
            from gcs_storage import gcs_read_json
            remote_ctx = gcs_read_json(gcs_context_path(agent, gcs_prefix))
            if remote_ctx is not None:
                return remote_ctx
        except Exception as e:
            log.warning(f"  [{agent['display']}] GCS context fetch failed: {e}. Falling back to local copy.")

    local_ctx = agent["data_dir"].parent / "context.json"
    if local_ctx.exists():
        return json.loads(local_ctx.read_text(encoding="utf-8"))

    return {
        "agent": agent["display"],
        "last_updated": "",
        "metrics_history": [],
        "coaching_insights": {"strengths": [], "weaknesses": [], "patterns": [], "evolution": ""},
    }


def upload_agent_artifacts(agent: Dict[str, Any], workdir: Path, date_label: str,
                           context: Dict[str, Any], *, gcs_prefix: str) -> None:
    from gcs_storage import gcs_upload_folder, gcs_write_json
    remote_data = gcs_data_prefix(agent, date_label, gcs_prefix)
    uploaded = gcs_upload_folder(workdir, remote_data)
    log.info(f"  [{agent['display']}] uploaded {uploaded} files to gs://.../{remote_data}/")
    gcs_write_json(gcs_context_path(agent, gcs_prefix), context)
    log.info(f"  [{agent['display']}] context.json uploaded")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: yesterday Europe/Lisbon)")
    ap.add_argument("--agent", help="Restringe a um agente (name)")
    ap.add_argument("--no-email", action="store_true", help="Não enviar email (staging)")
    ap.add_argument("--no-gcs", action="store_true", help="Não fazer upload para GCS (dev)")
    ap.add_argument("--dry-run", action="store_true", help="Parar após transcrição (não chamar DeepSeek nem PDF/email/upload)")
    ap.add_argument("--gcs-prefix", default="", help="Prefixo dentro do bucket (ex: 'test/'). Default: raiz.")
    ap.add_argument("--keep-workdir", action="store_true", help="Não fazer cleanup do /tmp/pipeline/<date>/")
    ap.add_argument("--email-override", help="Redirecciona TODOS os emails para este endereço (smoke-test)")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if args.date:
        target = args.date
    else:
        yesterday = datetime.now(TZ_LISBON) - timedelta(days=1)
        target = yesterday.strftime("%Y-%m-%d")

    run_date = datetime.now(TZ_LISBON).strftime("%Y-%m-%d")
    log_path = setup_logging(run_date)

    agents = AGENTS if not args.agent else [a for a in AGENTS if a["name"] == args.agent]
    if not agents:
        log.error(f"Unknown agent: {args.agent}")
        return 1

    if not os.getenv("HUBSPOT_TOKEN"):
        log.error("HUBSPOT_TOKEN not set"); return 1
    if not os.getenv("OPENAI_API_KEY"):
        log.error("OPENAI_API_KEY not set"); return 1
    if not args.dry_run and not os.getenv("DEEPSEEK_API_KEY"):
        log.error("DEEPSEEK_API_KEY not set (required unless --dry-run)"); return 1
    if not args.no_gcs and not os.getenv("GCS_BUCKET"):
        log.error("GCS_BUCKET not set (use --no-gcs for local dev)"); return 1

    log.info(f"=== Pipeline start | target_date={target} | agents={len(agents)} | gcs_prefix='{args.gcs_prefix}' ===")
    hc_ping("/start")
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    # Workdir under /tmp (Linux) or system tempdir (Mac dev)
    workdir_root = Path(tempfile.gettempdir()) / "pipeline" / target
    workdir_root.mkdir(parents=True, exist_ok=True)
    log.info(f"workdir={workdir_root}")

    summary: List[Dict[str, Any]] = []
    pipeline_exc: Optional[BaseException] = None
    try:
        for agent in agents:
            try:
                summary.append(process_agent(
                    agent, target, workdir_root, openai_client,
                    no_email=args.no_email,
                    no_gcs=args.no_gcs,
                    dry_run=args.dry_run,
                    gcs_prefix=args.gcs_prefix,
                    email_override=args.email_override,
                ))
            except Exception as e:
                log.exception(f"[{agent['display']}] failed: {e}")
                summary.append({"agent": agent["display"], "error": str(e)})
    except BaseException as e:
        pipeline_exc = e
        log.exception(f"Pipeline crashed: {e}")
    finally:
        log.info("=== Summary ===")
        for s in summary:
            log.info(f"  {s}")

        # Upload log file to GCS
        if not args.no_gcs:
            try:
                from gcs_storage import gcs_upload_file
                gcs_upload_file(log_path, gcs_log_path(target, args.gcs_prefix), content_type="text/plain")
                log.info(f"log uploaded to gs://.../{gcs_log_path(target, args.gcs_prefix)}")
            except Exception as e:
                log.error(f"log upload failed: {e}")

        # Cleanup workdir (only if no global exception, to allow manual recovery)
        if pipeline_exc is None and not args.keep_workdir:
            try:
                shutil.rmtree(workdir_root)
                log.info(f"cleaned up {workdir_root}")
            except Exception as e:
                log.warning(f"cleanup failed: {e}")
        elif pipeline_exc is not None:
            log.warning(f"keeping {workdir_root} (pipeline crashed — manual recovery possible)")

        # Healthcheck end-ping + optional failure alert
        if pipeline_exc is None:
            hc_ping("")
        else:
            hc_ping("/fail")
            send_failure_alert(target, log_path, "".join(traceback.format_exception(pipeline_exc)))

    return 0 if pipeline_exc is None else 1


if __name__ == "__main__":
    sys.exit(main())
