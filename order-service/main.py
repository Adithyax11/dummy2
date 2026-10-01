import contextvars
import logging
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

SERVICE = "order-service"

DB_PATH = os.getenv("DB_PATH", "/data/orders.db")
INTERNAL_API_KEY = os.environ["INTERNAL_API_KEY"]
if len(INTERNAL_API_KEY) < 32:
    raise RuntimeError("INTERNAL_API_KEY must be at least 32 characters (set it in .env)")

STATUSES = {"pending", "paid", "failed", "cancelled"}
CANCELLABLE = {"pending", "failed"}
# status changes the payment service is allowed to make
ALLOWED_TRANSITIONS = {"pending": {"paid", "failed"}, "failed": {"paid", "failed"}}


# ---------- logging ----------
request_id_var = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True


def setup_logging():
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        logging.Formatter(f"%(asctime)s %(levelname)s [{SERVICE}] [%(request_id)s] %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logging.getLogger("uvicorn.access").disabled = True


setup_logging()
logger = logging.getLogger(SERVICE)

app = FastAPI(title="Order Service")


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = (request.headers.get("x-request-id") or uuid.uuid4().hex[:12])[:64]
    token = request_id_var.set(rid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        response = JSONResponse({"detail": "Internal server error"}, status_code=500)
    ms = (time.perf_counter() - start) * 1000
    if request.url.path != "/health":
        logger.info("%s %s -> %s (%.0f ms)", request.method, request.url.path, response.status_code, ms)
    response.headers["X-Request-ID"] = rid
    request_id_var.reset(token)
    return response


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
        logger.warning("internal route called with a bad key")
        raise HTTPException(status_code=403, detail="Forbidden")


def row_to_order(row: sqlite3.Row) -> dict:
    return dict(row)


# ---------- routes ----------
@app.get("/health")
def health():
    return {"service": SERVICE, "status": "ok"}


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

    logger.info("order created id=%s user_id=%s total=%s", order["id"], user_id, total)
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

    logger.info("order cancelled id=%s user_id=%s", order_id, user_id)
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

        # idempotent: asking for the status it already has is a success
        if order["status"] == body.status:
            return row_to_order(order)

        if body.status not in ALLOWED_TRANSITIONS.get(order["status"], set()):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot change status from {order['status']} to {body.status}",
            )

        old_status = order["status"]
        db.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (body.status, int(time.time()), order_id),
        )
        order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    logger.info("order %s status %s -> %s", order_id, old_status, body.status)
    return row_to_order(order)