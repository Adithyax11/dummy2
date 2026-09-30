import os
import sqlite3
import time
from contextlib import contextmanager

import bcrypt
import jwt
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DB_PATH", "/data/users.db")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALGO = "HS256"
JWT_EXPIRE_SECONDS = int(os.getenv("JWT_EXPIRE_SECONDS", "3600"))

app = FastAPI(title="User Service")


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
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )


init_db()


# ---------- schemas ----------
class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=64)


class LoginRequest(BaseModel):
    username: str
    password: str


# ---------- helpers ----------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_token(user_id: int, username: str) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": now,
        "exp": now + JWT_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------- routes ----------
@app.get("/health")
def health():
    return {"service": "user-service", "status": "ok"}


@app.post("/register", status_code=201)
def register(body: RegisterRequest):
    if len(body.password.encode()) > 72:
        raise HTTPException(status_code=422, detail="Password too long")

    with get_db() as db:
        exists = db.execute(
            "SELECT 1 FROM users WHERE username = ? OR email = ?",
            (body.username, body.email.lower()),
        ).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="Username or email already taken")

        cur = db.execute(
            "INSERT INTO users (username, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (body.username, body.email.lower(), hash_password(body.password), int(time.time())),
        )
        user_id = cur.lastrowid

    return {"id": user_id, "username": body.username, "email": body.email.lower()}


@app.post("/login")
def login(body: LoginRequest):
    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE username = ?", (body.username,)
        ).fetchone()

    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    return {
        "access_token": create_token(user["id"], user["username"]),
        "token_type": "bearer",
        "expires_in": JWT_EXPIRE_SECONDS,
    }


@app.get("/me")
def me(authorization: str = Header(default="")):
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    claims = decode_token(token)

    with get_db() as db:
        user = db.execute(
            "SELECT id, username, email, created_at FROM users WHERE id = ?",
            (int(claims["sub"]),),
        ).fetchone()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return dict(user)