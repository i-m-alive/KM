import { useEffect, useState } from "react";
import { apiDelete, apiGet, apiPost } from "../api/client";
import { useAuth } from "../auth/AuthContext";

const CATEGORIES = ["domain", "use_case", "technology", "geography", "engagement_type"];

export default function TagVocabularyPage() {
  const { user } = useAuth();
  const [vocab, setVocab] = useState([]);
  const [error, setError] = useState(null);
  const [category, setCategory] = useState(CATEGORIES[0]);
  const [value, setValue] = useState("");

  const canManage = user?.role === "km_governance" || user?.role === "admin";
  const canPropose = canManage || user?.role === "practice_lead";

  function load() {
    apiGet("/tags/vocabulary").then(setVocab).catch((e) => setError(e.message));
  }
  useEffect(load, []);

  async function addTerm(e) {
    e.preventDefault();
    setError(null);
    try {
      await apiPost("/tags/vocabulary", { category, value });
      setValue("");
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  async function approve(id) {
    await apiPost(`/tags/vocabulary/${id}/approve`, {});
    load();
  }
  async function remove(id) {
    await apiDelete(`/tags/vocabulary/${id}`);
    load();
  }

  const byCategory = CATEGORIES.map((c) => ({ category: c, terms: vocab.filter((v) => v.category === c) }));
  const pending = vocab.filter((v) => v.status === "pending_approval");

  return (
    <div>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Tag vocabulary</h1>
          <p className="page-head__sub">
            The controlled vocabulary the Tagging agent classifies against — governed here, grown by proposal.
          </p>
        </div>
      </div>
      {error && <p className="error-text">{error}</p>}

      {canPropose && (
        <div className="card">
          <h3 className="card__title">{canManage ? "Add a term" : "Propose a term"}</h3>
          <form onSubmit={addTerm} style={{ flexDirection: "row", alignItems: "flex-end", gap: "0.75rem", flexWrap: "wrap" }}>
            <label>
              Category
              <select value={category} onChange={(e) => setCategory(e.target.value)}>
                {CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </label>
            <label style={{ flex: 1, minWidth: "180px" }}>
              Value
              <input value={value} onChange={(e) => setValue(e.target.value)} placeholder="e.g. logistics" required />
            </label>
            <button type="submit">{canManage ? "Add (approved)" : "Propose"}</button>
          </form>
        </div>
      )}

      {canManage && pending.length > 0 && (
        <div className="card section" style={{ borderColor: "#f3e0a8", background: "#fffdf5" }}>
          <h3 className="card__title">
            Pending proposals <span className="status-pill status-pill--awaiting_review">{pending.length} waiting</span>
          </h3>
          <p className="card__sub">Terms proposed by agents or practice leads — approve to make them taggable.</p>
          <ul className="flag-list" style={{ margin: 0 }}>
            {pending.map((t) => (
              <li key={t.id} className="flag-list__item flag-list__item--warning" style={{ alignItems: "center" }}>
                <span style={{ flex: 1 }}>
                  <strong>{t.category.replace(/_/g, " ")}</strong>: {t.value}
                </span>
                <button className="btn--sm" onClick={() => approve(t.id)}>
                  Approve
                </button>
                <button className="btn--ghost btn--sm" onClick={() => remove(t.id)}>
                  Reject
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="agent-grid section">
        {byCategory.map(({ category: c, terms }) => (
          <div key={c} className="agent-card">
            <h2 style={{ fontSize: "0.95rem", margin: "0 0 0.6rem", textTransform: "capitalize" }}>
              {c.replace(/_/g, " ")} <span className="agent-card__meta">({terms.length})</span>
            </h2>
            <div className="pillrow">
              {terms.map((t) => (
                <span
                  key={t.id}
                  className={`confidence-badge ${t.status === "approved" ? "confidence-badge--high" : "confidence-badge--medium"}`}
                >
                  {t.value}
                  {canManage && (
                    <button
                      onClick={() => remove(t.id)}
                      title="Remove term"
                      style={{
                        marginLeft: "0.3rem",
                        padding: 0,
                        width: "16px",
                        height: "16px",
                        fontSize: "0.7rem",
                        background: "transparent",
                        color: "inherit",
                        boxShadow: "none",
                        border: "none",
                      }}
                    >
                      ×
                    </button>
                  )}
                </span>
              ))}
              {terms.length === 0 && <span className="agent-card__meta">none yet</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
