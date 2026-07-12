"""Apply reviewer-approved image redactions to the ALREADY text-masked document.

DOCX/PPTX: overwrite the image's bytes inside the OOXML zip package with a
solid placeholder (frame size in the doc is governed by drawing extents, not
the image's intrinsic pixel size, so this doesn't distort layout).

PDF: draw a redaction annotation over the image's rect(s) and apply it -
PyMuPDF's apply_redactions() strips everything (text AND image) inside the
rect, the same primitive already used for text redaction in render.py.
"""

import io
import shutil
import zipfile

from app.documents.images import ImageRef

PLACEHOLDER_LABEL = "REDACTED"


def placeholder_png(width: int, height: int, label: str = PLACEHOLDER_LABEL) -> bytes:
    from PIL import Image, ImageDraw

    w, h = max(int(width), 1), max(int(height), 1)
    img = Image.new("RGB", (w, h), color=(60, 60, 60))
    draw = ImageDraw.Draw(img)
    try:
        text_w, text_h = draw.textsize(label)
    except AttributeError:
        bbox = draw.textbbox((0, 0), label)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if w >= text_w + 4 and h >= text_h + 4:
        draw.text(((w - text_w) / 2, (h - text_h) / 2), label, fill=(230, 230, 230))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _redact_zip_package(doc_path: str, partnames: set[str]) -> None:
    """Overwrite the given media partnames in-place inside a DOCX/PPTX zip."""
    if not partnames:
        return
    tmp_path = doc_path + ".tmp"
    with zipfile.ZipFile(doc_path, "r") as zin:
        original_sizes: dict[str, tuple[int, int]] = {}
        for name in partnames:
            try:
                data = zin.read(name)
                from app.documents.images import image_dimensions

                original_sizes[name] = image_dimensions(data)
            except Exception:
                original_sizes[name] = (200, 150)

        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename in partnames:
                    w, h = original_sizes[item.filename]
                    zout.writestr(item, placeholder_png(w, h))
                else:
                    zout.writestr(item, zin.read(item.filename))
    shutil.move(tmp_path, doc_path)


def redact_docx_images(doc_path: str, approved: list[ImageRef]) -> int:
    partnames = {ref.locator["partname"] for ref in approved if ref.locator.get("kind") == "docx"}
    _redact_zip_package(doc_path, partnames)
    return len(partnames)


def redact_pptx_images(doc_path: str, approved: list[ImageRef]) -> int:
    partnames = {ref.locator["partname"] for ref in approved if ref.locator.get("kind") == "pptx"}
    _redact_zip_package(doc_path, partnames)
    return len(partnames)


def redact_pdf_images(doc_path: str, approved: list[ImageRef]) -> tuple[int, int]:
    """Returns (images_redacted, images_with_no_rects) - the latter is a real,
    silent-no-op risk: PyMuPDF can't locate rects for images painted as tiling
    pattern fills, and an approved-but-unlocated image would otherwise vanish
    from the count with no signal that it was never actually touched."""
    import fitz

    by_page: dict[int, list[tuple]] = {}
    unlocated = 0
    for ref in approved:
        if ref.locator.get("kind") != "pdf":
            continue
        rects = ref.locator.get("rects", [])
        if not rects:
            unlocated += 1
            continue
        for rect in rects:
            by_page.setdefault(ref.locator["page_number"], []).append(rect)

    if not by_page:
        return 0, unlocated

    doc = fitz.open(doc_path)
    count = 0
    for page_number, rects in by_page.items():
        if page_number >= len(doc):
            continue
        page = doc[page_number]
        for rect in rects:
            page.add_redact_annot(fitz.Rect(*rect), fill=(0.24, 0.24, 0.24))
            count += 1
        page.apply_redactions()
    doc.saveIncr()
    doc.close()
    return count, unlocated


def redact_images(doc_path: str, content_type: str, filename: str, approved: list[ImageRef]) -> tuple[int, int]:
    """Dispatch by format. Returns (images_redacted, images_with_no_rects) -
    the second element is always 0 for docx/pptx (whole-part swap, nothing to
    locate) and only meaningful for PDF."""
    if not approved:
        return 0, 0
    lower = filename.lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        return redact_pdf_images(doc_path, approved)
    if lower.endswith(".docx") or "wordprocessingml" in content_type:
        return redact_docx_images(doc_path, approved), 0
    if lower.endswith(".pptx") or "presentationml" in content_type:
        return redact_pptx_images(doc_path, approved), 0
    return 0, 0
