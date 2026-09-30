import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

DB_PATH = os.getenv("DB_PATH", "/data/payments.db")
ORDER_SERVICE_URL = os.getenv("ORDER_SERVICE_URL", "http://order-service:8000")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "dev-internal-key")

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 0.5
STALE_SECONDS = 60
PAYABLE = {"pending", "failed"}

app = FastAPI(title="Payment Service")


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
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_SECONDS * (2 ** (attempt - 1)))
    return {"ok": False, "ref": None, "attempts": MAX_ATTEMPTS, "reason": last_error}


# ---------- order service calls ----------
def fetch_order(order_id: int, user_id: int) -> dict:
    try:
        r = httpx.get(
            f"{ORDER_SERVICE_URL}/{order_id}",
            headers={"X-User-Id": str(user_id)},
            timeout=5,
        )
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Order service unavailable")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Order not found")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Order service error")
    return r.json()


def update_order_status(order_id: int, status: str) -> bool:
    for attempt in range(3):
        try:
            r = httpx.patch(
                f"{ORDER_SERVICE_URL}/internal/{order_id}/status",
                json={"status": status},
                headers={"X-Internal-Key": INTERNAL_API_KEY},
                timeout=5,
            )
            if r.status_code == 200:
                return True
            if 400 <= r.status_code < 500:
                return False  # retrying won't fix a client error
        except httpx.RequestError:
            pass
        time.sleep(0.3 * (attempt + 1))
    return False


# ---------- helpers ----------
def current_user_id(x_user_id: Optional[str]) -> int:
    if not x_user_id or not x_user_id.isdigit():
        raise HTTPException(status_code=401, detail="Missing user identity")
    return int(x_user_id)


def row_to_payment(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["order_synced"] = bool(d["order_synced"])
    return d


# ---------- routes ----------
@app.get("/health")
def health():
    return {"service": "payment-service", "status": "ok"}


@app.post("/pay")
def pay(body: PayRequest, x_user_id: Optional[str] = Header(default=None)):
    user_id = current_user_id(x_user_id)

    order = fetch_order(body.order_id, user_id)
    if order["status"] not in PAYABLE:
        raise HTTPException(
            status_code=409, detail=f"Order is {order['status']} and cannot be paid"
        )

    amount = order["total"]
    now = int(time.time())

    # clear out payments that got stuck in 'processing' (e.g. a crash)
    with get_db() as db:
        db.execute(
            "UPDATE payments SET status = 'failed', failure_reason = 'timed_out', updated_at = ? "
            "WHERE order_id = ? AND status = 'processing' AND created_at < ?",
            (now, body.order_id, now - STALE_SECONDS),
        )

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

    synced = update_order_status(body.order_id, "paid" if result["ok"] else "failed")

    with get_db() as db:
        db.execute("UPDATE payments SET order_synced = ? WHERE id = ?", (int(synced), payment_id))
        row = db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()

    out = row_to_payment(row)
    if not synced:
        out["warning"] = "Payment recorded but the order status update is pending"
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