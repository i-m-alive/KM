import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { apiBlobUrl, apiGet, apiPost } from "../api/client";

export default function ComparePage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [run, setRun] = useState(null);
  const [originalUrl, setOriginalUrl] = useState(null);
  const [maskedUrl, setMaskedUrl] = useState(null);
  const [error, setError] = useState({ original: null, masked: null, general: null });
  const [rerunning, setRerunning] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let urls = [];

    apiGet(`/runs/${runId}`)
      .then(async (r) => {
        if (cancelled) return;
        setRun(r);
        const documentId = r.input?.document_id;

        if (documentId) {
          try {
            const u = await apiBlobUrl(`/documents/${documentId}/preview`);
            if (cancelled) return;
            urls.push(u);
            setOriginalUrl(u);
          } catch (err) {
            if (!cancelled) setError((prev) => ({ ...prev, original: err.message }));
          }
        }

        try {
          const u = await apiBlobUrl(`/runs/${runId}/masked/preview`);
          if (cancelled) return;
          urls.push(u);
          setMaskedUrl(u);
        } catch (err) {
          if (!cancelled) setError((prev) => ({ ...prev, masked: err.message }));
        }
      })
      .catch((err) => !cancelled && setError((prev) => ({ ...prev, general: err.message })));

    return () => {
      cancelled = true;
      urls.forEach((u) => URL.revokeObjectURL(u));
    };
  }, [runId]);

  async function resanitize() {
    if (!run?.input?.document_id) return;
    setRerunning(true);
    try {
      const fresh = await apiPost("/runs", { agent_id: "sanitization", input: { document_id: run.input.document_id } });
      navigate(`/runs/${fresh.id}`);
    } catch (err) {
      setError((prev) => ({ ...prev, general: err.message }));
      setRerunning(false);
    }
  }

  if (error.general) return <p className="error-text">{error.general}</p>;
  if (!run) return <p>Loading...</p>;

  return (
    <div>
      <h1>Compare original vs. sanitized</h1>
      <p className="agent-card__meta">
        {run.output?.filename} &middot; look closely at logos, screenshots, and any pixel content — text
        masking cannot edit images (see the flags on the run for what was found).
      </p>
      <button onClick={resanitize} disabled={rerunning} style={{ margin: "0.75rem 0" }}>
        {rerunning ? "Starting..." : "Not sanitized properly? Re-run Sanitization →"}
      </button>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "1rem",
          alignItems: "start",
        }}
      >
        <div>
          <h3>Original</h3>
          {error.original && <p className="error-text">{error.original}</p>}
          {originalUrl ? (
            <iframe title="original" src={originalUrl} style={{ width: "100%", height: "80vh", border: "1px solid var(--line)" }} />
          ) : (
            !error.original && <p className="agent-card__meta">Loading preview…</p>
          )}
        </div>
        <div>
          <h3>Sanitized</h3>
          {error.masked && <p className="error-text">{error.masked}</p>}
          {maskedUrl ? (
            <iframe title="masked" src={maskedUrl} style={{ width: "100%", height: "80vh", border: "1px solid var(--line)" }} />
          ) : (
            !error.masked && <p className="agent-card__meta">Loading preview… (run must be completed)</p>
          )}
        </div>
      </div>
    </div>
  );
}
