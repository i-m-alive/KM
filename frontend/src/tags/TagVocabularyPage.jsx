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
      <h1>Tag vocabulary</h1>
      {error && <p className="error-text">{error}</p>}

      {canPropose && (
        <form onSubmit={addTerm} style={{ flexDirection: "row", alignItems: "flex-end", gap: "0.75rem" }}>
          <label>
            Category
            <select value={category} onChange={(e) => setCategory(e.target.value)}>
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <label style={{ flex: 1 }}>
            Value
            <input value={value} onChange={(e) => setValue(e.target.value)} placeholder="e.g. logistics" required />
          </label>
          <button type="submit">{canManage ? "Add (approved)" : "Propose"}</button>
        </form>
      )}

      {canManage && pending.length > 0 && (
        <div style={{ marginTop: "1.5rem" }}>
          <h3>Pending proposals</h3>
          <ul className="flag-list">
            {pending.map((t) => (
              <li key={t.id} className="flag-list__item flag-list__item--warning">
                {t.category}: {t.value}
                <button style={{ marginLeft: "0.75rem" }} onClick={() => approve(t.id)}>
                  Approve
                </button>
                <button style={{ marginLeft: "0.4rem" }} onClick={() => remove(t.id)}>
                  Reject
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="agent-grid" style={{ marginTop: "1.5rem" }}>
        {byCategory.map(({ category: c, terms }) => (
          <div key={c} className="agent-card">
            <h2 style={{ fontSize: "1rem" }}>{c}</h2>
            <div className="pillrow" style={{ display: "flex", flexWrap: "wrap", gap: "0.35rem" }}>
              {terms.map((t) => (
                <span
                  key={t.id}
                  className={`confidence-badge ${t.status === "approved" ? "confidence-badge--high" : "confidence-badge--medium"}`}
                >
                  {t.value}
                  {canManage && (
                    <button style={{ marginLeft: "0.35rem", padding: "0 0.3rem" }} onClick={() => remove(t.id)}>
                      ×
                    </button>
                  )}
                </span>
              ))}
              {terms.length === 0 && <span className="agent-card__meta">none</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
