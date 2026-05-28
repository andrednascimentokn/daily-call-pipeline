#!/usr/bin/env python3
"""One-shot upload of the local ~9.7 GB history to GCS.

Walks each agent in agents_config and replicates the on-disk layout to GCS:
    agents/<Agent>_Call_Report/data/<dd-mm-yyyy>/audio/*.mp3
    agents/<Agent>_Call_Report/data/<dd-mm-yyyy>/transcripts/*.txt
    agents/<Agent>_Call_Report/data/<dd-mm-yyyy>/calls_meta.json
    agents/<Agent>_Call_Report/data/<dd-mm-yyyy>/analysis.json (if present)
    agents/<Agent>_Call_Report/data/<dd-mm-yyyy>/*.pdf (if present)
    agents/<Agent>_Call_Report/context.json

Idempotent — skips any object that already exists in the bucket. Safe to re-run.

Usage:
    GCS_BUCKET=... GOOGLE_APPLICATION_CREDENTIALS=... python3 migrate_history_to_gcs.py
    # or restrict scope:
    python3 migrate_history_to_gcs.py --agent ruben --since 2026-05-01
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from agents_config import AGENTS, BASE

load_dotenv(BASE / ".env")

log = logging.getLogger("migrate")


def _date_label_within(date_label: str, since: Optional[datetime]) -> bool:
    if since is None:
        return True
    try:
        d = datetime.strptime(date_label, "%d-%m-%Y")
    except ValueError:
        return False
    return d >= since


def _iter_date_dirs(data_dir: Path):
    if not data_dir.exists():
        return
    for entry in sorted(data_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Skip non-date folders (eg. legacy `audio/`, `transcripts/` shared caches)
        try:
            datetime.strptime(entry.name, "%d-%m-%Y")
        except ValueError:
            continue
        yield entry


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", help="Restringe a um agente (name)")
    ap.add_argument("--since", help="Apenas datas >= YYYY-MM-DD")
    ap.add_argument("--prefix", default="", help="Prefixo no bucket (ex: 'test/')")
    ap.add_argument("--force", action="store_true", help="Re-upload mesmo que já exista")
    args = ap.parse_args()

    if not os.getenv("GCS_BUCKET"):
        log.error("GCS_BUCKET not set")
        return 1
    if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        log.warning("GOOGLE_APPLICATION_CREDENTIALS not set — relying on ambient google-auth credentials")

    from gcs_storage import gcs_upload_folder, gcs_upload_file, gcs_exists  # noqa: E402

    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    agents = AGENTS if not args.agent else [a for a in AGENTS if a["name"] == args.agent]
    if not agents:
        log.error(f"Unknown agent: {args.agent}")
        return 1
    prefix = args.prefix.strip("/")

    total_files = 0
    for agent in agents:
        folder = agent["folder"]
        log.info(f"=== {agent['display']} ({folder}) ===")
        agent_root = agent["data_dir"].parent

        # context.json
        ctx = agent_root / "context.json"
        if ctx.exists():
            remote_ctx = "/".join(p for p in [prefix, "agents", folder, "context.json"] if p)
            if args.force or not gcs_exists(remote_ctx):
                gcs_upload_file(ctx, remote_ctx, content_type="application/json")
                log.info(f"  context.json -> {remote_ctx}")
                total_files += 1
            else:
                log.info(f"  context.json (already present)")

        # data/<date>/
        for date_dir in _iter_date_dirs(agent["data_dir"]):
            if not _date_label_within(date_dir.name, since):
                continue
            remote_data = "/".join(p for p in [prefix, "agents", folder, "data", date_dir.name] if p)
            uploaded = gcs_upload_folder(date_dir, remote_data, skip_existing=not args.force)
            log.info(f"  {date_dir.name}: uploaded {uploaded} files")
            total_files += uploaded

    log.info(f"=== Done. {total_files} files uploaded ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
