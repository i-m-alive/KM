import { useEffect, useState } from "react";
import { apiGet, apiPatch, apiPost, apiDelete } from "../api/client";
import { useAuth } from "../auth/AuthContext";

const ROLES = ["admin", "km_governance", "km_reviewer", "practice_lead", "delivery", "read_only"];

function UsersSection() {
  const [users, setUsers] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  function load() {
    setLoading(true);
    apiGet("/admin/users")
      .then(setUsers)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  async function handleRoleChange(userId, role) {
    setError(null);
    try {
      await apiPatch(`/admin/users/${userId}/role`, { role });
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  if (loading) return <p>Loading users...</p>;

  return (
    <div>
      <h2>Users</h2>
      {error && <p className="error-text">{error}</p>}
      <table className="run-table">
        <thead>
          <tr>
            <th>Email</th>
            <th>Role</th>
            <th>Joined</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>{u.email}</td>
              <td>
                <select value={u.role} onChange={(e) => handleRoleChange(u.id, e.target.value)}>
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
              </td>
              <td>{new Date(u.created_at).toLocaleDateString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AccountsSection() {
  const [accounts, setAccounts] = useState([]);
  const [users, setUsers] = useState([]);
  const [newAccountName, setNewAccountName] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  function load() {
    setLoading(true);
    Promise.all([apiGet("/governance/accounts"), apiGet("/governance/practice-leads")])
      .then(([accountsData, usersData]) => {
        setAccounts(accountsData);
        setUsers(usersData);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  async function handleCreateAccount(e) {
    e.preventDefault();
    setError(null);
    try {
      await apiPost("/governance/accounts", { name: newAccountName });
      setNewAccountName("");
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleAssign(accountId, userId) {
    if (!userId) return;
    setError(null);
    try {
      await apiPost(`/governance/accounts/${accountId}/owners`, { user_id: userId });
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleRemove(accountId, userId) {
    setError(null);
    try {
      await apiDelete(`/governance/accounts/${accountId}/owners/${userId}`);
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  if (loading) return <p>Loading accounts...</p>;

  return (
    <div>
      <h2>Client accounts &amp; ownership</h2>
      <p className="agent-card__meta">
        Placeholder registry - Sanitization will later link real masked documents to these accounts.
        Ownership here only restricts practice-lead client-name lookups once that endpoint exists.
      </p>
      {error && <p className="error-text">{error}</p>}

      <form onSubmit={handleCreateAccount} style={{ marginBottom: "1.5rem" }}>
        <label>
          New account name
          <input value={newAccountName} onChange={(e) => setNewAccountName(e.target.value)} required />
        </label>
        <button type="submit">Create account</button>
      </form>

      {accounts.map((account) => (
        <div key={account.id} className="agent-card" style={{ marginBottom: "1rem" }}>
          <h3>{account.name}</h3>
          <ul className="flag-list">
            {account.owners.length === 0 && <li className="flag-list__item">No owners assigned</li>}
            {account.owners.map((owner) => (
              <li key={owner.id} className="flag-list__item flag-list__item--info">
                {owner.email}
                <button style={{ marginLeft: "0.75rem" }} onClick={() => handleRemove(account.id, owner.id)}>
                  Remove
                </button>
              </li>
            ))}
          </ul>
          <select defaultValue="" onChange={(e) => handleAssign(account.id, e.target.value)}>
            <option value="" disabled>
              Assign a practice-lead...
            </option>
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.email}
              </option>
            ))}
          </select>
        </div>
      ))}
    </div>
  );
}

function MaskingDictionarySection() {
  const [entities, setEntities] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  function load() {
    setLoading(true);
    apiGet("/governance/masking-dictionary")
      .then(setEntities)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  async function handleSkip(entityId) {
    setError(null);
    try {
      await apiPost(`/governance/masking-dictionary/${entityId}/skip`, {});
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleUnskip(entityId) {
    setError(null);
    try {
      await apiPost(`/governance/masking-dictionary/${entityId}/unskip`, {});
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  if (loading) return <p>Loading masking dictionary...</p>;

  return (
    <div>
      <h2>Masking dictionary</h2>
      <p className="agent-card__meta">
        The global, cross-document mask token registry. "Skip" permanently stops a term from ever being
        proposed as client-identifying again (in text, OCR, or vision judgment) - use it for recurring
        false positives (common words, industry acronyms) rather than re-excluding the same term every run.
      </p>
      {error && <p className="error-text">{error}</p>}
      <table className="run-table">
        <thead>
          <tr>
            <th>Mask token</th>
            <th>Surface(s)</th>
            <th>Type</th>
            <th>Status</th>
            <th>Client account</th>
            <th>Created</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {entities.length === 0 && (
            <tr>
              <td colSpan={7}>No masking entities yet.</td>
            </tr>
          )}
          {entities.map((e) => (
            <tr key={e.id}>
              <td className="agent-card__meta">{e.mask_token}</td>
              <td>{e.aliases.join(", ")}</td>
              <td className="agent-card__meta">{e.entity_type}</td>
              <td>
                <span className={`status-pill status-pill--${e.status === "approved" ? "completed" : e.status === "skipped" ? "failed" : "awaiting_review"}`}>
                  {e.status}
                </span>
              </td>
              <td className="agent-card__meta">{e.client_account_name || "—"}</td>
              <td className="agent-card__meta">{new Date(e.created_at).toLocaleDateString()}</td>
              <td>
                {e.status === "skipped" ? (
                  <button type="button" onClick={() => handleUnskip(e.id)}>
                    Un-skip
                  </button>
                ) : (
                  <button type="button" onClick={() => handleSkip(e.id)}>
                    Skip
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AdminPage() {
  const { user } = useAuth();
  const canManageUsers = user?.role === "admin";
  const canManageAccounts = user?.role === "admin" || user?.role === "km_governance";
  const canViewMaskingDictionary = user?.role === "admin" || user?.role === "km_governance";

  if (!canManageUsers && !canManageAccounts && !canViewMaskingDictionary) {
    return <p>You don't have access to any admin features.</p>;
  }

  return (
    <div>
      <h1>Admin</h1>
      {canManageUsers && <UsersSection />}
      {canManageAccounts && <AccountsSection />}
      {canViewMaskingDictionary && <MaskingDictionarySection />}
    </div>
  );
}
