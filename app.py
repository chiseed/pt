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

ALLOWED_ORIGINS = [
    "https://partnerburger.netlify.app",
    "https://illustrious-centaur-327b59.netlify.app",
    "https://silly-marzipan-9f27a5.netlify.app",
]

ADMIN_PIN = os.environ.get("ADMIN_PIN", "2580").strip()

CORS(
    app,
    resources={r"/*": {"origins": ALLOWED_ORIGINS}},
    allow_headers=["Content-Type", "X-Admin-Pin"],
    methods=["GET", "POST", "PUT", "OPTIONS"],
)

socketio = SocketIO(
    app,
    cors_allowed_origins=ALLOWED_ORIGINS,
    async_mode="eventlet",
    ping_interval=20,
    ping_timeout=30
)

DB_FILE = "orders.db"
SESSION_TTL_SECONDS = 24 * 60 * 60
ORDER_STATUS_ALLOWED = {"new", "making", "done", "cancelled"}

# ================== DB ==================
def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def _col_exists(conn, table: str, col: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    return col in cols

def init_db():
    with get_conn() as conn:
        c = conn.cursor()

        # 主訂單（orderId 固定、items 累積）
        c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            table_num TEXT,
            time TEXT,
            items TEXT,
            status TEXT DEFAULT 'new'
        )""")

        # ✅ 每次送出（含加點）都新增/合併一張 ticket（存單區依 ticket 狀態）
        c.execute("""
        CREATE TABLE IF NOT EXISTS order_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            table_num TEXT,
            time TEXT,
            items TEXT,
            status TEXT DEFAULT 'new',
            batch_no INTEGER DEFAULT 1,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            cart_json TEXT NOT NULL,
            created_at TEXT,
            expires_at TEXT,
            updated_at TEXT
        )""")

        # ✅ migrate：sessions 增加 order_id（把代碼固定綁一個訂單編號）
        if not _col_exists(conn, "sessions", "order_id"):
            c.execute("ALTER TABLE sessions ADD COLUMN order_id INTEGER")
            conn.commit()

        c.execute("""
        CREATE TABLE IF NOT EXISTS soldout (
            category_idx INTEGER,
            item_idx INTEGER,
            updated_at TEXT,
            PRIMARY KEY (category_idx, item_idx)
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS call_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            code TEXT DEFAULT '',
            updated_at INTEGER DEFAULT 0
        )""")
        c.execute("INSERT OR IGNORE INTO call_state (id, code, updated_at) VALUES (1, '', 0)")

        c.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            category_idx INTEGER,
            item_idx INTEGER,
            stock INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        )""")

        conn.commit()

init_db()

# ================== Helpers ==================
def now_dt():
    return datetime.datetime.now(TZ)

def now_str():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")

def expires_str():
    return (now_dt() + datetime.timedelta(seconds=SESSION_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")

def to_ts_ms(s):
    try:
        d = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        return int(d.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)

def normalize_cart_item(item: dict) -> dict:
    item = item or {}
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
        "category": item.get("category"),
    }

def calc_total(items):
    total = 0
    for it in items or []:
        add = sum(int(a.get("price", 0)) for a in it.get("addOns", []) if isinstance(a, dict))
        total += (int(it.get("price", 0)) + add) * int(it.get("qty", 1))
    return total

# ================== Call State ==================
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

# ================== Inventory / Soldout 連動 ==================
def sync_soldout_for_inventory_row(cur, row):
    if not row:
        return
    _id, name, category, cidx, iidx, stock, updated_at = row
    if cidx is None or iidx is None:
        return
    if stock <= 0:
        cur.execute("""
            INSERT INTO soldout (category_idx, item_idx, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(category_idx, item_idx) DO UPDATE SET
                updated_at=excluded.updated_at
        """, (cidx, iidx, now_str()))
    else:
        cur.execute("DELETE FROM soldout WHERE category_idx=? AND item_idx=?", (cidx, iidx))

# ================== Session ==================
def session_is_active(session_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT expires_at FROM sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()
    if not row or not row[0]:
        return False
    try:
        exp = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        return now_dt() < exp
    except Exception:
        return False

def ensure_session(session_id, force_reset=False):
    if not session_id:
        return

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT expires_at FROM sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()

        if not row:
            c.execute("""
                INSERT INTO sessions (session_id, cart_json, created_at, expires_at, updated_at, order_id)
                VALUES (?, ?, ?, ?, ?, NULL)
            """, (session_id, "[]", now_str(), expires_str(), now_str()))
            conn.commit()
            return

        expired = False
        if row[0]:
            try:
                exp = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                expired = now_dt() >= exp
            except Exception:
                expired = False

        if expired or force_reset:
            c.execute("""
                UPDATE sessions
                SET cart_json=?, created_at=?, expires_at=?, updated_at=?, order_id=NULL
                WHERE session_id=?
            """, ("[]", now_str(), expires_str(), now_str(), session_id))
            conn.commit()

def create_unique_session_id():
    for _ in range(300):
        sid = str(random.randint(1000, 9999))
        if not session_is_active(sid):
            ensure_session(sid, force_reset=True)
            return sid
    raise RuntimeError("No available sessionId")

def get_session_cart(session_id):
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
    cart = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in (cart or [])]
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO sessions (session_id, cart_json, created_at, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                cart_json=excluded.cart_json,
                updated_at=excluded.updated_at,
                expires_at=excluded.expires_at
        """, (session_id, json.dumps(cart, ensure_ascii=False), now_str(), expires_str(), now_str()))
        conn.commit()

# ================== Orders: 固定 orderId + 存單合併 ==================
def _get_session_order_id(session_id: str):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT order_id FROM sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()
    if not row:
        return None
    return row[0]

def _set_session_order_id(session_id: str, order_id: int):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE sessions SET order_id=? WHERE session_id=?", (int(order_id), session_id))
        conn.commit()

def _find_existing_order_for_session(session_id: str):
    # 若你以前系統已經有 orders，先把 session 綁到「最新那張」(避免你看到的編號突然改掉)
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT MAX(id) FROM orders WHERE session_id=?", (session_id,))
        row = c.fetchone()
    return int(row[0]) if row and row[0] else None

def _create_order_header(session_id: str, table: str, items: list) -> int:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO orders (session_id, table_num, time, items, status)
            VALUES (?, ?, ?, ?, 'new')
        """, (session_id, table or "", now_str(), json.dumps(items, ensure_ascii=False)))
        conn.commit()
        return int(c.lastrowid)

def _append_items_to_header(order_id: int, table: str, new_items: list):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT items FROM orders WHERE id=?", (int(order_id),))
        row = c.fetchone()
        old_items = json.loads(row[0] or "[]") if row else []
        merged = old_items + new_items
        c.execute("""
            UPDATE orders
            SET items=?, table_num=?
            WHERE id=?
        """, (json.dumps(merged, ensure_ascii=False), table or "", int(order_id)))
        conn.commit()

def _get_open_new_ticket(order_id: int):
    # ✅ 找同 orderId 的「存單(new)」票，有就合併
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, items, batch_no
            FROM order_tickets
            WHERE order_id=? AND status='new'
            ORDER BY id DESC
            LIMIT 1
        """, (int(order_id),))
        row = c.fetchone()
    return row  # (ticketId, items_json, batch_no) or None

def _get_next_batch_no(order_id: int) -> int:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT COALESCE(MAX(batch_no), 0) FROM order_tickets WHERE order_id=?", (int(order_id),))
        n = c.fetchone()[0] or 0
    return int(n) + 1

def _create_ticket(order_id: int, session_id: str, table: str, items: list, batch_no: int) -> int:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO order_tickets (order_id, session_id, table_num, time, items, status, batch_no)
            VALUES (?, ?, ?, ?, ?, 'new', ?)
        """, (int(order_id), session_id, table or "", now_str(), json.dumps(items, ensure_ascii=False), int(batch_no)))
        conn.commit()
        return int(c.lastrowid)

def _merge_into_ticket(ticket_id: int, merged_items: list):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE order_tickets
            SET items=?, time=?
            WHERE id=?
        """, (json.dumps(merged_items, ensure_ascii=False), now_str(), int(ticket_id)))
        conn.commit()

def get_or_create_order_id_for_session(session_id: str, table: str, items_for_first: list) -> int:
    ensure_session(session_id)

    oid = _get_session_order_id(session_id)
    if oid:
        return int(oid)

    # 沒綁過：若舊資料已有訂單，就綁到最新那張（避免你看到的編號突然改）
    existing = _find_existing_order_for_session(session_id)
    if existing:
        _set_session_order_id(session_id, existing)
        return int(existing)

    # 完全沒有：建立第一張主訂單並固定
    new_oid = _create_order_header(session_id, table, items_for_first)
    _set_session_order_id(session_id, new_oid)
    return int(new_oid)

def submit_cart_create_or_merge_ticket(session_id: str, table: str, cart: list):
    cart_items = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in (cart or [])]
    if not cart_items:
        return None

    order_id = get_or_create_order_id_for_session(session_id, table, cart_items)

    # 主訂單永遠累積
    _append_items_to_header(order_id, table, cart_items)

    # ✅ 存單合併：如果 already have status=new ticket 就合併
    open_ticket = _get_open_new_ticket(order_id)
    if open_ticket:
        ticket_id, old_items_json, batch_no = open_ticket
        try:
            old_items = json.loads(old_items_json or "[]")
        except Exception:
            old_items = []
        merged = old_items + cart_items
        _merge_into_ticket(ticket_id, merged)
        return {"orderId": order_id, "ticketId": int(ticket_id), "batchNo": int(batch_no or 1), "merged": True}

    # 沒有存單 → 新增一張新的「加點存單」
    batch_no = _get_next_batch_no(order_id)
    ticket_id = _create_ticket(order_id, session_id, table, cart_items, batch_no)
    return {"orderId": order_id, "ticketId": ticket_id, "batchNo": batch_no, "merged": False}

def load_order_by_session(session_id):
    order_id = _get_session_order_id(session_id)
    if not order_id:
        # fallback：舊資料尚未綁定
        order_id = _find_existing_order_for_session(session_id)
        if not order_id:
            return None
        _set_session_order_id(session_id, order_id)

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, table_num, time, items, status, session_id
            FROM orders
            WHERE id=?
            LIMIT 1
        """, (int(order_id),))
        row = c.fetchone()

    if not row:
        return None

    oid, table, t, items, status, sid = row
    items = json.loads(items) if items else []
    items = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in items]

    return {
        "id": int(oid),                 # ✅ 固定訂單編號
        "sessionId": sid,               # 代碼
        "tableNo": table,
        "time": t,
        "status": status,
        "items": items,
        "total": calc_total(items),
        "timestamp": to_ts_ms(t),
    }

def load_all_tickets(limit=200):
    limit = max(1, min(int(limit or 200), 500))
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT
                t.id AS ticket_id,
                t.order_id AS order_id,
                t.session_id,
                t.table_num,
                t.time,
                t.items,
                t.status,
                t.batch_no
            FROM order_tickets t
            ORDER BY t.id DESC
            LIMIT ?
        """, (limit,))
        rows = c.fetchall()

    out = []
    for ticket_id, order_id, sid, table, t, items, status, batch_no in rows:
        items_list = json.loads(items) if items else []
        items_list = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in items_list]
        out.append({
            "id": int(ticket_id),         # ✅ ticketId（更新狀態用）
            "orderId": int(order_id),     # ✅ 顯示用：同代碼永遠相同
            "batchNo": int(batch_no or 1),
            "sessionId": sid,
            "tableNo": table,
            "time": t,
            "status": status,
            "items": items_list,
            "total": calc_total(items_list),
            "timestamp": to_ts_ms(t),
        })
    return out

def update_ticket_status(ticket_id: int, status: str) -> bool:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE order_tickets SET status=? WHERE id=?", (status, int(ticket_id)))
        conn.commit()
        return c.rowcount > 0

def get_session_id_by_ticket_id(ticket_id: int):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT session_id FROM order_tickets WHERE id=?", (int(ticket_id),))
        row = c.fetchone()
    return row[0] if row else None

# ================== Socket State ==================
users_in_room = {}
locks_in_room = {}

def locks_public(session_id):
    d = locks_in_room.get(session_id, {}) or {}
    return {lineId: {"byName": v.get("byName")} for lineId, v in d.items()}

def broadcast_state(session_id):
    cart = get_session_cart(session_id)
    socketio.emit(
        "session_state",
        {
            "sessionId": session_id,
            "cart": cart,
            "total": calc_total(cart),
            "users": list(users_in_room.get(session_id, {}).values()),
            "locks": locks_public(session_id),
        },
        room=session_id,
    )

def _find_item_idx(cart, line_id: str):
    for i, it in enumerate(cart or []):
        if str(it.get("lineId", "")) == str(line_id):
            return i
    return -1

@socketio.on("join_session")
def on_join(data):
    sid = str((data.get("sessionId") or "")).strip()
    name = str((data.get("nickname") or "訪客")).strip()[:12] or "訪客"
    ensure_session(sid)
    join_room(sid)
    users_in_room.setdefault(sid, {})[request.sid] = {"sid": request.sid, "nickname": name}
    broadcast_state(sid)

@socketio.on("set_nickname")
def on_set_nickname(data):
    sid = str((data.get("sessionId") or "")).strip()
    name = str((data.get("nickname") or "訪客")).strip()[:12] or "訪客"
    if not sid:
        return
    if sid in users_in_room and request.sid in users_in_room[sid]:
        users_in_room[sid][request.sid]["nickname"] = name
    broadcast_state(sid)

@socketio.on("lock_line")
def on_lock_line(data):
    sid = str((data.get("sessionId") or "")).strip()
    line_id = str((data.get("lineId") or "")).strip()
    name = str((data.get("nickname") or "訪客")).strip()[:12] or "訪客"
    if not sid or not line_id:
        return

    locks_in_room.setdefault(sid, {})
    cur = locks_in_room[sid].get(line_id)
    if cur and cur.get("bySid") != request.sid:
        emit("lock_denied", {"lineId": line_id, "byName": cur.get("byName", "別人")})
        return

    locks_in_room[sid][line_id] = {"bySid": request.sid, "byName": name, "ts": int(time.time())}
    socketio.emit("lock_update", {"lineId": line_id, "byName": name}, room=sid)
    broadcast_state(sid)

@socketio.on("unlock_line")
def on_unlock_line(data):
    sid = str((data.get("sessionId") or "")).strip()
    line_id = str((data.get("lineId") or "")).strip()
    if not sid or not line_id:
        return

    cur = (locks_in_room.get(sid, {}) or {}).get(line_id)
    if not cur or cur.get("bySid") != request.sid:
        return

    del locks_in_room[sid][line_id]
    socketio.emit("lock_remove", {"lineId": line_id}, room=sid)
    broadcast_state(sid)

@socketio.on("cart_add")
def on_cart_add(data):
    sid = str((data.get("sessionId") or "")).strip()
    ensure_session(sid)
    cart = get_session_cart(sid)
    cart.append(data.get("item", {}) or {})
    save_session_cart(sid, cart)
    broadcast_state(sid)

@socketio.on("cart_set_qty")
def on_cart_set_qty(data):
    sid = str((data.get("sessionId") or "")).strip()
    line_id = str((data.get("lineId") or "")).strip()
    qty = max(1, int(data.get("qty") or 1))
    if not sid or not line_id:
        return

    cur = (locks_in_room.get(sid, {}) or {}).get(line_id)
    if cur and cur.get("bySid") != request.sid:
        emit("op_rejected", {"reason": f"被 {cur.get('byName','別人')} 鎖定中"})
        return

    ensure_session(sid)
    cart = get_session_cart(sid)
    idx = _find_item_idx(cart, line_id)
    if idx < 0:
        emit("op_rejected", {"reason": "找不到項目"})
        return

    cart[idx]["qty"] = qty
    save_session_cart(sid, cart)
    broadcast_state(sid)

@socketio.on("cart_set_remark")
def on_cart_set_remark(data):
    sid = str((data.get("sessionId") or "")).strip()
    line_id = str((data.get("lineId") or "")).strip()
    remark = str((data.get("remark") or "")).strip()
    if not sid or not line_id:
        return

    cur = (locks_in_room.get(sid, {}) or {}).get(line_id)
    if cur and cur.get("bySid") != request.sid:
        emit("op_rejected", {"reason": f"被 {cur.get('byName','別人')} 鎖定中"})
        return

    ensure_session(sid)
    cart = get_session_cart(sid)
    idx = _find_item_idx(cart, line_id)
    if idx < 0:
        emit("op_rejected", {"reason": "找不到項目"})
        return

    cart[idx]["remark"] = remark
    save_session_cart(sid, cart)
    broadcast_state(sid)

@socketio.on("cart_remove")
def on_cart_remove(data):
    sid = str((data.get("sessionId") or "")).strip()
    line_id = str((data.get("lineId") or "")).strip()
    if not sid or not line_id:
        return

    cur = (locks_in_room.get(sid, {}) or {}).get(line_id)
    if cur and cur.get("bySid") != request.sid:
        emit("op_rejected", {"reason": f"被 {cur.get('byName','別人')} 鎖定中"})
        return

    ensure_session(sid)
    cart = get_session_cart(sid)
    idx = _find_item_idx(cart, line_id)
    if idx < 0:
        emit("op_rejected", {"reason": "找不到項目"})
        return

    cart.pop(idx)

    if sid in locks_in_room and line_id in locks_in_room[sid]:
        del locks_in_room[sid][line_id]
        socketio.emit("lock_remove", {"lineId": line_id}, room=sid)

    save_session_cart(sid, cart)
    broadcast_state(sid)

@socketio.on("order_detail")
def on_order_detail(data):
    sid = str((data.get("sessionId") or "")).strip()
    o = load_order_by_session(sid)
    emit("order_detail_result", {"ok": True, "exists": bool(o), "order": o})

@socketio.on("submit_cart_as_order")
def on_submit(data):
    sid = str((data.get("sessionId") or "")).strip()
    table = str(data.get("table", "") or "")
    if not sid:
        emit("submit_result", {"ok": False, "msg": "missing sessionId"})
        return

    ensure_session(sid)
    cart = get_session_cart(sid)
    if not cart:
        emit("submit_result", {"ok": False, "msg": "cart empty"}, room=sid)
        return

    result = submit_cart_create_or_merge_ticket(sid, table, cart)
    if not result:
        emit("submit_result", {"ok": False, "msg": "submit failed"}, room=sid)
        return

    save_session_cart(sid, [])
    locks_in_room[sid] = {}

    emit("submit_result", {
        "ok": True,
        "orderId": result["orderId"],   # ✅ 固定訂單編號（同代碼永遠相同）
        "ticketId": result["ticketId"], # ticketId（更新狀態/出單用）
        "batchNo": result["batchNo"],
        "merged": result["merged"],
    }, room=sid)

    socketio.emit(
        "order_detail_result",
        {"ok": True, "exists": True, "order": load_order_by_session(sid)},
        room=sid,
    )
    broadcast_state(sid)

@socketio.on("disconnect")
def on_disconnect():
    for sid, mp in list(users_in_room.items()):
        if request.sid in mp:
            del mp[request.sid]
            d = locks_in_room.get(sid, {}) or {}
            to_remove = [lineId for lineId, v in d.items() if v.get("bySid") == request.sid]
            for lineId in to_remove:
                del d[lineId]
                socketio.emit("lock_remove", {"lineId": lineId}, room=sid)
            broadcast_state(sid)

# ================== REST: 店員面板用（回 tickets） ==================
@app.route("/orders", methods=["GET"])
def list_orders():
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200
    tickets = load_all_tickets(limit)
    return jsonify({"ok": True, "count": len(tickets), "orders": tickets})

@app.route("/api/orders", methods=["GET"])
def list_orders_api():
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200
    tickets = load_all_tickets(limit)
    return jsonify({"ok": True, "orders": tickets})

# ✅ 更新 ticket 狀態（不是主訂單）
@app.route("/api/orders/<int:ticket_id>/status", methods=["POST"])
def update_order_status_api(ticket_id):
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip().lower()
    if status not in ORDER_STATUS_ALLOWED:
        return jsonify({"ok": False, "msg": "invalid status"}), 400

    ok = update_ticket_status(ticket_id, status)
    if not ok:
        return jsonify({"ok": False, "msg": "ticket not found"}), 404

    if status == "done":
        sid = get_session_id_by_ticket_id(ticket_id)
        if sid:
            set_call_code(str(sid))

    return jsonify({"ok": True})

@app.route("/orders/<int:ticket_id>/status", methods=["POST"])
def update_order_status_legacy(ticket_id):
    return update_order_status_api(ticket_id)

# ================== REST: 客戶明細用（回主訂單） ==================
@app.route("/session/new", methods=["POST"])
def new_session():
    sid = create_unique_session_id()
    return jsonify({"ok": True, "sessionId": sid})

@app.route("/session/exists/<sid>", methods=["GET"])
def session_exists(sid):
    sid = str(sid or "").strip()
    return jsonify({"ok": True, "exists": bool(sid) and session_is_active(sid)})

@app.route("/order_by_session/<sid>", methods=["GET"])
def order_by_session(sid):
    o = load_order_by_session(sid)
    return jsonify({"ok": True, "exists": bool(o), "order": o})

@app.route("/order_detail/<sid>", methods=["GET"])
def order_detail(sid):
    o = load_order_by_session(sid)
    return jsonify({"ok": True, "exists": bool(o), "order": o})

# ================== soldout ==================
@app.route("/soldout", methods=["GET", "POST", "PUT", "OPTIONS"])
def soldout_handler():
    if request.method == "OPTIONS":
        return ("", 204)

    if request.method == "GET":
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("SELECT category_idx, item_idx FROM soldout")
            rows = c.fetchall()
        items = [[int(a), int(b)] for a, b in rows]
        return jsonify({"ok": True, "items": items})

    pin = request.headers.get("X-Admin-Pin", "") or ""
    if not ADMIN_PIN or pin != ADMIN_PIN:
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    if not isinstance(items, list):
        return jsonify({"ok": False, "msg": "items must be list"}), 400

    clean = []
    seen = set()
    for x in items:
        if isinstance(x, (list, tuple)) and len(x) == 2:
            try:
                ci = int(x[0]); ii = int(x[1])
            except Exception:
                continue
            key = (ci, ii)
            if key in seen:
                continue
            seen.add(key)
            clean.append(key)

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM soldout")
        if clean:
            c.executemany(
                "INSERT INTO soldout (category_idx, item_idx, updated_at) VALUES (?, ?, ?)",
                [(ci, ii, now_str()) for (ci, ii) in clean]
            )
        conn.commit()

    return jsonify({"ok": True, "count": len(clean)})

# ================== Call API ==================
@app.route("/api/call", methods=["GET", "POST"])
def api_call():
    if request.method == "GET":
        st = get_call_state()
        return jsonify({"ok": True, "code": st["code"], "updatedAt": st["updated_at"]})

    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()

    if not (len(code) == 4 and code.isdigit()):
        return jsonify({"ok": False, "msg": "code 必須是 4 碼數字"}), 400

    set_call_code(code)
    return jsonify({"ok": True})

# ================== Inventory ==================
@app.route("/api/inventory", methods=["GET"])
def api_inventory_list():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT
                i.id,
                i.name,
                i.category,
                i.category_idx,
                i.item_idx,
                i.stock,
                EXISTS(
                    SELECT 1 FROM soldout s
                    WHERE s.category_idx = i.category_idx
                      AND s.item_idx = i.item_idx
                ) AS is_soldout
            FROM inventory i
            ORDER BY i.category, i.name
        """)
        rows = c.fetchall()

    items = []
    for row in rows:
        items.append({
            "id": row[0],
            "name": row[1],
            "category": row[2],
            "categoryIdx": row[3],
            "itemIdx": row[4],
            "stock": row[5],
            "soldout": bool(row[6]),
        })
    return jsonify({"ok": True, "items": items})

@app.route("/api/inventory/<int:item_id>", methods=["POST"])
def api_inventory_update(item_id):
    data = request.get_json(silent=True) or {}
    op = str(data.get("op", "set")).strip().lower()

    try:
        stock_val = int(data.get("stock", 0))
    except Exception:
        return jsonify({"ok": False, "msg": "invalid stock"}), 400

    with get_conn() as conn:
        c = conn.cursor()

        if op == "add":
            c.execute("SELECT stock FROM inventory WHERE id=?", (item_id,))
            row = c.fetchone()
            if not row:
                return jsonify({"ok": False, "msg": "not found"}), 404

            new_stock = max(0, row[0] + stock_val)
            c.execute("UPDATE inventory SET stock=?, updated_at=? WHERE id=?", (new_stock, now_str(), item_id))

            c.execute("""
                SELECT id, name, category, category_idx, item_idx, stock, updated_at
                FROM inventory
                WHERE id=?
            """, (item_id,))
            inv_row = c.fetchone()
            sync_soldout_for_inventory_row(c, inv_row)
            conn.commit()
            return jsonify({"ok": True, "stock": new_stock})

        else:
            if stock_val < 0:
                stock_val = 0
            c.execute("UPDATE inventory SET stock=?, updated_at=? WHERE id=?", (stock_val, now_str(), item_id))
            if c.rowcount == 0:
                return jsonify({"ok": False, "msg": "not found"}), 404

            c.execute("""
                SELECT id, name, category, category_idx, item_idx, stock, updated_at
                FROM inventory
                WHERE id=?
            """, (item_id,))
            inv_row = c.fetchone()
            sync_soldout_for_inventory_row(c, inv_row)
            conn.commit()
            return jsonify({"ok": True, "stock": stock_val})

@app.route("/health")
def health():
    return jsonify({"ok": True})

# ================== Run ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    socketio.run(app, host="0.0.0.0", port=port)
