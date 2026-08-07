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
  // Phase 3: the opt-IN counterpart to `removed` - for an entity whose
  // default_action is "flag"/"keep" (COMMERCIAL_TERM, COMPETITOR_NAME,
  // STRATEGY_MENTION, OWN_COST_DETAIL, ORG_CHART_STRUCTURE, or a non-
  // consented INTERNAL_TEAM_MEMBER), it is NOT masked unless the reviewer
  // explicitly opts it in here.
  const [included, setIncluded] = useState(new Set());
  // surface_text -> consent_status the reviewer has set for an
  // INTERNAL_TEAM_MEMBER row (not_required | pending | granted).
  const [consentUpdates, setConsentUpdates] = useState({});
  // One of approve_as_is | requires_client_signoff | remove_section - only
  // meaningful (and required before Approve) when the proposal's
  // discusses_negative_outcome is true.
  const [negativeOutcomeResolution, setNegativeOutcomeResolution] = useState("");
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
  // surface_text -> reviewer-chosen replacement string, used everywhere
  // instead of the [CLIENT_N] token for that entity (validated server-side
  // before being applied - a rejected alias falls back to the token and
  // shows up as a flag on the resulting run, not as an error here).
  const [aliases, setAliases] = useState({});
  // surface_text -> another proposal surface_text the reviewer has
  // recognized as the SAME real-world entity (e.g. "J&J" -> "Johnson &
  // Johnson") - merged surfaces share one token/alias instead of getting
  // their own.
  const [merges, setMerges] = useState({});

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
    // Generic PII (Phase 1) - alongside the CLIENT_* types above.
    "PERSON",
    "ORGANIZATION",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    // Infrastructure & Security (Phase 2). CREDENTIAL is mandatory/non-
    // overridable once proposed - see the locked "Include" checkbox below.
    "INFRA_IDENTIFIER",
    "CREDENTIAL",
    // Phase 3. Five of these default to NOT masked (flag-for-review) rather
    // than masked-by-default - the "Include" checkbox for these starts
    // UNCHECKED, driven by each entity's own default_action from the
    // proposal (see the table below), not a hardcoded list here.
    "COMMERCIAL_TERM",
    "COMPETITOR_NAME",
    "STRATEGY_MENTION",
    "OWN_COST_DETAIL",
    "ORG_CHART_STRUCTURE",
    "INTERNAL_TEAM_MEMBER",
    "CLIENT_PERSON_TITLE",
    "CLIENT_PHONE",
  ];

  const MANDATORY_ENTITY_TYPES = new Set(["CREDENTIAL"]);
  const OUTCOME_RESOLUTIONS = [
    { value: "approve_as_is", label: "Approve as-is", hint: "This outcome is fine to share as written." },
    { value: "requires_client_signoff", label: "Requires client sign-off", hint: "Hold distribution until the client explicitly approves sharing this." },
    { value: "remove_section", label: "Remove this section", hint: "Cut the sensitive passage before distributing (do this manually, then approve)." },
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

  function toggleInclude(surface) {
    setIncluded((prev) => {
      const next = new Set(prev);
      next.has(surface) ? next.delete(surface) : next.add(surface);
      return next;
    });
  }

  function setConsent(surface, status) {
    setConsentUpdates((prev) => {
      const next = { ...prev };
      if (status) next[surface] = status;
      else delete next[surface];
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

  function isIncluded(surface) {
    return addedEntities.some((e) => e.surface_text.toLowerCase() === surface.toLowerCase());
  }

  function setAlias(surface, value) {
    setAliases((prev) => {
      const next = { ...prev };
      if (value.trim()) next[surface] = value;
      else delete next[surface];
      return next;
    });
  }

  function setMerge(surface, canonicalSurface) {
    setMerges((prev) => {
      const next = { ...prev };
      if (canonicalSurface) next[surface] = canonicalSurface;
      else delete next[surface];
      return next;
    });
  }

  // Excluded candidates share the SAME added-entity path as manually-typed
  // ones (apply() merges edits.added_entities identically regardless of
  // where the surface came from) - ticking one here is just a pre-filled
  // "add entity" rather than a separate mechanism.
  function toggleExcluded(candidate) {
    setAddedEntities((prev) => {
      if (isIncluded(candidate.surface_text)) {
        return prev.filter((e) => e.surface_text.toLowerCase() !== candidate.surface_text.toLowerCase());
      }
      return [...prev, { surface_text: candidate.surface_text, entity_type: candidate.entity_type }];
    });
  }

  function willRedact(group) {
    if (group.mandatory_redaction) return true;
    // contains_real_data_sample (Phase 2) is recommended-by-default the
    // same way contains_client_identity already is - MUST match apply()'s
    // own re-derivation of "recommended" server-side (agent.py), or a
    // data-sample image left untouched would show checked here but not
    // actually get redacted.
    const recommended = group.contains_client_identity || group.contains_real_data_sample;
    const flipped = imageOverrides.has(group.group_index);
    return flipped ? !recommended : recommended;
  }

  async function decide(decision) {
    setError(null);
    setBusy(true);
    try {
      const edits = {};
      const p = detail.proposal || {};
      if (detail.agent_id === "sanitization") {
        edits.removed_surfaces = (p.entities || []).filter((e) => removed.has(e.surface_text)).map((e) => e.surface_text);
        const excludedImageGroups = [];
        const includedImageGroups = [];
        for (const g of p.images || []) {
          const flipped = imageOverrides.has(g.group_index);
          if (!flipped) continue;
          if (g.contains_client_identity) excludedImageGroups.push(g.group_index); // was recommended, reviewer unchecked it
          else includedImageGroups.push(g.group_index); // was not recommended, reviewer opted it in
        }
        edits.excluded_image_groups = excludedImageGroups;
        edits.included_image_groups = includedImageGroups;
        if (addedEntities.length > 0) edits.added_entities = addedEntities;
        if (clientEntitySurface) edits.client_entity_surface = clientEntitySurface;
        edits.masking_style = maskingStyle;
        if (Object.keys(aliases).length > 0) edits.entity_aliases = aliases;
        if (Object.keys(merges).length > 0) edits.entity_merges = merges;
        // Phase 3: opt-IN surfaces for flag/keep-default entities.
        if (included.size > 0) edits.included_surfaces = Array.from(included);
        if (Object.keys(consentUpdates).length > 0) edits.consent_updates = consentUpdates;
        if (p.discusses_negative_outcome) edits.negative_outcome_resolution = negativeOutcomeResolution;
      } else if (detail.agent_id === "tagging") {
        edits.removed_tags = (p.tags || [])
          .filter((t) => removed.has(`${t.category}:${t.value}`))
          .map((t) => ({ category: t.category, value: t.value }));
      }
      const edited =
        removed.size > 0 ||
        included.size > 0 ||
        Object.keys(consentUpdates).length > 0 ||
        imageOverrides.size > 0 ||
        addedEntities.length > 0 ||
        Boolean(clientEntitySurface) ||
        Object.keys(aliases).length > 0 ||
        Object.keys(merges).length > 0 ||
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

  if (error && !detail) return <p className="error-text">{error}</p>;
  if (!detail)
    return (
      <div className="loading-state">
        <span className="spinner" /> Loading proposal…
      </div>
    );

  const p = detail.proposal || {};
  const documentId = p.document_id;
  const images = p.images || [];
  // Every proposed surface (agent-proposed + reviewer-added), for the
  // "Merge into" dropdown - a surface can only merge into ANOTHER surface
  // actually present in this run's proposal.
  const allSurfaces = [...(p.entities || []).map((e) => e.surface_text), ...addedEntities.map((e) => e.surface_text)];
  const edited =
    removed.size > 0 ||
    included.size > 0 ||
    Object.keys(consentUpdates).length > 0 ||
    imageOverrides.size > 0 ||
    addedEntities.length > 0 ||
    Boolean(clientEntitySurface) ||
    Object.keys(aliases).length > 0 ||
    Object.keys(merges).length > 0;
  // Phase 3: a non-dismissible gate, not just a callout - Approve/Reject
  // stay enabled (rejecting doesn't need a resolution), but Approve is
  // disabled until the reviewer picks one, mirroring the server-side
  // enforcement in review/routes.py's submit_review.
  const outcomeResolutionMissing = Boolean(p.discusses_negative_outcome) && !negativeOutcomeResolution;

  return (
    <div>
      <div className="page-head">
        <div className="page-head__text">
          <h1 style={{ textTransform: "capitalize" }}>Review: {detail.agent_id}</h1>
          <p className="page-head__sub">{detail.summary}</p>
        </div>
      </div>

      {detail.status !== "awaiting_review" && (
        <div className="callout">This run is now "{detail.status}" — it may already have been reviewed.</div>
      )}
      {error && <p className="error-text">{error}</p>}
      <FlagList flags={detail.flags} />

      {detail.agent_id === "sanitization" && p.discusses_negative_outcome && (
        <div className="card section" style={{ borderColor: "var(--bad-fg)", borderWidth: "2px" }}>
          <h3 className="card__title" style={{ color: "var(--bad-fg)" }}>⚠ Sensitive outcome detected</h3>
          <p className="card__sub">
            {p.negative_outcome_summary || "This document discusses a failure, breach, negative metric, or regulatory finding."}
          </p>
          {p.negative_outcome_excerpts && p.negative_outcome_excerpts.length > 0 && (
            <ul style={{ fontSize: "0.88rem", color: "var(--ink-soft)" }}>
              {p.negative_outcome_excerpts.map((ex, i) => (
                <li key={i}>"{ex}"</li>
              ))}
            </ul>
          )}
          <p className="card__sub" style={{ marginTop: "0.5rem" }}>
            You must resolve this before approving or editing this run:
          </p>
          <div className="agent-grid">
            {OUTCOME_RESOLUTIONS.map((r) => (
              <label
                key={r.value}
                className="agent-card"
                style={{
                  display: "block",
                  cursor: "pointer",
                  borderColor: negativeOutcomeResolution === r.value ? "var(--brand-500)" : undefined,
                  boxShadow: negativeOutcomeResolution === r.value ? "0 0 0 3px var(--brand-100)" : undefined,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                  <input
                    type="radio"
                    name="negative-outcome-resolution"
                    value={r.value}
                    checked={negativeOutcomeResolution === r.value}
                    onChange={() => setNegativeOutcomeResolution(r.value)}
                  />
                  <strong>{r.label}</strong>
                </div>
                <p className="agent-card__meta" style={{ margin: "0.35rem 0 0" }}>{r.hint}</p>
              </label>
            ))}
          </div>
        </div>
      )}

      {detail.agent_id === "sanitization" && (
        <>
          <div className="card section">
            <h3 className="card__title">Sanitization style</h3>
            <p className="card__sub">How masked text is rendered in the output document.</p>
            <div className="agent-grid">
              {MASKING_STYLES.map((s) => (
                <label
                  key={s.value}
                  className="agent-card"
                  style={{
                    display: "block",
                    cursor: "pointer",
                    borderColor: maskingStyle === s.value ? "var(--brand-500)" : undefined,
                    boxShadow: maskingStyle === s.value ? "0 0 0 3px var(--brand-100)" : undefined,
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
                    e.g. <code>{s.example}</code> · {s.hint}
                  </p>
                </label>
              ))}
            </div>
          </div>

          <div className="card section">
            <h3 className="card__title">Proposed masks ({(p.entities || []).length + addedEntities.length})</h3>
            <p className="card__sub">
              Untick a masked-by-default entity to exclude it; tick a flagged (not-masked-by-default) one — commercial
              terms, competitors, strategy, own-cost detail, org charts — to include it instead. Add any the agent
              missed, and mark which one is the client — that's the only entity linked to a client account. Set an
              alias to replace the token with a chosen name everywhere (validated before it's applied); merge a
              duplicate spelling ("J&amp;J") into its canonical entity so they share one token/alias instead of two.
            </p>
            <div className="table-scroll">
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
                    <th>Consent</th>
                    <th>Client?</th>
                    <th>Alias</th>
                    <th>Merge into</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {(p.entities || []).map((e, i) => {
                    const isMandatory = MANDATORY_ENTITY_TYPES.has(e.entity_type);
                    // Phase 3: the entity's own default_action from the
                    // proposal is the single source of truth for which
                    // opt-in/opt-out set drives its checkbox - see
                    // agent.py's entity_actions.resolve_default_action.
                    const isFlagDefault = e.default_action === "flag" || e.default_action === "keep";
                    const isInternalTeamMember = e.entity_type === "INTERNAL_TEAM_MEMBER";
                    // Unified "is this row currently excluded from masking"
                    // signal, spanning both opt-out (mask-default) and
                    // opt-in (flag-default) semantics - everything below
                    // (client radio, alias, merge) should disable the same
                    // way regardless of which default this entity has.
                    const isExcluded = isMandatory ? false : isFlagDefault ? !included.has(e.surface_text) : removed.has(e.surface_text);
                    return (
                    <tr key={i} style={isExcluded ? { opacity: 0.45 } : undefined}>
                      <td><code>{e.mask_token || `[new ${e.entity_type}]`}</code></td>
                      <td style={{ fontWeight: 550 }}>{e.surface_text}</td>
                      <td className="agent-card__meta">
                        {e.entity_type}{isMandatory ? " (mandatory)" : isFlagDefault ? " (flagged)" : ""}
                      </td>
                      <td>{Math.round((e.confidence ?? 0) * 100)}%</td>
                      <td>{e.occurrences}</td>
                      <td>
                        <span className={`chip ${e.known ? "" : ""}`}>{e.known ? "known" : "new"}</span>
                      </td>
                      <td>
                        <input
                          type="checkbox"
                          checked={isMandatory || (isFlagDefault ? included.has(e.surface_text) : !removed.has(e.surface_text))}
                          disabled={isMandatory}
                          title={isMandatory ? "CREDENTIAL is masked unconditionally and cannot be excluded" : undefined}
                          onChange={() => (isFlagDefault ? toggleInclude(e.surface_text) : toggle(e.surface_text))}
                        />
                      </td>
                      <td>
                        {isInternalTeamMember ? (
                          <select
                            value={consentUpdates[e.surface_text] || ""}
                            onChange={(ev) => setConsent(e.surface_text, ev.target.value)}
                            style={{ width: "120px" }}
                          >
                            <option value="">(unset)</option>
                            <option value="pending">pending</option>
                            <option value="granted">granted</option>
                            <option value="not_required">not required</option>
                          </select>
                        ) : (
                          <span className="agent-card__meta">—</span>
                        )}
                      </td>
                      <td>
                        <input
                          type="radio"
                          name="client-entity"
                          disabled={isExcluded}
                          checked={clientEntitySurface.toLowerCase() === e.surface_text.toLowerCase()}
                          onChange={() => setClientEntitySurface(e.surface_text)}
                        />
                      </td>
                      <td>
                        <input
                          type="text"
                          placeholder="e.g. Acme Pharma"
                          value={aliases[e.surface_text] || ""}
                          disabled={isExcluded || Boolean(merges[e.surface_text])}
                          onChange={(ev) => setAlias(e.surface_text, ev.target.value)}
                          style={{ width: "140px" }}
                        />
                      </td>
                      <td>
                        <select
                          value={merges[e.surface_text] || ""}
                          disabled={isExcluded}
                          onChange={(ev) => setMerge(e.surface_text, ev.target.value)}
                          style={{ width: "150px" }}
                        >
                          <option value="">(none)</option>
                          {allSurfaces
                            .filter((s) => s.toLowerCase() !== e.surface_text.toLowerCase())
                            .map((s) => (
                              <option key={s} value={s}>
                                {s}
                              </option>
                            ))}
                        </select>
                      </td>
                      <td />
                    </tr>
                    );
                  })}
                  {addedEntities.map((e, i) => (
                    <tr key={`added-${i}`}>
                      <td><code>[new {e.entity_type}]</code></td>
                      <td style={{ fontWeight: 550 }}>{e.surface_text}</td>
                      <td className="agent-card__meta">{e.entity_type}</td>
                      <td>—</td>
                      <td>—</td>
                      <td><span className="chip">reviewer-added</span></td>
                      <td>
                        <input type="checkbox" checked disabled />
                      </td>
                      <td>
                        <span className="agent-card__meta">—</span>
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
                        <input
                          type="text"
                          placeholder="e.g. Acme Pharma"
                          value={aliases[e.surface_text] || ""}
                          disabled={Boolean(merges[e.surface_text])}
                          onChange={(ev) => setAlias(e.surface_text, ev.target.value)}
                          style={{ width: "140px" }}
                        />
                      </td>
                      <td>
                        <select
                          value={merges[e.surface_text] || ""}
                          onChange={(ev) => setMerge(e.surface_text, ev.target.value)}
                          style={{ width: "150px" }}
                        >
                          <option value="">(none)</option>
                          {allSurfaces
                            .filter((s) => s.toLowerCase() !== e.surface_text.toLowerCase())
                            .map((s) => (
                              <option key={s} value={s}>
                                {s}
                              </option>
                            ))}
                        </select>
                      </td>
                      <td>
                        <button type="button" className="btn--ghost btn--sm" onClick={() => removeAddedEntity(e.surface_text)}>
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginTop: "0.9rem", flexWrap: "wrap" }}>
              <input
                type="text"
                placeholder="Surface text the agent missed (e.g. a client name)"
                value={newSurface}
                onChange={(e) => setNewSurface(e.target.value)}
                style={{ flex: 1, minWidth: "220px" }}
              />
              <select value={newType} onChange={(e) => setNewType(e.target.value)}>
                {ENTITY_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
              <button type="button" className="btn--subtle" onClick={addEntity} disabled={!newSurface.trim()}>
                Add entity
              </button>
            </div>
          </div>

          {(p.excluded_entities || []).length > 0 && (
            <div className="card section">
              <h3 className="card__title">Excluded candidates ({p.excluded_entities.length})</h3>
              <p className="card__sub">
                Below the confidence bar, so left out of the mask list above by default — often true for a name that
                only appears once. Tick "Include" for any that are real; this adds it exactly like a manually-typed
                entity above.
              </p>
              <div className="table-scroll">
                <table className="run-table">
                  <thead>
                    <tr>
                      <th>Surface</th>
                      <th>Type</th>
                      <th>Conf.</th>
                      <th>Occurrences</th>
                      <th>Include</th>
                    </tr>
                  </thead>
                  <tbody>
                    {p.excluded_entities.map((e, i) => (
                      <tr key={`excluded-${i}`}>
                        <td style={{ fontWeight: 550 }}>{e.surface_text}</td>
                        <td className="agent-card__meta">{e.entity_type}</td>
                        <td>{Math.round((e.confidence ?? 0) * 100)}%</td>
                        <td>{e.occurrences}</td>
                        <td>
                          <input type="checkbox" checked={isIncluded(e.surface_text)} onChange={() => toggleExcluded(e)} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div className="card section">
            <h3 className="card__title">
              Embedded images ({images.length}
              {p.images_skipped ? `, ${p.images_skipped} not scanned` : ""})
            </h3>
            <p className="card__sub">
              Logos, screenshots, and other pixel content are scanned separately from text — check each one and confirm
              which should be blacked out.
            </p>
            {images.length === 0 && <p className="agent-card__meta">No embedded images found.</p>}
            <div className="agent-grid">
              {images.map((g) => {
                const borderColor = g.mandatory_redaction
                  ? "#fca5a5"
                  : g.needs_human_judgment
                    ? "#fcd34d"
                    : g.contains_client_identity
                      ? "#fda4af"
                      : g.contains_real_data_sample
                        ? "#fcd34d"
                        : undefined;
                return (
                  <div key={g.group_index} className="agent-card" style={{ borderColor }}>
                    {documentId && (
                      <div style={{ background: "var(--ink-100)", borderRadius: "8px", padding: "0.4rem", marginBottom: "0.6rem", textAlign: "center" }}>
                        <AuthImage
                          src={`/documents/${documentId}/images/${g.sample_index}`}
                          alt={g.description}
                          style={{ maxWidth: "100%", maxHeight: "150px", objectFit: "contain" }}
                        />
                      </div>
                    )}
                    <p style={{ margin: "0 0 0.35rem", fontSize: "0.85rem" }}>{g.description || "(no description)"}</p>
                    <p className="agent-card__meta">
                      {g.locations.join(", ")} · {g.occurrence_count} occurrence(s) · conf. {Math.round((g.confidence ?? 0) * 100)}%
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
                      <p style={{ margin: "0.4rem 0 0", color: "var(--warn-fg)", fontSize: "0.8rem" }}>
                        ⚠ Uncertain signal — stylized font, low-contrast mark, or borderline logo similarity. Please inspect
                        manually.
                      </p>
                    )}
                    {g.contains_real_data_sample && (
                      <p style={{ margin: "0.4rem 0 0", color: "var(--warn-fg)", fontSize: "0.8rem" }}>
                        📊 Looks like real (non-synthetic) data — consider a synthetic/representative replacement.
                      </p>
                    )}
                    {g.sensitive_text_matches && g.sensitive_text_matches.length > 0 && (
                      <p style={{ margin: "0.4rem 0 0", color: "var(--bad-fg)", fontSize: "0.8rem" }}>
                        🔒 Text in image: {g.sensitive_text_matches.map((m) => `${m.entity_type} "${m.surface_text}"`).join(", ")}
                      </p>
                    )}
                    {g.mandatory_redaction && (
                      <p style={{ margin: "0.4rem 0 0", color: "var(--bad-fg)", fontSize: "0.8rem", fontWeight: 600 }}>
                        Locked: {g.logo_match_token
                          ? `confirmed match to an already-approved masked entity (${g.logo_match_token})`
                          : "contains a CREDENTIAL"} — always redacted, regardless of the description above.
                      </p>
                    )}
                    <label
                      style={{
                        display: "flex",
                        flexDirection: "row",
                        alignItems: "center",
                        gap: "0.45rem",
                        marginTop: "0.6rem",
                        fontWeight: 600,
                        fontSize: "0.84rem",
                      }}
                    >
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
          </div>
        </>
      )}

      {detail.agent_id === "tagging" && (
        <div className="card section">
          <h3 className="card__title">Proposed tags ({(p.tags || []).length})</h3>
          <p className="card__sub">Untick a tag to exclude it. New terms stay pending governance and are not applied.</p>
          <div className="table-scroll">
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
                    <tr key={i} style={removed.has(key) ? { opacity: 0.45 } : undefined}>
                      <td className="agent-card__meta">{t.category}</td>
                      <td style={{ fontWeight: 550 }}>{t.value}</td>
                      <td>{Math.round((t.confidence ?? 0) * 100)}%</td>
                      <td>
                        <span className="chip">{t.status.replace(/_/g, " ")}</span>
                      </td>
                      <td>
                        <input type="checkbox" checked={!removed.has(key)} onChange={() => toggle(key)} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card section">
        <h3 className="card__title">Decision</h3>
        <label>
          Notes (optional)
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} />
        </label>
        {outcomeResolutionMissing && (
          <p className="error-text" style={{ marginTop: "0.5rem" }}>
            Resolve the sensitive outcome above before approving.
          </p>
        )}
        <div style={{ display: "flex", gap: "0.75rem", marginTop: "0.9rem" }}>
          <button onClick={() => decide("approved")} disabled={busy || outcomeResolutionMissing}>
            {busy ? "Submitting…" : `Approve${edited ? " (edited)" : ""}`}
          </button>
          <button className="btn--danger" onClick={() => decide("rejected")} disabled={busy}>
            Reject
          </button>
        </div>
      </div>

      <div className="section">
        <h3>Steps</h3>
        <StepTimeline steps={detail.steps} />
      </div>
    </div>
  );
}
