import os
import logging
import requests
from bson import ObjectId
from db import col_users

LINE_BRIDGE_URL   = os.environ.get("LINE_BRIDGE_URL", "https://line-chromeos.zeabur.app")
LINE_BRIDGE_TOKEN = os.environ.get("LINE_BRIDGE_TOKEN", "")
ORCHESTRATOR_URL  = os.environ.get("ORCHESTRATOR_URL", "").rstrip("/")
ORCHESTRATOR_TOKEN = os.environ.get("ORCHESTRATOR_TOKEN", "")


def resolve_bridge(user_id):
    """Return (endpoint, token) for the given user, falling back to global defaults."""
    if user_id and ObjectId.is_valid(user_id):
        u = col_users.find_one(
            {"_id": ObjectId(user_id)},
            {"bridge_endpoint": 1, "bridge_token": 1},
        )
        if u and u.get("bridge_endpoint"):
            return u["bridge_endpoint"].rstrip("/"), (u.get("bridge_token") or "")
    return LINE_BRIDGE_URL.rstrip("/"), LINE_BRIDGE_TOKEN


def _headers(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def bridge_get(path, timeout=8, user_id=None):
    endpoint, token = resolve_bridge(user_id)
    return requests.get(f"{endpoint}{path}", headers=_headers(token), timeout=timeout)


def bridge_post(path, payload, timeout=30, user_id=None):
    endpoint, token = resolve_bridge(user_id)
    return requests.post(f"{endpoint}{path}", json=payload, headers=_headers(token), timeout=timeout)


def extract_list(response, key="items"):
    """Normalize bridge list responses.

    Bridge endpoints like /contacts and /groups return a bare JSON array,
    not {"contacts": [...]}. This helper handles both shapes so callers
    never need isinstance() checks.

    Returns a plain Python list, or [] on any failure.
    """
    try:
        data = response.json()
    except Exception:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Try the given key, then common fallbacks
        for k in (key, "contacts", "groups", "items", "data", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def provision_user_bridge(user_id):
    """Spawn a bridge container for the user via orchestrator. Returns (endpoint, token) or None."""
    if not ORCHESTRATOR_URL or not ORCHESTRATOR_TOKEN:
        return None
    if ObjectId.is_valid(user_id):
        u = col_users.find_one({"_id": ObjectId(user_id)}, {"bridge_endpoint": 1, "bridge_token": 1})
        if u and u.get("bridge_endpoint") and u.get("bridge_token"):
            return u["bridge_endpoint"], u["bridge_token"]
    try:
        r = requests.post(
            f"{ORCHESTRATOR_URL}/spawn",
            headers={"X-Admin-Token": ORCHESTRATOR_TOKEN, "Content-Type": "application/json"},
            json={"user_id": str(user_id)},
            timeout=30,
        )
        d = r.json()
        if not d.get("ok"):
            logging.warning(f"[orchestrator] spawn failed: {d}")
            return None
        endpoint = d["endpoint"].rstrip("/")
        token = d["token"]
        col_users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"bridge_endpoint": endpoint, "bridge_token": token}},
            upsert=True,
        )
        return endpoint, token
    except Exception as e:
        logging.warning(f"[orchestrator] provision error: {e}")
        return None


def strip_data_uri(b64: str) -> tuple[str, str]:
    if not b64:
        return b64, "image/jpeg"
    if b64.startswith("data:"):
        try:
            header, payload = b64.split(",", 1)
            mime = header.split(";")[0].replace("data:", "") or "image/jpeg"
            return payload, mime
        except ValueError:
            return b64, "image/jpeg"
    return b64, "image/jpeg"
