"""Aggregate cluster and array state for the dashboard, with a short cache."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .. import store
from .arrays import ArrayError, OneFS
from .k8s import ClusterError, driver_status, kubeconfig_path, probe
from .tools import status as tool_status

_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()
TTL = 25.0


def cluster_snapshot(cluster: dict) -> dict:
    out = dict(cluster)
    kc = cluster.get("kubeconfig") or str(kubeconfig_path(cluster["id"]))
    out["kubeconfig"] = kc
    try:
        out.update(probe(kc))
        out["reachable"] = True
        out["error"] = ""
    except (ClusterError, Exception) as exc:              # noqa: BLE001
        out["reachable"] = False
        out["error"] = str(exc)[:200]
        return out
    try:
        out["driver"] = driver_status(kc, cluster.get("namespace", "isilon"))
    except Exception as exc:                              # noqa: BLE001
        out["driver"] = {"error": str(exc)[:200], "pods": []}
    return out


def array_snapshot(array: dict) -> dict:
    out = {k: v for k, v in array.items() if k != "password"}
    try:
        client = OneFS(array["endpoint"], int(array.get("port", 8080)),
                       array["username"], array["password"],
                       verify=not array.get("skip_cert_validation", True))
        out.update(client.inspect())
        out["policies"] = client.sync_policies()
        out["error"] = ""
    except ArrayError as exc:
        out["reachable"] = False
        out["error"] = str(exc)[:200]
    except Exception as exc:                              # noqa: BLE001
        out["reachable"] = False
        out["error"] = str(exc)[:200]
    return out


def snapshot(force: bool = False) -> dict:
    with _lock:
        if not force and _cache["data"] and time.time() - _cache["at"] < TTL:
            return _cache["data"]
    state = store.load()
    clusters = list(state["clusters"].values())
    arrays = list(state["arrays"].values())
    with ThreadPoolExecutor(max_workers=8) as pool:
        cluster_data = list(pool.map(cluster_snapshot, clusters))
        array_data = list(pool.map(array_snapshot, arrays))
    data = {
        "clusters": cluster_data,
        "arrays": array_data,
        "tools": tool_status(),
        "generated": time.time(),
    }
    data["counts"] = {
        "clusters": len(cluster_data),
        "clusters_ok": sum(1 for c in cluster_data if c.get("reachable")),
        "arrays": len(array_data),
        "arrays_ok": sum(1 for a in array_data if a.get("reachable")),
        "drivers_ok": sum(1 for c in cluster_data
                          if c.get("driver", {}).get("controller_ready")),
        "replication_groups": sum(len(c.get("driver", {}).get("replication_groups", []))
                                  for c in cluster_data),
    }
    with _lock:
        _cache.update({"at": time.time(), "data": data})
    return data


def invalidate() -> None:
    with _lock:
        _cache.update({"at": 0.0, "data": None})
