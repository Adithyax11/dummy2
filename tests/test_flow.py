import os
import uuid

import httpx
import pytest

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
PASSWORD = "password123"
GOOD_CARD = "4242424242424242"
DECLINED_CARD = "4000000000000002"
FLAKY_CARD = "4000000000000259"  # fails twice, succeeds on the 3rd attempt


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        yield c


def make_user(client):
    name = "t" + uuid.uuid4().hex[:10]
    r = client.post(
        "/users/register",
        json={"username": name, "email": f"{name}@example.com", "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    r = client.post("/users/login", json={"username": name, "password": PASSWORD})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = client.get("/users/me", headers=headers)
    assert me.status_code == 200, me.text
    return {"name": name, "headers": headers, "id": me.json()["id"]}


@pytest.fixture(scope="session")
def alice(client):
    return make_user(client)


@pytest.fixture(scope="session")
def bob(client):
    return make_user(client)


def new_order(client, user, item="Keyboard", qty=2, price=49.99):
    r = client.post(
        "/orders",
        headers=user["headers"],
        json={"item_name": item, "quantity": qty, "unit_price": price},
    )
    assert r.status_code == 201, r.text
    return r.json()


def pay(client, user, order_id, card, **extra):
    return client.post(
        "/payments/pay",
        headers=user["headers"],
        json={"order_id": order_id, "card_number": card, **extra},
    )


# ---------- platform ----------
def test_all_services_healthy(client):
    r = client.get("/health/all")
    assert r.status_code == 200
    assert r.json()["overall"] == "ok"


def test_responses_carry_a_request_id(client):
    r = client.get("/health")
    assert r.headers.get("x-request-id")


# ---------- auth ----------
def test_duplicate_registration_rejected(client, alice):
    r = client.post(
        "/users/register",
        json={"username": alice["name"], "email": "other@example.com", "password": PASSWORD},
    )
    assert r.status_code == 409


def test_wrong_password_rejected(client, alice):
    r = client.post("/users/login", json={"username": alice["name"], "password": "wrong-password"})
    assert r.status_code == 401


def test_protected_routes_need_a_valid_token(client):
    assert client.get("/orders").status_code == 401
    bad = {"Authorization": "Bearer not.a.token"}
    assert client.get("/orders", headers=bad).status_code == 401


# ---------- orders ----------
def test_create_and_list_order(client, alice):
    order = new_order(client, alice)
    assert order["status"] == "pending"
    assert order["total"] == 99.98

    r = client.get("/orders", headers=alice["headers"])
    assert r.status_code == 200
    assert order["id"] in [o["id"] for o in r.json()]


def test_order_validation(client, alice):
    bad_qty = client.post(
        "/orders", headers=alice["headers"],
        json={"item_name": "X", "quantity": 0, "unit_price": 5},
    )
    bad_price = client.post(
        "/orders", headers=alice["headers"],
        json={"item_name": "X", "quantity": 1, "unit_price": -1},
    )
    assert bad_qty.status_code == 422
    assert bad_price.status_code == 422


def test_users_cannot_touch_each_others_orders(client, alice, bob):
    order = new_order(client, alice)
    oid = order["id"]

    assert client.get(f"/orders/{oid}", headers=bob["headers"]).status_code == 404
    assert client.post(f"/orders/{oid}/cancel", headers=bob["headers"]).status_code == 404
    assert pay(client, bob, oid, GOOD_CARD).status_code == 404
    assert oid not in [o["id"] for o in client.get("/orders", headers=bob["headers"]).json()]


def test_spoofed_identity_headers_are_ignored(client, alice, bob):
    order = new_order(client, alice)
    headers = {**bob["headers"], "X-User-Id": str(alice["id"])}
    r = client.get(f"/orders/{order['id']}", headers=headers)
    assert r.status_code == 404


def test_internal_routes_are_blocked_at_the_gateway(client, alice):
    order = new_order(client, alice)
    r = client.patch(
        f"/orders/internal/{order['id']}/status",
        headers=alice["headers"],
        json={"status": "paid"},
    )
    assert r.status_code == 404
    assert client.get(f"/orders/{order['id']}", headers=alice["headers"]).json()["status"] == "pending"


def test_cancel_pending_order_once(client, alice):
    order = new_order(client, alice)
    r = client.post(f"/orders/{order['id']}/cancel", headers=alice["headers"])
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert client.post(f"/orders/{order['id']}/cancel", headers=alice["headers"]).status_code == 409


# ---------- payments ----------
def test_declined_then_successful_payment(client, alice):
    order = new_order(client, alice)
    oid = order["id"]

    r = pay(client, alice, oid, DECLINED_CARD)
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert r.json()["failure_reason"] == "card_declined"
    assert r.json()["attempts"] == 1
    assert client.get(f"/orders/{oid}", headers=alice["headers"]).json()["status"] == "failed"

    r = pay(client, alice, oid, GOOD_CARD)
    assert r.status_code == 200
    assert r.json()["status"] == "succeeded"
    assert r.json()["order_synced"] is True
    assert client.get(f"/orders/{oid}", headers=alice["headers"]).json()["status"] == "paid"

    history = client.get(f"/payments/order/{oid}", headers=alice["headers"]).json()
    assert [p["status"] for p in history] == ["succeeded", "failed"]


def test_paid_order_cannot_be_paid_or_cancelled(client, alice):
    order = new_order(client, alice)
    assert pay(client, alice, order["id"], GOOD_CARD).json()["status"] == "succeeded"

    assert pay(client, alice, order["id"], GOOD_CARD).status_code == 409
    assert client.post(f"/orders/{order['id']}/cancel", headers=alice["headers"]).status_code == 409


def test_flaky_provider_is_retried(client, alice):
    order = new_order(client, alice)
    r = pay(client, alice, order["id"], FLAKY_CARD)
    assert r.status_code == 200
    assert r.json()["status"] == "succeeded"
    assert r.json()["attempts"] == 3


def test_amount_comes_from_the_order_not_the_client(client, alice):
    order = new_order(client, alice, qty=2, price=49.99)
    r = pay(client, alice, order["id"], GOOD_CARD, amount=0.01)
    assert r.status_code == 200
    assert r.json()["amount"] == order["total"]


def test_full_card_number_is_never_returned(client, alice):
    order = new_order(client, alice)
    r = pay(client, alice, order["id"], GOOD_CARD)
    assert r.json()["card_last4"] == "4242"
    assert GOOD_CARD not in r.text


def test_invalid_card_number_rejected(client, alice):
    order = new_order(client, alice)
    assert pay(client, alice, order["id"], "1234").status_code == 422