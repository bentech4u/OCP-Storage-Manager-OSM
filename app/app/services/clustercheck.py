"""Check a cluster before it is added, from an address and a sign-in.

Adding a cluster that cannot be reached is a slow way to find out. Worse, the two
addresses involved are not the same one: oc talks to the API server on 6443 and
then to the OAuth endpoint, which lives on the applications domain on 443. A
firewall that opens the first and not the second does not refuse the sign-in, it
holds it open, and the console waits.

Everything here only reads, and nothing is stored: the sign-in is made into a
temporary file which is deleted before this returns.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ..config import tool_env
from .arrays import endpoint_certificate
from .k8s import cli, normalize_api_address

API_PORT = 6443


def _check(what: str, level: str, detail: str, fix: str = "") -> dict:
    return {"what": what, "level": level, "detail": detail, "fix": fix}


def _open(host: str, port: int, timeout: float = 6.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "open"
    except OSError as exc:
        return False, str(exc.strerror or exc)


def _get_json(url: str, timeout: int = 10, token: str = "") -> tuple[int, dict]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception:                                        # noqa: BLE001
        return 0, {}


def check(api: str, username: str = "", password: str = "", insecure: bool = True) -> dict:
    """Walk everything between this host and a working sign-in on that cluster."""
    out: list[dict] = []
    address = normalize_api_address(api)
    if not address:
        return {"findings": [_check("an address", "fail", "nothing was given")],
                "ready": False, "address": ""}
    parsed = urlparse(address)
    host, port = parsed.hostname or "", parsed.port or API_PORT
    out.append(_check("the address", "pass", address,
                      "" if address == (api or "").strip() else
                      "this is what the address you gave resolves to in oc's terms"))

    try:
        addresses = sorted({info[4][0] for info in
                            socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})
        out.append(_check("the name resolves", "pass", ", ".join(addresses)))
    except OSError as exc:
        out.append(_check("the name resolves", "fail", f"{host}: {exc.strerror or exc}",
                          "fix DNS on this host, or use the API server's IP address"))
        return {"findings": out, "ready": False, "address": address}

    ok, detail = _open(host, port)
    out.append(_check(f"the API port {port}", "pass" if ok else "fail", detail,
                      "" if ok else "this is the port the console, repctl and the "
                      "replication controller all use; open it from here"))
    if not ok:
        return {"findings": out, "ready": False, "address": address}

    cert = endpoint_certificate(host, port)
    if cert.get("known"):
        days = cert["days"]
        level = "fail" if days is not None and days < 0 else (
            "warn" if days is not None and days < 30 else "pass")
        out.append(_check("the API certificate", level,
                          ("expired" if days is not None and days < 0
                           else f"{days} days left") + f", {cert['subject'][:70]}"))
    verified = True
    try:
        urllib.request.urlopen(f"https://{host}:{port}/version", timeout=8)
    except urllib.error.HTTPError:
        pass
    except Exception:                                        # noqa: BLE001
        verified = False
    out.append(_check("the certificate is trusted by this host", "pass" if verified else "warn",
                      "yes" if verified else "no, this host does not trust the issuer",
                      "" if verified else "tick the skip-verification box when adding it, or "
                      "install the cluster's authority on this host"))

    status, body = _get_json(f"https://{host}:{port}/version")
    if status == 200 and body.get("major"):
        out.append(_check("it is a Kubernetes API", "pass",
                          f"v{body.get('major')}.{body.get('minor')}"))
    elif status in (401, 403):
        out.append(_check("it is a Kubernetes API", "pass",
                          f"it answered {status}, which means it is there and wants a sign-in"))
    else:
        out.append(_check("it is a Kubernetes API", "warn",
                          f"/version answered {status or 'nothing'}",
                          "something is listening, but it did not answer like an API server"))

    # the second address: oc signs in against the OAuth endpoint, not the API port
    status, oauth = _get_json(f"https://{host}:{port}/.well-known/oauth-authorization-server")
    token_url = oauth.get("token_endpoint") or oauth.get("issuer") or ""
    if token_url:
        oauth_parsed = urlparse(token_url)
        oauth_host = oauth_parsed.hostname or ""
        oauth_port = oauth_parsed.port or 443
        try:
            socket.gethostbyname(oauth_host)
            resolved = True
        except OSError:
            resolved = False
        if not resolved:
            out.append(_check("the sign-in endpoint resolves", "fail",
                              f"{oauth_host} does not resolve from this host",
                              "oc signs in there, not on the API port. It is the "
                              "applications domain, usually *.apps.<cluster>.<domain>"))
        else:
            ok, detail = _open(oauth_host, oauth_port)
            out.append(_check(f"the sign-in endpoint {oauth_host}:{oauth_port}",
                              "pass" if ok else "fail", detail,
                              "" if ok else "a username and password sign-in goes here after "
                              "the API port. A firewall that allows 6443 and not this does "
                              "not refuse the sign-in, it holds it open until it times out. "
                              "Either open it from this host, or add the cluster with the "
                              "paste a token route: a browser that can reach this endpoint "
                              "gets the token, and the token is used on the API port alone"))
    else:
        out.append(_check("the sign-in endpoint", "warn",
                          "the API did not advertise one",
                          "not an OpenShift OAuth cluster, or it is not exposed; a "
                          "kubeconfig with a token avoids needing it"))

    if not username or not password:
        out.append(_check("credentials", "warn", "none given, so the sign-in was not tried",
                          "fill in a user and password to check them, or add the cluster "
                          "with a kubeconfig"))
        return {"findings": out, "address": address,
                "ready": not any(f["level"] == "fail" for f in out)}

    handle, path = tempfile.mkstemp(suffix=".kubeconfig")
    os.close(handle)
    env = tool_env()
    env["KUBECONFIG"] = path
    try:
        cmd = [cli(), "login", address, "-u", username]
        if insecure:
            cmd.append("--insecure-skip-tls-verify=true")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env,
                                  input=password + "\n")
            text = (proc.stdout or "") + (proc.stderr or "")
            signed_in = proc.returncode == 0
        except subprocess.TimeoutExpired:
            signed_in, text = False, "it did not answer within 60s"
        if signed_in:
            who = subprocess.run([cli(), "whoami"], capture_output=True, text=True,
                                 timeout=30, env=env)
            out.append(_check("the sign-in", "pass",
                              f"signed in as {who.stdout.strip() or username}"))
        else:
            short = " ".join(line for line in text.splitlines()
                             if "Login failed" in line or "error" in line.lower())[:200]
            out.append(_check("the sign-in", "fail", short or text.strip()[:200],
                              "check the user and password, or whether this account is "
                              "allowed to sign in from here"))
            return {"findings": out, "address": address, "ready": False}

        admin = subprocess.run([cli(), "auth", "can-i", "*", "*", "--all-namespaces"],
                               capture_output=True, text=True, timeout=30, env=env)
        is_admin = admin.stdout.strip().startswith("yes")
        out.append(_check("the account can administer the cluster",
                          "pass" if is_admin else "fail",
                          "cluster-admin" if is_admin else "it cannot do everything",
                          "" if is_admin else "installing the driver and the replication "
                          "custom resource definitions needs cluster-admin"))

        version = subprocess.run([cli(), "version", "-o", "json"], capture_output=True,
                                 text=True, timeout=40, env=env)
        try:
            vdata = json.loads(version.stdout or "{}")
        except json.JSONDecodeError:
            vdata = {}
        ocp = vdata.get("openshiftVersion", "")
        if ocp:
            from .installer import support_warnings
            warnings = support_warnings(ocp, [])
            out.append(_check("OpenShift version", "warn" if warnings else "pass", ocp,
                              " ".join(warnings)))

        driver = subprocess.run([cli(), "get", "csidriver", "csi-isilon.dellemc.com"],
                                capture_output=True, text=True, timeout=30, env=env)
        if driver.returncode == 0:
            crd = subprocess.run(
                [cli(), "get", "crd",
                 "dellcsireplicationgroups.replication.storage.dell.com"],
                capture_output=True, text=True, timeout=30, env=env)
            out.append(_check("a PowerScale driver is already here", "warn",
                              "the CSIDriver object exists"
                              + (", with the replication CRDs" if crd.returncode == 0
                                 else ", without the replication CRDs"),
                              "do not install a second one beside it: adopt the release "
                              "that is there, on the driver step"))
        else:
            out.append(_check("no driver here yet", "pass",
                              "nothing claims csi-isilon.dellemc.com"))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"findings": out, "address": address,
            "ready": not any(f["level"] == "fail" for f in out)}
