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

  return (
    <div>
      <h1>Run history</h1>
      {runs.length === 0 && <p>No runs yet.</p>}
      <table className="run-table">
        <thead>
          <tr>
            <th>Agent</th>
            <th>Status</th>
            <th>Created</th>
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
