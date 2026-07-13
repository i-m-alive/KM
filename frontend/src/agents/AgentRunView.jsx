import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { apiGet, apiPost } from "../api/client";
import ConfidenceBadge from "../components/ConfidenceBadge";
import FlagList from "../components/FlagList";
import StepTimeline from "../components/StepTimeline";

// Renders any AgentResult-shaped run, regardless of which agent produced it.
function RunResult({ run }) {
  return (
    <div className="run-result">
      <div className="run-result__summary">
        <span className={`status-pill status-pill--${run.status}`}>{run.status}</span>
        <ConfidenceBadge confidence={run.confidence} />
        {run.estimated_cost_usd !== null && run.estimated_cost_usd !== undefined && (
          <span className="run-result__cost">
            {run.input_tokens ?? 0} in / {run.output_tokens ?? 0} out tokens &middot; $
            {Number(run.estimated_cost_usd).toFixed(4)}
          </span>
        )}
      </div>

      <FlagList flags={run.flags} />

      <h3>Output</h3>
      <pre className="run-result__output">{JSON.stringify(run.output, null, 2)}</pre>

      <h3>Steps</h3>
      <StepTimeline steps={run.steps} />

      {run.output_file_path && (
        <p className="run-result__file">Saved to: {run.output_file_path}</p>
      )}
    </div>
  );
}

export default function AgentRunView() {
  const { agentId, runId } = useParams();
  const isViewMode = Boolean(runId);

  const [agent, setAgent] = useState(null);
  const [text, setText] = useState("");
  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [loading, setLoading] = useState(isViewMode);

  useEffect(() => {
    if (isViewMode) {
      apiGet(`/runs/${runId}`)
        .then(setRun)
        .catch((err) => setError(err.message))
        .finally(() => setLoading(false));
      return;
    }

    apiGet("/agents")
      .then((agents) => setAgent(agents.find((a) => a.agent_id === agentId) || null))
      .catch((err) => setError(err.message));
  }, [agentId, runId, isViewMode]);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    setRun(null);
    try {
      const result = await apiPost("/runs", { agent_id: agentId, input: { text } });
      setRun(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) return <p>Loading run...</p>;

  return (
    <div>
      <div className="page-head">
        <div className="page-head__text">
          <h1>{isViewMode ? `Run ${runId}` : agent?.display_name || agentId}</h1>
          {!isViewMode && agent?.description && <p className="page-head__sub">{agent.description}</p>}
        </div>
      </div>

      {!isViewMode && (
        <form onSubmit={handleSubmit} className="card">
          <label>
            Input text
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={6}
              required
              placeholder="Paste or type some text for the agent to process..."
            />
          </label>
          <button type="submit" disabled={submitting} style={{ alignSelf: "flex-start" }}>
            {submitting ? "Running…" : "Run agent"}
          </button>
        </form>
      )}

      {error && <p className="error-text">{error}</p>}
      {run && <RunResult run={run} />}
    </div>
  );
}
