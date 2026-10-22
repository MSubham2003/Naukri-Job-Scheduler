"""HTML email digest + local report artifact.

The digest is always written to ``reports/digest_<timestamp>.html`` so the run
is inspectable even when SMTP is not configured (dry runs, local usage).
"""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .logging_setup import get_logger

logger = get_logger("notifier")

REPORT_DIR = Path("reports")


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def _mail_settings() -> Optional[Dict[str, Any]]:
    sender = _env("SMTP_FROM", "SENDER_EMAIL")
    password = _env("SMTP_PASSWORD", "SENDER_APP_PASSWORD")
    recipients_raw = _env("MAIL_TO", "RECIPIENT_EMAIL")
    if not (sender and password and recipients_raw):
        return None

    recipients = [r.strip() for r in recipients_raw.replace(";", ",").split(",") if r.strip()]
    if not recipients:
        return None

    host = _env("SMTP_HOST", default="smtp.gmail.com")
    try:
        port = int(_env("SMTP_PORT", default="465"))
    except ValueError:
        port = 465
    # Infer the transport from the port unless SMTP_SSL is set explicitly:
    # 465 is implicit TLS, 587/25 need STARTTLS. Using SSL on a STARTTLS port
    # fails with "WRONG_VERSION_NUMBER".
    explicit_ssl = _env("SMTP_SSL")
    if explicit_ssl is None:
        use_ssl = port == 465
    else:
        use_ssl = explicit_ssl.lower() not in {"0", "false", "no"}
    username = _env("SMTP_USER", default=sender)
    return {
        "sender": sender,
        "password": password,
        "recipients": recipients,
        "host": host,
        "port": port,
        "use_ssl": use_ssl,
        "username": username,
        # Credentials are tried in this order; both naming schemes are common
        # and only one of them is usually a valid app password.
        "credential_candidates": _credential_candidates(username, password, sender),
    }


def _credential_candidates(username: str, password: str, sender: str):
    candidates = [(username, password, "SMTP_USER/SMTP_PASSWORD")]
    legacy_user = _env("SENDER_EMAIL", default=sender)
    legacy_password = _env("SENDER_APP_PASSWORD")
    if legacy_password and legacy_password != password:
        candidates.append((legacy_user, legacy_password, "SENDER_EMAIL/SENDER_APP_PASSWORD"))
    return [c for c in candidates if c[0] and c[1]]


def _transport_options(port: int, use_ssl: bool):
    """Preferred transport first, then the alternative (some networks block 587)."""
    options = [(port, use_ssl)]
    if use_ssl:
        options.append((587, False))
    else:
        options.append((465, True))
    return options


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _link(url: Any, label: str) -> str:
    raw = "" if url is None else str(url)
    if not raw:
        return "-"
    return f'<a href="{_escape(raw)}">{_escape(label)}</a>'


def _rows(jobs: List[Dict[str, Any]], columns: List[Tuple[str, str]], empty: str) -> str:
    if not jobs:
        return f"<tr><td colspan='{len(columns)}'>{_escape(empty)}</td></tr>"
    out = []
    for job in jobs:
        cells = []
        for key, kind in columns:
            if kind == "company":
                cells.append(f"<td><b>{_escape(job.get(key, ''))}</b></td>")
            elif kind == "link":
                cells.append(f"<td>{_link(job.get(key), 'View job')}</td>")
            elif kind == "danger":
                cells.append(f"<td style='color:#c5221f;'><b>{_escape(job.get(key, ''))}</b></td>")
            else:
                cells.append(f"<td>{_escape(job.get(key, ''))}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "\n".join(out)


def build_digest(report: Dict[str, Any]) -> str:
    applied = report.get("applied", [])
    skipped_external = report.get("skipped_external", [])
    skipped_unanswered = report.get("skipped_unanswered", [])
    skipped_mismatch = report.get("skipped_mismatch", [])
    errors = report.get("errors", [])
    pending_questions = report.get("pending_questions", [])
    run_id = report.get("run_id", datetime.now().strftime("%Y-%m-%d %H:%M"))

    total_scanned = report.get("total_scanned")
    if total_scanned is None:
        total_scanned = (
            len(applied) + len(skipped_external) + len(skipped_unanswered) + len(skipped_mismatch)
        )

    profile_line = "Yes" if report.get("profile_refreshed") else "No"
    if report.get("profile_steps"):
        profile_line += f" ({_escape(report['profile_steps'])})"
    if report.get("dry_run"):
        profile_line += " - DRY RUN"
    duration = report.get("duration_seconds")

    pending_rows = ""
    for entry in pending_questions:
        question = entry.get("question", "") if isinstance(entry, dict) else str(entry)
        asked = entry.get("times_asked", "") if isinstance(entry, dict) else ""
        pending_rows += (
            f"<tr><td>{_escape(question)}</td><td>{_escape(asked)}</td></tr>"
        )
    if not pending_rows:
        pending_rows = "<tr><td colspan='2'>None - every question was answered.</td></tr>"

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #202124; background: #fff; }}
    h3 {{ margin: 24px 0 8px; font-size: 16px; }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 16px; }}
    th, td {{ border: 1px solid #e0e3e7; padding: 9px; text-align: left; font-size: 13px; vertical-align: top; }}
    th {{ background: #f6f8fa; font-weight: 600; }}
    .header {{ background: #0b57d0; color: #fff; padding: 16px; border-radius: 6px; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; }}
    .ok {{ background: #e6f4ea; color: #137333; }}
    .warn {{ background: #fef7e0; color: #b06000; }}
    .bad {{ background: #fce8e6; color: #c5221f; }}
    .muted {{ color: #5f6368; font-size: 12px; }}
  </style>
</head>
<body>
  <div class="header">
    <h2 style="margin:0;">Naukri Application Digest</h2>
    <p style="margin:6px 0 0;font-size:13px;">
      Run {_escape(run_id)} | Applied <b>{len(applied)}</b> | Scanned {total_scanned}
      | Profile refreshed: {profile_line}
      {f"| Duration {duration:.0f}s" if isinstance(duration, (int, float)) else ""}
    </p>
  </div>

  <h3><span class="badge ok">Applied</span> ({len(applied)})</h3>
  <table>
    <tr><th>Company</th><th>Role</th><th>Score</th><th>Naukri Link</th></tr>
    {_rows(applied, [("company", "company"), ("title", "text"), ("score", "text"), ("naukri_url", "link")], "No applications this run.")}
  </table>

  <h3><span class="badge warn">Needs training</span> Unanswered mandatory questions ({len(skipped_unanswered)})</h3>
  <table>
    <tr><th>Company</th><th>Role</th><th>Unanswered question</th><th>Job</th></tr>
    {_rows(skipped_unanswered, [("company", "company"), ("title", "text"), ("question", "danger"), ("naukri_url", "link")], "None - all screener questions were answered.")}
  </table>
  <p class="muted">
    Add an answer once in <code>config/qa_memory.json</code> (or
    <code>custom_answers</code> in <code>config/profile_data.json</code>) and the
    bot will retry these jobs automatically on the next run.
  </p>

  <h3>Questions saved for training ({len(pending_questions)})</h3>
  <table>
    <tr><th>Question</th><th>Times asked</th></tr>
    {pending_rows}
  </table>

  <h3><span class="badge warn">External</span> Skipped external portals ({len(skipped_external)})</h3>
  <table>
    <tr><th>Company</th><th>Role</th><th>Naukri Link</th></tr>
    {_rows(skipped_external, [("company", "company"), ("title", "text"), ("naukri_url", "link")], "None.")}
  </table>

  <h3>Filtered out ({len(skipped_mismatch)})</h3>
  <table>
    <tr><th>Company</th><th>Role</th><th>Reason</th></tr>
    {_rows(skipped_mismatch, [("company", "company"), ("title", "text"), ("reason", "text")], "None.")}
  </table>

  { "<h3>Errors</h3><table><tr><th>Error</th></tr>" + "".join(f"<tr><td>{_escape(e)}</td></tr>" for e in errors) + "</table>" if errors else "" }

  <p class="muted">Generated by the Naukri automation bot. Logs: <code>logs/naukri_bot.log</code></p>
</body>
</html>"""


def build_plain_text(report: Dict[str, Any]) -> str:
    lines = [
        f"Naukri Application Digest - {report.get('run_id', '')}",
        f"Applied: {len(report.get('applied', []))}",
        f"External portals: {len(report.get('skipped_external', []))}",
        f"Unanswered questions: {len(report.get('skipped_unanswered', []))}",
        "",
    ]
    for job in report.get("applied", []):
        lines.append(f"  [APPLIED] {job.get('company')} - {job.get('title')} {job.get('naukri_url', '')}")
    for entry in report.get("skipped_unanswered", []):
        lines.append(f"  [ANSWER NEEDED] {entry.get('question')} ({entry.get('company')})")
    return "\n".join(lines)


def write_digest_file(html_body: str, report: Dict[str, Any]) -> Optional[Path]:
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = str(report.get("run_id") or datetime.now().strftime("%Y%m%d_%H%M%S")).replace(":", "").replace(" ", "_")
        path = REPORT_DIR / f"digest_{stamp}.html"
        path.write_text(html_body, encoding="utf-8")
        logger.info("Saved HTML digest: %s", path)
        return path
    except OSError as exc:
        logger.error("Could not write digest file: %s", exc)
        return None


def send_digest_email(report: Dict[str, Any], dry_run: bool = False) -> bool:
    """Write the digest locally and email it when SMTP is configured."""
    html_body = build_digest(report)
    write_digest_file(html_body, report)

    settings = _mail_settings()
    if settings is None:
        logger.warning(
            "Email not configured (SMTP_FROM/SMTP_PASSWORD/MAIL_TO or SENDER_*/RECIPIENT_*). "
            "Digest written to reports/ only."
        )
        return False
    if dry_run:
        logger.info("Dry run: skipping email dispatch.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"[Naukri Bot] {len(report.get('applied', []))} applied | "
        f"{len(report.get('skipped_unanswered', []))} need answers | {datetime.now():%d %b %Y}"
    )
    msg["From"] = settings["sender"]
    msg["To"] = ", ".join(settings["recipients"])
    msg.attach(MIMEText(build_plain_text(report), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    timeout = 20
    errors = []
    for transport_port, transport_ssl in _transport_options(settings["port"], settings["use_ssl"]):
        for username, password, label in settings["credential_candidates"]:
            mode = "SSL" if transport_ssl else "STARTTLS"
            try:
                if transport_ssl:
                    with smtplib.SMTP_SSL(
                        settings["host"], transport_port, timeout=timeout,
                        context=ssl.create_default_context(),
                    ) as server:
                        server.login(username, password)
                        server.sendmail(settings["sender"], settings["recipients"], msg.as_string())
                else:
                    with smtplib.SMTP(settings["host"], transport_port, timeout=timeout) as server:
                        server.ehlo()
                        server.starttls(context=ssl.create_default_context())
                        server.ehlo()
                        server.login(username, password)
                        server.sendmail(settings["sender"], settings["recipients"], msg.as_string())
                logger.info(
                    "Digest email sent to %s via %s :%d (%s)",
                    ", ".join(settings["recipients"]), mode, transport_port, label,
                )
                return True
            except Exception as exc:
                errors.append(f"{mode}:{transport_port}/{label}: {exc}")
                logger.warning(
                    "Email attempt failed (%s :%d with %s): %s", mode, transport_port, label, exc
                )

    logger.error("Email dispatch failed after %d attempt(s): %s", len(errors), errors[-1] if errors else "unknown")
    return False
