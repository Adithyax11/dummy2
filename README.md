# dummy2: Microservices Orders App

A small microservices app: React frontend, an API gateway, and user / order / payment services (FastAPI), all run with Docker Compose.

```
Browser (5173) -> Gateway (8000) -+-> user-service     (register, login, JWT)
                                  +-> order-service    (orders)
                                  +-> payment-service  (mock provider, reconciler)
```

- Only the gateway (8000) and frontend (5173) are exposed. The services are reachable only inside the Docker network.
- The gateway verifies the JWT and passes `X-User-Id` to the services. Every query is scoped to that user.
- Payment charges the price stored on the order (never a client-sent amount), then tells the order service to mark it `paid` or `failed` through an internal route that the gateway blocks.
- A background reconciler in the payment service retries order updates that failed after a payment.
- Each service has its own SQLite database in a Docker volume.

## Setup

1. Create `.env` (copy `.env.example`) and set two secrets of at least 32 characters:

```bash
   docker run --rm python:3.12-slim python -c "import secrets; print(secrets.token_hex(32))"
```

   Use one for `JWT_SECRET` and a different one for `INTERNAL_API_KEY`.

2. Start everything:

```bash
   docker compose up --build -d
```

3. Open http://localhost:5173 (use `localhost`, not `127.0.0.1`, because of CORS).

## Useful commands

```bash
docker compose logs -f gateway payment-service   # follow logs
docker compose ps                                # container status
docker compose down                              # stop (keeps data)
docker compose down -v                           # stop and DELETE all data
```

## API (all through the gateway on :8000)

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | /users/register | no | Create an account |
| POST | /users/login | no | Get a JWT |
| GET | /users/me | yes | Current user |
| POST | /orders | yes | Create an order |
| GET | /orders?status= | yes | List your orders |
| GET | /orders/{id} | yes | One order |
| POST | /orders/{id}/cancel | yes | Cancel a pending or failed order |
| POST | /payments/pay | yes | Pay an order: `{order_id, card_number}` |
| GET | /payments | yes | Your payments |
| GET | /payments/order/{id} | yes | Payments for one order |
| GET | /health/all | no | Health of all services |

## Test cards (mock provider)

| Card | Result |
|---|---|
| 4242 4242 4242 4242 | Succeeds |
| 4000 0000 0000 0002 | Declined |
| 4000 0000 0000 9995 | Insufficient funds |
| 4000 0000 0000 0259 | Fails twice, succeeds on retry 3 |
| 4000 0000 0000 0119 | Always fails (3 attempts) |

## Automated tests

Run against the running stack:

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r tests/requirements.txt
pytest -v tests
```

## Logging

Every request gets an `X-Request-ID` at the gateway, which is passed to the other services and shows up in every log line, so one request can be followed across services:

```bash
docker compose logs gateway order-service payment-service | findstr a1b2c3d4e5f6
```

Set `LOG_LEVEL` in `.env` (default `INFO`).

## Reconciliation

If a payment goes through but the order service can't be updated, the payment is stored with `order_synced: false`. Every `RECONCILE_INTERVAL` seconds (default 30) the payment service retries. If the order can no longer be marked paid (for example it was cancelled during payment), the payment gets `needs_review: true` and an ERROR log line, so it can be refunded manually.

## Project layout

```
dummy2/
├── docker-compose.yml
├── .env / .env.example
├── frontend/          Vite + React
├── gateway/           routing, JWT check, CORS, request IDs
├── user-service/      register / login / JWT
├── order-service/     orders + internal status route
├── payment-service/   payments, mock provider, reconciler
└── tests/             end-to-end tests
```