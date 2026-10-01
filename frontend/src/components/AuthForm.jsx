import { useState } from "react";
import { getMe, login, register, setToken } from "../api";

export default function AuthForm({ onAuth, notice }) {
  const [mode, setMode] = useState("login");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const isRegister = mode === "register";

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      if (isRegister) await register({ username, email, password });
      const { access_token } = await login({ username, password });
      setToken(access_token);
      onAuth(await getMe());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  function switchMode() {
    setMode(isRegister ? "login" : "register");
    setError("");
  }

  return (
    <div className="center-screen">
      <div className="card auth-card">
        <h1>{isRegister ? "Create account" : "Log in"}</h1>

        {notice && <div className="alert alert-warn">{notice}</div>}
        {error && <div className="alert alert-error">{error}</div>}

        <form onSubmit={handleSubmit} className="stack">
          <label>
            Username
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              minLength={3}
              maxLength={50}
              required
              autoFocus
            />
          </label>

          {isRegister && (
            <label>
              Email
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </label>
          )}

          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              minLength={8}
              maxLength={64}
              required
            />
          </label>

          <button className="btn btn-primary" disabled={loading}>
            {loading ? "Please wait..." : isRegister ? "Register" : "Log in"}
          </button>
        </form>

        <p className="muted center">
          {isRegister ? "Already have an account?" : "No account yet?"}{" "}
          <button type="button" className="link" onClick={switchMode}>
            {isRegister ? "Log in" : "Register"}
          </button>
        </p>
      </div>
    </div>
  );
}