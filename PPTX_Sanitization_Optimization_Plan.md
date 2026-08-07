# PPTX Sanitization Agent — Optimization Plan (v2, code-grounded)

**Scope:** PPTX only, per current instruction. DOCX/XLSX/PDF share some of these modules (`dictionary.py`, `render.py`, `comment_scan.py`/`comment_scrub.py`, `verify.py`) but are explicitly out of scope for this pass — noted inline only where a fix is shared code and the PPTX-only framing matters for sequencing.
**Method:** every item below was checked against the current tree on `sanitization-updates` (not just the original 7-slide deck's assertions). File/line references are real, read on 2026-07-17. Items already resolved by the **pending, uncommitted diff** on this branch are marked ✅ done and excluded from the work plan so effort isn't spent re-fixing them.

---

## 0. Already shipped in the working tree (don't re-do)

The original improvement-plan doc was written against a slightly older snapshot. Several of its own P1/P2 items are already implemented in the uncommitted changes on this branch:

| Item | Where | Status |
|---|---|---|
| Speaker notes were invisible to extraction/detection/verification | `documents/extract.py:69-86` — notes now appended as a `kind="notes"` chunk | ✅ done |
| Layout/master **image** branding undercounted (2 occurrences instead of 21) | `documents/images.py:250-294` (`_show_master_sp`, per-slide layout/master `ImageRef` emission) | ✅ done |
| Detector tool-call budget too tight for large decks | `agents/sanitization/detector.py:83` — raised `16 → 32` | ✅ done |
| Image-channel occurrences reported as 0 when a name only appears on a logo | `agents/sanitization/agent.py:220-292` (`image_occurrences_by_key`) | ✅ done |
| Below-threshold candidates vanished with no recovery path | `agents/sanitization/agent.py` `excluded_entities` + `ReviewDetailPage.jsx` checkbox UI | ✅ done |
| Logo phash sensitive to background fill/transparency | `masking/logo_reference.py` `_normalize_for_hash` | ✅ done |
| Bedrock vision rejecting valid-looking images (CMYK JPEG, 16-bit PNG, BMP/TIFF) | `agents/sanitization/image_scan.py` `_normalize_for_vision`, `_raster_bytes` | ✅ done |
| Duplicate vision calls / fragmented cards for one logo across renditions | `agents/sanitization/image_scan.py` `_merge_visually_similar_groups` | ✅ done |
| Model occasionally emitting two fenced JSON blocks, breaking `_extract_json` | `llm/bedrock_client.py` `_extract_json` | ✅ done |

This plan covers what's **left**, prioritized the same way as the original doc (P0 = produces a false "clean" result today) but re-sequenced against what's already landed.

---

## 1. P0 — Close the chart / SmartArt / OLE text blind spot

**Confirmed still open.** `documents/extract.py:47-87` (`_extract_pptx`) walks `iter_shapes_recursive(slide.shapes)` and only branches on `shape.has_text_frame` (58-63) and `shape.has_table` (64-65). Grepping `app/documents/` for chart/graphicFrame/SmartArt/diagram/OLE returns zero matches — there is no code path that reads a chart's category labels, a SmartArt node's text, or an OLE object's display name. Because **every consumer shares this one function** — `agents/sanitization/tools.py`'s `fs_read_document` (LLM detector's only view of the document), the NER pre-pass, `dictionary.find_in_text`, and `documents/verify.py:19` (`find_residual_surfaces` calls the identical `extract_chunks`) — this is a genuine "false clean": a client name in a chart axis label or SmartArt node is invisible to detection **and** to the post-render check that's supposed to catch misses. Nothing downstream can catch what extraction never saw.

Also confirmed: `documents/images.py`'s `_label_for_partname` (~92-96) already recognizes `ppt/diagrams/*` (SmartArt) as an image label via the raw-media-glob layer — so a SmartArt node currently surfaces only as its static preview bitmap (Layer 2 glob), never as text. That's a partial, accidental mitigation, not a fix: OCR on the SmartArt preview image may catch some of the text, but bypasses the dictionary/masking/verification text path entirely and won't catch text that's present in the data model but rendered too small/stylized for OCR.

### Build

1. **New extraction sub-pass in `extract.py`**, run alongside `_extract_pptx`'s existing per-slide loop (reuses the same `Presentation`/slide iteration, so this is additive, not a second file walk):
   - Chart: for each slide, resolve the slide's `.rels` (via `slide.part.rels`, already exposed by python-pptx — no raw zip work needed) and follow any relationship whose `reltype` ends in `/chart`. Parse the linked `ChartPart`'s XML (`chart_part.chart._chartSpace` or a raw `lxml` parse of `chart_part.blob`) and pull text from `c:tx` (series names — handle both literal `c:v` and `c:strRef/c:strCache`), `c:cat//c:pt/c:v` (category labels), and record (don't discard) `c:f` formula strings as a lower-priority candidate, since a cell reference can embed a client/sheet name.
   - SmartArt: follow the `.../diagramData` relationship to the linked `data1.xml` part; parse `dgm:t//a:p//a:t` runs. Same shape as a text-frame paragraph — feed it through the same paragraph-join logic already used at `extract.py:60-63`.
   - OLE: follow `.../oleObject` relationships; for the inline case, `p:oleObj`'s own `name`/`spid` attributes carry a visible display title even when the embedded payload isn't parsed further — surface at least that. If the embedded part parses as a zip (Office Open XML), recursing into `extract_chunks` on the embedded bytes is the cheap win; if it's OLE Compound Binary (`D0CF11E0` magic — same check pattern `images.py`'s format-sniffing already established), treat as unparsed and flag for human review rather than silently skipping.
2. Tag new chunks `kind="chart"` / `kind="smartart"` / `kind="ole"` (matches the existing `kind="notes"` convention just added).
3. **Masking**: SmartArt reuses the existing run-level regex substitution (it's structurally a paragraph). Charts need `chart.replace_data(...)`-style handling in `render.py`, not a raw string swap against cached XML — python-pptx's chart data replacement rewrites both the XML cache and the embedded worksheet; a direct regex edit only fixes what PowerPoint shows until someone clicks "Edit Data" on the chart, which then reveals the original unmasked value.
4. **Verification**: `verify.py:19` already re-runs `extract_chunks` on the rendered file, so once step 1 lands, chart/SmartArt/OLE text is automatically covered by verification with zero changes to `verify.py` — this is the concrete payoff of fixing extraction rather than bolting on a parallel check.

**Effort:** genuinely bounded — three new parsers plus one masking branch, since `apply_masks.py`'s and `verify.py`'s logic don't change, only what's fed into them.

---

## 2. P0 — Modern (threaded) PowerPoint comments

**Confirmed still open, on both sides.** `documents/comment_scan.py:64-73` (`_scan_pptx`) matches only `ppt/comments/comment\d*\.xml$`; `documents/comment_scrub.py:64-72` (`_PART_RULES`) has the identical single pattern for scrubbing. Both docstrings say so explicitly (`comment_scan.py:10-13`, `comment_scrub.py:14-16`) — this is a known, named gap, not an oversight. `ppt/commentThreads/*.xml` (+ `ppt/commentAuthors.xml`, itself PII — author display names) is absent from both files. Given Microsoft's rollout of modern comments as default-on for managed M365 tenants, this is the highest-value fix left after the chart/SmartArt/OLE item, because a comment is content a reviewer sees directly in the Comments pane — a much shorter path to an actual leak than a rarely-clicked chart data table.

### Build

- Add a `ppt/commentThreads/comment\d*\.xml` pattern to `_scan_pptx` in `comment_scan.py`, reading modern comments' text (schema uses `p:text` runs inside `p:cm` elements, same tag name as legacy so `_part_text`'s generic `el.text` walk at line 37 likely already extracts it correctly once the filename pattern matches — verify against a real modern-comments PPTX before assuming zero XML-shape changes needed).
- Mirror the same pattern addition into `comment_scrub.py`'s `_PART_RULES` (line 64-72), and add `ppt/commentAuthors.xml` to both scan and scrub (`dc:cxr` or display-name element scrubbing — author names are PII on their own).
- Detect both schemas per file rather than branching on one (a file round-tripped through both comment systems can carry both parts simultaneously) — don't assume "if modern exists, skip legacy."
- Update the now-stale docstrings in both files once implemented (small, bundle with this change rather than a separate cleanup PR).

---

## 3. P1 — Run-boundary-aware masking (fixes a real, visible formatting regression today)

**Confirmed still open, exactly as described.** `documents/render.py:35-49` (`_mask_paragraph_runs`): joins all run text (38), regex-replaces the joined string (41), then **collapses**: `p.runs[0].text = masked; for run in p.runs[1:]: run.text = ""` (46-48). Any run past the first in a paragraph that contains a masked surface loses its formatting — the textbook python-pptx "rebuild into run 0" pitfall. This fires for PPTX (`_pptx_mask_table` at ~143-148 calls the same function per cell paragraph) and DOCX identically, since it's one shared function — but the fix is scoped to PPTX behavior here per the current ask; the DOCX call site benefits for free since it's the same code.

### Build

1. Build a flat `(run_index, local_offset)` map across a paragraph's runs.
2. Match against the concatenated text (unchanged — this is what correctly lets a surface span two runs, which is the reason the collapse-and-rebuild existed in the first place).
3. For a match entirely inside one run: substring-replace that run only. Zero blast radius on every other run.
4. For a match spanning runs: split only the boundary runs at the match edges (new `r` elements cloned from the original's `rPr` via `copy.deepcopy` on the `rPr` XML element, not the whole run), replace text only in the runs strictly inside the span.
5. Replace the body of `_mask_paragraph_runs` (`render.py:35-49`) with this; `_replace_text` (20-32) stays as the string-level matcher, only the run-application changes.

**Why P1 not P0:** this is a fidelity/formatting bug, not a leak — the client name still gets masked, it just take the paragraph's other runs' bold/italic/color with it. Real user-visible pain, but strictly lower severity than P0's false-clean risk.

---

## 4. P1 — Unify the two masking implementations

**Confirmed still open.** `apply_masks.py` (chunk-text, used for the preview/`.txt` channel and `MaskingOccurrence` rows) and `render.py:20-49` (regex directly against python-pptx/DOCX runs) are two independent implementations of "longest-surface-first, case-insensitive, word-boundary substitution." They agree today by convention, not by shared code — `_replace_text` in `render.py:20-32` and `apply_masks.py`'s span-finding are separately written and could silently drift the next time either is touched (e.g. this branch's own `agent.py` diff already had to special-case `_count_occurrences` to keep it in sync with `surface_pattern`, which is exactly the kind of drift this predicts).

### Build

- Extract the **conflict-resolution/span-claiming logic** (longest-match-first, non-overlapping-claim) into one function: `(text: str, spans_or_matches) -> resolved_replacement_plan`. Both `apply_masks.py` and `render.py` call it; they differ only in whether they materialize the plan into a flat string or into `p:r` run edits (which becomes the natural integration point for the run-boundary rewrite in item 3 — do these two together, not sequentially, since the shared plan object is exactly what the run-boundary code needs to know which spans fall in which run).
- Add a regression test that fuzzes overlapping/nested candidate spans and asserts identical resolved spans from both call sites — turns "these two happen to agree" into a CI-enforced invariant.

---

## 5. P2 — Dictionary containment + normalization gaps

Two independent, low-risk fixes in `masking/dictionary.py`, confirmed as described:

- **`is_own_firm` (lines 36-42)**: does `name in key` containment after stripping to `[a-z0-9]` — by design (docstring explains a prefix match would over-exclude), but pure containment means a firm name that's a substring of an unrelated real client name will still false-exclude it (e.g. an own-firm name "Spend" would wrongly suppress a client called "Spendly Inc" — same failure mode as before, just moved from prefix to containment). Fix: tokenize both sides and require a token-level match, not raw substring containment. `find_in_text` (line 52-72) already uses `surface_pattern` (word-boundary regex) for the actual text sweep — reuse that same boundary logic here instead of ad-hoc character stripping.
- **`normalize` (lines 28-33)**: no Unicode normalization at all — `re.sub(r"[^\w@.\s-]", "", v)` at line 31 operates on whatever code points are already there. Two different Unicode representations of the same visible name (precomposed `é` vs. combining `e` + `´`) currently produce two different dictionary keys, silently splitting one entity's aliases. Fix: `unicodedata.normalize("NFKC", v)` plus a diacritic-fold at the top of `normalize`, before the existing regex strip — keep the **display** string (in `MaskingAlias.raw_value`) exactly as entered; only the lookup key changes. Contained entirely to this one function; doesn't touch masking or verification.

---

## 6. P2 — Layout/master **text** placeholders

Images already got this treatment (`images.py:250-294`, item in section 0). Text placeholders inherited from `slideLayoutN.xml`/`slideMasterN.xml` (a footer, a confidentiality line, a template-level client name placeholder) are not currently walked by `extract.py:_extract_pptx`, which only iterates `slide.shapes` (line 58) — layout/master shapes reachable via `slide.slide_layout`/`slide_layout.slide_master` are never visited for text, only for images. Same asymmetry the original doc flagged, now half-closed.

### Build

- Extend `_extract_pptx` to also walk `slide.slide_layout.shapes` and `slide.slide_layout.slide_master.shapes` for text frames/tables, **gated by the same `_show_master_sp` check `images.py` already implements** (`images.py:217-226`) so a slide with "hide background graphics" doesn't get phantom text occurrences credited to it — reuse that function directly rather than reimplementing the `showMasterSp` attribute read.
- Tag as `kind="layout-text"` / `kind="master-text"`, same convention as `kind="notes"`.
- Emit once per slide that actually shows it (mirroring the images.py fix's per-slide `ImageRef` emission), so occurrence counts stay accurate rather than under-reporting a template-level name that appears on every slide as a single hit.

---

## 7. P3 — Performance / cost-specific items (the literal "performance" angle)

These aren't leak-risk gaps — they're runtime/cost efficiency, worth calling out separately since sanitization runs synchronously against Bedrock per document:

- **`masking/logo_reference.py:89` (`find_matches`)**: `db.query(LogoReference).all()` — full table scan, O(n) Hamming-distance comparisons per image, every image, every run. Fine at current scale (small reference set); once the reference set grows past low hundreds (every approved redaction auto-adds a reference via `store_reference`, `agent.py` `apply()` line ~614-624), this becomes the dominant per-image cost in `image_scan.py`. Cheapest fix: cache the full reference set in memory per request/run instead of re-querying per image (currently likely called once per image group already, but confirm no N+1 across groups); a real fix (LSH/BK-tree over phash) is only worth it once the reference table is actually large — don't build it preemptively.
- **Detector budget formula** (`detector.py:83`, `max(4, min(total_chunks + 3, 32))`): once the P0 chart/SmartArt/OLE extraction work lands, `total_chunks` rises for chart-heavy decks (each chart/SmartArt/OLE part becomes its own chunk per section 1). Revisit the `+3`/`32` constants together with that rollout, not before — tuning against today's chunk distribution would just need re-tuning again immediately after.
- **`image_scan.py` `MAX_IMAGES_SCANNED = 150` / `PERCEPTUAL_DEDUP_THRESHOLD`**: already well-optimized by the pending diff (phash pre-clustering before spending a Bedrock vision call, plus post-scan `_merge_visually_similar_groups` to collapse near-duplicates after scanning). No further action — flagging only so it's not mistaken for an open item.
- **Bedrock JSON parsing retries** (`bedrock_client.py` `_extract_json`): the fenced-block fix already landed reduces silent detector failures that would otherwise cost a full re-run of `detect_entities` (an expensive multi-turn tool-use loop) just to recover from a parse miss. No further action needed here either.

---

## 8. Sequencing

| Phase | Work | Why this order |
|---|---|---|
| 1 | Chart/SmartArt/OLE extraction + masking + verification (§1) | Only remaining item producing a *false clean* result |
| 1 | Modern threaded comments, scan + scrub + author names (§2) | Direct-visibility leak path, increasingly the M365 default |
| 2 | Run-boundary-aware masking (§3) + unify masking implementations (§4) | Do together — the shared conflict-resolution plan from §4 is what §3's run-splitting needs to consume; real formatting bug affecting every render today |
| 3 | Dictionary tokenization + Unicode normalization (§5) | Small, independent, low-risk |
| 3 | Layout/master text placeholders (§6) | Direct extension of the images.py pattern already proven in this branch |
| 4 | Logo-match caching, detector budget re-tuning (§7) | Cheap, best done once §1 changes the chunk-count baseline it needs tuning against |

---

## 9. Validation per item

- **§1 (chart/SmartArt/OLE):** build one test fixture PPTX with a chart containing a client name in a category label, one with a client name in a SmartArt node, and confirm (a) it appears in `extract_chunks` output, (b) the LLM detector proposes it, (c) `apply()` masks it, (d) opening "Edit Data" on the rendered chart does *not* reveal the original value, (e) `find_residual_surfaces` reports clean.
- **§2 (modern comments):** fixture PPTX with a `ppt/commentThreads` comment (produced by a recent PowerPoint build or hand-crafted OOXML) containing a client name; confirm scan flags it pre-render and scrub removes it post-render.
- **§3/§4 (run-boundary + unify):** fixture paragraph with 3+ runs of different formatting (bold/italic/color) where the masked surface spans exactly runs 2-3; assert runs 1 and 4+ keep original formatting untouched after masking. Plus the fuzz test described in §4.
- **§5 (dictionary):** unit tests for `normalize("Café Corp") == normalize("Cafe Corp")` (NFKC+diacritic fold) and for `is_own_firm` no longer false-excluding a token-level non-match (e.g. own-firm name that's a raw substring of an unrelated client name).
- **§6 (layout/master text):** fixture deck with a template footer/confidentiality line carrying a client name on the master; confirm one `kind="master-text"` chunk, correct occurrence count respecting `showMasterSp`, and that masking + verification both reach it.
