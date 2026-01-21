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

# Socket.IO（用 gevent，比 eventlet 更不容易踩雷）
socketio = SocketIO(
    app,
    cors_allowed_origins=ALLOWED_ORIGINS,
    async_mode="gevent",
    ping_interval=20,
    ping_timeout=30,
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

        # 櫃台訂單（你原本的 orders）
        c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            table_num TEXT,
            time TEXT,
            items TEXT,
            status TEXT DEFAULT 'new'
        )""")
        # 防呆：舊庫沒有欄位就補
        for ddl in [
            ("ALTER TABLE orders ADD COLUMN session_id TEXT",),
            ("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'new'",),
        ]:
            try:
                c.execute(ddl[0])
            except Exception:
                pass

        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_time ON orders(time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id)")

        # 估清表（跨裝置）
        c.execute("""
        CREATE TABLE IF NOT EXISTS soldout (
            category_idx INTEGER NOT NULL,
            item_idx INTEGER NOT NULL,
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (category_idx, item_idx)
        )""")

        # 共享購物車 session（多人同步用）
        c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            cart_json TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )""")

        # ✅ 加點紀錄表：同一個 order_code(session_id) 會有多筆 event
        c.execute("""
        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT NOT NULL,         -- = session_id / 4位代碼
            time TEXT NOT NULL,
            items_json TEXT NOT NULL,
            total INTEGER NOT NULL,
            status TEXT DEFAULT 'new'         -- 櫃台用：new/done
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_code ON order_events(order_code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_status ON order_events(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON order_events(time)")

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

def ensure_session(session_id: str):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()
        if row:
            return
        c.execute("INSERT INTO sessions (session_id, cart_json) VALUES (?, ?)", (session_id, "[]"))
        conn.commit()

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

# ✅ 把同一個 order_code 的所有 events 合併成「訂單明細」
def merge_order_events(events: list) -> dict:
    # events: [{time, items(list), total}]
    merged_items = {}  # key -> merged line
    grand_total = 0

    def key_of(it: dict):
        # 同品項+溫度+加價項 視為同一類（你也可以加 remark 進去）
        name = str(it.get("name", ""))
        temp = str(it.get("temp") or "")
        add = it.get("addOns") or []
        add_keys = ",".join(sorted([str(a.get("key","")) for a in add if isinstance(a, dict)]))
        return f"{name}|{temp}|{add_keys}"

    for ev in events:
        items = ev.get("items", [])
        for it in items:
            k = key_of(it)
            qty = int(it.get("qty", 1))
            if k not in merged_items:
                merged_items[k] = {**it}
            else:
                merged_items[k]["qty"] = int(merged_items[k].get("qty", 1)) + qty
        grand_total += int(ev.get("total", 0))

    return {
        "items": list(merged_items.values()),
        "total": grand_total
    }

# ---------------- presence & locks (in-memory) ----------------
users_in_room = {}  # users_in_room[session_id] = { sid: {"nickname": "..."} }
locks = {}          # locks[session_id][lineId] = {"bySid","byName","expiresAt"}
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
    return [{"sid": sid, "nickname": v.get("nickname", "??")} for sid, v in room.items()]

def get_room_locks(session_id: str):
    _cleanup_expired_locks(session_id)
    room_locks = locks.get(session_id, {})
    out = {}
    for line_id, v in room_locks.items():
        out[line_id] = {"byName": v.get("byName", "??")}
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

# 櫃台：抓「未處理加點單」(用 order_events)
@app.route('/orders', methods=['GET'])
def get_orders():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, order_code, time, items_json, total
            FROM order_events
            WHERE status='new'
            ORDER BY id DESC
        """)
        rows = c.fetchall()

    out = []
    for oid, code, tstr, items_json, total in rows:
        try:
            items = json.loads(items_json)
        except Exception:
            items = []
        out.append({
            "id": int(oid),
            "sessionId": str(code),
            "tableNo": str(code),  # ✅ 你要把桌號顯示成「訂單編號」
            "timestamp": to_ts_ms(tstr),
            "items": items,
            "total": int(total),
        })
    return jsonify(out)

@app.route('/orders/<int:order_id>/done', methods=['POST'])
def order_done(order_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE order_events SET status='done' WHERE id=?", (order_id,))
        conn.commit()
    return jsonify({"status": "ok"})

# 估清
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

# ✅ 訂單明細：依照訂單代碼抓全部加點紀錄 + 合併
@app.route('/order_history/<order_code>', methods=['GET'])
def order_history(order_code):
    code = str(order_code).strip()
    if not code:
        return jsonify({"status": "bad_request"}), 400

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, time, items_json, total, status
            FROM order_events
            WHERE order_code=?
            ORDER BY id ASC
        """, (code,))
        rows = c.fetchall()

    events = []
    for eid, tstr, items_json, total, status in rows:
        try:
            items = json.loads(items_json)
        except Exception:
            items = []
        events.append({
            "eventId": int(eid),
            "time": tstr,
            "timestamp": to_ts_ms(tstr),
            "items": items,
            "total": int(total),
            "status": status,
        })

    merged = merge_order_events(events)
    return jsonify({
        "orderCode": code,
        "events": events,          # 每次加點一筆
        "mergedItems": merged["items"],  # 合併後顯示用
        "mergedTotal": merged["total"],
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
        emit("lock_denied", {"lineId": line_id, "byName": cur.get("byName", "??")})
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
    save_session_cart(session_id, cart
