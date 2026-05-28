#!/usr/bin/env python3
"""Send an email with optional PDF attachment via Gmail SMTP.

Usage:
    python3 send_email.py \
        --to sandra@realestateinvest.pt \
        --cc andre.nascimento@karma-network.com,luis.correia@karma-network.com \
        --subject "Call Report - Rúben - 08-05-2026" \
        --body "Em anexo o report diário de chamadas." \
        --attachment agents/Ruben_Call_Report/reports/Ruben_Call_Report_08-05-2026.pdf

Credentials via .env: GMAIL_USER, GMAIL_APP_PASSWORD
"""
from __future__ import annotations

import argparse
import os
import smtplib
import sys
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")


def send_email(to: str, subject: str, body: str,
               attachment: str | None = None,
               cc: list[str] | None = None) -> None:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("Error: GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env")
        sys.exit(1)

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachment:
        att_path = Path(attachment)
        if not att_path.exists():
            print(f"Error: attachment not found: {att_path}")
            sys.exit(1)
        with open(att_path, "rb") as f:
            part = MIMEApplication(f.read(), Name=att_path.name)
        part["Content-Disposition"] = f'attachment; filename="{att_path.name}"'
        msg.attach(part)

    recipients = [to] + (cc or [])
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())

    cc_info = f" (cc: {', '.join(cc)})" if cc else ""
    print(f"Email sent to {to}{cc_info}: {subject}")


def main():
    parser = argparse.ArgumentParser(description="Send email via Gmail SMTP")
    parser.add_argument("--to", required=True, help="Recipient email")
    parser.add_argument("--cc", help="CC emails, comma-separated")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", required=True, help="Email body text")
    parser.add_argument("--attachment", help="Path to file to attach (e.g. PDF)")
    args = parser.parse_args()

    cc_list = [e.strip() for e in args.cc.split(",") if e.strip()] if args.cc else None
    send_email(args.to, args.subject, args.body, args.attachment, cc_list)


if __name__ == "__main__":
    main()
