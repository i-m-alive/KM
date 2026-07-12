import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet } from "../api/client";

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

  if (loading) return <p>Loading review queue...</p>;
  if (error) return <p className="error-text">{error}</p>;

  return (
    <div>
      <h1>Review queue</h1>
      {items.length === 0 && <p>Nothing awaiting review.</p>}
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
              <td>{it.agent_id}</td>
              <td>{it.summary}</td>
              <td className="agent-card__meta">{it.created_by_email}</td>
              <td>{new Date(it.created_at).toLocaleString()}</td>
              <td>
                <Link to={`/review/${it.run_id}`}>Review</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
