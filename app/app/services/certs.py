"""Keep up with a certificate the storage team renewed.

When an array's SyncIQ certificate is replaced, three things still refer to the old
one: the id this console recorded, the replicationCertificateID inside isilon-creds
on every cluster, and the target_certificate_id on any policy pointing at that array.
None of them announce themselves. Replication keeps working until the next policy is
created or the next connection is made, and then it does not.

Nothing here renews a certificate. The array is where that happens; this reads what
is there now and brings everything else into line with it.
"""
from __future__ import annotations

import hashlib
import ssl
import time

from .. import store
from .arrays import ArrayError, client_for
from .jobs import Job

WORK_DIR = "/ifs/data/csi/.osm-certs"


def fingerprint(pem: str | bytes) -> str:
    """A certificate's id on OneFS is the sha256 of its DER form."""
    text = pem.decode() if isinstance(pem, bytes) else pem
    try:
        der = ssl.PEM_cert_to_DER_cert(text)
    except (ValueError, ssl.SSLError) as exc:
        raise ArrayError(f"that does not look like a PEM certificate: {exc}") from exc
    return hashlib.sha256(der).hexdigest()


def survey() -> dict:
    """What each array presents now, against what everything else still believes."""
    state = store.load()
    arrays = list(state["arrays"].values())
    rows = []
    for array in arrays:
        row = {"id": array["id"], "name": array["name"], "endpoint": array["endpoint"],
               "stored": array.get("replication_certificate_id", ""), "current": "",
               "changed": False, "error": "", "expires": 0, "status": "",
               "expires_text": "", "days": None,
               "trusted_by": {}, "stale_policies": []}
        try:
            client = client_for(array)
            status, settings = client.get("/platform/16/sync/settings")
            if status == 200:
                row["current"] = (settings.get("settings", settings)
                                  .get("cluster_certificate_id", ""))
            for cert in client.sync_certificates("server"):
                if cert["id"] == row["current"]:
                    row["expires"] = cert["not_after"]
                    row["status"] = cert["status"]
        except (ArrayError, Exception) as exc:                # noqa: BLE001
            row["error"] = str(exc)[:200]
            rows.append(row)
            continue
        row["changed"] = bool(row["current"]) and row["current"] != row["stored"]
        if row["expires"]:
            days = int((row["expires"] - time.time()) // 86400)
            row["days"] = days
            row["expires_text"] = (
                time.strftime("%Y-%m-%d", time.gmtime(row["expires"]))
                + (f", {days} days" if days >= 0 else f", expired {abs(days)} days ago"))
        else:
            row["days"] = None
            row["expires_text"] = "unknown"
        rows.append(row)

    # does each peer trust what the other presents, and do its policies say so
    by_id = {r["id"]: r for r in rows}
    for array in arrays:
        others = [o for o in arrays if o["id"] != array["id"]]
        try:
            client = client_for(array)
            peers = {c["id"] for c in client.sync_certificates("peer")}
            policies = client.sync_policies()
        except (ArrayError, Exception):                       # noqa: BLE001
            continue
        for other in others:
            wanted = by_id[other["id"]]["current"]
            by_id[other["id"]]["trusted_by"][array["name"]] = bool(wanted) and wanted in peers
            for policy in policies:
                if policy.get("target_host") not in (other["endpoint"], other["name"]):
                    continue
                if wanted and policy.get("target_certificate_id") != wanted:
                    by_id[array["id"]]["stale_policies"].append({
                        "policy": policy.get("name", ""), "on": array["name"],
                        "wanted": wanted, "has": policy.get("target_certificate_id", "")})
    work = {
        "rows": rows,
        "changed": [r for r in rows if r["changed"]],
        "untrusted": [(r["name"], peer) for r in rows
                      for peer, ok in r["trusted_by"].items() if not ok],
        "stale": [p for r in rows for p in r["stale_policies"]],
    }
    work["clean"] = not (work["changed"] or work["untrusted"] or work["stale"])
    return work


def adopt(job: Job) -> dict:
    """Record the ids the arrays present now, and repoint policies at them.

    This is the usual case after a renewal done on the array: nothing needs to be
    uploaded, because the certificate is already where it belongs. What is left is
    everything that still quotes the old id.
    """
    found = survey()
    changed, fixed, failed = [], [], []
    for row in found["rows"]:
        if row["changed"]:
            job.log(f"{row['name']}: certificate id is now {row['current'][:16]}…, "
                    f"was {row['stored'][:16] or 'unset'}…")
            store.update(lambda s, r=row: s["arrays"][r["id"]].update(
                {"replication_certificate_id": r["current"]}))
            changed.append(row["name"])
        elif row["current"]:
            job.log(f"{row['name']}: certificate id unchanged ({row['current'][:16]}…)")

    for stale in found["stale"]:
        array = next((a for a in store.load()["arrays"].values()
                      if a["name"] == stale["on"]), None)
        if not array:
            continue
        job.log(f"{stale['on']}: policy {stale['policy']} still points at "
                f"{stale['has'][:16] or 'nothing'}…, setting {stale['wanted'][:16]}…")
        try:
            client_for(array).set_policy_certificate(stale["policy"], stale["wanted"])
            fixed.append(stale["policy"])
        except ArrayError as exc:
            job.log(f"  that failed: {exc}")
            failed.append(stale["policy"])

    for name, peer in found["untrusted"]:
        job.log(f"note: {peer} does not trust {name}'s current certificate. Import it there, "
                f"or push the certificate file from this page.")
    if not changed and not fixed:
        job.log("nothing needed changing")
    return {"changed": changed, "fixed": fixed, "failed": failed,
            "untrusted": found["untrusted"]}


def push_trust(job: Job, owner_id: str, peer_id: str, pem: bytes, name: str = "") -> str:
    """Make the peer array trust a certificate, from the file the storage team issued."""
    state = store.load()
    owner = state["arrays"].get(owner_id)
    peer = state["arrays"].get(peer_id)
    if not owner or not peer:
        raise ArrayError("both arrays have to be registered here")
    digest = fingerprint(pem)
    job.log(f"the certificate in that file is {digest[:16]}… ({len(pem)} bytes)")

    owner_client = client_for(owner)
    status, settings = owner_client.get("/platform/16/sync/settings")
    presented = ((settings.get("settings", settings) or {}).get("cluster_certificate_id", "")
                 if status == 200 else "")
    if presented and presented != digest:
        job.log(f"warning: {owner['name']} presents {presented[:16]}…, not this one. "
                "Pushing it anyway, but check you have the right file.")
    else:
        job.log(f"{owner['name']} does present this certificate")

    peer_client = client_for(peer)
    if digest in {c["id"] for c in peer_client.sync_certificates("peer")}:
        job.log(f"{peer['name']} already trusts it; nothing to import")
    else:
        target = f"{WORK_DIR}/{owner['name']}-{time.strftime('%Y%m%d-%H%M%S')}.pem"
        job.log(f"writing it to {target} on {peer['name']}")
        peer_client.put_file(target, pem)
        label = (name or f"{owner['name']}-{time.strftime('%Y%m%d')}")[:128]
        new_id = peer_client.import_peer_certificate(target, label,
                                                     f"pushed by OSM for {owner['name']}")
        job.log(f"{peer['name']} now trusts {new_id[:16]}… as {label}")

    store.update(lambda s: s["arrays"][owner_id].update(
        {"replication_certificate_id": digest}))
    job.log(f"recorded {digest[:16]}… as {owner['name']}'s certificate id here")
    return digest


def refresh_secrets(job: Job) -> list[str]:
    """Write isilon-creds again on every cluster, with the ids as they are now."""
    from .installer import create_array_secret
    state = store.load()
    arrays = list(state["arrays"].values())
    done = []
    for cluster in state["clusters"].values():
        ns = cluster.get("namespace", "isilon")
        default = (state["installs"].get(cluster["id"], {}) or {}).get("default_array")
        job.log(f"{cluster['id']}: writing isilon-creds in {ns}")
        create_array_secret(job, cluster["kubeconfig"], ns, arrays, default)
        done.append(cluster["id"])
    job.log("the driver reads the secret when it next needs it; no restart is required "
            "for a new policy, but a controller restart makes it immediate")
    return done
