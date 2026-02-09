import os
import json
import time
import uuid
import sqlite3
import datetime
import random

import eventlet
eventlet.monkey_patch()

from zoneinfo import ZoneInfo
TZ = ZoneInfo("Asia/Taipei")

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, join_room, emit

# ================== App ==================
app = Flask(__name__)

# ★ 記得把這裡的網域改成你實際在用的 Netlify 網址
ALLOWED_ORIGINS = [
    "https://partnerburger.netlify.app",
    "https://illustrious-centaur-327b59.netlify.app",
    "https://silly-marzipan-9f27a5.netlify.app",
]

CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGINS}})

socketio = SocketIO(
    app,
    cors_allowed_origins=ALLOWED_ORIGINS,
    async_mode="eventlet",
    ping_interval=20,
    ping_timeout=30
)

DB_FILE = "orders.db"
SESSION_TTL_SECONDS = 24 * 60 * 60  # 24 小時

# 可用的訂單狀態
ORDER_STATUS_ALLOWED = {"new", "making", "done", "cancelled"}


# ================== DB ==================
def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db():
    with get_conn() as conn:
        c = conn.cursor()

        c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            table_num TEXT,
            time TEXT,
            items TEXT,
            status TEXT DEFAULT 'new'
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            cart_json TEXT NOT NULL,
            created_at TEXT,
            expires_at TEXT,
            updated_at TEXT
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS soldout (
            category_idx INTEGER,
            item_idx INTEGER,
            updated_at TEXT,
            PRIMARY KEY (category_idx, item_idx)
        )""")

        # ✅ 新增：目前叫號狀態（精準叫號用）
        c.execute("""
        CREATE TABLE IF NOT EXISTS call_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            code TEXT DEFAULT '',
            updated_at INTEGER DEFAULT 0
        )""")
        c.execute("INSERT OR IGNORE INTO call_state (id, code, updated_at) VALUES (1, '', 0)")

        conn.commit()


init_db()


# ================== Helpers ==================
def now_dt():
    return datetime.datetime.now(TZ)


def now_str():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def expires_str():
    return (now_dt() + datetime.timedelta(seconds=SESSION_TTL_SECONDS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def to_ts_ms(s):
    try:
        d = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        return int(d.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def is_valid_sid(sid: str) -> bool:
    sid = str(sid or "").strip()
    return (len(sid) == 4 and sid.isdigit())


def normalize_cart_item(item: dict) -> dict:
    return {
        "lineId": item.get("lineId") or uuid.uuid4().hex,
        "name": str(item.get("name", "")),
        "enName": item.get("enName"),
        "price": int(item.get("price", 0)),
        "qty": max(1, int(item.get("qty", 1))),
        "remark": str(item.get("remark", "")),
        "temp": item.get("temp"),
        "addOns": item.get("addOns", []),
        "addedBy": str(item.get("addedBy", "")).strip()[:20] or None,
        # 前端如果有帶分類，就會被存起來（吃堡/單點/飲品/甜點）
        "category": item.get("category"),
    }


def calc_total(cart):
    total = 0
    for it in cart or []:
        add = sum(
            int(a.get("price", 0))
            for a in it.get("addOns", [])
            if isinstance(a, dict)
        )
        total += (int(it.get("price", 0)) + add) * int(it.get("qty", 1))
    return total


# ================== Call State (精準叫號) ==================
def get_call_state():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT code, updated_at FROM call_state WHERE id=1")
        row = c.fetchone()
    if not row:
        return {"code": "", "updated_at": 0}
    return {"code": row[0] or "", "updated_at": int(row[1] or 0)}


def set_call_code(code: str):
    code = (code or "").strip()
    now_ts = int(time.time())
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE call_state SET code=?, updated_at=? WHERE id=1", (code, now_ts))
        conn.commit()
    socketio.emit("call_update", {"ok": True, "code": code, "updatedAt": now_ts})
    return True


def get_session_id_by_order_id(oid: int):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT session_id FROM orders WHERE id=?", (oid,))
        row = c.fetchone()
    return row[0] if row else None


# ================== Session ==================
def session_is_active(session_id):
    if not is_valid_sid(session_id):
        return False

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT expires_at FROM sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()

    if not row or not row[0]:
        return False

    try:
        exp = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=TZ
        )
        return now_dt() < exp
    except Exception:
        return False


def session_exists(session_id) -> bool:
    """只檢查資料列是否存在（不管過期與否）"""
    if not is_valid_sid(session_id):
        return False
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM sessions WHERE session_id=? LIMIT 1", (session_id,))
        return c.fetchone() is not None


def ensure_session(session_id, force_reset=False):
    if not session_id:
        return
    if not is_valid_sid(session_id):
        return

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT expires_at FROM sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()

        if not row:
            c.execute(
                """
                INSERT INTO sessions (session_id, cart_json, created_at, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """,
                (session_id, "[]", now_str(), expires_str(), now_str()),
            )
            conn.commit()
            return

        expired = False
        if row[0]:
            try:
                exp = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=TZ
                )
                expired = now_dt() >= exp
            except Exception:
                expired = False

        if expired or force_reset:
            c.execute(
                """
                UPDATE sessions
                SET cart_json=?, created_at=?, expires_at=?, updated_at=?
                WHERE session_id=?
            """,
                ("[]", now_str(), expires_str(), now_str(), session_id),
            )
            conn.commit()


def create_unique_session_id():
    for _ in range(300):
        sid = str(random.randint(1000, 9999))
        if not session_is_active(sid):
            ensure_session(sid, force_reset=True)
            return sid
    raise RuntimeError("No available sessionId")


def get_session_cart(session_id):
    if not is_valid_sid(session_id):
        return []
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT cart_json FROM sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()

    if not row:
        return []
    try:
        return json.loads(row[0])
    except Exception:
        return []


def save_session_cart(session_id, cart):
    if not is_valid_sid(session_id):
        return
    cart = [normalize_cart_item(x) for x in cart or []]
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO sessions (session_id, cart_json, created_at, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                cart_json=excluded.cart_json,
                updated_at=excluded.updated_at
        """,
            (
                session_id,
                json.dumps(cart, ensure_ascii=False),
                now_str(),
                expires_str(),
                now_str(),
            ),
        )
        conn.commit()


def require_active_session_or_reject(sid: str, room: str = None, reason: str = "查無此訂單代碼或已過期"):
    """Socket 用：不存在/過期就拒絕，避免自動開單。"""
    if not is_valid_sid(sid) or not session_is_active(sid):
        # 前端有接 op_rejected
        emit("op_rejected", {"reason": reason}, room=room)
        return False
    return True


# ================== Orders ==================
def load_order_by_session(session_id):
    if not is_valid_sid(session_id):
        return None

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, table_num, time, items, status
            FROM orders
            WHERE session_id=?
            ORDER BY id DESC LIMIT 1
        """,
            (session_id,),
        )
        row = c.fetchone()

    if not row:
        return None

    oid, table, t, items, status = row
    items = json.loads(items) if items else []

    return {
        "id": oid,
        "sessionId": session_id,
        "tableNo": table,
        "time": t,
        "status": status,
        "items": items,
        "total": calc_total(items),
        "timestamp": to_ts_ms(t),
    }


def load_all_orders(limit=200):
    limit = max(1, min(int(limit or 200), 500))

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, session_id, table_num, time, items, status
            FROM orders
            ORDER BY id DESC
            LIMIT ?
        """,
            (limit,),
        )
        rows = c.fetchall()

    orders = []
    for oid, sid, table, t, items, status in rows:
        items_list = json.loads(items) if items else []
        orders.append(
            {
                "id": oid,
                "sessionId": sid,
                "tableNo": table,
                "time": t,
                "status": status,
                "items": items_list,
                "total": calc_total(items_list),
                "timestamp": to_ts_ms(t),
            }
        )
    return orders


def append_items_to_order(session_id, table, new_items):
    new_items = [normalize_cart_item(x) for x in new_items or []]
    if not new_items:
        return None

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, items FROM orders
            WHERE session_id=?
            ORDER BY id DESC LIMIT 1
        """,
            (session_id,),
        )
        row = c.fetchone()

        if not row:
            c.execute(
                """
                INSERT INTO orders (session_id, table_num, time, items)
                VALUES (?, ?, ?, ?)
            """,
                (session_id, table, now_str(), json.dumps(new_items, ensure_ascii=False)),
            )
            conn.commit()
            return c.lastrowid

        oid, old = row
        merged = json.loads(old or "[]") + new_items
        c.execute(
            """
            UPDATE orders SET items=?, table_num=? WHERE id=?
        """,
            (json.dumps(merged, ensure_ascii=False), table, oid),
        )
        conn.commit()
        return oid


# ================== Socket ==================
users_in_room = {}


def broadcast_state(session_id):
    socketio.emit(
        "session_state",
        {
            "sessionId": session_id,
            "cart": get_session_cart(session_id),
            "total": calc_total(get_session_cart(session_id)),
            "users": list(users_in_room.get(session_id, {}).values()),
        },
        room=session_id,
    )


@socketio.on("disconnect")
def on_disconnect():
    # 清掉 users_in_room 裡這個 socket
    for sid, members in list(users_in_room.items()):
        if request.sid in members:
            del members[request.sid]
            if not members:
                users_in_room.pop(sid, None)
            else:
                # 有人在房內才廣播更新
                broadcast_state(sid)
            break


@socketio.on("create_session")
def on_create_session(_):
    try:
        sid = create_unique_session_id()
        emit("create_session_result", {"ok": True, "sessionId": sid})
    except Exception as e:
        emit("create_session_result", {"ok": False, "msg": str(e)})


@socketio.on("join_session")
def on_join(data):
    data = data or {}
    sid = str(data.get("sessionId", "")).strip()
    name = str(data.get("nickname", "訪客")).strip()[:12] or "訪客"

    # ✅ 核心：加入只允許「已存在且 active」，不准自動建立
    if not is_valid_sid(sid) or not session_is_active(sid):
        emit("error_msg", {"msg": "查無此訂單代碼或已過期，請確認後再加入"})
        return

    join_room(sid)
    users_in_room.setdefault(sid, {})[request.sid] = {
        "sid": request.sid,
        "nickname": name,
    }
    broadcast_state(sid)


@socketio.on("cart_add")
def on_cart_add(data):
    data = data or {}
    sid = str(data.get("sessionId", "")).strip()

    if not require_active_session_or_reject(sid, reason="訂單代碼不存在或已過期，無法加入購物車"):
        return

    cart = get_session_cart(sid)
    cart.append(data.get("item", {}))
    save_session_cart(sid, cart)
    broadcast_state(sid)


@socketio.on("submit_cart_as_order")
def on_submit(data):
    data = data or {}
    sid = str(data.get("sessionId", "")).strip()
    table = data.get("table", "")

    if not require_active_session_or_reject(sid, reason="訂單代碼不存在或已過期，無法送出訂單"):
        return

    cart = get_session_cart(sid)
    oid = append_items_to_order(sid, table, cart)
    save_session_cart(sid, [])
    emit("submit_result", {"ok": True, "orderId": oid}, room=sid)
    emit(
        "order_detail_result",
        {"ok": True, "exists": True, "order": load_order_by_session(sid)},
        room=sid,
    )
    broadcast_state(sid)


# ✅ 補：前端會用到的 order_detail（不會建立 session，只回最新訂單）
@socketio.on("order_detail")
def on_order_detail(data):
    data = data or {}
    sid = str(data.get("sessionId", "")).strip()

    if not is_valid_sid(sid):
        emit("order_detail_result", {"ok": False, "msg": "invalid sessionId"})
        return

    o = load_order_by_session(sid)
    emit("order_detail_result", {"ok": True, "exists": bool(o), "order": o})


# ================== REST ==================
@app.route("/orders", methods=["GET"])
def list_orders():
    """
    列出最近的訂單列表（給員工端 / 管理端用）
    GET /orders?limit=200
    """
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200

    orders = load_all_orders(limit)
    return jsonify({"ok": True, "count": len(orders), "orders": orders})


# ✅ 新增：給你 App 新版相容 /api/orders
@app.route("/api/orders", methods=["GET"])
def list_orders_api():
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200

    orders = load_all_orders(limit)
    return jsonify({"ok": True, "orders": orders})


@app.route("/orders/<int:oid>/status", methods=["POST"])
def update_order_status(oid):
    """
    更新單一訂單狀態：new / making / done / cancelled
    POST /orders/123/status
    JSON: { "status": "making" }
    """
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip().lower()

    if status not in ORDER_STATUS_ALLOWED:
        return jsonify({"ok": False, "msg": "invalid status"}), 400

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
        conn.commit()
        if c.rowcount == 0:
            return jsonify({"ok": False, "msg": "order not found"}), 404

    # ✅ 精準叫號：只要狀態改 done，就自動把該筆的四碼 session_id 寫入 call_state
    if status == "done":
        sid = get_session_id_by_order_id(oid)
        if sid:
            set_call_code(str(sid))

    return jsonify({"ok": True})


# ✅ 新增：給你 App 新版相容 /api/orders/<id>/status
@app.route("/api/orders/<int:oid>/status", methods=["POST"])
def update_order_status_api(oid):
    return update_order_status(oid)


@app.route("/session/new", methods=["POST"])
def new_session():
    sid = create_unique_session_id()
    return jsonify({"ok": True, "sessionId": sid})


# ✅ 新增：前端可先查代碼是否存在（且 active）
@app.route("/session/exists/<sid>", methods=["GET"])
def session_exists_api(sid):
    sid = str(sid or "").strip()
    if not is_valid_sid(sid):
        return jsonify({"ok": True, "exists": False})
    return jsonify({"ok": True, "exists": bool(session_is_active(sid))})


@app.route("/order_detail/<sid>")
def order_detail(sid):
    o = load_order_by_session(sid)
    return jsonify({"ok": True, "exists": bool(o), "order": o})


# ✅ 補相容：你前端有用 /order_by_session/<sid>
@app.route("/order_by_session/<sid>")
def order_by_session(sid):
    o = load_order_by_session(sid)
    return jsonify({"ok": True, "exists": bool(o), "order": o})


@app.route("/health")
def health():
    return jsonify({"ok": True})


# ✅ 新增：叫號 API（網站讀取/（可選）App 手動指定）
# GET  /api/call -> { ok:true, code:"1234", updatedAt: 1700000000 }
# POST /api/call { "code":"1234" } -> { ok:true }
@app.route("/api/call", methods=["GET", "POST"])
def api_call():
    if request.method == "GET":
        st = get_call_state()
        return jsonify({"ok": True, "code": st["code"], "updatedAt": st["updated_at"]})

    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()

    # 你要四碼數字就開這個（建議開）
    if not (len(code) == 4 and code.isdigit()):
        return jsonify({"ok": False, "msg": "code 必須是 4 碼數字"}), 400

    set_call_code(code)
    return jsonify({"ok": True})


# ================== Run ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    socketio.run(app, host="0.0.0.0", port=port)
