"""Render a masked document in the SAME format the user uploaded.

Given the original file and the approved surface->mask-token map, produce a
sanitized copy: masked PDF for a PDF, masked DOCX for a DOCX, masked PPTX for a
PPTX, masked XLSX for an XLSX. Falls back to a .txt only for unknown types.
"""

import io
import os
import re

from app.config import get_settings
from app.documents.pptx_walk import iter_shapes_recursive
from app.masking.pattern import surface_pattern
from app.masking.style import DEFAULT_MASKING_STYLE, replacement_for

settings = get_settings()


def _replace_text(text: str, surface_to_token: dict[str, str], style: str = DEFAULT_MASKING_STYLE) -> str:
    """Case-insensitive, longest-surface-first replacement (mirrors apply_masks)."""
    if not text:
        return text
    out = text
    for surface in sorted(surface_to_token.keys(), key=len, reverse=True):
        out = re.sub(
            surface_pattern(surface),
            lambda m: replacement_for(m.group(0), surface_to_token[surface], style),
            out,
            flags=re.IGNORECASE,
        )
    return out


def _mask_paragraph_runs(p, surface_to_token: dict[str, str], style: str = DEFAULT_MASKING_STYLE) -> bool:
    """Shared by DOCX and PPTX: rewrite a paragraph's runs so the concatenated
    text is masked. Returns True if anything changed."""
    joined = "".join(run.text for run in p.runs)
    if not joined:
        return False
    masked = _replace_text(joined, surface_to_token, style)
    if masked == joined or not p.runs:
        return False
    # Put the whole masked text in the first run; clear the rest (keeps the
    # paragraph's leading formatting; surfaces often span runs anyway).
    p.runs[0].text = masked
    for run in p.runs[1:]:
        run.text = ""
    return True


# ---- DOCX ----
def _docx_mask_table(table, surface_to_token: dict[str, str], style: str) -> None:
    for row in table.rows:
        for cell in row.cells:
            for p in cell.paragraphs:
                _mask_paragraph_runs(p, surface_to_token, style)
            for nested in cell.tables:  # tables nested inside a cell
                _docx_mask_table(nested, surface_to_token, style)


def _render_docx(src: str, dst: str, surface_to_token: dict[str, str], style: str) -> None:
    import docx

    document = docx.Document(src)

    for p in document.paragraphs:
        _mask_paragraph_runs(p, surface_to_token, style)
    for table in document.tables:
        _docx_mask_table(table, surface_to_token, style)

    # Headers/footers - letterhead-style templates often carry the client
    # name here, and this was previously never touched at all.
    for section in document.sections:
        for part in (section.header, section.footer):
            for p in part.paragraphs:
                _mask_paragraph_runs(p, surface_to_token, style)
            for table in part.tables:
                _docx_mask_table(table, surface_to_token, style)

    document.save(dst)


# ---- PPTX ----
def _pptx_mask_table(table, surface_to_token: dict[str, str], style: str) -> None:
    for row in table.rows:
        for cell in row.cells:
            for p in cell.text_frame.paragraphs:
                _mask_paragraph_runs(p, surface_to_token, style)


def _render_pptx(src: str, dst: str, surface_to_token: dict[str, str], style: str) -> None:
    from pptx import Presentation

    prs = Presentation(src)

    for slide in prs.slides:
        # Recurse into grouped shapes - the same gap fixed in extract.py.
        # Without this, anything inside a PowerPoint "Group" (logo+label,
        # org charts, diagrams) is silently left unmasked in the output file
        # even though it may have been detected via the slide's full text.
        for shape in iter_shapes_recursive(slide.shapes):
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    _mask_paragraph_runs(p, surface_to_token, style)
            elif getattr(shape, "has_table", False):
                _pptx_mask_table(shape.table, surface_to_token, style)
        # Speaker notes can also carry client-identifying text.
        if slide.has_notes_slide:
            for p in slide.notes_slide.notes_text_frame.paragraphs:
                _mask_paragraph_runs(p, surface_to_token, style)

    prs.save(dst)


# ---- XLSX ----
def _render_xlsx(
    src: str, dst: str, surface_to_token: dict[str, str], style: str, approved_image_refs: list | None = None
) -> int:
    """Text masking AND image redaction happen in this ONE load->mutate->save
    pass, deliberately - openpyxl renumbers every image's media partname on
    every save(), so a partname captured from a separate, earlier extraction
    would not match this file after it's been rendered once (the exact
    silent-failure mode this whole feature exists to fix, just relocated to
    xlsx). Matching is by raw byte content instead: ws._images[i]._data()
    returns the same bytes as the source zip's media part for any format
    openpyxl can load, since neither loading nor this in-place mutation
    involves a save cycle beforehand. Returns images_redacted count."""
    import openpyxl

    wb = openpyxl.load_workbook(src)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value:
                    masked = _replace_text(cell.value, surface_to_token, style)
                    if masked != cell.value:
                        cell.value = masked

    images_redacted = 0
    if approved_image_refs:
        from app.documents.image_redact import placeholder_png

        approved_bytes = {ref.image_bytes for ref in approved_image_refs if ref.locator.get("kind") == "xlsx"}
        for ws in wb.worksheets:
            for im in ws._images:
                try:
                    # .getvalue() reads the buffer without consuming/closing it -
                    # Image._data() does close its underlying stream as a side
                    # effect, which would corrupt this image at save time if we
                    # called it here just to compare bytes.
                    data = im.ref.getvalue()
                except Exception:
                    continue
                if data in approved_bytes:
                    im.ref = io.BytesIO(placeholder_png(im.width, im.height))
                    im.format = "png"
                    images_redacted += 1

    wb.save(dst)
    return images_redacted


# ---- PDF (redaction) ----
def _is_isolated_match(page, rect, surface: str) -> bool:
    """page.search_for() is a plain substring search - it would happily match
    "RIA" inside "Variance". Grab a couple points of surrounding context
    around the matched rect and confirm the surface actually sits at a word
    boundary there, the same check the OOXML paths get via regex \\b."""
    import fitz

    pad = 2
    expanded = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
    context = page.get_textbox(expanded)
    return re.search(surface_pattern(surface), context, flags=re.IGNORECASE) is not None


def _render_pdf(src: str, dst: str, surface_to_token: dict[str, str], style: str) -> None:
    import fitz  # PyMuPDF

    doc = fitz.open(src)
    for page in doc:
        # Longest first so "Acme Corp" is redacted before "Acme".
        for surface in sorted(surface_to_token.keys(), key=len, reverse=True):
            token = surface_to_token[surface]
            for rect in page.search_for(surface, quads=False):
                if not _is_isolated_match(page, rect, surface):
                    continue
                if style == "black":
                    # Solid black redaction bar - no replacement text, nothing readable survives.
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                elif style == "remove":
                    # Blank the region out entirely - no marker left behind.
                    page.add_redact_annot(rect, fill=(1, 1, 1))
                else:
                    # White-out the original text and overlay the traceable mask token.
                    page.add_redact_annot(rect, text=token, fill=(1, 1, 1), text_color=(0, 0, 0), fontsize=8)
        page.apply_redactions()
    doc.save(dst, garbage=4, deflate=True)
    doc.close()


def render_masked_document(
    run_id: str,
    src_path: str,
    content_type: str,
    filename: str,
    surface_to_token: dict[str, str],
    style: str = DEFAULT_MASKING_STYLE,
    approved_image_refs: list | None = None,
) -> tuple[str, int]:
    """Produce a masked copy in the original format. Returns (path,
    xlsx_images_redacted) - the second element is only ever non-zero for
    xlsx, where image redaction must happen in this same pass (see
    _render_xlsx); every other format redacts images separately, after this
    call, via image_redact.redact_images()."""
    out_dir = os.path.join(settings.OUTPUTS_DIR, "sanitization")
    os.makedirs(out_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(filename))
    ext = ext.lower()
    dst = os.path.join(out_dir, f"{run_id}__{stem}.sanitized{ext or '.txt'}")

    xlsx_images_redacted = 0
    lower = filename.lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        _render_pdf(src_path, dst, surface_to_token, style)
    elif lower.endswith(".docx") or "wordprocessingml" in content_type:
        _render_docx(src_path, dst, surface_to_token, style)
    elif lower.endswith(".pptx") or "presentationml" in content_type:
        _render_pptx(src_path, dst, surface_to_token, style)
    elif lower.endswith(".xlsx") or "spreadsheetml" in content_type:
        xlsx_images_redacted = _render_xlsx(src_path, dst, surface_to_token, style, approved_image_refs)
    else:
        with open(dst, "w") as f:
            f.write("\n".join(f"{k} -> {v}" for k, v in surface_to_token.items()))
    return dst, xlsx_images_redacted
