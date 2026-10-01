import { useCallback, useEffect, useState } from "react";
import Navbar from "./components/Navbar";
import AuthForm from "./components/AuthForm";
import OrderForm from "./components/OrderForm";
import OrderList from "./components/OrderList";
import { clearToken, getMe, getToken, setUnauthorizedHandler } from "./api";

export default function App() {
  const [user, setUser] = useState(null);
  const [checking, setChecking] = useState(!!getToken());
  const [refreshKey, setRefreshKey] = useState(0);
  const [notice, setNotice] = useState("");

  const logout = useCallback((message = "") => {
    clearToken();
    setUser(null);
    setNotice(message);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => logout("Your session expired. Please log in again."));
    if (!getToken()) return;
    getMe()
      .then(setUser)
      .catch(() => {})
      .finally(() => setChecking(false));
  }, [logout]);

  if (checking) {
    return <div className="center-screen">Loading...</div>;
  }

  if (!user) {
    return (
      <AuthForm
        notice={notice}
        onAuth={(u) => {
          setNotice("");
          setUser(u);
        }}
      />
    );
  }

  return (
    <>
      <Navbar user={user} onLogout={() => logout()} />
      <main className="container">
        <OrderForm onCreated={() => setRefreshKey((k) => k + 1)} />
        <OrderList refreshKey={refreshKey} />
      </main>
    </>
  );
}