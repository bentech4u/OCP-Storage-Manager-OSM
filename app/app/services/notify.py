"""Delivering an alert: mail, webhook, or both."""
from __future__ import annotations

import json
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

SEVERITY_MARK = {"critical": "CRITICAL", "warning": "WARNING", "cleared": "CLEARED"}


def _subject(alert: dict) -> str:
    return f"[{SEVERITY_MARK.get(alert['severity'], alert['severity'].upper())}] {alert['title']}"


def _body(alert: dict) -> str:
    lines = [alert["detail"], ""]
    if alert.get("where"):
        lines.append(f"where:   {alert['where']}")
    lines.append(f"since:   {alert.get('first_seen', '')}")
    if alert.get("advice"):
        lines += ["", alert["advice"]]
    return "\n".join(lines)


def send_mail(cfg: dict, alert: dict) -> tuple[bool, str]:
    host, recipients = cfg.get("smtp_host"), cfg.get("mail_to") or []
    if not host or not recipients:
        return False, "no mail server or recipients"
    msg = EmailMessage()
    msg["Subject"] = _subject(alert)
    msg["From"] = cfg.get("mail_from") or "osm@localhost"
    msg["To"] = ", ".join(recipients)
    msg.set_content(_body(alert))
    try:
        with smtplib.SMTP(host, int(cfg.get("smtp_port", 25)), timeout=20) as smtp:
            if cfg.get("smtp_tls"):
                smtp.starttls()
            if cfg.get("smtp_user"):
                smtp.login(cfg["smtp_user"], cfg.get("smtp_password", ""))
            smtp.send_message(msg)
        return True, f"mailed {', '.join(recipients)}"
    except Exception as exc:                                  # noqa: BLE001
        return False, f"mail failed: {exc}"


def send_webhook(cfg: dict, alert: dict) -> tuple[bool, str]:
    url = cfg.get("webhook_url")
    if not url:
        return False, "no webhook"
    payload = {
        "text": f"{_subject(alert)}\n{alert['detail']}",
        "severity": alert["severity"], "title": alert["title"], "detail": alert["detail"],
        "where": alert.get("where", ""), "key": alert["key"],
        "first_seen": alert.get("first_seen", ""), "advice": alert.get("advice", ""),
        "source": "OCP Storage Manager",
    }
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return True, f"webhook answered {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"webhook refused with {exc.code}"
    except Exception as exc:                                  # noqa: BLE001
        return False, f"webhook failed: {exc}"


def deliver(cfg: dict, alert: dict) -> list[str]:
    """Send by every route configured, reporting what happened on each."""
    results = []
    if cfg.get("smtp_host"):
        ok, detail = send_mail(cfg, alert)
        results.append(("sent: " if ok else "not sent: ") + detail)
    if cfg.get("webhook_url"):
        ok, detail = send_webhook(cfg, alert)
        results.append(("sent: " if ok else "not sent: ") + detail)
    if not results:
        results.append("nowhere to send: no mail server and no webhook configured")
    return results
