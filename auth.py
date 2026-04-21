import hashlib
import functools
import logging
from datetime import datetime
from bson import ObjectId
from flask import request, jsonify
from db import col_api_keys, col_api_usage, col_users

API_SCOPES = {"read", "send", "admin"}


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def api_key_required(*required_scopes):
    if len(required_scopes) == 1 and callable(required_scopes[0]):
        return _build_wrapper(())(required_scopes[0])
    return _build_wrapper(required_scopes)


def _build_wrapper(required_scopes):
    def decorator(f):
        @functools.wraps(f)
        def _wrap(*args, **kwargs):
            header = request.headers.get("X-API-Key") or ""
            if not header.startswith("pk_"):
                return jsonify({"ok": False, "error": "missing or invalid api key"}), 401
            h = _hash_key(header)
            rec = col_api_keys.find_one({"key_hash": h, "revoked_at": {"$exists": False}})
            if not rec:
                return jsonify({"ok": False, "error": "invalid or revoked api key"}), 401
            key_scopes = set(rec.get("scopes") or ["send", "read", "admin"])
            if required_scopes:
                needed = set(required_scopes)
                if "admin" not in key_scopes and not needed.issubset(key_scopes):
                    return jsonify({
                        "ok": False,
                        "error": "insufficient_scope",
                        "required": list(needed),
                        "granted": sorted(key_scopes),
                    }), 403
            # Admin keys may delegate to another user via X-User-Id header
            target_uid = rec["user_id"]
            if "admin" in key_scopes:
                override = (request.headers.get("X-User-Id") or "").strip()
                if override and ObjectId.is_valid(override):
                    target_uid = override
            user = col_users.find_one({"_id": ObjectId(target_uid)}, {"password_hash": 0})
            if not user:
                return jsonify({"ok": False, "error": "api key user not found"}), 401
            request.api_user = user
            request.api_user_id = str(user["_id"])
            request.api_key_id = str(rec["_id"])
            request.api_key_scopes = key_scopes
            try:
                col_api_keys.update_one({"_id": rec["_id"]}, {
                    "$set": {"last_used_at": datetime.utcnow()},
                    "$inc": {"usage_count": 1},
                })
                col_api_usage.insert_one({
                    "user_id": str(user["_id"]),
                    "key_id": str(rec["_id"]),
                    "path": request.path,
                    "method": request.method,
                    "ts": datetime.utcnow(),
                    "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
                })
            except Exception as e:
                logging.warning(f"[api_usage] {e}")
            return f(*args, **kwargs)
        return _wrap
    return decorator
