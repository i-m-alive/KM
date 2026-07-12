import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { apiDownload, apiGet, apiPost } from "../api/client";
import ConfidenceBadge from "../components/ConfidenceBadge";
import FlagList from "../components/FlagList";
import StepTimeline from "../components/StepTimeline";

const POLLING = new Set(["pending", "working", "detecting", "tagging", "applying"]);

export default function RunStatusPage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [run, setRun] = useState(null);
  const [tags, setTags] = useState([]);
  const [masked, setMasked] = useState(null);
  const [error, setError] = useState(null);
  const timer = useRef(null);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const r = await apiGet(`/runs/${runId}`);
        if (cancelled) return;
        setRun(r);
        if (r.agent_id === "tagging" && r.status === "completed") {
          apiGet(`/tags/runs/${runId}`).then(setTags).catch(() => {});
        }
        if (POLLING.has(r.status)) {
          timer.current = setTimeout(poll, 2000);
        }
      } catch (err) {
        if (!cancelled) setError(err.message);
      }
    }
    poll();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [runId]);

  async function runTagging() {
    setError(null);
    try {
      const t = await apiPost("/runs", { agent_id: "tagging", input: { sanitization_run_id: runId } });
      navigate(`/runs/${t.id}`);
    } catch (err) {
      setError(err.message);
    }
  }

  async function loadMasked() {
    setError(null);
    try {
      setMasked(await apiGet(`/runs/${runId}/masked`));
    } catch (err) {
      setError(err.message);
    }
  }

  if (error) return <p className="error-text">{error}</p>;
  if (!run) return <p>Loading run...</p>;

  const isPolling = POLLING.has(run.status);

  return (
    <div>
      <h1>{run.agent_id} run</h1>
      <div className="run-result__summary">
        <span className={`status-pill status-pill--${run.status}`}>{run.status}</span>
        {isPolling && <span className="agent-card__meta">updating…</span>}
        <ConfidenceBadge confidence={run.confidence} />
        {run.estimated_cost_usd != null && (
          <span className="run-result__cost">
            {run.input_tokens ?? 0} in / {run.output_tokens ?? 0} out · ${Number(run.estimated_cost_usd).toFixed(4)}
          </span>
        )}
      </div>

      {run.status === "awaiting_review" && (
        <div className="callout">
          This run is awaiting human review.{" "}
          <Link to={`/review/${run.id}`}>Open in the review queue →</Link>
        </div>
      )}

      <FlagList flags={run.flags} />

      {run.status === "completed_with_issues" && (
        <div className="callout callout--warning">
          Verification found masked content still present in this file (see the flags above) — the run finished, but
          the output is NOT safe to distribute as-is. Re-run Sanitization on the original document and address the
          flagged item(s) before approving.
        </div>
      )}

      {(run.status === "completed" || run.status === "completed_with_issues") && run.agent_id === "sanitization" && (
        <div style={{ display: "flex", gap: "0.75rem", margin: "0.5rem 0 1rem", flexWrap: "wrap" }}>
          <button onClick={loadMasked}>View sanitized document</button>
          <Link to={`/runs/${runId}/compare`}>
            <button type="button">Compare original vs. sanitized (side by side)</button>
          </Link>
          <button onClick={() => apiDownload(`/runs/${runId}/masked/download`).catch((e) => setError(e.message))}>
            Download sanitized {run.output?.filename?.split(".").pop()?.toUpperCase() || "file"}
          </button>
          {run.status === "completed" && <button onClick={runTagging}>Run Tagging on this run →</button>}
        </div>
      )}

      {masked && (
        <div>
          <h3>Sanitized document — {masked.filename}</h3>
          {run.output?.masked_document_path && (
            <p className="agent-card__meta">Saved locally at: {run.output.masked_document_path}</p>
          )}
          <div className="pillrow" style={{ display: "flex", flexWrap: "wrap", gap: "0.35rem", marginBottom: "0.5rem" }}>
            {(masked.entities_masked || []).map((e, i) => (
              <span key={i} className="confidence-badge confidence-badge--low">
                {e.mask_token}
              </span>
            ))}
          </div>
          <pre className="run-result__output" style={{ maxHeight: "480px", overflow: "auto", whiteSpace: "pre-wrap" }}>
            {masked.masked_text}
          </pre>
        </div>
      )}

      {run.agent_id === "tagging" && tags.length > 0 && (
        <div>
          <h3>Applied tags</h3>
          <div className="pillrow" style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem" }}>
            {tags.map((t, i) => (
              <span key={i} className="confidence-badge confidence-badge--high">
                {t.category}: {t.value}
              </span>
            ))}
          </div>
        </div>
      )}

      <h3>Output</h3>
      <pre className="run-result__output">{JSON.stringify(run.output, null, 2)}</pre>

      <h3>Steps</h3>
      <StepTimeline steps={run.steps} />
    </div>
  );
}
