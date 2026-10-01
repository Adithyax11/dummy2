import contextvars
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

SERVICE = "payment-service"

DB_PATH = os.getenv("DB_PATH", "/data/payments.db")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8000")
INTERNAL_API_KEY = os.environ["INTERNAL_API_KEY"]
if len(INTERNAL_API_KEY) < 32:
    raise RuntimeError("INTERNAL_API_KEY must be at least 32 characters (set it in .env)")

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 0.5
STALE_SECONDS = 60
PAYABLE = {"pending", "failed"}

RECONCILE_INTERVAL = int(os.getenv("RECONCILE_INTERVAL", "30"))
RECONCILE_MIN_AGE = 15  # don't touch rows a live request may still be working on

# payments.order_synced values
SYNC_PENDING = 0       # order status update still owed
SYNCED = 1             # order status updated
NEEDS_REVIEW = 2       # customer charged but order can't be marked paid


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
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                card_last4 TEXT NOT NULL,
                status TEXT NOT NULL,
                provider_ref TEXT,
                failure_reason TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                order_synced INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        # only one in-flight payment per order
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_processing "
            "ON payments(order_id) WHERE status = 'processing'"
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)")


init_db()


# ---------- schemas ----------
class PayRequest(BaseModel):
    order_id: int = Field(gt=0)
    card_number: str

    @field_validator("card_number")
    @classmethod
    def clean_card(cls, v: str) -> str:
        v = re.sub(r"[\s-]", "", v)
        if not re.fullmatch(r"\d{13,19}", v):
            raise ValueError("card_number must be 13-19 digits")
        return v


# ---------- mock payment provider ----------
class ProviderDeclined(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class ProviderTransientError(Exception):
    pass


def charge_provider(card_number: str, amount: float, attempt: int) -> str:
    """Fake provider. Behaviour is chosen by the card number (see test cards)."""
    time.sleep(0.2)  # pretend network latency

    if card_number == "4000000000000002":
        raise ProviderDeclined("card_declined")
    if card_number == "4000000000009995":
        raise ProviderDeclined("insufficient_funds")
    if card_number == "4000000000000119":
        raise ProviderTransientError("provider_unavailable")  # always fails
    if card_number == "4000000000000259" and attempt < 3:
        raise ProviderTransientError("provider_timeout")  # works on 3rd try

    return f"mock_{uuid.uuid4().hex[:12]}"


def run_charge(card_number: str, amount: float) -> dict:
    last_error = "provider_unavailable"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            ref = charge_provider(card_number, amount, attempt)
            return {"ok": True, "ref": ref, "attempts": attempt, "reason": None}
        except ProviderDeclined as e:
            # declines are final, retrying won't help
            return {"ok": False, "ref": None, "attempts": attempt, "reason": e.reason}
        except ProviderTransientError as e:
            last_error = str(e)
            logger.warning("provider transient error (%s), attempt %s/%s", last_error, attempt, MAX_ATTEMPTS)
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_SECONDS * (2 ** (attempt - 1)))
    return {"ok": False, "ref": None, "attempts": MAX_ATTEMPTS, "reason": last_error}


# ---------- order service calls ----------
def fetch_order(order_id: int, user_id: int) -> dict:
    try:
        r = httpx.get(
            f"{ORDER_SERVICE_URL}/{order_id}",
            headers={"X-User-Id": str(user_id), "X-Request-ID": request_id_var.get()},
            timeout=5,
        )
    except httpx.RequestError as e:
        logger.warning("order service unreachable: %s", e)
        raise HTTPException(status_code=502, detail="Order service unavailable")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Order not found")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Order service error")
    return r.json()


def update_order_status(order_id: int, status: str) -> str:
    """Returns 'ok', 'conflict' (order refuses the change) or 'error' (try again later)."""
    headers = {"X-Internal-Key": INTERNAL_API_KEY, "X-Request-ID": request_id_var.get()}
    for attempt in range(3):
        try:
            r = httpx.patch(
                f"{ORDER_SERVICE_URL}/internal/{order_id}/status",
                json={"status": status},
                headers=headers,
                timeout=5,
            )
            if r.status_code == 200:
                return "ok"
            if r.status_code in (404, 409):
                return "conflict"
            if 400 <= r.status_code < 500:
                logger.error("order service rejected status update: HTTP %s", r.status_code)
                return "error"
        except httpx.RequestError as e:
            logger.warning("order status update failed (attempt %s/3): %s", attempt + 1, e)
        time.sleep(0.3 * (attempt + 1))
    return "error"


def record_sync(payment_id: int, order_id: int, succeeded: bool, outcome: str) -> int:
    """Turn the order-service result into an order_synced state, log it, save it."""
    if outcome == "ok":
        state = SYNCED
    elif outcome == "conflict":
        if succeeded:
            state = NEEDS_REVIEW
            logger.error(
                "NEEDS REVIEW: payment=%s order=%s was charged but the order can no longer be "
                "marked paid (cancelled or changed). Refund or fix manually.",
                payment_id, order_id,
            )
        else:
            state = SYNCED  # a failed payment on an order that moved on: nothing to do
    else:
        state = SYNC_PENDING
        logger.warning("order sync pending payment=%s order=%s (reconciler will retry)", payment_id, order_id)

    with get_db() as db:
        db.execute("UPDATE payments SET order_synced = ? WHERE id = ?", (state, payment_id))
    return state


# ---------- background reconciliation ----------
def expire_stale(order_id: Optional[int] = None):
    """Payments stuck in 'processing' (e.g. the service crashed mid-charge) are marked failed.
    With a real provider you would ask the provider what happened before doing this."""
    now = int(time.time())
    query = (
        "UPDATE payments SET status = 'failed', failure_reason = 'timed_out', "
        "order_synced = ?, updated_at = ? WHERE status = 'processing' AND created_at < ?"
    )
    params: list = [SYNCED, now, now - STALE_SECONDS]
    if order_id is not None:
        query += " AND order_id = ?"
        params.append(order_id)
    with get_db() as db:
        cur = db.execute(query, params)
        if cur.rowcount:
            logger.warning("expired %s stuck payment(s)", cur.rowcount)


def reconcile_once():
    request_id_var.set(f"recon-{uuid.uuid4().hex[:8]}")
    expire_stale()

    cutoff = int(time.time()) - RECONCILE_MIN_AGE
    with get_db() as db:
        rows = db.execute(
            "SELECT id, order_id, status FROM payments "
            "WHERE order_synced = ? AND status IN ('succeeded', 'failed') AND updated_at < ? "
            "ORDER BY id ASC LIMIT 100",
            (SYNC_PENDING, cutoff),
        ).fetchall()

    for row in rows:
        succeeded = row["status"] == "succeeded"
        outcome = update_order_status(row["order_id"], "paid" if succeeded else "failed")
        state = record_sync(row["id"], row["order_id"], succeeded, outcome)
        if state != SYNC_PENDING:
            logger.info(
                "reconciled payment=%s order=%s -> %s",
                row["id"], row["order_id"], "synced" if state == SYNCED else "needs review",
            )


def reconcile_loop(stop: threading.Event):
    logger.info("reconciler started (every %ss)", RECONCILE_INTERVAL)
    while not stop.wait(RECONCILE_INTERVAL):
        try:
            reconcile_once()
        except Exception:
            logger.exception("reconciler error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    thread = threading.Thread(target=reconcile_loop, args=(stop,), daemon=True)
    thread.start()
    yield
    stop.set()


app = FastAPI(title="Payment Service", lifespan=lifespan)


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


# ---------- helpers ----------
def current_user_id(x_user_id: Optional[str]) -> int:
    if not x_user_id or not x_user_id.isdigit():
        raise HTTPException(status_code=401, detail="Missing user identity")
    return int(x_user_id)


def row_to_payment(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["needs_review"] = d["order_synced"] == NEEDS_REVIEW
    d["order_synced"] = d["order_synced"] == SYNCED
    return d


# ---------- routes ----------
@app.get("/health")
def health():
    return {"service": SERVICE, "status": "ok"}


@app.post("/pay")
def pay(body: PayRequest, x_user_id: Optional[str] = Header(default=None)):
    user_id = current_user_id(x_user_id)

    order = fetch_order(body.order_id, user_id)
    if order["status"] not in PAYABLE:
        raise HTTPException(
            status_code=409, detail=f"Order is {order['status']} and cannot be paid"
        )

    amount = order["total"]  # always the server's price, never the client's
    now = int(time.time())

    expire_stale(body.order_id)

    with get_db() as db:
        already_paid = db.execute(
            "SELECT 1 FROM payments WHERE order_id = ? AND status = 'succeeded'",
            (body.order_id,),
        ).fetchone()
        if already_paid:
            raise HTTPException(status_code=409, detail="Order already has a successful payment")

        try:
            cur = db.execute(
                """
                INSERT INTO payments (order_id, user_id, amount, card_last4, status, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'processing', 0, ?, ?)
                """,
                (body.order_id, user_id, amount, body.card_number[-4:], now, now),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409, detail="A payment for this order is already in progress"
            )
        payment_id = cur.lastrowid

    # talk to the provider (no DB connection held open while we wait)
    result = run_charge(body.card_number, amount)
    status = "succeeded" if result["ok"] else "failed"

    with get_db() as db:
        db.execute(
            "UPDATE payments SET status = ?, provider_ref = ?, failure_reason = ?, "
            "attempts = ?, updated_at = ? WHERE id = ?",
            (status, result["ref"], result["reason"], result["attempts"], int(time.time()), payment_id),
        )
    logger.info(
        "payment %s order=%s user_id=%s amount=%s -> %s (attempts=%s, reason=%s)",
        payment_id, body.order_id, user_id, amount, status, result["attempts"], result["reason"],
    )

    outcome = update_order_status(body.order_id, "paid" if result["ok"] else "failed")
    state = record_sync(payment_id, body.order_id, result["ok"], outcome)

    with get_db() as db:
        row = db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()

    out = row_to_payment(row)
    if state == SYNC_PENDING:
        out["warning"] = "Payment recorded but the order status update is pending"
    elif state == NEEDS_REVIEW:
        out["warning"] = "Payment succeeded but the order could not be marked paid. Flagged for manual review"
    return out


@app.get("/")
def list_payments(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    x_user_id: Optional[str] = Header(default=None),
):
    user_id = current_user_id(x_user_id)
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
    return [row_to_payment(r) for r in rows]


@app.get("/order/{order_id}")
def payments_for_order(order_id: int, x_user_id: Optional[str] = Header(default=None)):
    user_id = current_user_id(x_user_id)
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM payments WHERE order_id = ? AND user_id = ? ORDER BY id DESC",
            (order_id, user_id),
        ).fetchall()
    return [row_to_payment(r) for r in rows]


@app.get("/{payment_id}")
def get_payment(payment_id: int, x_user_id: Optional[str] = Header(default=None)):
    user_id = current_user_id(x_user_id)
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM payments WHERE id = ? AND user_id = ?", (payment_id, user_id)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Payment not found")
    return row_to_payment(row)