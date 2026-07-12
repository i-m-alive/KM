import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet } from "../api/client";

export default function RunHistoryPage() {
  const [runs, setRuns] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiGet("/runs")
      .then(setRuns)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p>Loading run history...</p>;
  if (error) return <p className="error-text">{error}</p>;

  const totalIn = runs.reduce((sum, r) => sum + (r.input_tokens ?? 0), 0);
  const totalOut = runs.reduce((sum, r) => sum + (r.output_tokens ?? 0), 0);
  const totalCost = runs.reduce((sum, r) => sum + (r.estimated_cost_usd ?? 0), 0);

  return (
    <div>
      <h1>Run history</h1>
      {runs.length === 0 && <p>No runs yet.</p>}
      {runs.length > 0 && (
        <p className="agent-card__meta">
          {runs.length} run{runs.length === 1 ? "" : "s"} &middot; {totalIn.toLocaleString()} in / {totalOut.toLocaleString()} out
          tokens &middot; total ${totalCost.toFixed(4)}
        </p>
      )}
      <table className="run-table">
        <thead>
          <tr>
            <th>Agent</th>
            <th>Status</th>
            <th>Created</th>
            <th>Tokens</th>
            <th>Cost</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <td>{run.agent_id}</td>
              <td>
                <span className={`status-pill status-pill--${run.status}`}>{run.status}</span>
              </td>
              <td>{new Date(run.created_at).toLocaleString()}</td>
              <td className="agent-card__meta">
                {run.input_tokens != null ? `${run.input_tokens.toLocaleString()} in / ${(run.output_tokens ?? 0).toLocaleString()} out` : "—"}
              </td>
              <td className="agent-card__meta">{run.estimated_cost_usd != null ? `$${Number(run.estimated_cost_usd).toFixed(4)}` : "—"}</td>
              <td>
                <Link to={`/runs/${run.id}`}>View result</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
