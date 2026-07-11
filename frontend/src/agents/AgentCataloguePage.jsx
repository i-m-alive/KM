import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet } from "../api/client";

export default function AgentCataloguePage() {
  const [agents, setAgents] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiGet("/agents")
      .then(setAgents)
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p>Loading agents...</p>;
  if (error) return <p className="error-text">{error}</p>;

  return (
    <div>
      <h1>Agents</h1>
      <div className="agent-grid">
        {agents.map((agent) => (
          <div key={agent.agent_id} className="agent-card">
            <h2>{agent.display_name}</h2>
            <p>{agent.description}</p>
            <p className="agent-card__meta">
              Mode: {agent.mode} &middot; Tools: {agent.tools.join(", ") || "none"}
            </p>
            <Link to={`/agents/${agent.agent_id}/run`}>Run this agent</Link>
          </div>
        ))}
      </div>
    </div>
  );
}
