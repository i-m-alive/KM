import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { apiGet, apiPost } from "../api/client";
import AuthImage from "../components/AuthImage";
import FlagList from "../components/FlagList";
import StepTimeline from "../components/StepTimeline";

export default function ReviewDetailPage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState(null);
  const [removed, setRemoved] = useState(new Set());
  // Image groups default to the model's recommendation (contains_client_identity);
  // this set holds groups whose recommendation the reviewer has FLIPPED.
  const [imageOverrides, setImageOverrides] = useState(new Set());
  const [notes, setNotes] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  // Entities the agent missed entirely; reviewer adds them by hand.
  const [addedEntities, setAddedEntities] = useState([]);
  const [newSurface, setNewSurface] = useState("");
  const [newType, setNewType] = useState("CLIENT_NAME");
  // Which single entity (by surface text, lowercased) links to a client account.
  const [clientEntitySurface, setClientEntitySurface] = useState("");
  // How masked surfaces are rendered in the output document.
  const [maskingStyle, setMaskingStyle] = useState("token");

  const MASKING_STYLES = [
    { value: "token", label: "Mask with token", example: "[CLIENT_1]", hint: "Traceable — the same stable token everywhere this entity appears." },
    { value: "black", label: "Black out", example: "████████", hint: "Replaced with solid black blocks; nothing readable survives." },
    { value: "remove", label: "Remove entirely", example: "(deleted)", hint: "The text is deleted outright, no marker left behind." },
  ];

  const ENTITY_TYPES = [
    "CLIENT_NAME",
    "CLIENT_PERSON",
    "CLIENT_LOCATION",
    "CLIENT_EMAIL_DOMAIN",
    "CLIENT_SYSTEM_NAME",
    "CLIENT_CONTRACT_ID",
  ];

  useEffect(() => {
    apiGet(`/review/${runId}`)
      .then(setDetail)
      .catch((e) => setError(e.message));
  }, [runId]);

  function toggle(key) {
    setRemoved((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }

  function toggleImage(groupIndex) {
    setImageOverrides((prev) => {
      const next = new Set(prev);
      next.has(groupIndex) ? next.delete(groupIndex) : next.add(groupIndex);
      return next;
    });
  }

  function addEntity() {
    const surface = newSurface.trim();
    if (!surface) return;
    setAddedEntities((prev) => [...prev, { surface_text: surface, entity_type: newType }]);
    setNewSurface("");
  }

  function removeAddedEntity(surface) {
    setAddedEntities((prev) => prev.filter((e) => e.surface_text !== surface));
    if (clientEntitySurface.toLowerCase() === surface.toLowerCase()) setClientEntitySurface("");
  }

  function willRedact(group) {
    if (group.mandatory_redaction) return true;
    const flipped = imageOverrides.has(group.group_index);
    return flipped ? !group.contains_client_identity : group.contains_client_identity;
  }

  async function decide(decision) {
    setError(null);
    setBusy(true);
    try {
      const edits = {};
      const p = detail.proposal || {};
      if (detail.agent_id === "sanitization") {
        edits.removed_surfaces = (p.entities || []).filter((e) => removed.has(e.surface_text)).map((e) => e.surface_text);
        const excluded = [];
        const included = [];
        for (const g of p.images || []) {
          const flipped = imageOverrides.has(g.group_index);
          if (!flipped) continue;
          if (g.contains_client_identity) excluded.push(g.group_index); // was recommended, reviewer unchecked it
          else included.push(g.group_index); // was not recommended, reviewer opted it in
        }
        edits.excluded_image_groups = excluded;
        edits.included_image_groups = included;
        if (addedEntities.length > 0) edits.added_entities = addedEntities;
        if (clientEntitySurface) edits.client_entity_surface = clientEntitySurface;
        edits.masking_style = maskingStyle;
      } else if (detail.agent_id === "tagging") {
        edits.removed_tags = (p.tags || [])
          .filter((t) => removed.has(`${t.category}:${t.value}`))
          .map((t) => ({ category: t.category, value: t.value }));
      }
      const edited =
        removed.size > 0 ||
        imageOverrides.size > 0 ||
        addedEntities.length > 0 ||
        Boolean(clientEntitySurface) ||
        (detail.agent_id === "sanitization" && maskingStyle !== "token");
      const finalDecision = decision === "approved" && edited ? "edited" : decision;
      await apiPost(`/review/${runId}`, { decision: finalDecision, notes: notes || null, edits });
      // Back to this run's own flow (status, masked-doc view/download, compare,
      // "run Tagging on this") rather than dumping the reviewer into the queue.
      navigate(`/runs/${runId}`);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  if (error) return <p className="error-text">{error}</p>;
  if (!detail) return <p>Loading proposal...</p>;

  const p = detail.proposal || {};
  const documentId = p.document_id;
  const images = p.images || [];

  return (
    <div>
      <h1>Review: {detail.agent_id}</h1>
      <p className="agent-card__meta">{detail.summary}</p>
      {detail.status !== "awaiting_review" && (
        <div className="callout">This run is now "{detail.status}" — it may already have been reviewed.</div>
      )}
      <FlagList flags={detail.flags} />

      {detail.agent_id === "sanitization" && (
        <>
          <h3>Sanitization style</h3>
          <div className="agent-grid">
            {MASKING_STYLES.map((s) => (
              <label
                key={s.value}
                className="agent-card"
                style={{
                  display: "block",
                  cursor: "pointer",
                  borderColor: maskingStyle === s.value ? "#7c9cff" : undefined,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                  <input
                    type="radio"
                    name="masking-style"
                    value={s.value}
                    checked={maskingStyle === s.value}
                    onChange={() => setMaskingStyle(s.value)}
                  />
                  <strong>{s.label}</strong>
                </div>
                <p className="agent-card__meta" style={{ margin: "0.35rem 0 0" }}>
                  e.g. <code>{s.example}</code> &middot; {s.hint}
                </p>
              </label>
            ))}
          </div>

          <h3 style={{ marginTop: "1.5rem" }}>Proposed masks ({(p.entities || []).length + addedEntities.length})</h3>
          <p className="agent-card__meta">
            Untick an entity to exclude it from masking, add any the agent missed, and mark which one is the
            client — that's the only entity linked to a client account.
          </p>
          <table className="run-table">
            <thead>
              <tr>
                <th>Mask</th>
                <th>Surface</th>
                <th>Type</th>
                <th>Conf.</th>
                <th>Occurrences</th>
                <th>Known?</th>
                <th>Include</th>
                <th>Client?</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(p.entities || []).map((e, i) => (
                <tr key={i}>
                  <td className="agent-card__meta">{e.mask_token || `[new ${e.entity_type}]`}</td>
                  <td>{e.surface_text}</td>
                  <td className="agent-card__meta">{e.entity_type}</td>
                  <td>{Math.round((e.confidence ?? 0) * 100)}%</td>
                  <td>{e.occurrences}</td>
                  <td>{e.known ? "yes" : "new"}</td>
                  <td>
                    <input type="checkbox" checked={!removed.has(e.surface_text)} onChange={() => toggle(e.surface_text)} />
                  </td>
                  <td>
                    <input
                      type="radio"
                      name="client-entity"
                      disabled={removed.has(e.surface_text)}
                      checked={clientEntitySurface.toLowerCase() === e.surface_text.toLowerCase()}
                      onChange={() => setClientEntitySurface(e.surface_text)}
                    />
                  </td>
                  <td />
                </tr>
              ))}
              {addedEntities.map((e, i) => (
                <tr key={`added-${i}`}>
                  <td className="agent-card__meta">[new {e.entity_type}]</td>
                  <td>{e.surface_text}</td>
                  <td className="agent-card__meta">{e.entity_type}</td>
                  <td>—</td>
                  <td>—</td>
                  <td className="agent-card__meta">reviewer-added</td>
                  <td>
                    <input type="checkbox" checked disabled />
                  </td>
                  <td>
                    <input
                      type="radio"
                      name="client-entity"
                      checked={clientEntitySurface.toLowerCase() === e.surface_text.toLowerCase()}
                      onChange={() => setClientEntitySurface(e.surface_text)}
                    />
                  </td>
                  <td>
                    <button type="button" onClick={() => removeAddedEntity(e.surface_text)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginTop: "0.75rem" }}>
            <input
              type="text"
              placeholder="Surface text the agent missed (e.g. a client name)"
              value={newSurface}
              onChange={(e) => setNewSurface(e.target.value)}
              style={{ flex: 1 }}
            />
            <select value={newType} onChange={(e) => setNewType(e.target.value)}>
              {ENTITY_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            <button type="button" onClick={addEntity} disabled={!newSurface.trim()}>
              Add entity
            </button>
          </div>

          <h3 style={{ marginTop: "1.5rem" }}>
            Embedded images ({images.length}{p.images_skipped ? `, ${p.images_skipped} not scanned` : ""})
          </h3>
          <p className="agent-card__meta">
            Logos, screenshots, and other pixel content are scanned separately from text — they cannot be
            edited the same way, so check each one and confirm which should be blacked out.
          </p>
          {images.length === 0 && <p className="agent-card__meta">No embedded images found.</p>}
          <div className="agent-grid">
            {images.map((g) => {
              const borderColor = g.needs_human_judgment
                ? "#D9A441"
                : g.contains_client_identity
                  ? "#E3AFAF"
                  : undefined;
              return (
                <div key={g.group_index} className="agent-card" style={{ borderColor }}>
                  {documentId && (
                    <AuthImage
                      src={`/documents/${documentId}/images/${g.sample_index}`}
                      alt={g.description}
                      style={{ maxWidth: "100%", maxHeight: "160px", objectFit: "contain", marginBottom: "0.5rem" }}
                    />
                  )}
                  <p style={{ margin: "0 0 0.35rem", fontSize: "0.88rem" }}>{g.description || "(no description)"}</p>
                  <p className="agent-card__meta">
                    {g.locations.join(", ")} &middot; {g.occurrence_count} occurrence(s) &middot; conf.{" "}
                    {Math.round((g.confidence ?? 0) * 100)}%
                  </p>
                  {g.ocr_text && g.ocr_text.length > 0 && (
                    <p className="agent-card__meta" style={{ marginTop: "0.35rem" }}>
                      OCR: <em>{g.ocr_text.join(", ")}</em>
                    </p>
                  )}
                  {g.logo_match_token && (
                    <p className="agent-card__meta" style={{ marginTop: "0.35rem" }}>
                      Possible logo match: <strong>{g.logo_match_token}</strong> (distance {g.logo_match_distance})
                    </p>
                  )}
                  {g.needs_human_judgment && (
                    <p style={{ margin: "0.35rem 0 0", color: "#8a5a00", fontSize: "0.85rem" }}>
                      Uncertain signal — stylized font, low-contrast mark, or borderline logo similarity. Please inspect manually.
                    </p>
                  )}
                  {g.mandatory_redaction && (
                    <p style={{ margin: "0.35rem 0 0", color: "#8a1f1f", fontSize: "0.85rem", fontWeight: 600 }}>
                      Locked: confirmed match to an already-approved masked entity ({g.logo_match_token}) — always
                      redacted, regardless of the description above.
                    </p>
                  )}
                  <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", marginTop: "0.4rem" }}>
                    <input
                      type="checkbox"
                      checked={willRedact(g)}
                      disabled={g.mandatory_redaction}
                      onChange={() => toggleImage(g.group_index)}
                    />
                    Black this image out
                  </label>
                </div>
              );
            })}
          </div>
        </>
      )}

      {detail.agent_id === "tagging" && (
        <>
          <h3>Proposed tags ({(p.tags || []).length})</h3>
          <p className="agent-card__meta">Untick a tag to exclude it. New terms stay pending governance and are not applied.</p>
          <table className="run-table">
            <thead>
              <tr>
                <th>Category</th>
                <th>Value</th>
                <th>Conf.</th>
                <th>Status</th>
                <th>Include</th>
              </tr>
            </thead>
            <tbody>
              {(p.tags || []).map((t, i) => {
                const key = `${t.category}:${t.value}`;
                return (
                  <tr key={i}>
                    <td className="agent-card__meta">{t.category}</td>
                    <td>{t.value}</td>
                    <td>{Math.round((t.confidence ?? 0) * 100)}%</td>
                    <td>{t.status}</td>
                    <td>
                      <input type="checkbox" checked={!removed.has(key)} onChange={() => toggle(key)} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}

      <label>
        Notes (optional)
        <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} />
      </label>
      <div style={{ display: "flex", gap: "0.75rem", marginTop: "0.75rem" }}>
        <button onClick={() => decide("approved")} disabled={busy}>
          Approve
          {removed.size > 0 || imageOverrides.size > 0 || addedEntities.length > 0 || clientEntitySurface ? " (edited)" : ""}
        </button>
        <button onClick={() => decide("rejected")} disabled={busy}>
          Reject
        </button>
      </div>

      <h3 style={{ marginTop: "1.5rem" }}>Steps</h3>
      <StepTimeline steps={detail.steps} />
    </div>
  );
}
