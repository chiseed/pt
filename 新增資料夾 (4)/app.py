from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import os
import datetime
import json
import time

app = Flask(__name__)
CORS(app)

DB_FILE = "orders.db"

def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_num TEXT,
                time TEXT,            -- 存字串，例如 2025-08-12 12:34:56
                items TEXT,           -- JSON
                status TEXT DEFAULT 'new'
            )
        ''')
        try:
            c.execute("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'new'")
        except:
            pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_time ON orders(time)")
        conn.commit()

def to_ts_ms(dt_str: str) -> int:
    # 將 'YYYY-mm-dd HH:MM:SS' 轉毫秒
    try:
        dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)

def normalize_item(item: dict) -> dict:
    """正規化成 Android RawOrder->SimpleCartItem 能理解的結構"""
    name = str(item.get("name", item.get("product", {}).get("name", "")))
    price = int(item.get("price", item.get("product", {}).get("price", 0)))
    qty = int(item.get("qty", 1))
    remark = str(item.get("remark", ""))

    drink_obj = item.get("drink")
    if isinstance(drink_obj, dict):
        drink = {
            "name": str(drink_obj.get("name", "")),
            "price": int(drink_obj.get("price", 0))
        }
    else:
        drink = None

    main_option = item.get("mainOption")
    if main_option is not None:
        main_option = str(main_option)

    return {
        "name": name,
        "price": price,
        "qty": qty,
        "remark": remark,
        "drink": drink,
        "mainOption": main_option
    }

@app.route('/order', methods=['POST'])
def order():
    data = request.get_json(force=True, silent=True) or {}
    table_num = str(data.get("table", ""))
    order_time = str(data.get("time", datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

    # 正規化 items
    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        raw_items = []
    norm_items = [normalize_item(i) for i in raw_items]
    items_json = json.dumps(norm_items, ensure_ascii=False)

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO orders (table_num, time, items, status) VALUES (?, ?, ?, 'new')",
            (table_num, order_time, items_json)
        )
        conn.commit()
        new_id = c.lastrowid

    print(f"[NEW ORDER] id={new_id} table={table_num} time={order_time} items={items_json}")
    return jsonify({"status": "ok", "id": new_id})

@app.route('/orders', methods=['GET'])
def get_orders():
    # 只回傳未處理的新訂單
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, table_num, time, items FROM orders WHERE status='new' ORDER BY id DESC")
        rows = c.fetchall()

    orders = []
    for oid, table_num, time_str, items_json in rows:
        try:
            items = json.loads(items_json)
        except Exception:
            items = []

        # 計價：若你要後端主導，這裡可放規則；否則就純前端計
        total = 0
        for it in items:
            price = int(it.get("price", 0))
            qty = int(it.get("qty", 1))
            subtotal = price * qty
            drink = it.get("drink")
            if isinstance(drink, dict):
                # 若有飲料加購差價規則，這裡放清楚；以下僅示例（請改成你實際規則）
                drink_price = int(drink.get("price", 0))
                # 假設基底為 40
                subtotal += max(0, drink_price - 40) * qty
            total += subtotal

        orders.append({
            "id": int(oid),
            "tableNo": str(table_num),
            "total": int(total),
            "timestamp": to_ts_ms(time_str),  # ★ 改成毫秒
            "items": items                    # 給 Android RawOrder 解析
        })
    return jsonify(orders)

@app.route('/orders/ack', methods=['POST'])
def ack_orders():
    """
    批次標記已處理：接收 {"ids":[1,2,3]}
    Android 列印成功後呼叫這個，不要用 delete_all。
    """
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get("ids", [])
    if not isinstance(ids, list) or not ids:
        return jsonify({"status": "bad_request", "msg": "ids must be a non-empty list"}), 400

    q_marks = ",".join(["?"] * len(ids))
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE orders SET status='done' WHERE id IN ({q_marks})", ids)
        conn.commit()

    return jsonify({"status": "ok", "updated": len(ids)})

@app.route('/orders/<int:order_id>/done', methods=['POST'])
def order_done(order_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE orders SET status='done' WHERE id=?", (order_id,))
        conn.commit()
    return jsonify({"status": "ok"})

@app.route('/orders/delete_all', methods=['POST'])
def delete_all_orders():
    # 強烈建議不要在正式流程用這個；改用 /orders/ack
    with get_conn() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM orders')
        conn.commit()
    return jsonify({"result": "ok"})

@app.route('/orders/purge_done', methods=['POST'])
def purge_done():
    """
    可選：清掉已完成且超過 N 天的訂單。預設 3 天。
    body: {"days": 3}
    """
    data = request.get_json(force=True, silent=True) or {}
    days = int(data.get("days", 3))
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        c = conn.cursor()
        # time 欄位是字串，這裡以字串比較；你也可以另外加一個 timestamp_ms 欄位做數值比較
        c.execute("DELETE FROM orders WHERE status='done' AND time < ?", (cutoff_str,))
        conn.commit()
        count = c.rowcount

    return jsonify({"status": "ok", "deleted": count})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8000)
