import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet } from "../api/client";
import { PlayIcon } from "../components/Icons";

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

  if (loading)
    return (
      <div className="loading-state">
        <span className="spinner" /> Loading run history…
      </div>
    );
  if (error) return <p className="error-text">{error}</p>;

  const totalIn = runs.reduce((sum, r) => sum + (r.input_tokens ?? 0), 0);
  const totalOut = runs.reduce((sum, r) => sum + (r.output_tokens ?? 0), 0);
  const totalCost = runs.reduce((sum, r) => sum + (r.estimated_cost_usd ?? 0), 0);

  return (
    <div>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Runs</h1>
          <p className="page-head__sub">Every agent execution, with live status, token usage, and cost.</p>
        </div>
      </div>

      {runs.length > 0 && (
        <div className="kpi-row">
          <div className="kpi">
            <div className="kpi__n">{runs.length}</div>
            <div className="kpi__l">Total runs</div>
          </div>
          <div className="kpi">
            <div className="kpi__n">{(totalIn + totalOut).toLocaleString()}</div>
            <div className="kpi__l">Tokens processed</div>
          </div>
          <div className="kpi">
            <div className="kpi__n">${totalCost.toFixed(4)}</div>
            <div className="kpi__l">Total spend</div>
          </div>
        </div>
      )}

      {runs.length === 0 ? (
        <div className="empty-state">
          <PlayIcon />
          <p>No runs yet — upload a document and run Sanitization to see it here.</p>
        </div>
      ) : (
        <div className="table-scroll">
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
                  <td style={{ fontWeight: 550, textTransform: "capitalize" }}>{run.agent_id}</td>
                  <td>
                    <span className={`status-pill status-pill--${run.status}`}>{run.status.replace(/_/g, " ")}</span>
                  </td>
                  <td className="agent-card__meta">{new Date(run.created_at).toLocaleString()}</td>
                  <td className="agent-card__meta">
                    {run.input_tokens != null
                      ? `${run.input_tokens.toLocaleString()} in / ${(run.output_tokens ?? 0).toLocaleString()} out`
                      : "—"}
                  </td>
                  <td className="agent-card__meta">
                    {run.estimated_cost_usd != null ? `$${Number(run.estimated_cost_usd).toFixed(4)}` : "—"}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <Link to={`/runs/${run.id}`}>View →</Link>
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
