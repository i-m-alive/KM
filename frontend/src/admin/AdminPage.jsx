import { useEffect, useState } from "react";
import { apiBlobUrl, apiGet, apiPatch, apiPost, apiDelete } from "../api/client";
import { useAuth } from "../auth/AuthContext";

// Thumbnails need an Authorization header a plain <img src> can't attach -
// same reason ComparePage.jsx fetches its previews via apiBlobUrl instead of
// pointing an <img>/<iframe> straight at the API path.
function LogoThumbnail({ logoId }) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    let cancelled = false;
    let objectUrl = null;
    apiBlobUrl(`/governance/logo-references/${logoId}/thumbnail`).then((u) => {
      if (cancelled) return;
      objectUrl = u;
      setUrl(u);
    }).catch(() => {});
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [logoId]);
  if (!url) return null;
  return (
    <img
      src={url}
      alt="matched logo"
      title="Image matched to this entity via perceptual-hash logo matching"
      style={{ width: 40, height: 40, objectFit: "contain", border: "1px solid var(--ink-200)", borderRadius: 4, background: "#fff" }}
    />
  );
}

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

  if (loading)
    return (
      <div className="loading-state">
        <span className="spinner" /> Loading users…
      </div>
    );

  return (
    <div className="card section">
      <h3 className="card__title">Users</h3>
      <p className="card__sub">Assign roles — role determines which capabilities each account has.</p>
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

  if (loading)
    return (
      <div className="loading-state">
        <span className="spinner" /> Loading accounts…
      </div>
    );

  return (
    <div className="card section">
      <h3 className="card__title">Client accounts &amp; ownership</h3>
      <p className="card__sub">
        Placeholder registry - Sanitization will later link real masked documents to these accounts. Ownership here only
        restricts practice-lead client-name lookups once that endpoint exists.
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

  if (loading)
    return (
      <div className="loading-state">
        <span className="spinner" /> Loading masking dictionary…
      </div>
    );

  return (
    <div className="card section">
      <h3 className="card__title">Masking dictionary</h3>
      <p className="card__sub">
        The global, cross-document mask token registry. "Skip" permanently stops a term from ever being proposed as
        client-identifying again (in text, OCR, or vision judgment) - use it for recurring false positives (common
        words, industry acronyms) rather than re-excluding the same term every run.
      </p>
      {error && <p className="error-text">{error}</p>}
      <table className="run-table">
        <thead>
          <tr>
            <th>Mask token</th>
            <th>Surface(s)</th>
            <th>Logos</th>
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
              <td colSpan={8}>No masking entities yet.</td>
            </tr>
          )}
          {entities.map((e) => (
            <tr key={e.id}>
              <td className="agent-card__meta">{e.mask_token}</td>
              <td>{e.aliases.join(", ")}</td>
              <td>
                {e.logos.length === 0 ? (
                  <span className="agent-card__meta">—</span>
                ) : (
                  <div style={{ display: "flex", gap: "0.3rem", flexWrap: "wrap" }}>
                    {e.logos.map((logo) =>
                      logo.thumbnail_available ? (
                        <LogoThumbnail key={logo.id} logoId={logo.id} />
                      ) : (
                        <span
                          key={logo.id}
                          className="agent-card__meta"
                          title="Matched via perceptual hash, but no preview image was stored for this one"
                          style={{ width: 40, height: 40, display: "flex", alignItems: "center", justifyContent: "center", border: "1px dashed var(--ink-200)", borderRadius: 4, fontSize: "0.7rem" }}
                        >
                          n/a
                        </span>
                      )
                    )}
                  </div>
                )}
              </td>
              <td className="agent-card__meta">{e.entity_type}</td>
              <td>
                <span className={`status-pill status-pill--${e.status === "approved" ? "completed" : e.status === "skipped" ? "failed" : "awaiting_review"}`}>
                  {e.status}
                </span>
                {e.stale && (
                  <span
                    className="status-pill status-pill--failed"
                    style={{ marginLeft: "0.4rem" }}
                    title="Pending approval for over two weeks - it will keep re-surfacing in every run's proposal until someone approves or skips it."
                  >
                    needs decision
                  </span>
                )}
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

function ReviewDeltasSection() {
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiGet("/governance/review-deltas")
      .then(setSummary)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading)
    return (
      <div className="loading-state">
        <span className="spinner" /> Loading review deltas…
      </div>
    );

  const rows = summary?.by_entity_type || [];

  return (
    <div className="card section">
      <h3 className="card__title">Review deltas (Task 3 accuracy signal)</h3>
      <p className="card__sub">
        Recall misses = entities the model missed that a reviewer added by hand; precision misses = entities the
        model over-flagged that a reviewer removed. This is the cheapest real accuracy signal the system has —
        {summary ? ` drawn from ${summary.runs_with_data} of ${summary.total_runs_checked} Sanitization run(s)` : ""}{" "}
        with at least one reviewer edit — use it to decide which entity type's confidence gate
        (<code>SANITIZATION_CONFIDENCE_THRESHOLDS</code>) is actually worth moving, instead of guessing.
      </p>
      {error && <p className="error-text">{error}</p>}
      {rows.length === 0 ? (
        <p className="agent-card__meta">No reviewer edits recorded yet — every run so far was approved as proposed.</p>
      ) : (
        <table className="run-table">
          <thead>
            <tr>
              <th>Entity type</th>
              <th>Recall misses (model missed)</th>
              <th>Precision misses (model over-flagged)</th>
              <th>Suggests</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.entity_type}>
                <td className="agent-card__meta">{r.entity_type}</td>
                <td>{r.recall_misses}</td>
                <td>{r.precision_misses}</td>
                <td className="agent-card__meta">
                  {r.recall_misses > r.precision_misses * 2
                    ? "consider a lower confidence gate — the model is missing more than it over-flags"
                    : r.precision_misses > r.recall_misses * 2
                      ? "consider a higher confidence gate — the model over-flags more than it misses"
                      : "roughly balanced so far"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
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
      <div className="page-head">
        <div className="page-head__text">
          <h1>Admin</h1>
          <p className="page-head__sub">Users, client accounts, and the global masking dictionary.</p>
        </div>
      </div>
      {canManageUsers && <UsersSection />}
      {canManageAccounts && <AccountsSection />}
      {canViewMaskingDictionary && <MaskingDictionarySection />}
      {canViewMaskingDictionary && <ReviewDeltasSection />}
    </div>
  );
}
