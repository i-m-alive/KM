import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiGet, apiPost, apiUpload } from "../api/client";

export default function DocumentsPage() {
  const navigate = useNavigate();
  const [docs, setDocs] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState(null);

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
    try {
      const run = await apiPost("/runs", { agent_id: "sanitization", input: { document_id: documentId } });
      navigate(`/runs/${run.id}`);
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <div>
      <h1>Documents</h1>
      <p className="agent-card__meta">Upload a PDF, DOCX, PPTX, or XLSX, then run Sanitization on it.</p>
      {error && <p className="error-text">{error}</p>}

      <form onSubmit={handleUpload} style={{ flexDirection: "row", alignItems: "center", gap: "0.75rem" }}>
        <input type="file" accept=".pdf,.docx,.pptx,.xlsx" onChange={(e) => setFile(e.target.files[0])} />
        <button type="submit" disabled={busy || !file}>
          {busy ? "Uploading..." : "Upload"}
        </button>
      </form>

      <table className="run-table" style={{ marginTop: "1.5rem" }}>
        <thead>
          <tr>
            <th>Filename</th>
            <th>Type</th>
            <th>Uploaded</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {docs.length === 0 && (
            <tr>
              <td colSpan={4}>No documents yet.</td>
            </tr>
          )}
          {docs.map((d) => (
            <tr key={d.id}>
              <td>{d.filename}</td>
              <td className="agent-card__meta">{d.content_type.split(".").pop()}</td>
              <td>{new Date(d.uploaded_at).toLocaleString()}</td>
              <td>
                <button onClick={() => sanitize(d.id)}>Run Sanitization</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
