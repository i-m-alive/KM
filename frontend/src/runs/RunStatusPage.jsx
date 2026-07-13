import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { apiDownload, apiGet, apiPost } from "../api/client";
import ConfidenceBadge from "../components/ConfidenceBadge";
import FlagList from "../components/FlagList";
import StepTimeline from "../components/StepTimeline";

const POLLING = new Set(["pending", "working", "detecting", "tagging", "applying"]);

const CHANNELS = [
  ["verified_text", "Text"],
  ["verified_images", "Images"],
  ["verified_metadata", "Metadata"],
  ["verified_comments", "Comments"],
  ["verified_hyperlinks", "Hyperlinks"],
];

function VerificationPanel({ output }) {
  if (!output || output.native_masking_verified === undefined || output.native_masking_verified === null) return null;
  const allClean = output.native_masking_verified;
  return (
    <div className="card" style={{ margin: "1rem 0" }}>
      <h3 className="card__title">
        Verification — {allClean ? "all channels clean" : "issues found"}
        <span className={`status-pill status-pill--${allClean ? "completed" : "failed"}`}>
          {allClean ? "verified" : "not clean"}
        </span>
      </h3>
      <p className="card__sub">Each channel independently re-checks the rendered file — nothing is trusted just because a step ran.</p>
      <div className="verify-grid">
        {CHANNELS.map(([key, label]) => {
          const v = output[key];
          const cls = v === true ? "pass" : v === false ? "fail" : "na";
          const mark = v === true ? "✓" : v === false ? "✗" : "—";
          return (
            <div key={key} className={`verify-chip verify-chip--${cls}`}>
              <span>{mark}</span> {label}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function RunStatusPage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [run, setRun] = useState(null);
  const [tags, setTags] = useState([]);
  const [masked, setMasked] = useState(null);
  const [error, setError] = useState(null);
  const [remediating, setRemediating] = useState(false);
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

  async function remediate() {
    setError(null);
    setRemediating(true);
    try {
      const updated = await apiPost(`/runs/${runId}/remediate`, {});
      setRun(updated);
      setMasked(null); // stale after the file was mutated in place
    } catch (err) {
      setError(err.message);
    } finally {
      setRemediating(false);
    }
  }

  if (error && !run) return <p className="error-text">{error}</p>;
  if (!run)
    return (
      <div className="loading-state">
        <span className="spinner" /> Loading run…
      </div>
    );

  const isPolling = POLLING.has(run.status);
  const filename = run.output?.filename;

  return (
    <div>
      <div className="page-head">
        <div className="page-head__text">
          <h1 style={{ textTransform: "capitalize" }}>{run.agent_id} run</h1>
          {filename && <p className="page-head__sub">{filename}</p>}
        </div>
      </div>

      <div className="run-result__summary">
        <span className={`status-pill status-pill--${run.status}`}>{run.status.replace(/_/g, " ")}</span>
        {isPolling && (
          <span className="agent-card__meta" style={{ display: "inline-flex", alignItems: "center", gap: "0.4rem" }}>
            <span className="spinner" style={{ width: 13, height: 13 }} /> working…
          </span>
        )}
        <ConfidenceBadge confidence={run.confidence} />
        {run.estimated_cost_usd != null && (
          <span className="run-result__cost">
            {(run.input_tokens ?? 0).toLocaleString()} in / {(run.output_tokens ?? 0).toLocaleString()} out · $
            {Number(run.estimated_cost_usd).toFixed(4)}
          </span>
        )}
      </div>

      {error && <p className="error-text">{error}</p>}

      {run.status === "awaiting_review" && (
        <div className="callout">
          This run is awaiting human review. <Link to={`/review/${run.id}`}>Open in the review queue →</Link>
        </div>
      )}

      <VerificationPanel output={run.output} />

      <FlagList flags={run.flags} />

      {run.status === "completed_with_issues" && (
        <div className="callout callout--warning">
          Verification found masked content still present in this file (see the flags above) — the run finished, but the
          output is NOT safe to distribute as-is.
          {run.agent_id === "sanitization" && (
            <div style={{ marginTop: "0.6rem" }}>
              <button onClick={remediate} disabled={remediating}>
                {remediating ? "Re-sanitizing flagged content…" : "Re-run sanitization on the flagged content"}
              </button>
              <p className="agent-card__meta" style={{ marginTop: "0.4rem" }}>
                Fixes the sanitized file in place: redacts only the flagged images (on the specific slides/pages the
                verifier identified), re-scrubs metadata / comments / hyperlink targets, and re-verifies every channel.
                A residual in the document's own text still requires a full re-run on the original.
              </p>
            </div>
          )}
        </div>
      )}

      {(run.status === "completed" || run.status === "completed_with_issues") && run.agent_id === "sanitization" && (
        <div style={{ display: "flex", gap: "0.6rem", margin: "1rem 0", flexWrap: "wrap" }}>
          <button className="btn--ghost" onClick={loadMasked}>View sanitized document</button>
          <Link to={`/runs/${runId}/compare`}>
            <button type="button" className="btn--ghost">Compare original vs. sanitized</button>
          </Link>
          <button className="btn--ghost" onClick={() => apiDownload(`/runs/${runId}/masked/download`).catch((e) => setError(e.message))}>
            Download sanitized {run.output?.filename?.split(".").pop()?.toUpperCase() || "file"}
          </button>
          {run.status === "completed" && <button onClick={runTagging}>Run Tagging on this run →</button>}
        </div>
      )}

      {masked && (
        <div className="card section">
          <h3 className="card__title">Sanitized document — {masked.filename}</h3>
          {run.output?.masked_document_path && (
            <p className="agent-card__meta">Saved locally at: {run.output.masked_document_path}</p>
          )}
          <div className="pillrow" style={{ margin: "0.5rem 0" }}>
            {(masked.entities_masked || []).map((e, i) => (
              <span key={i} className="chip">{e.mask_token}</span>
            ))}
          </div>
          <pre className="run-result__output" style={{ maxHeight: "480px", overflow: "auto", whiteSpace: "pre-wrap" }}>
            {masked.masked_text}
          </pre>
        </div>
      )}

      {run.agent_id === "tagging" && tags.length > 0 && (
        <div className="card section">
          <h3 className="card__title">Applied tags</h3>
          <div className="pillrow">
            {tags.map((t, i) => (
              <span key={i} className="confidence-badge confidence-badge--high">
                {t.category}: {t.value}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="section">
        <h3>Steps</h3>
        <StepTimeline steps={run.steps} />
      </div>

      <details className="json-details section">
        <summary>Raw output (JSON)</summary>
        <pre className="run-result__output">{JSON.stringify(run.output, null, 2)}</pre>
      </details>
    </div>
  );
}
