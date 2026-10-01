const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";
const TOKEN_KEY = "token";

export const getToken = () => localStorage.getItem(TOKEN_KEY);
export const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

let onUnauthorized = () => {};
export const setUnauthorizedHandler = (fn) => {
  onUnauthorized = fn;
};

function errorMessage(data, fallback) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ");
  }
  return fallback;
}

async function request(path, { method = "GET", body, auth = true } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth && getToken()) headers.Authorization = `Bearer ${getToken()}`;

  let res;
  try {
    res = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error("Cannot reach the server. Is the gateway running?");
  }

  let data = null;
  try {
    data = await res.json();
  } catch {
    /* empty or non-JSON body */
  }

  if (!res.ok) {
    if (res.status === 401 && auth) onUnauthorized();
    throw new Error(errorMessage(data, `Request failed (${res.status})`));
  }
  return data;
}

// ---- users ----
export const register = (payload) =>
  request("/users/register", { method: "POST", body: payload, auth: false });

export const login = (payload) =>
  request("/users/login", { method: "POST", body: payload, auth: false });

export const getMe = () => request("/users/me");

// ---- orders ----
export const listOrders = (status) =>
  request(status && status !== "all" ? `/orders?status=${status}` : "/orders");

export const createOrder = (payload) =>
  request("/orders", { method: "POST", body: payload });

export const cancelOrder = (id) =>
  request(`/orders/${id}/cancel`, { method: "POST" });

// ---- payments ----
export const payOrder = (orderId, cardNumber) =>
  request("/payments/pay", {
    method: "POST",
    body: { order_id: orderId, card_number: cardNumber },
  });