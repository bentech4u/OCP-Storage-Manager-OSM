"""OneFS Platform API client and the driver secret it feeds.

OneFS 9.15 refuses basic auth on the Platform API, so this client logs in at
/session/1/session and reuses the isisessid cookie with the isicsrf token.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import yaml

REQUIRED_PRIVS = {
    "ISI_PRIV_LOGIN_PAPI": "r", "ISI_PRIV_NFS": "rw", "ISI_PRIV_QUOTA": "rw",
    "ISI_PRIV_SNAPSHOT": "rw", "ISI_PRIV_IFS_RESTORE": "r", "ISI_PRIV_NS_IFS_ACCESS": "r",
    "ISI_PRIV_IFS_BACKUP": "r", "ISI_PRIV_AUTH_ZONES": "r", "ISI_PRIV_STATISTICS": "r",
}
REPLICATION_PRIV = "ISI_PRIV_SYNCIQ"


class ArrayError(RuntimeError):
    pass


class OneFS:
    def __init__(self, endpoint: str, port: int, user: str, password: str,
                 verify: bool = False, timeout: int = 15):
        host = endpoint.replace("https://", "").replace("http://", "").strip("/")
        self.base = f"https://{host}:{port}"
        self.user, self.password, self.timeout = user, password, timeout
        self.ctx = ssl.create_default_context()
        if not verify:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE
        self.cookie: str | None = None
        self.csrf: str | None = None

    # -- transport ------------------------------------------------------------
    def _req(self, method: str, path: str, body: Any = None, auth: str = "session") -> tuple[int, Any]:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth == "session":
            if not self.cookie:
                self.login()
            if self.cookie:
                req.add_header("Cookie", self.cookie)
                req.add_header("X-CSRF-Token", self.csrf or "")
                req.add_header("Referer", self.base)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                payload = resp.read()
                return resp.status, _json(payload)
        except urllib.error.HTTPError as exc:
            return exc.code, _json(exc.read())
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            raise ArrayError(f"cannot reach {self.base}: {exc}") from exc

    def login(self) -> None:
        body = {"username": self.user, "password": self.password,
                "services": ["platform", "namespace"]}
        data = json.dumps(body).encode()
        req = urllib.request.Request(self.base + "/session/1/session", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                cookies = "; ".join(resp.headers.get_all("Set-Cookie") or [])
        except urllib.error.HTTPError as exc:
            raise ArrayError(f"login failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
            raise ArrayError(f"cannot reach {self.base}: {exc}") from exc
        import re
        sess = re.search(r"isisessid=([^;]+)", cookies)
        csrf = re.search(r"isicsrf=([^;]+)", cookies)
        if not sess:
            raise ArrayError("the array did not return a session cookie")
        self.cookie = f"isisessid={sess.group(1)}"
        if csrf:
            self.cookie += f"; isicsrf={csrf.group(1)}"
            self.csrf = csrf.group(1)

    def get(self, path: str) -> tuple[int, Any]:
        return self._req("GET", path)

    # -- discovery ------------------------------------------------------------
    def inspect(self) -> dict:
        """Everything the UI shows about an array, in one round trip set."""
        out: dict = {"reachable": False, "errors": []}
        st, ident = self.get("/platform/3/cluster/identity")
        out["name"] = (ident or {}).get("name", "") if st == 200 else ""
        self.login()
        out["reachable"] = True
        st, cfg = self.get("/platform/3/cluster/config")
        if st == 200:
            out["name"] = cfg.get("name", out.get("name", ""))
            out["onefs_version"] = (cfg.get("onefs_version") or {}).get("release", "")
            out["guid"] = cfg.get("guid", "")
        st, ver = self.get("/platform/3/cluster/version")
        if st == 200 and ver.get("nodes"):
            out["nodes"] = len(ver["nodes"])
        st, zones = self.get("/platform/3/zones")
        out["zones"] = [z["name"] for z in zones.get("zones", [])] if st == 200 else []
        st, nfs = self.get("/platform/3/protocols/nfs/settings/global")
        if st == 200:
            s = nfs.get("settings", {})
            out["nfs_enabled"] = bool(s.get("service"))
            out["nfs_v3"], out["nfs_v4"] = s.get("nfsv3_enabled"), s.get("nfsv4_enabled")
        st, lic = self.get("/platform/5/license/licenses")
        if st == 200:
            table = {str(l.get("name") or l.get("id")).upper(): l.get("status")
                     for l in lic.get("licenses", [])}
            out["licenses"] = {k: table.get(k, "not listed")
                               for k in ("SMARTQUOTAS", "SNAPSHOTIQ", "SYNCIQ")}
        st, ident2 = self.get("/platform/1/auth/id")
        privs: dict[str, bool] = {}
        if st == 200:
            for p in (ident2.get("ntoken") or {}).get("privilege", []):
                privs[p["id"]] = not p.get("read_only", True)
        out["privileges"] = privs
        out["missing_privileges"] = [
            p for p, need in REQUIRED_PRIVS.items()
            if p not in privs or (need == "rw" and not privs[p])
        ]
        out["can_replicate"] = privs.get(REPLICATION_PRIV, False)
        st, sync = self.get("/platform/16/sync/settings")
        if st == 200:
            s = sync.get("settings", sync)
            out["synciq_service"] = s.get("service")
            out["synciq_encryption_required"] = s.get("encryption_required")
            out["synciq_cluster_certificate_id"] = s.get("cluster_certificate_id", "")
        return out

    def check_path(self, isi_path: str) -> dict:
        st, data = self.get("/namespace" + urllib.parse.quote(isi_path) + "?detail=mode,type,owner")
        return {"exists": st == 200, "detail": data if st == 200 else {}, "status": st}

    def create_path(self, isi_path: str, mode: str = "0777") -> bool:
        url = self.base + "/namespace" + urllib.parse.quote(isi_path) + "?recursive=true"
        req = urllib.request.Request(url, method="PUT")
        if not self.cookie:
            self.login()
        req.add_header("Cookie", self.cookie or "")
        req.add_header("X-CSRF-Token", self.csrf or "")
        req.add_header("Referer", self.base)
        req.add_header("x-isi-ifs-target-type", "container")
        req.add_header("x-isi-ifs-access-control", mode)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                return resp.status in (200, 201)
        except urllib.error.HTTPError:
            return False

    def sync_policies(self) -> list[dict]:
        st, data = self.get("/platform/16/sync/policies")
        if st != 200:
            return []
        return [{
            "name": p.get("name"), "enabled": p.get("enabled"),
            "source": p.get("source_root_path"), "target_host": p.get("target_host"),
            "target_path": p.get("target_path"), "action": p.get("action"),
            "last_job_state": p.get("last_job_state"), "schedule": p.get("schedule"),
            "encrypted": bool(p.get("target_certificate_id")),
        } for p in data.get("policies", [])]

    def sync_reports(self, limit: int = 10) -> list[dict]:
        st, data = self.get(f"/platform/16/sync/reports?limit={limit}")
        if st != 200:
            return []
        return [{
            "policy": r.get("policy_name"), "state": r.get("state"),
            "start": r.get("start_time"), "duration": r.get("duration"),
            "bytes": r.get("total_bytes"), "errors": r.get("errors") or [],
            "encrypted": r.get("encrypted"),
        } for r in data.get("reports", [])]


def _json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        text = payload.decode(errors="replace")
        return {"raw": text[:400]}


def secret_yaml(arrays: list[dict]) -> str:
    """Build the isilon-creds payload from the registered arrays.

    For replication every cluster's secret lists every array, so the driver on either
    site can talk to both ends of a SyncIQ policy.
    """
    entries = []
    for a in arrays:
        entry = {
            "clusterName": a["name"],
            "username": a["username"],
            "password": a["password"],
            "endpoint": a["endpoint"],
            "endpointPort": int(a.get("port", 8080)),
            "isDefault": bool(a.get("is_default")),
            "skipCertificateValidation": bool(a.get("skip_cert_validation", True)),
            "isiPath": a.get("isi_path", "/ifs/data/csi"),
            "isiVolumePathPermissions": a.get("volume_permissions", "0777"),
        }
        if a.get("replication_certificate_id"):
            entry["replicationCertificateID"] = a["replication_certificate_id"]
        entries.append(entry)
    return yaml.safe_dump({"isilonClusters": entries}, sort_keys=False)
