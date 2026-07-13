import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiGet } from "../api/client";
import { BotIcon, CheckShieldIcon, TagIcon } from "../components/Icons";

const AGENT_ICONS = {
  sanitization: CheckShieldIcon,
  tagging: TagIcon,
};

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

  if (loading)
    return (
      <div className="loading-state">
        <span className="spinner" /> Loading agents…
      </div>
    );
  if (error) return <p className="error-text">{error}</p>;

  function action(agent) {
    if (agent.agent_id === "sanitization")
      return (
        <Link to="/documents" style={{ marginTop: "auto" }}>
          <button className="btn--subtle" style={{ width: "100%" }}>Upload a document to sanitize →</button>
        </Link>
      );
    if (agent.agent_id === "tagging")
      return (
        <span className="agent-card__meta" style={{ marginTop: "auto" }}>
          Launched from a completed Sanitization run — see <Link to="/runs">Runs</Link>.
        </span>
      );
    return (
      <Link to={`/agents/${agent.agent_id}/run`} style={{ marginTop: "auto" }}>
        <button className="btn--subtle" style={{ width: "100%" }}>Run this agent →</button>
      </Link>
    );
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Agents</h1>
          <p className="page-head__sub">
            AI agents that sanitize, classify, and govern your engagement documents — every action reviewed and verified.
          </p>
        </div>
      </div>
      <div className="agent-grid">
        {agents.map((agent) => {
          const Icon = AGENT_ICONS[agent.agent_id] || BotIcon;
          return (
            <div key={agent.agent_id} className="agent-card">
              <div className="agent-card__icon">
                <Icon />
              </div>
              <h2>{agent.display_name}</h2>
              <p>{agent.description}</p>
              <div className="pillrow" style={{ marginBottom: "0.8rem" }}>
                <span className="chip">{agent.mode}</span>
                {agent.tools.map((t) => (
                  <span key={t} className="chip">{t}</span>
                ))}
              </div>
              {action(agent)}
            </div>
          );
        })}
      </div>
    </div>
  );
}
