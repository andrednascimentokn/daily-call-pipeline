#!/usr/bin/env python3
"""HubSpot fetch + Whisper transcription utilities.

Originally a standalone macOS launchd entry point, this module is now also
imported by `run_pipeline.py` (Linux VPS cron). The orchestration helpers
(`process_agent`, `main`) keep working for the legacy Mac flow that writes
into `agents/<X>/data/<date>/`; new code on the VPS should call the lower-level
utilities (`fetch_calls_for_date`, `download_recording`, `transcribe_audio`,
etc.) directly with an explicit `workdir`.

Idempotent: dias já com `has_transcript=True` em qualquer entrada são saltados.

Uso manual (legado Mac):
    python3 daily_fetch.py                  # processa ontem para todos os agentes
    python3 daily_fetch.py --date 2026-05-08 # força uma data específica
    python3 daily_fetch.py --agent ruben     # restringe a um agente
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv
from openai import OpenAI

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

from agents_config import AGENTS, BASE, FFMPEG_BIN, FFPROBE_BIN

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PIPELINE_DIR = Path(__file__).resolve().parent
LOGS_DIR = PIPELINE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

load_dotenv(BASE / ".env")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

HUBSPOT_API = "https://api.hubapi.com"
CALL_PROPERTIES = [
    "hs_call_title", "hs_call_body", "hs_call_duration",
    "hs_call_recording_url", "hs_call_status", "hs_call_direction",
    "hs_call_from_number", "hs_call_to_number", "hs_timestamp",
    "hubspot_owner_id", "hs_call_disposition",
]
MAX_WHISPER_BYTES = 25 * 1024 * 1024
DOWNLOAD_TIMEOUT_SEC = 180
TZ_LISBON = ZoneInfo("Europe/Lisbon")

log = logging.getLogger("daily_fetch")


def setup_logging(run_date: str) -> None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = logging.FileHandler(LOGS_DIR / f"{run_date}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------
def hubspot_headers(token: Optional[str] = None) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token or HUBSPOT_TOKEN}", "Content-Type": "application/json"}


def fetch_calls_for_date(owner_id: str, date_str: str, token: Optional[str] = None) -> list:
    start = f"{date_str}T00:00:00.000Z"
    end = f"{date_str}T23:59:59.999Z"
    all_calls: list = []
    after = 0
    while True:
        payload = {
            "filterGroups": [{"filters": [
                {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id},
                {"propertyName": "hs_timestamp", "operator": "GTE", "value": start},
                {"propertyName": "hs_timestamp", "operator": "LTE", "value": end},
            ]}],
            "properties": CALL_PROPERTIES,
            "sorts": [{"propertyName": "hs_timestamp", "direction": "DESCENDING"}],
            "limit": 100,
            "after": after,
        }
        resp = requests.post(
            f"{HUBSPOT_API}/crm/v3/objects/calls/search",
            headers=hubspot_headers(token), json=payload, timeout=30,
        )
        if resp.status_code != 200:
            log.error(f"HubSpot API error: {resp.status_code} — {resp.text[:200]}")
            break
        data = resp.json()
        all_calls.extend(data.get("results", []))
        next_after = data.get("paging", {}).get("next", {}).get("after")
        if not next_after:
            break
        after = next_after
        time.sleep(0.2)
    return all_calls


def get_contact_name(call_id: str, token: Optional[str] = None) -> Optional[str]:
    try:
        resp = requests.get(
            f"{HUBSPOT_API}/crm/v3/objects/calls/{call_id}/associations/contacts",
            headers=hubspot_headers(token), timeout=15,
        )
        if resp.status_code != 200:
            return None
        results = resp.json().get("results", [])
        if not results:
            return None
        contact_id = results[0].get("id")
        if not contact_id:
            return None
        resp2 = requests.get(
            f"{HUBSPOT_API}/crm/v3/objects/contacts/{contact_id}",
            headers=hubspot_headers(token), timeout=15,
            params={"properties": "firstname,lastname"},
        )
        if resp2.status_code != 200:
            return None
        props = resp2.json().get("properties", {})
        first = props.get("firstname", "") or ""
        last = props.get("lastname", "") or ""
        name = f"{first} {last}".strip()
        return name if name else None
    except Exception as e:
        log.warning(f"Contact lookup failed for {call_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------
def _stream_download(rec_url: str, audio_path: Path, token: Optional[str] = None) -> int:
    """Stream a URL to disk. Returns HTTP status or 0 on connection failure."""
    resp = requests.get(rec_url, headers=hubspot_headers(token), timeout=(15, 60), stream=True)
    if resp.status_code in (401, 403):
        resp = requests.get(rec_url, timeout=(15, 60), stream=True)
    status = resp.status_code
    if status == 200:
        with open(audio_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    return status


def download_recording(call_id: str, rec_url: str, audio_dir: Path, token: Optional[str] = None) -> Optional[str]:
    """Download an MP3 with an absolute 180s cap (cross-platform via thread + future)."""
    audio_path = audio_dir / f"{call_id}.mp3"
    if audio_path.exists():
        return str(audio_path)
    if not rec_url:
        return None
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_stream_download, rec_url, audio_path, token)
        try:
            status = future.result(timeout=DOWNLOAD_TIMEOUT_SEC)
        except FutureTimeoutError:
            log.warning(f"    {call_id} download skipped — exceeded {DOWNLOAD_TIMEOUT_SEC}s total timeout")
            if audio_path.exists():
                audio_path.unlink()
            return None
        except Exception as e:
            log.warning(f"    {call_id} download error: {e}")
            if audio_path.exists():
                audio_path.unlink()
            return None
    if status == 200:
        mb = audio_path.stat().st_size / (1024 * 1024)
        log.info(f"    {call_id} downloaded ({mb:.1f} MB)")
        return str(audio_path)
    log.warning(f"    {call_id} download HTTP {status}")
    if audio_path.exists():
        audio_path.unlink()
    return None


def split_if_needed(audio_path: str, audio_dir: Path) -> List[str]:
    size = os.path.getsize(audio_path)
    if size <= MAX_WHISPER_BYTES:
        return [audio_path]
    stem = Path(audio_path).stem
    try:
        dur_out = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", audio_path],
            capture_output=True, text=True, check=True, timeout=60,
        )
        duration = float(dur_out.stdout.strip())
    except Exception as e:
        log.warning(f"    ffprobe failed: {e} — skipping split")
        return [audio_path]
    half = duration / 2
    parts: List[str] = []
    for idx, ss in enumerate([0, half]):
        part_path = str(audio_dir / f"{stem}_part{idx+1}.mp3")
        if not os.path.exists(part_path):
            subprocess.run([
                FFMPEG_BIN, "-y", "-i", audio_path, "-ss", str(ss), "-t", str(half),
                "-acodec", "libmp3lame", "-b:a", "64k", part_path,
            ], capture_output=True, timeout=120)
        parts.append(part_path)
    return parts


def transcribe_audio(audio_path: str, transcript_dir: Path, client: OpenAI) -> Optional[str]:
    transcript_path = transcript_dir / f"{Path(audio_path).stem}.txt"
    if transcript_path.exists():
        return transcript_path.read_text(encoding="utf-8")
    try:
        parts = split_if_needed(audio_path, Path(audio_path).parent)
        full_text: List[str] = []
        for part in parts:
            with open(part, "rb") as f:
                result = client.audio.transcriptions.create(
                    model="whisper-1", file=f, language="pt", response_format="text",
                    timeout=300,  # 5 min max per Whisper request
                )
            full_text.append(result)
        text = " ".join(full_text).strip()
        if text:
            transcript_path.write_text(text, encoding="utf-8")
        return text or None
    except Exception as e:
        log.error(f"    Transcription failed for {Path(audio_path).name}: {e}")
        return None


def _transcribe_one(call: dict, transcript_dir: Path, client: OpenAI) -> dict:
    """Transcribe a single call — designed to run in a thread pool."""
    call_id = call["call_id"]
    audio_path = call.get("audio_path")
    if not audio_path:
        return {"call_id": call_id, "has_transcript": False, "transcript_words": 0}
    text = transcribe_audio(audio_path, transcript_dir, client)
    has = bool(text and len(text) > 50)
    words = len(text.split()) if text else 0
    if has:
        log.info(f"    {call_id} transcribed ({words} words)")
    return {"call_id": call_id, "has_transcript": has, "transcript_words": words}


# ---------------------------------------------------------------------------
# Higher-level helpers used by both Mac and VPS flows
# ---------------------------------------------------------------------------
def normalize_calls(raw_calls: list) -> List[dict]:
    """Flatten HubSpot's raw call payload into the lightweight dicts the pipeline uses."""
    out: List[dict] = []
    for rc in raw_calls:
        props = rc.get("properties", {})
        dur_ms = int(props.get("hs_call_duration") or 0)
        dur_sec = dur_ms // 1000
        out.append({
            "call_id": rc["id"],
            "status": props.get("hs_call_status", ""),
            "direction": props.get("hs_call_direction", ""),
            "duration_sec": dur_sec,
            "duration_str": f"{round(dur_sec / 60, 1)} min",
            "recording_url": props.get("hs_call_recording_url", ""),
            "timestamp": props.get("hs_timestamp", ""),
            "title": props.get("hs_call_title", ""),
        })
    return out


def fetch_and_transcribe(
    agent: dict,
    date_str: str,
    audio_dir: Path,
    transcript_dir: Path,
    openai_client: OpenAI,
    *,
    max_workers: int = 5,
) -> List[dict]:
    """Fetch + download + transcribe for one agent/day into an explicit workdir.

    Returns the list of calls (with `audio_path`, `has_transcript`, `transcript_words`,
    `contact_name`). Caller is responsible for writing calls_meta.json.
    """
    audio_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    token = os.getenv(agent["hubspot_token_env"]) if agent.get("hubspot_token_env") else HUBSPOT_TOKEN
    raw = fetch_calls_for_date(agent["owner_id"], date_str, token)
    calls = normalize_calls(raw)
    with_rec = [c for c in calls if c["recording_url"]]
    log.info(f"  [{agent['display']}] {len(calls)} calls, {len(with_rec)} with recordings")

    for c in with_rec:
        c["contact_name"] = get_contact_name(c["call_id"], token) or c.get("title", f"Call {c['call_id']}")
        time.sleep(0.15)

    for c in with_rec:
        c["audio_path"] = download_recording(c["call_id"], c["recording_url"], audio_dir, token)

    to_transcribe = [c for c in with_rec if c.get("audio_path")]
    results_map: Dict[str, dict] = {}
    if to_transcribe:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_transcribe_one, c, transcript_dir, openai_client): c["call_id"]
                for c in to_transcribe
            }
            for future in as_completed(futures):
                res = future.result()
                results_map[res["call_id"]] = res
    for c in with_rec:
        res = results_map.get(c["call_id"])
        c["has_transcript"] = res["has_transcript"] if res else False
        c["transcript_words"] = res["transcript_words"] if res else 0

    # Calls without recordings carry default flags
    for c in calls:
        c.setdefault("has_transcript", False)
        c.setdefault("transcript_words", 0)
        c.setdefault("contact_name", c.get("title", ""))
    return calls


def build_meta(calls: List[dict], date_str: str) -> List[dict]:
    """Project the in-memory call list into the calls_meta.json schema."""
    return [{
        "call_id": c["call_id"],
        "contact_name": c.get("contact_name", c.get("title", "")),
        "status": c.get("status", ""),
        "direction": c.get("direction", ""),
        "duration_sec": c.get("duration_sec", 0),
        "duration_str": c.get("duration_str", ""),
        "timestamp": c.get("timestamp", ""),
        "has_transcript": c.get("has_transcript", False),
        "transcript_words": c.get("transcript_words", 0),
        "date": date_str,
    } for c in calls]


# ---------------------------------------------------------------------------
# Legacy Mac flow — writes into agents/<X>/data/<date>/
# ---------------------------------------------------------------------------
def _update_context(agent: dict, date_str: str, total: int, with_rec: int,
                    transcribed: int, completed: int) -> None:
    """Append daily metrics to the agent's context.json (file-based, Mac legacy path)."""
    context_path = agent["data_dir"].parent / "context.json"
    report_date = datetime.strptime(date_str, "%Y-%m-%d")
    date_label = report_date.strftime("%d-%m-%Y")

    if context_path.exists():
        ctx = json.loads(context_path.read_text(encoding="utf-8"))
    else:
        ctx = {
            "agent": agent["display"],
            "last_updated": "",
            "metrics_history": [],
            "coaching_insights": {"strengths": [], "weaknesses": [], "patterns": [], "evolution": ""},
        }

    existing_dates = {e["date"] for e in ctx.get("metrics_history", [])}
    if date_label not in existing_dates:
        ctx["metrics_history"].append({
            "date": date_label,
            "total_calls": total,
            "with_recording": with_rec,
            "transcribed": transcribed,
            "completed_calls": completed,
        })

    ctx["last_updated"] = datetime.now(TZ_LISBON).strftime("%Y-%m-%d")
    context_path.write_text(json.dumps(ctx, indent=2, ensure_ascii=False), encoding="utf-8")


def update_context_metrics(ctx: dict, agent: dict, date_str: str, total: int, with_rec: int,
                           transcribed: int, completed: int) -> dict:
    """Pure variant of `_update_context` for the VPS flow.

    Mutates and returns the passed-in context dict, appending a metrics entry for
    `date_str` if not already present and bumping `last_updated`.
    """
    report_date = datetime.strptime(date_str, "%Y-%m-%d")
    date_label = report_date.strftime("%d-%m-%Y")

    ctx.setdefault("agent", agent["display"])
    ctx.setdefault("metrics_history", [])
    ctx.setdefault("coaching_insights", {"strengths": [], "weaknesses": [], "patterns": [], "evolution": ""})

    existing_dates = {e["date"] for e in ctx["metrics_history"]}
    if date_label not in existing_dates:
        ctx["metrics_history"].append({
            "date": date_label,
            "total_calls": total,
            "with_recording": with_rec,
            "transcribed": transcribed,
            "completed_calls": completed,
        })
    ctx["last_updated"] = datetime.now(TZ_LISBON).strftime("%Y-%m-%d")
    return ctx


def process_agent(agent: dict, date_str: str, openai_client: OpenAI) -> dict:
    """Legacy Mac flow — writes into agents/<X>/data/<date>/."""
    display = agent["display"]
    base_dir = agent["data_dir"]

    report_date = datetime.strptime(date_str, "%Y-%m-%d")
    date_label = report_date.strftime("%d-%m-%Y")
    data_dir = base_dir / date_label
    audio_dir = data_dir / "audio"
    transcript_dir = data_dir / "transcripts"
    for d in [data_dir, audio_dir, transcript_dir]:
        d.mkdir(parents=True, exist_ok=True)
    meta_path = data_dir / "calls_meta.json"

    if meta_path.exists():
        existing = json.loads(meta_path.read_text(encoding="utf-8"))
        if any(c.get("has_transcript") for c in existing):
            log.info(f"  [{display}] already processed ({len(existing)} calls) — skip")
            return {"agent": display, "skipped": True, "total": len(existing)}

    log.info(f"  [{display}] fetching {date_str}…")
    calls = fetch_and_transcribe(agent, date_str, audio_dir, transcript_dir, openai_client)
    if not calls:
        meta_path.write_text("[]", encoding="utf-8")
        log.info(f"  [{display}] 0 calls (empty day)")
        return {"agent": display, "total": 0, "with_recording": 0, "transcribed": 0}

    meta = build_meta(calls, date_str)
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    with_rec = sum(1 for c in calls if c.get("recording_url"))
    transcribed_count = sum(1 for c in calls if c.get("has_transcript"))
    completed_calls = sum(1 for c in calls if c.get("status") == "COMPLETED")
    _update_context(agent, date_str, len(calls), with_rec, transcribed_count, completed_calls)

    log.info(f"  [{display}] done — transcribed {transcribed_count}/{with_rec}")
    return {
        "agent": display, "total": len(calls),
        "with_recording": with_rec, "transcribed": transcribed_count,
    }


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: yesterday Europe/Lisbon)")
    ap.add_argument("--agent", help="Restringe a um agente (name): ruben/david/sandra/emanuel")
    return ap.parse_args()


def macos_notify(title: str, body: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def main():
    args = parse_args()
    if args.date:
        target = args.date
    else:
        yesterday = datetime.now(TZ_LISBON) - timedelta(days=1)
        target = yesterday.strftime("%Y-%m-%d")

    run_date = datetime.now(TZ_LISBON).strftime("%Y-%m-%d")
    setup_logging(run_date)

    if not HUBSPOT_TOKEN:
        log.error("HUBSPOT_TOKEN not set")
        sys.exit(1)
    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY not set")
        sys.exit(1)

    agents = AGENTS if not args.agent else [a for a in AGENTS if a["name"] == args.agent]
    if not agents:
        log.error(f"Unknown agent: {args.agent}")
        sys.exit(1)

    log.info(f"=== Daily call pipeline — target date {target} — {len(agents)} agent(s) ===")
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

    summary = []
    for agent in agents:
        try:
            summary.append(process_agent(agent, target, openai_client))
        except Exception as e:
            log.exception(f"[{agent['display']}] failed: {e}")
            summary.append({"agent": agent["display"], "error": str(e)})

    log.info("=== Summary ===")
    for s in summary:
        log.info(f"  {s}")

    macos_notify("Daily call pipeline", f"{target} done — {len(summary)} agents")


if __name__ == "__main__":
    main()
