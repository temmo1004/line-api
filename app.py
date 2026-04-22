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
    provision_user_bridge, strip_data_uri, extract_list,
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

def _sync_profile_from_bridge(uid):
    """Fetch /me from bridge and persist line_mid + profile if not yet stored."""
    try:
        r = bridge_get("/me", timeout=8, user_id=uid)
        if not r.ok:
            return
        me = r.json()
        if not isinstance(me, dict):
            return
        mid = (me.get("mid") or "").strip()
        if not mid:
            return
        col_users.update_one(
            {"_id": ObjectId(uid)},
            {"$set": {"line_mid": mid}},
        )
        col_line_cache.update_one(
            {"user_id": uid},
            {"$set": {"profile": me}},
            upsert=True,
        )
        logging.info("[sync_profile] uid=%s mid=%s", uid, mid)
    except Exception as e:
        logging.warning("[sync_profile] uid=%s: %s", uid, e)


@app.route("/v1/status", methods=["GET"])
@api_key_required("read")
@limiter.limit("60 per minute")
def api_v1_status():
    uid = request.api_user_id
    u = col_users.find_one({"_id": ObjectId(uid)}, {"line_status": 1, "line_status_at": 1, "line_mid": 1, "bridge_endpoint": 1})
    cache = col_line_cache.find_one({"user_id": uid}, {"profile": 1}) or {}
    profile = cache.get("profile") or {}
    line_mid = (u or {}).get("line_mid") or profile.get("mid") or None
    # Auto-heal: if logged in but no MID stored, fetch from bridge
    if not line_mid and (u or {}).get("line_status") == "logged_in":
        _sync_profile_from_bridge(uid)
        u = col_users.find_one({"_id": ObjectId(uid)}, {"line_status": 1, "line_status_at": 1, "line_mid": 1})
        line_mid = (u or {}).get("line_mid") or None
    return jsonify({
        "ok": True,
        "line_logged_in": (u or {}).get("line_status") == "logged_in",
        "line_status": (u or {}).get("line_status") or "unknown",
        "line_status_at": (u["line_status_at"].isoformat() if u and isinstance(u.get("line_status_at"), datetime) else None),
        "line_mid": line_mid,
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


@app.route("/v1/contacts/refresh", methods=["POST"])
@api_key_required("read")
@limiter.limit("10 per minute")
def api_v1_contacts_refresh():
    uid = request.api_user_id
    try:
        r = bridge_get("/contacts", timeout=30, user_id=uid)
        if not r.ok:
            return jsonify({"ok": False, "error": "bridge_error"}), 502
        contacts = extract_list(r, "contacts")
        if not contacts:
            return jsonify({"ok": False, "error": "bridge_returned_empty", "hint": "bridge may still be initializing"}), 503
        col_line_cache.update_one(
            {"user_id": uid},
            {"$set": {"contacts": contacts, "updated_at": datetime.utcnow()}},
            upsert=True,
        )
        return jsonify({"ok": True, "contacts_count": len(contacts)})
    except Exception as e:
        logging.warning("[v1/contacts/refresh] uid=%s: %s", uid, e)
        return jsonify({"ok": False, "error": "bridge_unreachable"}), 503


@app.route("/v1/groups", methods=["GET"])
@api_key_required("read")
@limiter.limit("30 per minute")
def api_v1_groups():
    uid = request.api_user_id
    cache = col_line_cache.find_one({"user_id": uid}, {"groups": 1}) or {}
    groups = cache.get("groups") or []
    return jsonify({"ok": True, "count": len(groups), "groups": groups})


@app.route("/v1/groups/refresh", methods=["POST"])
@api_key_required("read")
@limiter.limit("10 per minute")
def api_v1_groups_refresh():
    uid = request.api_user_id
    try:
        r = bridge_get("/groups", timeout=30, user_id=uid)
        if not r.ok:
            return jsonify({"ok": False, "error": "bridge_error"}), 502
        groups = extract_list(r, "groups")
        if not groups:
            return jsonify({"ok": False, "error": "bridge_returned_empty", "hint": "bridge may still be initializing"}), 503
        col_line_cache.update_one(
            {"user_id": uid},
            {"$set": {"groups": groups, "updated_at": datetime.utcnow()}},
            upsert=True,
        )
        return jsonify({"ok": True, "groups_count": len(groups)})
    except Exception as e:
        logging.warning("[v1/groups/refresh] uid=%s: %s", uid, e)
        return jsonify({"ok": False, "error": "bridge_unreachable"}), 503


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
            d = r.json() if r.ok else {}
            return {"to": mid, "ok": bool(isinstance(d, dict) and d.get("ok"))}
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
    since = request.args.get("since")
    if since:
        try:
            q["created_time"] = {"$gt": int(since)}
        except (ValueError, TypeError):
            pass
    sort_order = 1 if since else -1
    rows = list(col_messages.find(q, {"_id": 0, "user_id": 0}).sort("created_time", sort_order).limit(limit))
    for r in rows:
        if r.get("created_time") is not None:
            try:
                r["created_time"] = int(r["created_time"])
            except Exception:
                pass
        if isinstance(r.get("synced_at"), datetime):
            r["synced_at"] = r["synced_at"].isoformat()
    return jsonify({"ok": True, "count": len(rows), "messages": rows})


@app.route("/v1/chats", methods=["GET"])
@api_key_required("read")
@limiter.limit("60 per minute")
def api_v1_chats():
    uid = request.api_user_id
    try:
        limit = min(int(request.args.get("limit", "100")), 500)
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "invalid_limit"}), 400
    pipeline = [
        {"$match": {"user_id": uid}},
        {"$sort": {"created_time": -1}},
        {"$group": {
            "_id": "$peer",
            "last_text": {"$first": "$text"},
            "last_time": {"$first": "$created_time"},
            "last_content_type": {"$first": "$content_type"},
            "count": {"$sum": 1},
        }},
        {"$sort": {"last_time": -1}},
        {"$limit": limit},
    ]
    rows = list(col_messages.aggregate(pipeline))
    cache = col_line_cache.find_one({"user_id": uid}) or {}
    contacts = {c["mid"]: c for c in (cache.get("contacts") or []) if c.get("mid")}
    chats = []
    for r in rows:
        peer = r["_id"]
        c = contacts.get(peer) or {}
        peer_name = c.get("name") or c.get("realName") or (peer or "")[-6:]
        last_time = r.get("last_time")
        try:
            last_time = int(last_time) if last_time is not None else None
        except Exception:
            pass
        chats.append({
            "peer": peer,
            "peer_name": peer_name,
            "last_text": r.get("last_text"),
            "last_time": last_time,
            "last_content_type": r.get("last_content_type"),
            "count": r.get("count", 0),
        })
    return jsonify({"ok": True, "chats": chats})


@app.route("/api/_hook/state", methods=["POST"])
@limiter.limit("60 per minute")
def api_hook_state():
    """Bridge pushes state-change events here (same pattern as line-codex)."""
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
    if not token:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    user = col_users.find_one({"bridge_token": token})
    if not user:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    uid = str(user["_id"])
    data = request.get_json(force=True) or {}
    new_state = data.get("to") or data.get("state")
    col_users.update_one(
        {"_id": user["_id"]},
        {"$set": {"line_status": new_state, "line_status_at": datetime.utcnow()}},
    )
    if new_state == "logged_in" and not user.get("line_mid"):
        _sync_profile_from_bridge(uid)
    logging.info("[hook/state] uid=%s → %s", uid, new_state)
    return jsonify({"ok": True})


@app.route("/v1/admin/fix-peer", methods=["POST"])
@api_key_required("admin")
@limiter.limit("5 per minute")
def api_v1_fix_peer():
    """One-shot: fix sent messages where peer was incorrectly set to own MID."""
    uid = request.api_user_id
    user = col_users.find_one({"_id": ObjectId(uid)})
    self_mid = (user or {}).get("line_mid", "").strip()
    if not self_mid:
        return jsonify({"ok": False, "error": "line_mid not set for user"}), 400
    broken = list(col_messages.find(
        {"user_id": uid, "from": self_mid, "peer": self_mid, "to": {"$exists": True, "$ne": None}},
        {"_id": 1, "to": 1},
    ))
    fixed = 0
    for m in broken:
        to_mid = m.get("to")
        if to_mid and to_mid != self_mid:
            col_messages.update_one({"_id": m["_id"]}, {"$set": {"peer": to_mid}})
            fixed += 1
    logging.info("[fix-peer] uid=%s self_mid=%s broken=%d fixed=%d", uid, self_mid, len(broken), fixed)
    return jsonify({"ok": True, "self_mid": self_mid, "broken": len(broken), "fixed": fixed})


@app.route("/")
def index():
    return jsonify({
        "service": "LINE API",
        "version": "1.0",
        "auth": "X-API-Key: pk_...  (admin key 可加 X-User-Id 委派)",
        "docs": "/docs",
        "endpoints": [
            {"method": "GET",  "path": "/v1/status",                  "scope": "read",  "desc": "LINE 連線狀態"},
            {"method": "GET",  "path": "/v1/qr",                      "scope": "admin", "desc": "QR code 圖片 (PNG)，?refresh=1 強制重整"},
            {"method": "GET",  "path": "/v1/qr-status",               "scope": "read",  "desc": "QR 登入狀態 (waiting/pin_required/logged_in)"},
            {"method": "POST", "path": "/v1/login-password",          "scope": "admin", "desc": "密碼登入", "body": {"email": "str", "password": "str"}},
            {"method": "POST", "path": "/v1/login-password-verify",   "scope": "admin", "desc": "簡訊驗證碼", "body": {"code": "str"}},
            {"method": "POST", "path": "/v1/users/logout",            "scope": "admin", "desc": "登出 LINE"},
            {"method": "GET",  "path": "/v1/contacts",                "scope": "read",  "desc": "從快取讀取聯絡人"},
            {"method": "POST", "path": "/v1/contacts/refresh",        "scope": "read",  "desc": "從 bridge 更新聯絡人快取"},
            {"method": "GET",  "path": "/v1/groups",                  "scope": "read",  "desc": "從快取讀取群組"},
            {"method": "POST", "path": "/v1/groups/refresh",          "scope": "read",  "desc": "從 bridge 更新群組快取"},
            {"method": "GET",  "path": "/v1/messages",                "scope": "read",  "desc": "訊息記錄", "params": {"peer": "MID (選)", "limit": "int 最多200", "since": "Unix ms"}},
            {"method": "GET",  "path": "/v1/chats",                   "scope": "read",  "desc": "聊天列表含名稱", "params": {"limit": "int 最多500"}},
            {"method": "POST", "path": "/v1/send",                    "scope": "send",  "desc": "傳送文字", "body": {"to": "MID", "text": "str"}},
            {"method": "POST", "path": "/v1/send-image",              "scope": "send",  "desc": "傳送圖片", "body": {"to": "MID", "image_base64": "str", "mime_type": "選"}},
            {"method": "POST", "path": "/v1/broadcast",               "scope": "send",  "desc": "批次傳送 (最多50)", "body": {"to": ["MID"], "text": "str"}},
            {"method": "POST", "path": "/v1/schedule",                "scope": "send",  "desc": "排程傳送 (台灣時間)", "body": {"to": ["MID"], "text": "str", "send_at": "ISO datetime"}},
        ],
    })


@app.route("/docs")
def docs():
    import pathlib
    md = pathlib.Path(__file__).parent / "API.md"
    if md.exists():
        from flask import Response
        return Response(md.read_text(encoding="utf-8"), mimetype="text/markdown; charset=utf-8")
    return jsonify({"ok": False, "error": "docs not found"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
