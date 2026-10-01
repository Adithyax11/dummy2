import { useCallback, useEffect, useState } from "react";
import { cancelOrder, listOrders, payOrder } from "../api";

const FILTERS = ["all", "pending", "paid", "failed", "cancelled"];

const money = (n) => `$${Number(n).toFixed(2)}`;
const when = (ts) => new Date(ts * 1000).toLocaleString();

export default function OrderList({ refreshKey }) {
  const [orders, setOrders] = useState([]);
  const [filter, setFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState(null);
  const [payingId, setPayingId] = useState(null);
  const [card, setCard] = useState("");
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(async () => {
    setError("");
    try {
      setOrders(await listOrders(filter));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    setLoading(true);
    load();
  }, [load, refreshKey]);

  async function handlePay(e, order) {
    e.preventDefault();
    setMessage(null);
    setBusyId(order.id);
    try {
      const p = await payOrder(order.id, card);
      let text =
        p.status === "succeeded"
          ? `Order #${order.id} paid.`
          : `Payment for order #${order.id} failed: ${p.failure_reason}`;
      if (p.warning) text += ` (${p.warning})`;
      setMessage({ type: p.status === "succeeded" ? "ok" : "error", text });
      setPayingId(null);
      setCard("");
      await load();
    } catch (err) {
      setMessage({ type: "error", text: err.message });
    } finally {
      setBusyId(null);
    }
  }

  async function handleCancel(order) {
    if (!window.confirm(`Cancel order #${order.id}?`)) return;
    setMessage(null);
    setBusyId(order.id);
    try {
      await cancelOrder(order.id);
      setMessage({ type: "ok", text: `Order #${order.id} cancelled.` });
      await load();
    } catch (err) {
      setMessage({ type: "error", text: err.message });
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="card">
      <div className="list-header">
        <h2>Your orders</h2>
        <div className="filters">
          {FILTERS.map((f) => (
            <button
              key={f}
              className={`chip ${filter === f ? "chip-active" : ""}`}
              onClick={() => setFilter(f)}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {message && (
        <div className={`alert ${message.type === "ok" ? "alert-ok" : "alert-error"}`}>
          {message.text}
        </div>
      )}
      {error && <div className="alert alert-error">{error}</div>}

      {loading ? (
        <p className="muted">Loading...</p>
      ) : orders.length === 0 ? (
        <p className="muted">No orders yet.</p>
      ) : (
        <ul className="orders">
          {orders.map((o) => {
            const actionable = o.status === "pending" || o.status === "failed";
            const busy = busyId === o.id;
            return (
              <li key={o.id} className="order">
                <div className="order-main">
                  <div>
                    <strong>
                      #{o.id} · {o.item_name}
                    </strong>
                    <div className="muted small">
                      {o.quantity} × {money(o.unit_price)} · {when(o.created_at)}
                    </div>
                  </div>
                  <div className="order-right">
                    <strong>{money(o.total)}</strong>
                    <span className={`badge badge-${o.status}`}>{o.status}</span>
                  </div>
                </div>

                {actionable && (
                  <div className="order-actions">
                    <button
                      className="btn btn-primary btn-sm"
                      disabled={busy}
                      onClick={() => {
                        setPayingId(payingId === o.id ? null : o.id);
                        setCard("");
                      }}
                    >
                      {o.status === "failed" ? "Retry payment" : "Pay"}
                    </button>
                    <button
                      className="btn btn-ghost btn-sm"
                      disabled={busy}
                      onClick={() => handleCancel(o)}
                    >
                      Cancel
                    </button>
                  </div>
                )}

                {payingId === o.id && (
                  <form className="pay-form" onSubmit={(e) => handlePay(e, o)}>
                    <input
                      value={card}
                      onChange={(e) => setCard(e.target.value)}
                      placeholder="Card number (test: 4242 4242 4242 4242)"
                      inputMode="numeric"
                      maxLength={23}
                      required
                      autoFocus
                    />
                    <button className="btn btn-primary btn-sm" disabled={busy}>
                      {busy ? "Processing..." : `Pay ${money(o.total)}`}
                    </button>
                  </form>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}