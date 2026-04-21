#!/usr/bin/env python3
"""LINE API Service — standalone /v1/* endpoints, X + LINE concept."""
import os
import logging
import functools
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from bson import ObjectId
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from db import (
    MONGO_URI, col_users, col_api_keys, col_api_usage,
    col_line_cache, col_messages, col_schedules,
)
from auth import api_key_required
from bridge import (
    bridge_get, bridge_post, resolve_bridge,
    provision_user_bridge, strip_data_uri,
    LINE_BRIDGE_URL,
)

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

_CORS_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
CORS(app, resources={r"/v1/*": {"origins": _CORS_ORIGINS}})
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=MONGO_URI if MONGO_URI.startswith("mongodb") else "memory://",
)


@app.after_request
def _strip_server(resp):
    resp.headers["Server"] = "nginx"
    resp.headers.pop("X-Powered-By", None)
    return resp


@app.errorhandler(Exception)
def _handle_exc(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    logging.exception("unhandled exception")
    return jsonify({"ok": False, "error": "internal"}), 500


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_tw_time(s):
    """Accept naive TW-local ISO or timezone-aware ISO. Return naive UTC."""
    try:
        dt = datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        raise ValueError("時間格式錯誤")
    if dt.tzinfo is None:
        dt = dt - timedelta(hours=8)
    else:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.utcnow()
    if dt < now - timedelta(minutes=5):
        raise ValueError("排程時間不能在過去")
    if dt > now + timedelta(days=90):
        raise ValueError("排程時間不能超過 90 天")
    return dt


def _verify_attempt_check(uid, limit=5, window_seconds=600):
    from datetime import timedelta
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=window_seconds)
    try:
        recent = col_api_usage.count_documents({
            "user_id": uid,
            "path": "/v1/login-password-verify",
            "ts": {"$gte": cutoff},
        })
        return recent < limit, recent
    except Exception:
        return True, 0


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/v1/status", methods=["GET"])
@api_key_required("read")
@limiter.limit("60 per minute")
def api_v1_status():
    uid = request.api_user_id
    u = col_users.find_one({"_id": ObjectId(uid)}, {"line_status": 1, "line_status_at": 1, "line_mid": 1, "bridge_endpoint": 1})
    cache = col_line_cache.find_one({"user_id": uid}, {"profile": 1}) or {}
    profile = cache.get("profile") or {}
    return jsonify({
        "ok": True,
        "line_logged_in": (u or {}).get("line_status") == "logged_in",
        "line_status": (u or {}).get("line_status") or "unknown",
        "line_status_at": (u["line_status_at"].isoformat() if u and isinstance(u.get("line_status_at"), datetime) else None),
        "line_mid": (u or {}).get("line_mid") or profile.get("mid") or None,
        "line_name": profile.get("displayName") or profile.get("name") or None,
        "provisioned": bool((u or {}).get("bridge_endpoint")),
    })


@app.route("/v1/qr", methods=["GET"])
@api_key_required("admin")
@limiter.limit("30 per minute")
def api_v1_qr():
    uid = request.api_user_id
    ep, _ = resolve_bridge(uid)
    if ep == LINE_BRIDGE_URL.rstrip("/"):
        if not provision_user_bridge(uid):
            return jsonify({"ok": False, "error": "provision_failed"}), 503
    u = col_users.find_one({"_id": ObjectId(uid)}, {"line_status": 1})
    if u and u.get("line_status") == "logged_in":
        return jsonify({"ok": False, "error": "already_connected"}), 409
    if request.args.get("refresh") == "1":
        try:
            bridge_post("/refresh-qr", {}, timeout=10, user_id=uid)
        except Exception:
            pass
    try:
        r = bridge_get("/qr-canvas", timeout=10, user_id=uid)
        if not r.ok:
            return jsonify({"ok": False, "error": "qr_unavailable"}), 503
        resp = app.make_response(r.content)
        resp.headers["Content-Type"] = "image/png"
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception as e:
        logging.warning(f"[v1/qr] uid={uid} error: {e}")
        return jsonify({"ok": False, "error": "qr_unavailable"}), 503


@app.route("/v1/qr-status", methods=["GET"])
@api_key_required("read")
@limiter.limit("60 per minute")
def api_v1_qr_status():
    uid = request.api_user_id
    try:
        r = bridge_get("/status", timeout=5, user_id=uid)
        d = r.json() if r.ok else {}
        logged_in = d.get("loggedIn", False)
        pin = d.get("pin")
        state = "logged_in" if logged_in else ("pin_required" if pin else "waiting")
        return jsonify({"ok": True, "state": state, "pin": pin, "logged_in": logged_in})
    except Exception:
        return jsonify({"ok": False, "error": "bridge_unreachable"}), 503


@app.route("/v1/users/logout", methods=["POST"])
@api_key_required("admin")
@limiter.limit("10 per minute")
def api_v1_logout():
    uid = request.api_user_id
    try:
        bridge_post("/logout", {}, timeout=15, user_id=uid)
    except Exception as e:
        logging.warning(f"[v1/logout] uid={uid} error: {e}")
    return jsonify({"ok": True})


@app.route("/v1/login-password", methods=["POST"])
@api_key_required("admin")
@limiter.limit("5 per minute")
def api_v1_login_password():
    uid = request.api_user_id
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"ok": False, "error": "email_and_password_required"}), 400
    ep, _ = resolve_bridge(user_id=uid)
    if ep == LINE_BRIDGE_URL.rstrip("/"):
        if not provision_user_bridge(uid):
            return jsonify({"ok": False, "error": "provision_failed"}), 503
    try:
        r = bridge_post("/login-password", {"email": email, "password": password}, timeout=30, user_id=uid)
        return jsonify(r.json() if r.ok else {"ok": False, "error": "bridge_error"})
    except Exception as e:
        logging.warning(f"[v1/login-password] uid={uid} error: {e}")
        return jsonify({"ok": False, "error": "login_failed"}), 502


@app.route("/v1/login-password-verify", methods=["POST"])
@api_key_required("admin")
@limiter.limit("10 per minute")
def api_v1_login_password_verify():
    uid = request.api_user_id
    ok_attempts, used = _verify_attempt_check(uid)
    if not ok_attempts:
        return jsonify({"ok": False, "error": "too_many_attempts", "attempts_used": used}), 429
    data = request.get_json(force=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "code_required"}), 400
    try:
        r = bridge_post("/login-password-verify", {"code": code}, timeout=20, user_id=uid)
        return jsonify(r.json() if r.ok else {"ok": False, "error": "bridge_error"})
    except Exception as e:
        logging.warning(f"[v1/login-password-verify] uid={uid} error: {e}")
        return jsonify({"ok": False, "error": "verify_failed"}), 502


@app.route("/v1/contacts", methods=["GET"])
@api_key_required("read")
@limiter.limit("30 per minute")
def api_v1_contacts():
    uid = request.api_user_id
    cache = col_line_cache.find_one({"user_id": uid}, {"contacts": 1}) or {}
    contacts = cache.get("contacts") or []
    return jsonify({"ok": True, "count": len(contacts), "contacts": contacts})


@app.route("/v1/groups", methods=["GET"])
@api_key_required("read")
@limiter.limit("30 per minute")
def api_v1_groups():
    uid = request.api_user_id
    cache = col_line_cache.find_one({"user_id": uid}, {"groups": 1}) or {}
    groups = cache.get("groups") or []
    return jsonify({"ok": True, "count": len(groups), "groups": groups})


@app.route("/v1/send", methods=["POST"])
@api_key_required("send")
@limiter.limit("60 per minute")
def api_v1_send():
    uid = request.api_user_id
    data = request.get_json(force=True) or {}
    to = (data.get("to") or "").strip()
    text = (data.get("text") or "").strip()
    if not to or not text:
        return jsonify({"ok": False, "error": "to and text required"}), 400
    try:
        r = bridge_post("/send", {"to": to, "text": text}, timeout=30, user_id=uid)
        return jsonify(r.json() if r.ok else {"ok": False, "error": "send failed", "status": r.status_code})
    except Exception as e:
        logging.warning(f"[v1/send] uid={uid} error: {e}")
        return jsonify({"ok": False, "error": "send failed"}), 502


@app.route("/v1/send-image", methods=["POST"])
@api_key_required("send")
@limiter.limit("20 per minute")
def api_v1_send_image():
    uid = request.api_user_id
    data = request.get_json(force=True) or {}
    to = (data.get("to") or "").strip()
    img = (data.get("image_base64") or "").strip()
    if not to or not img:
        return jsonify({"ok": False, "error": "to and image_base64 required"}), 400
    img_b64, auto_mime = strip_data_uri(img)
    try:
        r = bridge_post("/send-image", {
            "to": to,
            "imageBase64": img_b64,
            "mimeType": data.get("mime_type") or auto_mime,
        }, timeout=60, user_id=uid)
        return jsonify(r.json() if r.ok else {"ok": False, "error": "send failed"})
    except Exception as e:
        logging.warning(f"[v1/send-image] uid={uid} error: {e}")
        return jsonify({"ok": False, "error": "send failed"}), 502


@app.route("/v1/broadcast", methods=["POST"])
@api_key_required("send")
@limiter.limit("10 per minute")
def api_v1_broadcast():
    uid = request.api_user_id
    data = request.get_json(force=True) or {}
    recipients = data.get("to") or []
    text = (data.get("text") or "").strip()
    if not isinstance(recipients, list) or not recipients or not text:
        return jsonify({"ok": False, "error": "to (array) and text required"}), 400
    if len(recipients) > 50:
        return jsonify({"ok": False, "error": "max 50 recipients per broadcast call"}), 400

    def _one(mid):
        try:
            r = bridge_post("/send", {"to": mid, "text": text}, timeout=15, user_id=uid)
            return {"to": mid, "ok": bool(r.ok and (r.json() or {}).get("ok"))}
        except Exception as e:
            return {"to": mid, "ok": False, "error": str(e)[:60]}

    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_one, mid): mid for mid in recipients}
        try:
            for fut in as_completed(futures, timeout=45):
                try:
                    results.append(fut.result())
                except Exception:
                    results.append({"to": futures[fut], "ok": False, "error": "timeout"})
        except TimeoutError:
            for fut, mid in futures.items():
                if not fut.done():
                    results.append({"to": mid, "ok": False, "error": "overall_timeout"})

    success = sum(1 for r in results if r["ok"])
    return jsonify({"ok": True, "total": len(results), "success": success, "results": results})


@app.route("/v1/schedule", methods=["POST"])
@api_key_required("send")
@limiter.limit("30 per minute")
def api_v1_schedule():
    uid = request.api_user_id
    data = request.get_json(force=True) or {}
    mids = data.get("to") or []
    text = (data.get("text") or "").strip()
    send_at = data.get("send_at") or ""
    if not mids or not text or not send_at:
        return jsonify({"ok": False, "error": "to, text, send_at required"}), 400
    try:
        parsed = _parse_tw_time(send_at)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    doc = {
        "user_id": uid,
        "mids": mids,
        "text": text,
        "image": data.get("image_base64"),
        "send_at": parsed,
        "status": "pending",
        "created_at": datetime.utcnow(),
        "created_via": "api_v1",
        "api_key_id": request.api_key_id,
    }
    res = col_schedules.insert_one(doc)
    return jsonify({"ok": True, "schedule_id": str(res.inserted_id), "send_at": parsed.isoformat()})


@app.route("/v1/messages", methods=["GET"])
@api_key_required("read")
@limiter.limit("60 per minute")
def api_v1_messages():
    uid = request.api_user_id
    peer = request.args.get("peer", "").strip()
    try:
        limit = min(int(request.args.get("limit", "50")), 200)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "invalid_limit"}), 400
    q = {"user_id": uid}
    if peer:
        q["peer"] = peer
    rows = list(col_messages.find(q, {"_id": 0, "user_id": 0}).sort("created_time", -1).limit(limit))
    for r in rows:
        if r.get("created_time") is not None:
            try:
                r["created_time"] = int(r["created_time"])
            except Exception:
                pass
        if isinstance(r.get("synced_at"), datetime):
            r["synced_at"] = r["synced_at"].isoformat()
    return jsonify({"ok": True, "count": len(rows), "messages": rows})


@app.route("/v1/_bootstrap-key", methods=["POST"])
def bootstrap_key():
    """One-time bootstrap: create first API key. Requires BOOTSTRAP_SECRET env var."""
    secret = os.environ.get("BOOTSTRAP_SECRET", "")
    if not secret:
        return jsonify({"ok": False, "error": "not_available"}), 404
    data = request.get_json(force=True) or {}
    if data.get("secret") != secret:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    email = (data.get("email") or "").strip()
    user = col_users.find_one({"email": email}) if email else col_users.find_one({})
    if not user:
        return jsonify({"ok": False, "error": "no_user_found"}), 404
    uid = str(user["_id"])
    import hashlib, secrets as _sec
    raw = _sec.token_urlsafe(32)
    plaintext = f"pk_live_{raw}"
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    col_api_keys.insert_one({
        "user_id": uid,
        "key_hash": key_hash,
        "scopes": ["read", "send", "admin"],
        "label": data.get("label", "realty-line system"),
        "created_at": datetime.utcnow(),
    })
    return jsonify({"ok": True, "key": plaintext, "user_id": uid, "email": user.get("email")})


@app.route("/")
def index():
    return jsonify({
        "service": "LINE API",
        "version": "1.0",
        "endpoints": [
            "GET  /v1/status",
            "GET  /v1/qr",
            "GET  /v1/qr-status",
            "POST /v1/users/logout",
            "POST /v1/login-password",
            "POST /v1/login-password-verify",
            "GET  /v1/contacts",
            "GET  /v1/groups",
            "POST /v1/send",
            "POST /v1/send-image",
            "POST /v1/broadcast",
            "POST /v1/schedule",
            "GET  /v1/messages",
        ],
        "auth": "X-API-Key: pk_...",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
