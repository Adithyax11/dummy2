export default function Navbar({ user, onLogout }) {
  return (
    <header className="navbar">
      <span className="brand">Orders</span>
      <div className="nav-right">
        <span className="muted">Signed in as {user.username}</span>
        <button className="btn btn-ghost" onClick={onLogout}>
          Log out
        </button>
      </div>
    </header>
  );
}