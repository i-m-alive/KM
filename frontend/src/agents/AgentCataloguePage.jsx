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

  function action(agent) {
    if (agent.agent_id === "sanitization") return <Link to="/documents">Upload a document to sanitize →</Link>;
    if (agent.agent_id === "tagging")
      return <span className="agent-card__meta">Launched from a completed Sanitization run (see Runs).</span>;
    return <Link to={`/agents/${agent.agent_id}/run`}>Run this agent</Link>;
  }

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
            {action(agent)}
          </div>
        ))}
      </div>
    </div>
  );
}
