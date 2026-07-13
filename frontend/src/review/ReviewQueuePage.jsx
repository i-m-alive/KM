import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet } from "../api/client";
import { CheckShieldIcon } from "../components/Icons";

export default function ReviewQueuePage() {
  const [items, setItems] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiGet("/review/queue")
      .then(setItems)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading)
    return (
      <div className="loading-state">
        <span className="spinner" /> Loading review queue…
      </div>
    );
  if (error) return <p className="error-text">{error}</p>;

  return (
    <div>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Review queue</h1>
          <p className="page-head__sub">
            Agent proposals paused for human sign-off — nothing is applied to a document without approval here.
          </p>
        </div>
      </div>

      {items.length === 0 ? (
        <div className="empty-state">
          <CheckShieldIcon />
          <p>Nothing awaiting review — all caught up.</p>
        </div>
      ) : (
        <div className="table-scroll">
          <table className="run-table">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Summary</th>
                <th>Submitted by</th>
                <th>When</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr key={it.run_id}>
                  <td style={{ fontWeight: 550, textTransform: "capitalize" }}>{it.agent_id}</td>
                  <td>{it.summary}</td>
                  <td className="agent-card__meta">{it.created_by_email}</td>
                  <td className="agent-card__meta">{new Date(it.created_at).toLocaleString()}</td>
                  <td style={{ textAlign: "right" }}>
                    <Link to={`/review/${it.run_id}`}>
                      <button className="btn--sm">Review</button>
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
