import os

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALGO = "HS256"
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")]

SERVICES = {
    "user-service": os.getenv("USER_SERVICE_URL", "http://user-service:8000"),
    "order-service": os.getenv("ORDER_SERVICE_URL", "http://order-service:8000"),
    "payment-service": os.getenv("PAYMENT_SERVICE_URL", "http://payment-service:8000"),
}

# URL prefix on the gateway -> downstream service
ROUTES = {
    "users": SERVICES["user-service"],
    "orders": SERVICES["order-service"],
    "payments": SERVICES["payment-service"],
}

# (prefix, path) pairs that don't need a token
PUBLIC = {("users", "register"), ("users", "login")}

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
# never trust these from the client, the gateway sets them itself
STRIP_REQUEST = HOP_BY_HOP | {"host", "content-length", "x-user-id", "x-username", "x-internal-key"}
STRIP_RESPONSE = HOP_BY_HOP | {"content-length", "content-encoding", "content-type"}

app = FastAPI(title="API Gateway")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- auth ----------
def verify_token(request: Request) -> dict:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# ---------- health ----------
@app.get("/health")
def health():
    return {"service": "gateway", "status": "ok"}


@app.get("/health/all")
async def health_all():
    results = {}
    async with httpx.AsyncClient(timeout=3) as client:
        for name, url in SERVICES.items():
            try:
                r = await client.get(f"{url}/health")
                results[name] = r.json()
            except Exception as e:
                results[name] = {"status": "unreachable", "error": str(e)}
    overall = "ok" if all(v.get("status") == "ok" for v in results.values()) else "degraded"
    return {"gateway": "ok", "overall": overall, "services": results}


# ---------- proxy ----------
async def proxy(prefix: str, path: str, request: Request) -> Response:
    base = ROUTES.get(prefix)
    if base is None:
        raise HTTPException(status_code=404, detail="Not found")

    segments = path.split("/") if path else []

    # service-to-service routes are never reachable from outside
    if segments and segments[0] == "internal":
        raise HTTPException(status_code=404, detail="Not found")
    if ".." in segments:
        raise HTTPException(status_code=400, detail="Bad path")

    headers = {k: v for k, v in request.headers.items() if k.lower() not in STRIP_REQUEST}

    if (prefix, path) not in PUBLIC:
        claims = verify_token(request)
        headers["X-User-Id"] = str(claims["sub"])
        headers["X-Username"] = str(claims.get("username", ""))

    url = f"{base}/{path}" if path else base
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.request(
                request.method,
                url,
                params=request.query_params,
                headers=headers,
                content=body,
            )
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail=f"{prefix} service unavailable")

    resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in STRIP_RESPONSE}
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers=resp_headers,
        media_type=r.headers.get("content-type"),
    )


METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


@app.api_route("/{prefix}", methods=METHODS)
async def proxy_root(prefix: str, request: Request):
    return await proxy(prefix, "", request)


@app.api_route("/{prefix}/{path:path}", methods=METHODS)
async def proxy_path(prefix: str, path: str, request: Request):
    return await proxy(prefix, path, request)