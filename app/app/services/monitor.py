"""Watch the lab and raise alerts when something that matters changes.

Rules are evaluated on a timer. Each finding has a stable key, so the same problem is
reported once when it starts, repeated only after a quiet period, and reported again
when it clears. That keeps a broken link from mailing someone every two minutes.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from .. import store
from . import audit, inventory, notify, replication

CHECK_SECONDS_DEFAULT = 120
RENOTIFY_HOURS_DEFAULT = 6

# how long a group may go without a successful update, per objective in its name
RPO_SECONDS = {
    "Five_Minutes": 300, "Fifteen_Minutes": 900, "Thirty_Minutes": 1800,
    "One_Hour": 3600, "Six_Hours": 21600, "Twelve_Hours": 43200, "One_Day": 86400,
}
RPO_GRACE = 2.0                      # alert once the age passes twice the objective

_thread: threading.Thread | None = None
_stop = threading.Event()
_last_run: dict = {"at": "", "findings": 0, "sent": 0, "error": ""}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now().isoformat(timespec="seconds")


def _age(iso: str) -> timedelta | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _now() - then


def _finding(key: str, severity: str, title: str, detail: str, where: str = "",
             advice: str = "") -> dict:
    return {"key": key, "severity": severity, "title": title, "detail": detail,
            "where": where, "advice": advice}


# --- the rules -----------------------------------------------------------------
def evaluate() -> list[dict]:
    """Everything currently wrong, as a flat list."""
    found: list[dict] = []
    data = inventory.snapshot(force=True)
    state = store.load()

    unreachable = {c["id"] for c in data["clusters"] if not c.get("reachable")}

    for cluster in data["clusters"]:
        cid = cluster["id"]
        if not cluster.get("reachable"):
            found.append(_finding(
                f"cluster:{cid}:unreachable", "critical",
                f"Cluster {cid} is unreachable",
                cluster.get("error", "no answer from the API server"), cid,
                "Check the API address, the network, and whether the stored credentials expired."))
            continue

        driver = cluster.get("driver") or {}
        if driver.get("pods"):
            not_ready = [p["name"] for p in driver["pods"] if not p["all_ready"]]
            if not_ready or not driver.get("controller_ready") or not driver.get("node_ready"):
                found.append(_finding(
                    f"cluster:{cid}:driver", "critical",
                    f"The storage driver on {cid} is not healthy",
                    (f"{len(not_ready)} pod(s) not ready: {', '.join(not_ready[:4])}"
                     if not_ready else "the controller or the node plugin is not ready"),
                    f"{cid}/{driver.get('namespace', '')}",
                    "Look at the pods on the Install page, then the driver logs."))

        for group in replication.replication_groups(cluster["kubeconfig"]):
            name = group["name"]
            link = (group.get("link_state") or "").upper()
            where = f"{cid}/{name}"
            if "IN_PROGRESS" in (group.get("state") or "").upper():
                continue                       # an action is running; judge it when it settles
            peer_down = group.get("remote_cluster") in unreachable
            if (group.get("state") or "") == "Error":
                reason = next((c for c in (group.get("conditions") or [])
                               if "failed" in c.lower()), "")
                found.append(_finding(
                    f"rg:{name}:{cid}:error", "critical",
                    f"Replication group {name} reports an error",
                    reason or group.get("last_action") or group.get("condition") or "see the group",
                    where, "Open Operations and read the last action on that group."))
            if peer_down:
                continue          # the peer being down already has its own alert
            if link == "FAILEDOVER":
                found.append(_finding(
                    f"rg:{name}:failedover", "warning",
                    f"Replication group {name} is failed over and unprotected",
                    "The pair has no running replication until it is reprotected.", where,
                    "Run Reprotect at the site that should serve from now on."))
            elif link in ("EMPTY", "UNKNOWN", ""):
                found.append(_finding(
                    f"rg:{name}:link", "warning",
                    f"Replication group {name} has no link state",
                    "neither side is reporting the link, which usually means the peer cluster or "
                    "its replication controller cannot be reached", where,
                    "Check the peer cluster and its dell-replication-controller."))
            elif link != "SYNCHRONIZED":
                found.append(_finding(
                    f"rg:{name}:link", "warning",
                    f"Replication group {name} is not synchronized",
                    f"the link reports {link}", where,
                    "Check the SyncIQ policy on the source array."))

            if group.get("is_source"):
                point = replication.recovery_point(group, data["arrays"])
                if point["state"] in ("drifting", "breached"):
                    found.append(_finding(
                        f"rg:{name}:rpo", "critical" if point["state"] == "breached" else "warning",
                        f"Replication group {name} is behind its recovery point objective",
                        f"last successful sync {replication.describe_age(point['lag'])} ago "
                        f"against an objective of {replication.describe_age(point['objective'])}"
                        f", {point['note']}", where,
                        "Run a sync, and read the policy report on the source array."))

    for array in data["arrays"]:
        name = array.get("name") or array.get("id")
        if not array.get("reachable"):
            found.append(_finding(
                f"array:{name}:unreachable", "critical", f"Array {name} is unreachable",
                array.get("error", "no answer from the Platform API"), array.get("endpoint", ""),
                "Check the management address, the credentials and the API service."))
            continue
        # an array can answer and still have a node that refuses, which is what makes the
        # console flip between reachable and not
        try:
            from .arrays import node_check, resolve_all
            record = next((a for a in store.load()["arrays"].values()
                           if a.get("name") == name or a.get("id") == name), None)
            if record and len(resolve_all(record["endpoint"])) > 1:
                nodes = node_check(record["endpoint"], int(record.get("port", 8080)),
                                   record["username"], record["password"],
                                   int(record.get("auth_type", 1)), timeout=6)
                bad = [n for n in nodes if not n["ok"]]
                if bad and len(bad) < len(nodes):
                    found.append(_finding(
                        f"array:{name}:nodes", "critical",
                        f"Array {name} answers differently depending on the node",
                        f"{len(bad)} of {len(nodes)} nodes refused: "
                        + ", ".join(f"{n['address']} ({n['detail'][:40]})" for n in bad[:3]),
                        record.get("endpoint", ""),
                        "Check that every node carries the same authentication settings and that "
                        "the web service was restarted on all of them."))
        except Exception:                                 # noqa: BLE001
            pass

        if array.get("missing_privileges"):
            found.append(_finding(
                f"array:{name}:privileges", "warning",
                f"Array {name} is missing privileges",
                ", ".join(array["missing_privileges"]), array.get("endpoint", ""),
                "Grant them to the role the driver user holds."))
        for policy in array.get("policies") or []:
            if policy.get("last_job_state") in ("failed", "needs_attention"):
                found.append(_finding(
                    f"policy:{name}:{policy['name']}", "critical",
                    f"SyncIQ policy {policy['name']} failed on {name}",
                    f"last job: {policy['last_job_state']}", array.get("endpoint", ""),
                    "Read the policy report on the array for the reason."))

    for cid, install in (state.get("installs") or {}).items():
        if cid not in state["clusters"]:
            continue
    return found


# --- state, so the same problem is not mailed every cycle ------------------------
def _settings() -> dict:
    cfg = dict(store.load()["setup"]["alerts"])
    cfg.setdefault("check_seconds", CHECK_SECONDS_DEFAULT)
    cfg.setdefault("renotify_hours", RENOTIFY_HOURS_DEFAULT)
    return cfg


def open_alerts() -> list[dict]:
    return sorted(store.load().get("alert_state", {}).values(),
                  key=lambda a: (a.get("severity") != "critical", a.get("first_seen", "")))


def run_once(send: bool | None = None) -> dict:
    """Evaluate the rules, then raise, repeat or clear each alert."""
    cfg = _settings()
    if send is None:
        send = bool(cfg.get("enabled"))
    findings = {f["key"]: f for f in evaluate()}
    now = _stamp()
    renotify = timedelta(hours=float(cfg.get("renotify_hours", RENOTIFY_HOURS_DEFAULT)))
    outgoing: list[dict] = []
    previous = store.load().get("alert_state", {})

    updated: dict = {}
    for key, finding in findings.items():
        before = previous.get(key)
        record = dict(finding)
        record["first_seen"] = before.get("first_seen", now) if before else now
        record["last_seen"] = now
        record["notified_at"] = before.get("notified_at", "") if before else ""
        due = not record["notified_at"]
        if not due and record["notified_at"]:
            age = _age(record["notified_at"])
            due = age is not None and age > renotify
        if due:
            record["notified_at"] = now
            outgoing.append(record)
        updated[key] = record

    for key, before in previous.items():
        if key in findings:
            continue
        cleared = dict(before)
        cleared.update({"severity": "cleared", "title": "Cleared: " + before["title"],
                        "detail": "This no longer applies.", "notified_at": now})
        if before.get("notified_at"):
            outgoing.append(cleared)

    store.update(lambda s: s.__setitem__("alert_state", updated))

    sent = 0
    for alert in outgoing:
        audit.record("alert." + ("cleared" if alert["severity"] == "cleared" else "raised"),
                     actor="watcher", via="timer", target=alert.get("where", ""),
                     outcome=alert["severity"], detail=alert["title"], key=alert["key"])
        if not send:
            continue
        for line in notify.deliver(cfg, alert):
            if line.startswith("sent:"):
                sent += 1
    _last_run.update({"at": now, "findings": len(findings), "sent": sent, "error": ""})
    return {"findings": list(findings.values()), "notified": outgoing, "sent": sent,
            "delivering": send}


def last_run() -> dict:
    return dict(_last_run)


# --- the timer -------------------------------------------------------------------
def _loop() -> None:
    while not _stop.is_set():
        try:
            run_once()
        except Exception as exc:                              # noqa: BLE001
            _last_run.update({"at": _stamp(), "error": str(exc)[:200]})
        wait = float(_settings().get("check_seconds", CHECK_SECONDS_DEFAULT))
        _stop.wait(max(30.0, wait))


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="osm-monitor", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
