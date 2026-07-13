import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiGet, apiPost, apiUpload } from "../api/client";
import { FileIcon, UploadIcon } from "../components/Icons";

const TYPE_LABELS = { pdf: "PDF", docx: "Word", pptx: "PowerPoint", xlsx: "Excel" };

function typeOf(doc) {
  const ext = doc.filename.split(".").pop().toLowerCase();
  return TYPE_LABELS[ext] || ext.toUpperCase();
}

export default function DocumentsPage() {
  const navigate = useNavigate();
  const [docs, setDocs] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState(null);
  const [startingId, setStartingId] = useState(null);

  function load() {
    apiGet("/documents").then(setDocs).catch((e) => setError(e.message));
  }
  useEffect(load, []);

  async function handleUpload(e) {
    e.preventDefault();
    if (!file) return;
    setError(null);
    setBusy(true);
    try {
      await apiUpload("/documents", file);
      setFile(null);
      e.target.reset();
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function sanitize(documentId) {
    setError(null);
    setStartingId(documentId);
    try {
      const run = await apiPost("/runs", { agent_id: "sanitization", input: { document_id: documentId } });
      navigate(`/runs/${run.id}`);
    } catch (err) {
      setError(err.message);
      setStartingId(null);
    }
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Documents</h1>
          <p className="page-head__sub">Upload an engagement document, then run Sanitization to strip client identity from it.</p>
        </div>
      </div>
      {error && <p className="error-text">{error}</p>}

      <div className="card">
        <h3 className="card__title">
          <UploadIcon style={{ width: 16, height: 16 }} /> Upload a document
        </h3>
        <p className="card__sub">PDF, DOCX, PPTX, or XLSX.</p>
        <form onSubmit={handleUpload} style={{ flexDirection: "row", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
          <input type="file" accept=".pdf,.docx,.pptx,.xlsx" onChange={(e) => setFile(e.target.files[0])} />
          <button type="submit" disabled={busy || !file}>
            {busy ? "Uploading…" : "Upload"}
          </button>
        </form>
      </div>

      <div className="section">
        {docs.length === 0 ? (
          <div className="empty-state">
            <FileIcon />
            <p>No documents yet — upload one above to get started.</p>
          </div>
        ) : (
          <div className="table-scroll">
            <table className="run-table">
              <thead>
                <tr>
                  <th>Filename</th>
                  <th>Type</th>
                  <th>Uploaded</th>
                  <th style={{ textAlign: "right" }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id}>
                    <td style={{ fontWeight: 550 }}>{d.filename}</td>
                    <td>
                      <span className="chip">{typeOf(d)}</span>
                    </td>
                    <td className="agent-card__meta">{new Date(d.uploaded_at).toLocaleString()}</td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn--sm" onClick={() => sanitize(d.id)} disabled={startingId === d.id}>
                        {startingId === d.id ? "Starting…" : "Run Sanitization"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
