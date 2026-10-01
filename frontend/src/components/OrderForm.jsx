import { useState } from "react";
import { createOrder } from "../api";

export default function OrderForm({ onCreated }) {
  const [itemName, setItemName] = useState("");
  const [quantity, setQuantity] = useState(1);
  const [unitPrice, setUnitPrice] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const total = (Number(quantity) || 0) * (Number(unitPrice) || 0);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");

    const qty = Number(quantity);
    const price = Number(unitPrice);
    if (!Number.isInteger(qty) || qty < 1) return setError("Quantity must be a whole number of at least 1");
    if (!(price > 0)) return setError("Price must be greater than 0");

    setLoading(true);
    try {
      await createOrder({ item_name: itemName.trim(), quantity: qty, unit_price: price });
      setItemName("");
      setQuantity(1);
      setUnitPrice("");
      onCreated();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="card">
      <h2>New order</h2>
      {error && <div className="alert alert-error">{error}</div>}

      <form onSubmit={handleSubmit} className="order-form">
        <label className="grow">
          Item
          <input
            value={itemName}
            onChange={(e) => setItemName(e.target.value)}
            maxLength={100}
            required
          />
        </label>

        <label>
          Qty
          <input
            type="number"
            min="1"
            step="1"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            required
          />
        </label>

        <label>
          Unit price ($)
          <input
            type="number"
            min="0.01"
            step="0.01"
            value={unitPrice}
            onChange={(e) => setUnitPrice(e.target.value)}
            required
          />
        </label>

        <div className="total">
          <span className="muted">Total</span>
          <strong>${total.toFixed(2)}</strong>
        </div>

        <button className="btn btn-primary" disabled={loading}>
          {loading ? "Creating..." : "Create order"}
        </button>
      </form>
    </section>
  );
}