from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, join_room, leave_room, emit
import sqlite3, datetime, json, time, os, uuid

app = Flask(__name__)

# 允許你的前端網域（Netlify）
ALLOWED_ORIGINS = [
    "https://comfy-puffpuff-2afc75.netlify.app",
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


# ---------------- DB ----------------
def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db():
    with get_conn() as conn:
        c = conn.cursor()

        # 主單（同一 session_id 只會有一張主單）
        c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE,
            table_num TEXT,
            time TEXT,
            items TEXT,
            status TEXT DEFAULT 'new'
        )""")

        # 為了相容你舊資料庫：可能沒有 UNIQUE / 欄位
        try:
            c.execute("ALTER TABLE orders ADD COLUMN session_id TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'new'")
        except Exception:
            pass

        # 若舊表沒有 unique，這裡用 index 先頂著（SQLite 無法直接 ALTER UNIQUE）
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_time ON orders(time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id)")

        # 估清表
        c.execute("""
        CREATE TABLE IF NOT EXISTS soldout (
            category_idx INTEGER NOT NULL,
            item_idx INTEGER NOT NULL,
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (category_idx, item_idx)
        )""")

        # 共享訂單 session（多人同步購物車）
        c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            cart_json TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )""")

        conn.commit()


init_db()


# ---------------- helpers ----------------
def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def to_ts_ms(dt_str: str) -> int:
    try:
        dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def normalize_cart_item(item: dict) -> dict:
    line_id = str(item.get("lineId") or uuid.uuid4().hex)
    name = str(item.get("name", ""))
    enName = item.get("enName")
    price = int(item.get("price", 0))
    qty = int(item.get("qty", 1))
    remark = str(item.get("remark", ""))
    temp = item.get("temp", None)

    add_ons = item.get("addOns", [])
    if not isinstance(add_ons, list):
        add_ons = []

    cleaned_addons = []
    for a in add_ons:
        if not isinstance(a, dict):
            continue
        cleaned_addons.append({
            "key": str(a.get("key", "")),
            "name": str(a.get("name", "")),
            "enName": a.get("enName"),
            "price": int(a.get("price", 0))
        })

    return {
        "lineId": line_id,
        "name": name,
        "enName": enName,
        "price": price,
        "qty": max(1, qty),
        "remark": remark,
        "temp": temp,
        "addOns": cleaned_addons
    }


def calc_total(cart: list) -> int:
    total = 0
    for it in cart or []:
        try:
            price = int(it.get("price", 0))
            qty = int(it.get("qty", 1))
            add_ons = it.get("addOns", []) or []
            add_sum = sum(int(a.get("price", 0)) for a in add_ons if isinstance(a, dict))
            total += (price + add_sum) * qty
        except Exception:
            pass
    return int(total)


def ensure_session(session_id: str):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()
        if row:
            return
        c.execute("INSERT INTO sessions (session_id, cart_json) VALUES (?, ?)", (session_id, "[]"))
        conn.commit()


def get_session_cart(session_id: str):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT cart_json FROM sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()
    if not row:
        return []
    try:
        return json.loads(row[0]) if row[0] else []
    except Exception:
        return []


def save_session_cart(session_id: str, cart: list):
    cart2 = [normalize_cart_item(x) for x in (cart or []) if isinstance(x, dict)]
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO sessions (session_id, cart_json, updated_at)
            VALUES (?, ?, datetime('now','localtime'))
            ON CONFLICT(session_id) DO UPDATE SET
                cart_json=excluded.cart_json,
                updated_at=datetime('now','localtime')
        """, (session_id, json.dumps(cart2, ensure_ascii=False)))
        conn.commit()
    return cart2


def load_order_by_session(session_id: str):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, session_id, table_num, time, items, status FROM orders WHERE session_id=?", (session_id,))
        row = c.fetchone()
    if not row:
        return None
    oid, sid, table_num, time_str, items_json, status = row
    try:
        items = json.loads(items_json) if items_json else []
    except Exception:
        items = []
    return {
        "id": int(oid),
        "sessionId": sid,
        "tableNo": str(table_num or ""),
        "time": time_str,
        "status": status,
        "items": items,
        "total": calc_total(items),
        "timestamp": to_ts_ms(time_str or now_str()),
    }


def append_items_to_order(session_id: str, table_num: str, new_items: list):
    """
    核心：同一個 sessionId 只保留一張主單。
    - 若主單不存在：建立一張 orders
    - 若已存在：把 new_items 追加到 items JSON（累加明細）
    """
    new_items = [normalize_cart_item(i) for i in (new_items or []) if isinstance(i, dict)]
    if not new_items:
        return None

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, items FROM orders WHERE session_id=?", (session_id,))
        row = c.fetchone()

        if not row:
            # 建立主單
            order_time = now_str()
            items_json = json.dumps(new_items, ensure_ascii=False)
            c.execute(
                "INSERT INTO orders (session_id, table_num, time, items, status) VALUES (?, ?, ?, ?, 'new')",
                (session_id, table_num, order_time, items_json)
            )
            conn.commit()
            return int(c.lastrowid)

        # 已有主單：追加明細
        order_id, old_items_json = row
        try:
            old_items = json.loads(old_items_json) if old_items_json else []
        except Exception:
            old_items = []

        merged = (old_items or []) + new_items
        c.execute(
            "UPDATE orders SET items=?, table_num=?, time=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), table_num, now_str(), int(order_id))
        )
        conn.commit()
        return int(order_id)


# ---------------- presence & locks (in-memory) ----------------
users_in_room = {}  # users_in_room[session_id] = { sid: {"nickname": "..."} }
locks = {}          # locks[session_id][lineId] = {"bySid": "...", "byName": "...", "expiresAt": epoch_ms}
LOCK_TTL_MS = 12_000


def _cleanup_expired_locks(session_id: str):
    now_ms = int(time.time() * 1000)
    room_locks = locks.get(session_id, {})
    dead = [line_id for line_id, v in room_locks.items() if v.get("expiresAt", 0) < now_ms]
    for line_id in dead:
        room_locks.pop(line_id, None)
    if room_locks:
        locks[session_id] = room_locks
    else:
        locks.pop(session_id, None)


def get_room_users(session_id: str):
    room = users_in_room.get(session_id, {})
    return [{"sid": sid, "nickname": v.get("nickname", "訪客")} for sid, v in room.items()]


def get_room_locks(session_id: str):
    _cleanup_expired_locks(session_id)
    room_locks = locks.get(session_id, {})
    out = {}
    for line_id, v in room_locks.items():
        out[line_id] = {"byName": v.get("byName", "訪客")}
    return out


def broadcast_state(session_id: str):
    cart = get_session_cart(session_id)
    socketio.emit("session_state", {
        "sessionId": session_id,
        "cart": cart,
        "total": calc_total(cart),
        "users": get_room_users(session_id),
        "locks": get_room_locks(session_id)
    }, room=session_id)


# ---------------- REST ----------------
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


@app.route('/orders', methods=['GET'])
def get_orders():
    """
    櫃台：顯示所有 new 的主單
    """
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, session_id, table_num, time, items FROM orders WHERE status='new' ORDER BY id DESC")
        rows = c.fetchall()

    orders = []
    for oid, sid, table_num, time_str, items_json in rows:
        try:
            items = json.loads(items_json) if items_json else []
        except Exception:
            items = []
        orders.append({
            "id": int(oid),
            "sessionId": sid,
            "tableNo": str(table_num or ""),
            "total": int(calc_total(items)),
            "timestamp": to_ts_ms(time_str),
            "items": items
        })
    return jsonify(orders)


@app.route('/orders/<int:order_id>/done', methods=['POST'])
def order_done(order_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE orders SET status='done' WHERE id=?", (order_id,))
        conn.commit()
    return jsonify({"status": "ok"})


@app.route('/order_by_session/<session_id>', methods=['GET'])
def order_by_session(session_id):
    """
    ✅ 給前端「訂單明細頁」用：用 4位訂單代碼（sessionId）查主單明細
    """
    session_id = str(session_id).strip()
    if not session_id:
        return jsonify({"ok": False, "msg": "sessionId required"}), 400

    od = load_order_by_session(session_id)
    if not od:
        return jsonify({"ok": True, "exists": False, "order": None})
    return jsonify({"ok": True, "exists": True, "order": od})


@app.route('/soldout', methods=['GET'])
def soldout_get():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT category_idx, item_idx FROM soldout ORDER BY category_idx, item_idx")
        rows = c.fetchall()
    items = [[int(r[0]), int(r[1])] for r in rows]
    return jsonify({"items": items})


@app.route('/soldout', methods=['PUT'])
def soldout_put():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items", [])
    if not isinstance(items, list):
        items = []

    cleaned = []
    seen = set()
    for v in items:
        if not (isinstance(v, (list, tuple)) and len(v) == 2):
            continue
        c_idx, i_idx = int(v[0]), int(v[1])
        key = (c_idx, i_idx)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(key)

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM soldout")
        if cleaned:
            c.executemany(
                "INSERT INTO soldout (category_idx, item_idx, updated_at) VALUES (?, ?, datetime('now','localtime'))",
                cleaned
            )
        conn.commit()
    return jsonify({"status": "ok", "count": len(cleaned)})


@app.route('/session/<session_id>', methods=['GET'])
def session_get(session_id):
    session_id = str(session_id).strip()
    ensure_session(session_id)
    cart = get_session_cart(session_id)
    return jsonify({
        "sessionId": session_id,
        "cart": cart,
        "total": calc_total(cart),
        "users": get_room_users(session_id),
        "locks": get_room_locks(session_id)
    })


# ---------------- Socket.IO events ----------------
@socketio.on("join_session")
def on_join_session(data):
    session_id = str((data or {}).get("sessionId", "")).strip()
    nickname = str((data or {}).get("nickname", "訪客")).strip()[:20] or "訪客"

    if not session_id:
        emit("error_msg", {"msg": "sessionId required"})
        return

    ensure_session(session_id)
    join_room(session_id)

    room = users_in_room.get(session_id, {})
    room[request.sid] = {"nickname": nickname, "joinedAt": int(time.time() * 1000)}
    users_in_room[session_id] = room

    broadcast_state(session_id)


@socketio.on("leave_session")
def on_leave_session(data):
    session_id = str((data or {}).get("sessionId", "")).strip()
    if not session_id:
        return
    leave_room(session_id)

    room = users_in_room.get(session_id, {})
    room.pop(request.sid, None)
    if room:
        users_in_room[session_id] = room
    else:
        users_in_room.pop(session_id, None)

    _cleanup_expired_locks(session_id)
    room_locks = locks.get(session_id, {})
    dead = [line_id for line_id, v in room_locks.items() if v.get("bySid") == request.sid]
    for line_id in dead:
        room_locks.pop(line_id, None)
    if room_locks:
        locks[session_id] = room_locks
    else:
        locks.pop(session_id, None)

    broadcast_state(session_id)


@socketio.on("disconnect")
def on_disconnect():
    for session_id, room in list(users_in_room.items()):
        if request.sid in room:
            room.pop(request.sid, None)
            if room:
                users_in_room[session_id] = room
            else:
                users_in_room.pop(session_id, None)

            _cleanup_expired_locks(session_id)
            room_locks = locks.get(session_id, {})
            dead = [line_id for line_id, v in room_locks.items() if v.get("bySid") == request.sid]
            for line_id in dead:
                room_locks.pop(line_id, None)
            if room_locks:
                locks[session_id] = room_locks
            else:
                locks.pop(session_id, None)

            broadcast_state(session_id)


def _require_lock_or_reject(session_id: str, line_id: str):
    _cleanup_expired_locks(session_id)
    room_locks = locks.get(session_id, {})
    lock = room_locks.get(line_id)
    if not lock:
        return False, "no_lock"
    if lock.get("bySid") != request.sid:
        return False, "locked_by_other"
    return True, "ok"


@socketio.on("lock_line")
def on_lock_line(data):
    session_id = str((data or {}).get("sessionId", "")).strip()
    line_id = str((data or {}).get("lineId", "")).strip()
    nickname = str((data or {}).get("nickname", "訪客")).strip()[:20] or "訪客"
    if not session_id or not line_id:
        return

    ensure_session(session_id)
    _cleanup_expired_locks(session_id)

    room_locks = locks.get(session_id, {})
    now_ms = int(time.time() * 1000)
    cur = room_locks.get(line_id)

    if cur and cur.get("bySid") != request.sid and cur.get("expiresAt", 0) >= now_ms:
        emit("lock_denied", {"lineId": line_id, "byName": cur.get("byName", "訪客")})
        return

    room_locks[line_id] = {
        "bySid": request.sid,
        "byName": nickname,
        "expiresAt": now_ms + LOCK_TTL_MS
    }
    locks[session_id] = room_locks
    socketio.emit("lock_update", {"lineId": line_id, "byName": nickname}, room=session_id)


@socketio.on("unlock_line")
def on_unlock_line(data):
    session_id = str((data or {}).get("sessionId", "")).strip()
    line_id = str((data or {}).get("lineId", "")).strip()
    if not session_id or not line_id:
        return

    _cleanup_expired_locks(session_id)
    room_locks = locks.get(session_id, {})
    cur = room_locks.get(line_id)
    if cur and cur.get("bySid") == request.sid:
        room_locks.pop(line_id, None)
        if room_locks:
            locks[session_id] = room_locks
        else:
            locks.pop(session_id, None)
        socketio.emit("lock_remove", {"lineId": line_id}, room=session_id)


@socketio.on("cart_add")
def on_cart_add(data):
    session_id = str((data or {}).get("sessionId", "")).strip()
    item = (data or {}).get("item", {})
    if not session_id or not isinstance(item, dict):
        return
    ensure_session(session_id)

    cart = get_session_cart(session_id)
    cart.append(normalize_cart_item(item))
    cart = save_session_cart(session_id, cart)
    broadcast_state(session_id)


@socketio.on("cart_set_qty")
def on_cart_set_qty(data):
    session_id = str((data or {}).get("sessionId", "")).strip()
    line_id = str((data or {}).get("lineId", "")).strip()
    qty = int((data or {}).get("qty", 1))
    if not session_id or not line_id:
        return

    ok, reason = _require_lock_or_reject(session_id, line_id)
    if not ok:
        emit("op_rejected", {"reason": reason, "lineId": line_id})
        return

    cart = get_session_cart(session_id)
    for it in cart:
        if it.get("lineId") == line_id:
            it["qty"] = max(1, qty)
            break
    cart = save_session_cart(session_id, cart)
    broadcast_state(session_id)


@socketio.on("cart_set_remark")
def on_cart_set_remark(data):
    session_id = str((data or {}).get("sessionId", "")).strip()
    line_id = str((data or {}).get("lineId", "")).strip()
    remark = str((data or {}).get("remark", ""))[:80]
    if not session_id or not line_id:
        return

    ok, reason = _require_lock_or_reject(session_id, line_id)
    if not ok:
        emit("op_rejected", {"reason": reason, "lineId": line_id})
        return

    cart = get_session_cart(session_id)
    for it in cart:
        if it.get("lineId") == line_id:
            it["remark"] = remark
            break
    cart = save_session_cart(session_id, cart)
    broadcast_state(session_id)


@socketio.on("cart_remove")
def on_cart_remove(data):
    session_id = str((data or {}).get("sessionId", "")).strip()
    line_id = str((data or {}).get("lineId", "")).strip()
    if not session_id or not line_id:
        return

    ok, reason = _require_lock_or_reject(session_id, line_id)
    if not ok:
        emit("op_rejected", {"reason": reason, "lineId": line_id})
        return

    cart = get_session_cart(session_id)
    cart = [it for it in cart if it.get("lineId") != line_id]
    cart = save_session_cart(session_id, cart)

    _cleanup_expired_locks(session_id)
    room_locks = locks.get(session_id, {})
    room_locks.pop(line_id, None)
    if room_locks:
        locks[session_id] = room_locks
    else:
        locks.pop(session_id, None)

    broadcast_state(session_id)


@socketio.on("submit_cart_as_order")
def on_submit_cart_as_order(data):
    """
    ✅ 改造重點：同一 sessionId 只會寫入同一張主單（追加 items）。
    """
    session_id = str((data or {}).get("sessionId", "")).strip()
    table_num = str((data or {}).get("table", "")).strip()
    if not session_id:
        emit("submit_result", {"ok": False, "msg": "sessionId required"})
        return

    ensure_session(session_id)
    cart = get_session_cart(session_id)
    if not cart:
        emit("submit_result", {"ok": False, "msg": "cart empty"})
        return

    order_id = append_items_to_order(session_id=session_id, table_num=table_num, new_items=cart)
    if not order_id:
        emit("submit_result", {"ok": False, "msg": "append failed"})
        return

    # 清空購物車但保留 session（可再加點）
    save_session_cart(session_id, [])
    locks.pop(session_id, None)

    socketio.emit("submit_result", {"ok": True, "orderId": int(order_id)}, room=session_id)
    broadcast_state(session_id)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    socketio.run(app, host='0.0.0.0', port=port)
