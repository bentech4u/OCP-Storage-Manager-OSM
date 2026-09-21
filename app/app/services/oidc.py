"""Microsoft Entra ID, and any other OpenID Connect provider.

Two ways in, both ending at the same place, a set of claims:

  password  the console posts the username and password to the provider's token
            endpoint (the password grant). Microsoft allows this only for cloud
            accounts without multi-factor or conditional access.
  redirect  the browser signs in on the provider's own page (the authorization code
            flow), which is what to use when those policies are in force.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

_discovery_cache: dict[str, tuple[float, dict]] = {}
DISCOVERY_TTL = 3600


class OidcError(RuntimeError):
    pass


def issuer_for(cfg: dict) -> str:
    issuer = (cfg.get("issuer") or "").strip().rstrip("/")
    if issuer:
        return issuer
    tenant = (cfg.get("tenant_id") or "").strip()
    if tenant:
        return f"https://login.microsoftonline.com/{tenant}/v2.0"
    raise OidcError("no issuer or tenant is configured")


def discovery(cfg: dict) -> dict:
    issuer = issuer_for(cfg)
    hit = _discovery_cache.get(issuer)
    if hit and time.time() - hit[0] < DISCOVERY_TTL:
        return hit[1]
    url = issuer + "/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            doc = json.load(resp)
    except Exception as exc:                                  # noqa: BLE001
        raise OidcError(f"cannot read {url}: {exc}") from exc
    _discovery_cache[issuer] = (time.time(), doc)
    return doc


def _post(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
        except Exception:                                     # noqa: BLE001
            body = {}
        raise OidcError(_describe(body, exc.code)) from exc
    except Exception as exc:                                  # noqa: BLE001
        raise OidcError(f"cannot reach the provider: {exc}") from exc


def _describe(body: dict, status: int) -> str:
    """Turn the provider's error codes into something an operator can act on."""
    code = body.get("error", "")
    detail = body.get("error_description", "") or ""
    first = detail.split("\r\n")[0][:200]
    if "AADSTS50126" in detail:
        return "that username or password was rejected by Entra."
    if "AADSTS50034" in detail:
        return "that account does not exist in this tenant."
    if "AADSTS90002" in detail:
        return ("the account's domain is not part of this tenant, so Entra could not find it. "
                "Check the address, or the tenant id in Setup.")
    if "AADSTS50076" in detail or "AADSTS50079" in detail:
        return ("this account needs multi-factor authentication, which the password form cannot "
                "do. Use the Microsoft sign-in page instead.")
    if "AADSTS53003" in detail or "AADSTS50105" in detail:
        return ("a conditional access policy blocked this sign-in, or the account is not assigned "
                "to the application.")
    if "AADSTS7000218" in detail or code == "invalid_client":
        return ("the application rejected the client secret, or the app registration does not "
                "allow this flow. Enable the public client or check the secret.")
    if "AADSTS700016" in detail:
        return "that application (client) id was not found in the tenant."
    return first or f"{code or 'sign-in failed'} (HTTP {status})"


def _claims_from_token(token: str) -> dict:
    """Read the token payload. It arrives over TLS from the endpoint we just called."""
    parts = token.split(".")
    if len(parts) < 2:
        raise OidcError("the provider returned a token this console cannot read")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    import base64
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:                                  # noqa: BLE001
        raise OidcError("the provider returned a token this console cannot read") from exc


def password_login(cfg: dict, username: str, password: str) -> dict:
    doc = discovery(cfg)
    form = {
        "grant_type": "password", "username": username, "password": password,
        "client_id": cfg.get("client_id", ""),
        "scope": cfg.get("scope") or "openid profile email",
    }
    if cfg.get("client_secret"):
        form["client_secret"] = cfg["client_secret"]
    data = _post(doc["token_endpoint"], form)
    token = data.get("id_token") or data.get("access_token")
    if not token:
        raise OidcError("the provider accepted the sign-in but returned no token")
    return _claims_from_token(token)


def auth_url(cfg: dict, state: str, redirect_uri: str) -> str:
    doc = discovery(cfg)
    params = {
        "client_id": cfg.get("client_id", ""), "response_type": "code",
        "redirect_uri": redirect_uri, "response_mode": "query",
        "scope": cfg.get("scope") or "openid profile email", "state": state,
    }
    return doc["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)


def exchange_code(cfg: dict, code: str, redirect_uri: str) -> dict:
    doc = discovery(cfg)
    form = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": cfg.get("client_id", ""),
        "scope": cfg.get("scope") or "openid profile email",
    }
    if cfg.get("client_secret"):
        form["client_secret"] = cfg["client_secret"]
    data = _post(doc["token_endpoint"], form)
    token = data.get("id_token") or data.get("access_token")
    if not token:
        raise OidcError("the provider returned no token")
    return _claims_from_token(token)


def account_name(claims: dict) -> str:
    for key in ("preferred_username", "upn", "email", "unique_name", "name", "sub"):
        if claims.get(key):
            return str(claims[key])
    return "unknown"


def groups_of(claims: dict) -> list[str]:
    """Entra sends group object ids in 'groups', names in 'roles' or 'wids'."""
    found: list[str] = []
    for key in ("groups", "roles", "wids"):
        value = claims.get(key)
        if isinstance(value, list):
            found += [str(v) for v in value]
        elif isinstance(value, str):
            found += [v.strip() for v in value.split(",") if v.strip()]
    return found


def role_for(cfg: dict, claims: dict) -> str:
    """admin, viewer, or an empty string when the account is not allowed in."""
    groups = {g.lower() for g in groups_of(claims)}
    admins = {g.lower() for g in cfg.get("allowed_groups") or []}
    viewers = {g.lower() for g in cfg.get("viewer_groups") or []}
    if not admins and not viewers:
        return "admin"                       # no lists configured: anyone in the tenant
    if admins & groups:
        return "admin"
    if viewers & groups:
        return "viewer"
    return ""
