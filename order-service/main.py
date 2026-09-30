import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DB_PATH", "/data/orders.db")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "dev-internal-key")

STATUSES = {"pending", "paid", "failed", "cancelled"}
CANCELLABLE = {"pending", "failed"}
# status changes the payment service is allowed to make
ALLOWED_TRANSITIONS = {"pending": {"paid", "failed"}, "failed": {"paid", "failed"}}

app = FastAPI(title="Order Service")


# ---------- database ----------
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price REAL NOT NULL,
                total REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")


init_db()


# ---------- schemas ----------
class OrderCreate(BaseModel):
    item_name: str = Field(min_length=1, max_length=100)
    quantity: int = Field(gt=0, le=1000)
    unit_price: float = Field(gt=0, le=1_000_000)


class StatusUpdate(BaseModel):
    status: Literal["paid", "failed"]


# ---------- helpers ----------
def current_user_id(x_user_id: Optional[str]) -> int:
    """The gateway sets X-User-Id after verifying the JWT."""
    if not x_user_id or not x_user_id.isdigit():
        raise HTTPException(status_code=401, detail="Missing user identity")
    return int(x_user_id)


def require_internal_key(x_internal_key: Optional[str]):
    if not x_internal_key or not secrets.compare_digest(
        x_internal_key.encode(), INTERNAL_API_KEY.encode()
    ):
        raise HTTPException(status_code=403, detail="Forbidden")


def row_to_order(row: sqlite3.Row) -> dict:
    return dict(row)


# ---------- routes ----------
@app.get("/health")
def health():
    return {"service": "order-service", "status": "ok"}


@app.post("/", status_code=201)
def create_order(body: OrderCreate, x_user_id: Optional[str] = Header(default=None)):
    user_id = current_user_id(x_user_id)
    now = int(time.time())
    total = round(body.quantity * body.unit_price, 2)

    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO orders (user_id, item_name, quantity, unit_price, total, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (user_id, body.item_name.strip(), body.quantity, body.unit_price, total, now, now),
        )
        order = db.execute("SELECT * FROM orders WHERE id = ?", (cur.lastrowid,)).fetchone()

    return row_to_order(order)


@app.get("/")
def list_orders(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    x_user_id: Optional[str] = Header(default=None),
):
    user_id = current_user_id(x_user_id)

    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(STATUSES)}")

    query = "SELECT * FROM orders WHERE user_id = ?"
    params: list = [user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]

    with get_db() as db:
        rows = db.execute(query, params).fetchall()

    return [row_to_order(r) for r in rows]


@app.get("/{order_id}")
def get_order(order_id: int, x_user_id: Optional[str] = Header(default=None)):
    user_id = current_user_id(x_user_id)

    with get_db() as db:
        order = db.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
        ).fetchone()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return row_to_order(order)


@app.post("/{order_id}/cancel")
def cancel_order(order_id: int, x_user_id: Optional[str] = Header(default=None)):
    user_id = current_user_id(x_user_id)

    with get_db() as db:
        order = db.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
        ).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order["status"] not in CANCELLABLE:
            raise HTTPException(
                status_code=409,
                detail=f"Only pending or failed orders can be cancelled (this one is {order['status']})",
            )

        db.execute(
            "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (int(time.time()), order_id),
        )
        order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    return row_to_order(order)


# ---------- internal (service to service only, blocked at the gateway) ----------
@app.patch("/internal/{order_id}/status")
def internal_update_status(
    order_id: int,
    body: StatusUpdate,
    x_internal_key: Optional[str] = Header(default=None),
):
    require_internal_key(x_internal_key)

    with get_db() as db:
        order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if body.status not in ALLOWED_TRANSITIONS.get(order["status"], set()):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot change status from {order['status']} to {body.status}",
            )

        db.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (body.status, int(time.time()), order_id),
        )
        order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    return row_to_order(order)