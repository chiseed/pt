import os
import json
import time
import uuid
import sqlite3
import datetime
import random
import hmac
import hashlib
import logging

import eventlet
eventlet.monkey_patch()

from zoneinfo import ZoneInfo
TZ = ZoneInfo("Asia/Taipei")

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, join_room, emit

# ================== App ==================
app = Flask(__name__)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
app.logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

DEFAULT_ALLOWED_ORIGINS = [
    "https://partnertake.netlify.app",
    "https://partnerburger.netlify.app",
    "https://illustrious-centaur-327b59.netlify.app",
    "https://silly-marzipan-9f27a5.netlify.app",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "null",
]
ALLOWED_ORIGINS = [
    x.strip()
    for x in os.environ.get("CORS_ORIGINS", ",".join(DEFAULT_ALLOWED_ORIGINS)).split(",")
    if x.strip()
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

def resolve_db_file() -> str:
    explicit = os.environ.get("DB_FILE", "").strip()
    if explicit:
        return explicit
    volume_root = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume_root:
        os.makedirs(volume_root, exist_ok=True)
        return os.path.join(volume_root, "orders.db")
    return "orders.db"


DB_FILE = resolve_db_file()
SESSION_TTL_SECONDS = 24 * 60 * 60
ORDER_STATUS_ALLOWED = {"new", "making", "done", "cancelled"}
QUEUE_QR_SECRET = os.environ.get("QUEUE_QR_SECRET", "partner-queue-secret").strip()


def make_request_id(data: dict | None = None) -> str:
    data = data or {}
    header_id = ""
    try:
        header_id = request.headers.get("X-Request-Id", "")
    except RuntimeError:
        header_id = ""
    rid = str(data.get("requestId") or header_id or "").strip()
    return rid[:120] or f"server-order-{uuid.uuid4().hex}"


def summarize_items(items: list) -> list:
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        out.append({
            "lineId": it.get("lineId"),
            "name": it.get("name"),
            "qty": it.get("qty"),
            "price": it.get("price"),
            "addOns": [a.get("name") for a in it.get("addOns", []) if isinstance(a, dict)],
            "extras": [
                {"name": ex.get("name"), "qty": ex.get("qty")}
                for ex in it.get("extras", [])
                if isinstance(ex, dict)
            ],
        })
    return out


def log_order_flow(request_id: str, stage: str, **fields):
    payload = json.dumps(fields, ensure_ascii=False, default=str)
    if len(payload) > 4000:
        payload = payload[:4000] + "...(truncated)"
    app.logger.info("[order:%s] %s %s", request_id, stage, payload)


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
        CREATE TABLE IF NOT EXISTS order_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            table_num TEXT,
            time TEXT,
            items TEXT,
            status TEXT DEFAULT 'new',
            batch_no INTEGER DEFAULT 1,
            daily_no INTEGER DEFAULT 0,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS daily_counters (
            day TEXT PRIMARY KEY,
            last_no INTEGER NOT NULL DEFAULT 0
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS daily_order_counters (
            day TEXT PRIMARY KEY,
            last_no INTEGER NOT NULL DEFAULT 0
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            cart_json TEXT NOT NULL,
            created_at TEXT,
            expires_at TEXT,
            updated_at TEXT
        )""")

        if not _col_exists(conn, "sessions", "order_id"):
            c.execute("ALTER TABLE sessions ADD COLUMN order_id INTEGER")
            conn.commit()

        if not _col_exists(conn, "sessions", "last_ticket_id"):
            c.execute("ALTER TABLE sessions ADD COLUMN last_ticket_id INTEGER")
            conn.commit()

        if not _col_exists(conn, "order_tickets", "daily_no"):
            c.execute("ALTER TABLE order_tickets ADD COLUMN daily_no INTEGER DEFAULT 0")
            conn.commit()

        if not _col_exists(conn, "orders", "daily_order_no"):
            c.execute("ALTER TABLE orders ADD COLUMN daily_order_no INTEGER DEFAULT 0")
            conn.commit()

        c.execute("""
            SELECT id, substr(time, 1, 10)
            FROM orders
            WHERE COALESCE(daily_order_no, 0) = 0
            ORDER BY time, id
        """)
        missing_order_numbers = c.fetchall()
        for order_id, day_str in missing_order_numbers:
            day_str = str(day_str or "")[:10] or datetime.datetime.now(TZ).strftime("%Y-%m-%d")
            c.execute("""
                SELECT COALESCE(MAX(daily_order_no), 0)
                FROM orders
                WHERE substr(time, 1, 10) = ?
                  AND daily_order_no > 0
            """, (day_str,))
            existing_max = int((c.fetchone() or [0])[0] or 0)
            c.execute("""
                INSERT OR IGNORE INTO daily_order_counters (day, last_no)
                VALUES (?, ?)
            """, (day_str, existing_max))
            c.execute("""
                UPDATE daily_order_counters
                SET last_no = CASE
                    WHEN last_no < ? THEN ? + 1
                    ELSE last_no + 1
                END
                WHERE day = ?
            """, (existing_max, existing_max, day_str))
            c.execute("SELECT last_no FROM daily_order_counters WHERE day=?", (day_str,))
            next_order_no = int((c.fetchone() or [1])[0] or 1)
            c.execute(
                "UPDATE orders SET daily_order_no=? WHERE id=?",
                (int(next_order_no), int(order_id)),
            )
        if missing_order_numbers:
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

        c.execute("""
        CREATE TABLE IF NOT EXISTS queue_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            no TEXT NOT NULL UNIQUE,
            surname TEXT DEFAULT '',
            party_size INTEGER DEFAULT 0,
            phone TEXT DEFAULT '',
            line_user_id TEXT DEFAULT '',
            line_display_name TEXT DEFAULT '',
            id_token_sub TEXT DEFAULT '',
            status TEXT DEFAULT 'waiting',
            created_at TEXT NOT NULL,
            called_at TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        )""")

        c.execute("""
        CREATE TABLE IF NOT EXISTS queue_line_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            ticket_no TEXT NOT NULL UNIQUE,
            line_user_id TEXT NOT NULL,
            line_display_name TEXT DEFAULT '',
            id_token_sub TEXT DEFAULT '',
            bound_at TEXT NOT NULL,
            notified_at TEXT DEFAULT '',
            notify_status TEXT DEFAULT '',
            FOREIGN KEY(ticket_id) REFERENCES queue_tickets(id)
        )""")

        if not _col_exists(conn, "queue_tickets", "line_user_id"):
            c.execute("ALTER TABLE queue_tickets ADD COLUMN line_user_id TEXT DEFAULT ''")
            conn.commit()

        if not _col_exists(conn, "queue_tickets", "line_display_name"):
            c.execute("ALTER TABLE queue_tickets ADD COLUMN line_display_name TEXT DEFAULT ''")
            conn.commit()

        if not _col_exists(conn, "queue_tickets", "id_token_sub"):
            c.execute("ALTER TABLE queue_tickets ADD COLUMN id_token_sub TEXT DEFAULT ''")
            conn.commit()

        conn.commit()


init_db()


# ================== Helpers ==================
def now_dt():
    return datetime.datetime.now(TZ)


def now_str():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def normalize_order_table(table: str | None) -> str:
    value = str(table or "").strip()
    return value if value else "內用"


def parse_local_dt(value: str):
    try:
        return datetime.datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except Exception:
        return None


def expires_str():
    return (now_dt() + datetime.timedelta(seconds=SESSION_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")


def to_ts_ms(s):
    try:
        d = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        return int(d.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


def normalize_status(status: str, default: str = "new") -> str:
    s = str(status or default).strip().lower()
    return s if s in ORDER_STATUS_ALLOWED else default


def today_str():
    return now_dt().strftime("%Y-%m-%d")


def sign_queue_entry_day(day_str: str) -> str:
    day_value = str(day_str or "").strip()
    if not day_value:
        return ""
    return hmac.new(
        QUEUE_QR_SECRET.encode("utf-8"),
        day_value.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def validate_queue_entry(day_str: str, token: str) -> bool:
    day_value = str(day_str or "").strip()
    token_value = str(token or "").strip()
    if not day_value or not token_value:
        return False
    if day_value != today_str():
        return False
    expected = sign_queue_entry_day(day_value)
    return bool(expected) and hmac.compare_digest(expected, token_value)


def mask_phone(phone: str) -> str:
    raw = str(phone or "").strip()
    if len(raw) <= 4:
        return raw
    return raw[:3] + "****" + raw[-3:]


def normalize_phone(phone: str) -> str:
    return "".join(ch for ch in str(phone or "").strip() if ch.isdigit())


def normalize_ticket_no(ticket_no: str) -> str:
    raw = str(ticket_no or "").strip().upper()
    if not raw:
        return ""

    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    return f"{int(digits):03d}"


def make_queue_no(n: int) -> str:
    return f"{int(n):03d}"


def get_next_queue_no() -> str:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT COALESCE(MAX(CAST(no AS INTEGER)), 0)
            FROM queue_tickets
        """)
        row = c.fetchone()
    return make_queue_no(int(row[0] or 0) + 1)


def serialize_queue_ticket(row):
    if not row:
        return None

    return {
        "id": int(row[0]),
        "no": row[1],
        "surname": row[2] or "",
        "party_size": int(row[3] or 0),
        "phone": row[4] or "",
        "line_user_id": row[5] or "",
        "line_display_name": row[6] or "",
        "id_token_sub": row[7] or "",
        "status": row[8] or "waiting",
        "created_at": row[9] or "",
        "called_at": row[10] or "",
        "updated_at": row[11] or "",
    }


def get_queue_ticket_by_no(ticket_no: str):
    normalized = normalize_ticket_no(ticket_no)
    if not normalized:
        return None

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT
                id,
                no,
                surname,
                party_size,
                phone,
                line_user_id,
                line_display_name,
                id_token_sub,
                status,
                created_at,
                called_at,
                updated_at
            FROM queue_tickets
            WHERE no = ?
            LIMIT 1
        """, (normalized,))
        row = c.fetchone()
    return serialize_queue_ticket(row)


def find_queue_ticket_for_binding(ticket_no: str, surname: str = "", party_size: int = 0, phone: str = ""):
    exact = get_queue_ticket_by_no(ticket_no)
    if exact:
        return exact

    phone_norm = normalize_phone(phone)
    surname = str(surname or "").strip()
    party_size = int(party_size or 0)
    if not phone_norm:
        return None

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT
                id,
                no,
                surname,
                party_size,
                phone,
                line_user_id,
                line_display_name,
                id_token_sub,
                status,
                created_at,
                called_at,
                updated_at
            FROM queue_tickets
            WHERE SUBSTR(created_at, 1, 10) = ?
              AND status IN ('waiting', 'called', 'passed')
            ORDER BY id DESC
        """, (today_str(),))
        rows = c.fetchall()

    matches = []
    for row in rows:
        ticket = serialize_queue_ticket(row)
        if normalize_phone(ticket.get("phone", "")) != phone_norm:
            continue
        if surname and str(ticket.get("surname", "")).strip() != surname:
            continue
        if party_size > 0 and int(ticket.get("party_size", 0) or 0) != party_size:
            continue
        matches.append(ticket)

    if len(matches) == 1:
        return matches[0]
    return None


def get_queue_binding_by_ticket_no(ticket_no: str):
    normalized = normalize_ticket_no(ticket_no)
    if not normalized:
        return None

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT ticket_no, line_user_id, line_display_name, id_token_sub, bound_at, notified_at, notify_status
            FROM queue_line_bindings
            WHERE ticket_no = ?
            LIMIT 1
        """, (normalized,))
        row = c.fetchone()

    if not row:
        return None

    return {
        "ticket_no": row[0] or "",
        "line_user_id": row[1] or "",
        "line_display_name": row[2] or "",
        "id_token_sub": row[3] or "",
        "bound_at": row[4] or "",
        "notified_at": row[5] or "",
        "notify_status": row[6] or "",
        "is_bound": bool(row[1]),
    }


def serialize_public_binding(ticket_no: str, binding: dict | None):
    binding = binding or {}
    normalized = normalize_ticket_no(ticket_no)
    return {
        "ticket_no": normalized or str(binding.get("ticket_no") or ""),
        "line_display_name": binding.get("line_display_name", "") or "",
        "bound_at": binding.get("bound_at", "") or "",
        "notified_at": binding.get("notified_at", "") or "",
        "notify_status": binding.get("notify_status", "") or "",
        "is_bound": bool(binding.get("is_bound")),
    }


def get_current_called_ticket():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT
                id,
                no,
                surname,
                party_size,
                phone,
                line_user_id,
                line_display_name,
                id_token_sub,
                status,
                created_at,
                called_at,
                updated_at
            FROM queue_tickets
            WHERE status = 'called'
            ORDER BY datetime(called_at) DESC, id DESC
            LIMIT 1
        """)
        row = c.fetchone()
    return serialize_queue_ticket(row)


def get_waiting_queue():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT
                id,
                no,
                surname,
                party_size,
                phone,
                line_user_id,
                line_display_name,
                id_token_sub,
                status,
                created_at,
                called_at,
                updated_at
            FROM queue_tickets
            WHERE status = 'waiting'
            ORDER BY id ASC
        """)
        rows = c.fetchall()
    return [serialize_queue_ticket(row) for row in rows]


def get_waiting_count() -> int:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM queue_tickets WHERE status = 'waiting'")
        row = c.fetchone()
    return int(row[0] or 0)


def create_queue_ticket(
    surname: str,
    party_size: int,
    phone: str,
    line_user_id: str = "",
    line_display_name: str = "",
    id_token_sub: str = "",
):
    surname = str(surname or "").strip()
    phone = str(phone or "").strip()
    party_size = max(1, int(party_size or 1))
    line_user_id = str(line_user_id or "").strip()
    line_display_name = str(line_display_name or "").strip()
    id_token_sub = str(id_token_sub or "").strip()
    created = now_str()

    ticket_no = get_next_queue_no()

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO queue_tickets (
                no, surname, party_size, phone,
                line_user_id, line_display_name, id_token_sub,
                status, created_at, called_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?, '', ?)
        """, (
            ticket_no,
            surname,
            party_size,
            phone,
            line_user_id,
            line_display_name,
            id_token_sub,
            created,
            created,
        ))
        ticket_id = int(c.lastrowid)

        if line_user_id:
            c.execute("""
                INSERT INTO queue_line_bindings (
                    ticket_id, ticket_no, line_user_id, line_display_name, id_token_sub, bound_at, notified_at, notify_status
                ) VALUES (?, ?, ?, ?, ?, ?, '', '')
                ON CONFLICT(ticket_no) DO UPDATE SET
                    ticket_id = excluded.ticket_id,
                    line_user_id = excluded.line_user_id,
                    line_display_name = excluded.line_display_name,
                    id_token_sub = excluded.id_token_sub,
                    bound_at = excluded.bound_at
            """, (
                ticket_id,
                ticket_no,
                line_user_id,
                line_display_name,
                id_token_sub,
                created,
            ))
        conn.commit()

    return get_queue_ticket_by_no(ticket_no)


def call_next_queue_ticket():
    called_at = now_str()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, no
            FROM queue_tickets
            WHERE status = 'waiting'
            ORDER BY id ASC
            LIMIT 1
        """)
        row = c.fetchone()
        if not row:
            return None

        ticket_id = int(row[0])
        c.execute("UPDATE queue_tickets SET status='passed', updated_at=? WHERE status='called'", (called_at,))
        c.execute("""
            UPDATE queue_tickets
            SET status='called', called_at=?, updated_at=?
            WHERE id=?
        """, (called_at, called_at, ticket_id))
        conn.commit()

    ticket = get_queue_ticket_by_no(row[1])
    if ticket:
        set_call_code(ticket["no"])
    return ticket


def clear_queue_today():
    now_value = now_str()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE queue_tickets
            SET status='cleared', updated_at=?
            WHERE status IN ('waiting', 'called', 'passed')
        """, (now_value,))
        conn.commit()
    set_call_code("")



def normalize_cart_item(item: dict) -> dict:
    item = item or {}
    extras = item.get("extras", [])
    if not isinstance(extras, list):
        extras = []
    return {
        "lineId": item.get("lineId") or uuid.uuid4().hex,
        "name": str(item.get("name", "")),
        "enName": item.get("enName"),
        "price": int(item.get("price", 0)),
        "qty": max(1, int(item.get("qty", 1))),
        "remark": str(item.get("remark", "")),
        "temp": item.get("temp"),
        "addOns": item.get("addOns", []),
        "extras": extras,
        "addedBy": str(item.get("addedBy") or "").strip()[:20] or None,
        "category": item.get("category"),
    }


def calc_total(items):
    total = 0
    for it in items or []:
        add = sum(int(a.get("price", 0)) for a in it.get("addOns", []) if isinstance(a, dict))
        extra = sum(
            int(ex.get("price", 0)) * max(1, int(ex.get("qty", 1) or 1))
            for ex in it.get("extras", [])
            if isinstance(ex, dict)
        )
        total += (int(it.get("price", 0)) + add) * int(it.get("qty", 1)) + extra
    return total


def dedupe_by_line_id(items: list) -> list:
    seen = set()
    out = []
    for it in items or []:
        it = it or {}
        lid = str(it.get("lineId") or "")
        if lid:
            if lid in seen:
                continue
            seen.add(lid)
        out.append(it)
    return out


def get_next_print_daily_no(ticket_time: str) -> int:
    day_str = str(ticket_time or "")[:10] or now_str()[:10]
    with get_conn() as conn:
        return allocate_print_daily_no(conn, day_str)


def allocate_print_daily_no(conn, day_str: str) -> int:
    day_str = str(day_str or "")[:10] or now_str()[:10]
    c = conn.cursor()
    c.execute("""
        SELECT COALESCE(MAX(daily_no), 0)
        FROM order_tickets
        WHERE substr(time, 1, 10) = ?
          AND daily_no > 0
    """, (day_str,))
    existing_max = int((c.fetchone() or [0])[0] or 0)

    c.execute("""
        INSERT OR IGNORE INTO daily_counters (day, last_no)
        VALUES (?, ?)
    """, (day_str, existing_max))
    c.execute("""
        UPDATE daily_counters
        SET last_no = CASE
            WHEN last_no < ? THEN ? + 1
            ELSE last_no + 1
        END
        WHERE day = ?
    """, (existing_max, existing_max, day_str))
    c.execute("SELECT last_no FROM daily_counters WHERE day=?", (day_str,))
    row = c.fetchone()
    return int(row[0] or 1) if row else 1


def allocate_daily_order_no(conn, day_str: str) -> int:
    day_str = str(day_str or "")[:10] or now_str()[:10]
    c = conn.cursor()
    c.execute("""
        SELECT COALESCE(MAX(daily_order_no), 0)
        FROM orders
        WHERE substr(time, 1, 10) = ?
          AND daily_order_no > 0
    """, (day_str,))
    existing_max = int((c.fetchone() or [0])[0] or 0)

    c.execute("""
        INSERT OR IGNORE INTO daily_order_counters (day, last_no)
        VALUES (?, ?)
    """, (day_str, existing_max))
    c.execute("""
        UPDATE daily_order_counters
        SET last_no = CASE
            WHEN last_no < ? THEN ? + 1
            ELSE last_no + 1
        END
        WHERE day = ?
    """, (existing_max, existing_max, day_str))
    c.execute("SELECT last_no FROM daily_order_counters WHERE day=?", (day_str,))
    row = c.fetchone()
    return int(row[0] or 1) if row else 1


def assign_daily_no_when_done(ticket_id: int) -> int:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")

        c.execute("""
            SELECT time, daily_no
            FROM order_tickets
            WHERE id = ?
            LIMIT 1
        """, (int(ticket_id),))
        row = c.fetchone()

        if not row:
            conn.rollback()
            return 0

        ticket_time, current_daily_no = row

        if int(current_daily_no or 0) > 0:
            conn.commit()
            return int(current_daily_no)

        day_str = str(ticket_time or "")[:10] or now_str()[:10]
        next_no = allocate_print_daily_no(conn, day_str)

        c.execute("""
            UPDATE order_tickets
            SET daily_no = ?
            WHERE id = ?
        """, (int(next_no), int(ticket_id)))
        conn.commit()

        return int(next_no)


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
                SET cart_json=?, created_at=?, expires_at=?, updated_at=?, order_id=NULL, last_ticket_id=NULL
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


def _clear_session_order_id(session_id: str):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE sessions SET order_id=NULL WHERE session_id=?", (session_id,))
        conn.commit()


def _get_session_last_ticket_id(session_id: str):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT last_ticket_id FROM sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()
    if not row:
        return None
    return row[0]


def _set_session_last_ticket_id(session_id: str, ticket_id: int):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE sessions SET last_ticket_id=?, updated_at=? WHERE session_id=?",
            (int(ticket_id), now_str(), session_id),
        )
        conn.commit()


def _find_existing_order_for_session(session_id: str):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT MAX(id) FROM orders WHERE session_id=?", (session_id,))
        row = c.fetchone()
    return int(row[0]) if row and row[0] else None


def _find_latest_ticket_for_session(session_id: str):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id
            FROM order_tickets
            WHERE session_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id,),
        )
        row = c.fetchone()
    return int(row[0]) if row and row[0] else None


def get_daily_order_no(order_id: int, order_time: str) -> int:
    try:
        day_str = str(order_time or "")[:10]
        if not day_str:
            return int(order_id)
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE substr(time, 1, 10) = ?
                  AND id <= ?
                """,
                (day_str, int(order_id)),
            )
            row = c.fetchone()
        return int(row[0] or 0) or int(order_id)
    except Exception:
        return int(order_id)


def get_order_display_no(order_id: int, order_time: str, daily_order_no: int | None = None) -> int:
    if int(daily_order_no or 0) > 0:
        return int(daily_order_no)
    return get_daily_order_no(int(order_id), order_time)


def _create_order_header(session_id: str, table: str, status: str = "new") -> int:
    status = normalize_status(status, "new")
    table = normalize_order_table(table)
    with get_conn() as conn:
        c = conn.cursor()
        created_at = now_str()
        daily_order_no = allocate_daily_order_no(conn, created_at[:10])
        c.execute("""
            INSERT INTO orders (session_id, table_num, time, items, status, daily_order_no)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (session_id, table or "", created_at, "[]", status, int(daily_order_no)))
        conn.commit()
        return int(c.lastrowid)


def _append_items_to_header(order_id: int, table: str, new_items: list, status: str | None = None):
    table = normalize_order_table(table)
    new_items = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in (new_items or [])]
    if not new_items:
        return

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT items, status FROM orders WHERE id=?", (int(order_id),))
        row = c.fetchone()
        old_items = json.loads(row[0] or "[]") if row else []

        merged = (old_items or []) + new_items
        merged = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in merged]
        merged = dedupe_by_line_id(merged)

        if status and normalize_status(status, "new") != "new":
            c.execute("""
                UPDATE orders
                SET items=?, table_num=?, time=?, status=?
                WHERE id=?
            """, (
                json.dumps(merged, ensure_ascii=False),
                table or "",
                now_str(),
                normalize_status(status, "new"),
                int(order_id)
            ))
        else:
            c.execute("""
                UPDATE orders
                SET items=?, table_num=?, time=?
                WHERE id=?
            """, (
                json.dumps(merged, ensure_ascii=False),
                table or "",
                now_str(),
                int(order_id)
            ))
        conn.commit()


def _get_open_new_ticket(order_id: int):
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
    return row


def _get_next_batch_no(order_id: int) -> int:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT COALESCE(MAX(batch_no), 0) FROM order_tickets WHERE order_id=?", (int(order_id),))
        n = c.fetchone()[0] or 0
    return int(n) + 1


def _create_ticket(order_id: int, session_id: str, table: str, items: list, batch_no: int, status: str = "new") -> int:
    status = normalize_status(status, "new")
    table = normalize_order_table(table)
    ticket_time = now_str()

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO order_tickets (order_id, session_id, table_num, time, items, status, batch_no, daily_no)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(order_id),
            session_id,
            table or "",
            ticket_time,
            json.dumps(items, ensure_ascii=False),
            status,
            int(batch_no),
            0
        ))
        conn.commit()
        return int(c.lastrowid)


def _merge_into_ticket(ticket_id: int, merged_items: list):
    merged_items = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in (merged_items or [])]
    merged_items = dedupe_by_line_id(merged_items)

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE order_tickets
            SET items=?, time=?
            WHERE id=?
        """, (json.dumps(merged_items, ensure_ascii=False), now_str(), int(ticket_id)))
        conn.commit()


def get_or_create_order_id_for_session(session_id: str, table: str, status: str = "new") -> int:
    ensure_session(session_id)

    oid = _get_session_order_id(session_id)
    if oid:
        return int(oid)

    new_oid = _create_order_header(session_id, table, status=status)
    _set_session_order_id(session_id, new_oid)
    return int(new_oid)


def submit_cart_create_or_merge_ticket(session_id: str, table: str, cart: list, status: str = "new", request_id: str | None = None):
    status = normalize_status(status, "new")
    cart_items = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in (cart or [])]
    cart_items = dedupe_by_line_id(cart_items)
    if not cart_items:
        return None

    request_id = request_id or f"server-order-{uuid.uuid4().hex}"
    table = normalize_order_table(table)
    ensure_session(session_id)

    with get_conn() as conn:
        c = conn.cursor()
        try:
            c.execute("BEGIN IMMEDIATE")
            created_at = now_str()
            daily_order_no = allocate_daily_order_no(conn, created_at[:10])
            items_json = json.dumps(cart_items, ensure_ascii=False)
            c.execute("""
                INSERT INTO orders (session_id, table_num, time, items, status, daily_order_no)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                session_id,
                table or "",
                created_at,
                items_json,
                status,
                int(daily_order_no),
            ))
            order_id = int(c.lastrowid)
            batch_no = 1
            c.execute("""
                INSERT INTO order_tickets (order_id, session_id, table_num, time, items, status, batch_no, daily_no)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_id,
                session_id,
                table or "",
                created_at,
                items_json,
                status,
                int(batch_no),
                0,
            ))
            ticket_id = int(c.lastrowid)
            c.execute(
                """
                UPDATE sessions
                SET cart_json=?, order_id=NULL, last_ticket_id=?, updated_at=?
                WHERE session_id=?
                """,
                ("[]", ticket_id, now_str(), session_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            app.logger.exception("[order:%s] db_write_failed session=%s", request_id, session_id)
            raise

    log_order_flow(
        request_id,
        "db_written",
        sessionId=session_id,
        orderId=order_id,
        ticketId=ticket_id,
        itemCount=len(cart_items),
        items=summarize_items(cart_items),
    )
    return {
        "orderId": order_id,
        "ticketId": ticket_id,
        "batchNo": batch_no,
        "merged": False,
        "status": status,
    }

    order_id = get_or_create_order_id_for_session(session_id, table, status=status)

    # 主訂單永遠累積一次
    _append_items_to_header(order_id, table, cart_items, status=status)

    # 只有 new 才允許合併進既有存單
    if status == "new":
        open_ticket = _get_open_new_ticket(order_id)
        if open_ticket:
            ticket_id, old_items_json, batch_no = open_ticket
            try:
                old_items = json.loads(old_items_json or "[]")
            except Exception:
                old_items = []
            merged = (old_items or []) + cart_items
            _merge_into_ticket(ticket_id, merged)
            return {
                "orderId": order_id,
                "ticketId": int(ticket_id),
                "batchNo": int(batch_no or 1),
                "merged": True,
                "status": "new",
            }

    batch_no = _get_next_batch_no(order_id)
    ticket_id = _create_ticket(order_id, session_id, table, cart_items, batch_no, status=status)
    return {
        "orderId": order_id,
        "ticketId": ticket_id,
        "batchNo": batch_no,
        "merged": False,
        "status": status,
    }


def load_order_by_session(session_id):
    order_id = _get_session_order_id(session_id)
    if not order_id:
        last_ticket_id = _get_session_last_ticket_id(session_id)
        if last_ticket_id:
            ticket = load_ticket_by_id(int(last_ticket_id))
            if ticket:
                return ticket

        latest_ticket_id = _find_latest_ticket_for_session(session_id)
        if latest_ticket_id:
            ticket = load_ticket_by_id(int(latest_ticket_id))
            if ticket:
                return ticket

        order_id = _find_existing_order_for_session(session_id)
        if not order_id:
            return None

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, table_num, time, items, status, session_id, daily_order_no
            FROM orders
            WHERE id=?
            LIMIT 1
        """, (int(order_id),))
        row = c.fetchone()

    if not row:
        return None

    oid, table, t, items, status, sid, daily_order_no = row
    items = json.loads(items) if items else []
    items = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in items]
    items = dedupe_by_line_id(items)

    return {
        "id": int(oid),
        "orderId": get_order_display_no(int(oid), t, daily_order_no),
        "dailyNo": None,
        "sessionId": sid,
        "tableNo": normalize_order_table(table),
        "time": t,
        "status": status,
        "items": items,
        "total": calc_total(items),
        "timestamp": to_ts_ms(t),
    }


def load_ticket_by_id(ticket_id: int):
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
                t.batch_no,
                t.daily_no,
                o.daily_order_no
            FROM order_tickets t
            JOIN orders o ON o.id = t.order_id
            WHERE t.id = ?
            LIMIT 1
        """, (int(ticket_id),))
        row = c.fetchone()

    if not row:
        return None

    ticket_id, order_id, sid, table, t, items, status, batch_no, daily_no, daily_order_no = row
    items_list = json.loads(items) if items else []
    items_list = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in items_list]
    items_list = dedupe_by_line_id(items_list)

    return {
        "id": int(ticket_id),
        "orderId": get_order_display_no(int(order_id), t, daily_order_no),
        "dailyNo": int(daily_no or 0),
        "batchNo": int(batch_no or 1),
        "sessionId": sid,
        "tableNo": normalize_order_table(table),
        "time": t,
        "status": status,
        "items": items_list,
        "total": calc_total(items_list),
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
                t.batch_no,
                t.daily_no,
                o.daily_order_no
            FROM order_tickets t
            JOIN orders o ON o.id = t.order_id
            ORDER BY t.id DESC
            LIMIT ?
        """, (limit,))
        rows = c.fetchall()

    out = []
    for ticket_id, order_id, sid, table, t, items, status, batch_no, daily_no, daily_order_no in rows:
        items_list = json.loads(items) if items else []
        items_list = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in items_list]
        items_list = dedupe_by_line_id(items_list)

        out.append({
            "id": int(ticket_id),
            "orderId": get_order_display_no(int(order_id), t, daily_order_no),
            "dailyNo": int(daily_no or 0),
            "batchNo": int(batch_no or 1),
            "sessionId": sid,
            "tableNo": normalize_order_table(table),
            "time": t,
            "status": status,
            "items": items_list,
            "total": calc_total(items_list),
            "timestamp": to_ts_ms(t),
        })
    return out


def update_ticket_status(ticket_id: int, status: str) -> bool:
    status = normalize_status(status, "new")
    status_time = now_str()

    with get_conn() as conn:
        c = conn.cursor()

        if status == "done":
            c.execute("UPDATE order_tickets SET status=?, time=? WHERE id=?", (status, status_time, int(ticket_id)))
        else:
            c.execute("UPDATE order_tickets SET status=? WHERE id=?", (status, int(ticket_id)))
        ok = c.rowcount > 0

        if ok:
            c.execute("SELECT order_id FROM order_tickets WHERE id=?", (int(ticket_id),))
            row = c.fetchone()
            if row and row[0]:
                c.execute("UPDATE orders SET status=?, time=? WHERE id=?", (status, status_time, int(row[0])))

        conn.commit()

    if not ok:
        return False

    if status == "done":
        assign_daily_no_when_done(ticket_id)

    return True


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
    data = data or {}
    request_id = make_request_id(data)
    sid = str((data.get("sessionId") or "")).strip()
    o = load_order_by_session(sid)
    log_order_flow(
        request_id,
        "frontend_order_detail_response",
        sessionId=sid,
        exists=bool(o),
        order=o,
    )
    emit("order_detail_result", {"ok": True, "exists": bool(o), "order": o, "requestId": request_id})


@socketio.on("submit_cart_as_order")
def on_submit(data):
    data = data or {}
    request_id = make_request_id(data)
    sid = str((data.get("sessionId") or "")).strip()
    table = normalize_order_table(data.get("table", ""))
    status = normalize_status(data.get("status", "new"), "new")

    if not sid:
        emit("submit_result", {"ok": False, "msg": "missing sessionId", "requestId": request_id})
        return

    ensure_session(sid)
    session_cart = get_session_cart(sid)
    raw_items = data.get("items")
    cart = raw_items if isinstance(raw_items, list) else session_cart
    cart = [normalize_cart_item(x if isinstance(x, dict) else {}) for x in (cart or [])]
    cart = dedupe_by_line_id(cart)
    log_order_flow(
        request_id,
        "socket_received",
        sessionId=sid,
        table=table,
        source="client_items" if isinstance(raw_items, list) else "session_cart",
        clientItems=summarize_items(raw_items if isinstance(raw_items, list) else []),
        sessionCart=summarize_items(session_cart),
        usedItems=summarize_items(cart),
    )
    if isinstance(raw_items, list) and summarize_items(raw_items) != summarize_items(session_cart):
        log_order_flow(
            request_id,
            "socket_cart_snapshot_diff",
            sessionId=sid,
            clientItems=summarize_items(raw_items),
            sessionCart=summarize_items(session_cart),
        )
    if not cart:
        emit("submit_result", {"ok": False, "msg": "cart empty", "requestId": request_id}, room=sid)
        return

    result = submit_cart_create_or_merge_ticket(sid, table, cart, status=status, request_id=request_id)
    if not result:
        emit("submit_result", {"ok": False, "msg": "submit failed", "requestId": request_id})
        socketio.emit("submit_result", {"ok": False, "msg": "submit failed", "requestId": request_id}, room=sid, include_self=False)
        return

    save_session_cart(sid, [])
    locks_in_room[sid] = {}

    order_detail = load_order_by_session(sid)
    ticket_detail = load_ticket_by_id(result["ticketId"])
    _set_session_last_ticket_id(sid, result["ticketId"])

    payload = {
        "ok": True,
        "requestId": request_id,
        "orderId": int(ticket_detail["orderId"]) if ticket_detail else int(order_detail["orderId"] if order_detail else result["orderId"]),
        "dailyNo": int(ticket_detail["dailyNo"] if ticket_detail else 0),
        "ticketId": result["ticketId"],
        "batchNo": result["batchNo"],
        "merged": result["merged"],
        "status": result["status"],
        "order": ticket_detail,
    }
    _clear_session_order_id(sid)
    log_order_flow(
        request_id,
        "socket_response",
        sessionId=sid,
        ticketId=result["ticketId"],
        order=ticket_detail,
    )
    log_order_flow(
        request_id,
        "android_payload",
        endpoint="socket:submit_result/order_detail_result",
        order=ticket_detail or order_detail,
    )
    emit("submit_result", payload)
    socketio.emit("submit_result", payload, room=sid, include_self=False)

    socketio.emit(
        "order_detail_result",
        {"ok": True, "exists": True, "order": ticket_detail or order_detail},
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
    request_id = make_request_id({})
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200
    tickets = load_all_tickets(limit)
    log_order_flow(
        request_id,
        "orders_list_response",
        endpoint=request.path,
        count=len(tickets),
        orders=[
            {
                "ticketId": o.get("id"),
                "orderId": o.get("orderId"),
                "sessionId": o.get("sessionId"),
                "items": summarize_items(o.get("items", [])),
            }
            for o in tickets[:20]
        ],
    )
    return jsonify({"ok": True, "count": len(tickets), "orders": tickets})


@app.route("/api/orders", methods=["GET"])
def list_orders_api():
    request_id = make_request_id({})
    try:
        limit = int(request.args.get("limit", "200"))
    except ValueError:
        limit = 200
    tickets = load_all_tickets(limit)
    log_order_flow(
        request_id,
        "android_payload",
        endpoint=request.path,
        count=len(tickets),
        orders=[
            {
                "ticketId": o.get("id"),
                "orderId": o.get("orderId"),
                "sessionId": o.get("sessionId"),
                "items": summarize_items(o.get("items", [])),
            }
            for o in tickets[:20]
        ],
    )
    return jsonify({"ok": True, "orders": tickets})


# ================== REST: 建單（給 Android / Web） ==================
def _parse_create_order_payload(data: dict):
    data = data or {}
    session_id = str(data.get("sessionId") or "").strip()
    if not session_id:
        session_id = create_unique_session_id()

    table = normalize_order_table(data.get("tableNo") or data.get("table") or "")
    status = normalize_status(data.get("status", "new"), "new")

    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        raw_items = []

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = {
            "lineId": raw.get("lineId") or uuid.uuid4().hex,
            "name": str(raw.get("name", "")),
            "enName": raw.get("enName"),
            "price": int(raw.get("price", 0)),
            "qty": max(1, int(raw.get("qty", 1))),
            "remark": str(raw.get("remark", "") or ""),
            "temp": raw.get("temp"),
            "addOns": raw.get("addOns", []) or [],
            "extras": raw.get("extras", []) or [],
            "addedBy": str(raw.get("addedBy") or "").strip()[:20] or None,
            "category": raw.get("category"),
        }
        items.append(normalize_cart_item(item))

    items = dedupe_by_line_id(items)
    total = int(data.get("total", calc_total(items)) or 0)

    return session_id, table, status, items, total


def _create_order_common():
    data = request.get_json(silent=True) or {}
    request_id = make_request_id(data)
    session_id, table, status, items, total = _parse_create_order_payload(data)
    log_order_flow(
        request_id,
        "rest_received",
        sessionId=session_id,
        table=table,
        total=total,
        items=summarize_items(items),
    )

    if not items:
        return jsonify({"ok": False, "msg": "items empty", "requestId": request_id}), 400

    result = submit_cart_create_or_merge_ticket(
        session_id=session_id,
        table=table,
        cart=items,
        status=status,
        request_id=request_id,
    )
    if not result:
        return jsonify({"ok": False, "msg": "create failed", "requestId": request_id}), 500

    created_ticket = load_ticket_by_id(result["ticketId"])
    if not created_ticket:
        return jsonify({"ok": False, "msg": "ticket not found after create", "requestId": request_id}), 500

    _set_session_last_ticket_id(session_id, result["ticketId"])
    _clear_session_order_id(session_id)

    response_body = {
        "ok": True,
        "requestId": request_id,
        "id": created_ticket["id"],
        "ticketId": created_ticket["id"],
        "orderId": created_ticket["orderId"],
        "dailyNo": created_ticket["dailyNo"],
        "batchNo": created_ticket["batchNo"],
        "merged": result["merged"],
        "status": created_ticket["status"],
        "order": created_ticket,
    }
    log_order_flow(
        request_id,
        "rest_response",
        sessionId=session_id,
        ticketId=created_ticket["id"],
        order=created_ticket,
    )
    log_order_flow(
        request_id,
        "android_payload",
        endpoint=request.path,
        order=created_ticket,
    )
    return jsonify(response_body)


@app.route("/api/orders", methods=["POST"])
def create_order_api():
    return _create_order_common()


@app.route("/api/order", methods=["POST"])
def create_order_api2():
    return _create_order_common()


@app.route("/orders", methods=["POST"])
def create_order_legacy():
    return _create_order_common()


# 更新 ticket 狀態（不是主訂單）
@app.route("/api/orders/<int:ticket_id>/status", methods=["POST"])
def update_order_status_api(ticket_id):
    data = request.get_json(silent=True) or {}
    status = normalize_status(data.get("status", "new"), "new")

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
    request_id = make_request_id({})
    o = load_order_by_session(sid)
    log_order_flow(
        request_id,
        "frontend_order_detail_response",
        endpoint=request.path,
        sessionId=sid,
        exists=bool(o),
        order=o,
    )
    return jsonify({"ok": True, "exists": bool(o), "order": o, "requestId": request_id})


@app.route("/order_detail/<sid>", methods=["GET"])
def order_detail(sid):
    request_id = make_request_id({})
    o = load_order_by_session(sid)
    log_order_flow(
        request_id,
        "frontend_order_detail_response",
        endpoint=request.path,
        sessionId=sid,
        exists=bool(o),
        order=o,
    )
    return jsonify({"ok": True, "exists": bool(o), "order": o, "requestId": request_id})


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


# ================== Queue Ticket API ==================
@app.route("/api/status", methods=["GET"])
def api_queue_status():
    return jsonify({
        "ok": True,
        "current_call": get_current_called_ticket(),
        "waiting_count": get_waiting_count(),
    })


@app.route("/api/queue/entry-token", methods=["GET"])
def api_queue_entry_token():
    day_value = today_str()
    token = sign_queue_entry_day(day_value)
    return jsonify({
        "ok": True,
        "day": day_value,
        "token": token,
    })


@app.route("/api/queue/validate-entry", methods=["GET"])
def api_queue_validate_entry():
    day_value = str(request.args.get("day", "")).strip()
    token_value = str(request.args.get("token", "")).strip()
    return jsonify({
        "ok": True,
        "valid": validate_queue_entry(day_value, token_value),
        "today": today_str(),
    })


@app.route("/api/tickets", methods=["POST"])
def api_queue_take_ticket():
    data = request.get_json(silent=True) or {}
    surname = str(data.get("surname", "")).strip()
    party_size = int(data.get("party_size", 0) or 0)
    phone = str(data.get("phone", "")).strip()

    if not surname:
        return jsonify({"ok": False, "detail": "surname required"}), 400
    if party_size <= 0:
        return jsonify({"ok": False, "detail": "party_size required"}), 400

    ticket = create_queue_ticket(
        surname=surname,
        party_size=party_size,
        phone=phone,
    )

    return jsonify(ticket)


@app.route("/api/tickets/<ticket_no>", methods=["GET"])
def api_queue_ticket_detail(ticket_no):
    ticket = get_queue_ticket_by_no(ticket_no)
    if not ticket:
        return jsonify({"ok": False, "detail": "ticket not found"}), 404
    return jsonify({
        "ok": True,
        "ticket": ticket,
    })


@app.route("/api/admin/tickets", methods=["POST"])
def api_admin_create_ticket():
    return api_queue_take_ticket()


@app.route("/api/admin/queue", methods=["GET"])
def api_admin_queue():
    return jsonify(get_waiting_queue())


@app.route("/api/admin/next", methods=["POST"])
def api_admin_next():
    ticket = call_next_queue_ticket()
    if not ticket:
        return jsonify({"ok": False, "detail": "目前沒有等待中的客人"}), 404

    return jsonify({
        "ok": True,
        "current_call": ticket,
        "waiting_count": get_waiting_count(),
    })


@app.route("/api/admin/repeat", methods=["GET", "POST"])
def api_admin_repeat():
    ticket = get_current_called_ticket()
    if not ticket:
        return jsonify({"ok": True, "current_call": None, "waiting_count": get_waiting_count()})

    set_call_code(ticket["no"])
    return jsonify({
        "ok": True,
        "current_call": ticket,
        "waiting_count": get_waiting_count(),
    })


@app.route("/api/admin/clear", methods=["POST"])
def api_admin_clear():
    clear_queue_today()
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
