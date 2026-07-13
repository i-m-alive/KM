"""Standing regression suite for Sanitization's deterministic pipeline.

Every masking bug found in real usage lived in the DETERMINISTIC layer
(word-boundary corruption, un-enumerated master/orphan images, metadata /
comment / hyperlink channels never checked or never fixed) - not in the LLM.
So this suite plants known leaks in freshly-built fixture documents (docx,
pptx, xlsx, pdf), runs the real render + scrub functions, and asserts:

  1. each scan channel DETECTS its planted leak on the raw file
     (a channel that can't catch a planted leak is a channel that can
     silently pass a real one), and
  2. after render + scrub, every deterministic channel verifies clean, and
  3. masking is word-boundary safe (masking "RIA" must not corrupt
     "MATERIAL"), and
  4. image enumeration sees orphaned media with no relationship chain
     (the slide-master / dangling-media class of miss).

No Bedrock calls and no DB - runs offline in seconds:

    cd backend && .venv/bin/python scripts/regression_check.py
"""

import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.documents.comment_scan import find_residual_comments
from app.documents.comment_scrub import scrub_comments
from app.documents.hyperlink_scan import find_residual_hyperlinks
from app.documents.hyperlink_scrub import scrub_hyperlinks
from app.documents.images import extract_images
from app.documents.metadata_scan import find_residual_metadata
from app.documents.metadata_scrub import scrub_metadata
from app.documents.render import render_masked_document
from app.documents.verify import find_residual_surfaces

CLIENT = "BAJAJ"
SHORT_CLIENT = "RIA"  # word-boundary trap: must not corrupt MATERIAL
MULTI_CLIENT = "Tata Capital"  # multi-word: must mask even when line-wrapped
SURFACE_TO_TOKEN = {CLIENT: "[CLIENT_1]", SHORT_CLIENT: "[CLIENT_2]", MULTI_CLIENT: "[CLIENT_3]"}
SURFACES = list(SURFACE_TO_TOKEN.keys())
STYLE = "token"
BODY_TEXT = (
    f"Engagement overview for {CLIENT}. The MATERIAL scope covers {SHORT_CLIENT} operations. "
    f"Benchmarked against {MULTI_CLIENT} programs."
)
LEAKY_URL = f"https://www.{CLIENT.lower()}.com/portal"

failures: list[str] = []
passes = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passes
    if ok:
        passes += 1
        print(f"  ok    {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def _png_bytes(color=(200, 30, 30)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (120, 60), color).save(buf, format="PNG")
    return buf.getvalue()


# ---------- fixture builders ----------

def build_docx(path: str) -> None:
    import docx
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsmap, qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    doc = docx.Document()
    para = doc.add_paragraph(BODY_TEXT)
    doc.add_comment(para.runs, text=f"Confirm with {CLIENT} legal first", author="reviewer")
    doc.core_properties.subject = f"{CLIENT} discovery phase"

    # Tracked deletion - deleted-but-present text lives in w:delText, which no
    # normal text-extraction path sees. python-docx has no API for this; raw XML.
    w = nsmap["w"]
    del_para = doc.add_paragraph()
    del_para._p.append(parse_xml(
        f'<w:del xmlns:w="{w}" w:id="99" w:author="reviewer" w:date="2026-01-01T00:00:00Z">'
        f"<w:r><w:delText>Old {CLIENT} pricing removed</w:delText></w:r></w:del>"
    ))

    # External hyperlink whose TARGET (not display text) leaks the client.
    link_para = doc.add_paragraph()
    r_id = link_para.part.relate_to(LEAKY_URL, RT.HYPERLINK, is_external=True)
    link_para._p.append(parse_xml(
        f'<w:hyperlink xmlns:w="{w}" xmlns:r="{nsmap["r"]}" r:id="{r_id}">'
        "<w:r><w:t>client portal</w:t></w:r></w:hyperlink>"
    ))

    # A VML text box - python-docx's .paragraphs never sees w:txbxContent, so
    # without the dedicated textbox walk this leak is invisible end to end.
    tb_para = doc.add_paragraph()
    tb_para._p.append(parse_xml(
        f'<w:r xmlns:w="{w}" xmlns:v="urn:schemas-microsoft-com:vml"><w:pict>'
        '<v:shape style="width:220pt;height:60pt"><v:textbox><w:txbxContent>'
        f"<w:p><w:r><w:t>Cover note for the {CLIENT} board</w:t></w:r></w:p>"
        "</w:txbxContent></v:textbox></v:shape></w:pict></w:r>"
    ))
    doc.save(path)


def build_pptx(path: str) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    tb = slide.shapes.add_textbox(Inches(0.5), Inches(0.5), Inches(8), Inches(1))
    tb.text_frame.text = BODY_TEXT
    run = tb.text_frame.paragraphs[0].add_run()
    run.text = " portal"
    run.hyperlink.address = LEAKY_URL
    slide.shapes.add_picture(io.BytesIO(_png_bytes()), Inches(1), Inches(2))
    prs.save(path)

    # Orphaned media: present in ppt/media/ with NO relationship from any
    # slide - exactly what a rels-graph walk misses and the raw glob must find.
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("ppt/media/orphan_logo.png", _png_bytes((30, 30, 200)))


def build_xlsx(path: str) -> None:
    import openpyxl
    from openpyxl.comments import Comment

    wb = openpyxl.Workbook()
    ws = wb.active
    # Sheet tab name and print header both leak the client, and neither
    # appears in any cell - previously invisible to the whole pipeline.
    ws.title = f"{CLIENT} Data"
    ws.oddHeader.center.text = f"{CLIENT} - Confidential"
    ws["A1"] = BODY_TEXT
    ws["A2"] = "client portal"
    ws["A2"].hyperlink = LEAKY_URL
    ws["A3"] = "note"
    ws["A3"].comment = Comment(f"Check {CLIENT} numbers", "reviewer")
    wb.save(path)


def build_pdf(path: str) -> None:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    # insert_textbox (not insert_text) so the body wraps within the page -
    # a single insert_text line runs past the page edge, where PyMuPDF's
    # word extraction clips it but pdfplumber's verify still reads it.
    page.insert_textbox(fitz.Rect(72, 80, 540, 190), BODY_TEXT, fontsize=11)
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(300, 200, 420, 220), "uri": LEAKY_URL})
    annot = page.add_text_annot((72, 160), f"Verify with {CLIENT} finance")
    annot.update()
    # A narrow text box forces "Tata Capital" to wrap across two lines - the
    # exact case the old single-line substring search could never match.
    page.insert_textbox(fitz.Rect(72, 200, 116, 420), f"Meeting with {MULTI_CLIENT} leadership", fontsize=12)
    doc.set_metadata({**doc.metadata, "subject": f"{CLIENT} discovery"})
    doc.save(path)
    doc.close()


# ---------- per-format run ----------

FIXTURES = [
    ("regression.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", build_docx),
    ("regression.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", build_pptx),
    ("regression.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", build_xlsx),
    ("regression.pdf", "application/pdf", build_pdf),
]

# Which planted leaks each format actually carries (fixture-building APIs
# don't exist for every channel in every format - e.g. no pptx comment API).
HAS_COMMENT_LEAK = {"regression.docx", "regression.xlsx", "regression.pdf"}
HAS_METADATA_LEAK = {"regression.docx", "regression.pdf"}


def run_format(workdir: Path, filename: str, content_type: str, builder) -> None:
    print(f"\n== {filename} ==")
    src = str(workdir / filename)
    builder(src)

    # 1. Every applicable channel must DETECT its planted leak on the raw file.
    check("hyperlink scan detects planted target", len(find_residual_hyperlinks(src, content_type, filename, SURFACES)) > 0)
    if filename in HAS_COMMENT_LEAK:
        check("comment scan detects planted comment", len(find_residual_comments(src, content_type, filename, SURFACES)) > 0)
    if filename in HAS_METADATA_LEAK:
        check("metadata scan detects planted property", len(find_residual_metadata(src, content_type, filename, SURFACES)) > 0)
    check("text verify detects unmasked surface", len(find_residual_surfaces(src, content_type, filename, SURFACES)) > 0)

    # 1b. Channels that were once invisible to extraction must stay visible -
    # if extraction goes blind here again, masking and verification both go
    # blind with it (the silent-leak class, not the flagged class).
    from app.documents.extract import extract_chunks

    pre_text = " ".join(c.text for c in extract_chunks(src, content_type, filename))
    if filename.endswith(".docx"):
        check("docx textbox text visible to extraction", "Cover note" in pre_text)
    if filename.endswith(".xlsx"):
        check("xlsx sheet name visible to extraction", f"{CLIENT} Data" in pre_text)
        check("xlsx print header visible to extraction", "Confidential" in pre_text)
    if filename.endswith(".pdf"):
        wrapped = find_residual_surfaces(src, content_type, filename, [MULTI_CLIENT])
        check("pdf line-wrapped multi-word name detected", len(wrapped) > 0)

    # 2. Render + scrub with the real pipeline functions, in pipeline order.
    masked_path, _ = render_masked_document("regression-check", src, content_type, filename, SURFACE_TO_TOKEN, style=STYLE)
    scrub_metadata(masked_path, content_type, filename, SURFACE_TO_TOKEN, STYLE)
    scrub_hyperlinks(masked_path, content_type, filename, SURFACE_TO_TOKEN, STYLE)
    scrub_comments(masked_path, content_type, filename, SURFACE_TO_TOKEN, STYLE)

    # 3. Every deterministic channel must now verify clean.
    residual_text = find_residual_surfaces(masked_path, content_type, filename, SURFACES)
    check("text channel clean after mask", len(residual_text) == 0, "; ".join(residual_text[:3]))
    residual_meta = find_residual_metadata(masked_path, content_type, filename, SURFACES)
    check("metadata channel clean after scrub", len(residual_meta) == 0, "; ".join(residual_meta[:3]))
    residual_comments = find_residual_comments(masked_path, content_type, filename, SURFACES)
    check("comments channel clean after scrub", len(residual_comments) == 0, "; ".join(residual_comments[:3]))
    residual_links = find_residual_hyperlinks(masked_path, content_type, filename, SURFACES)
    check("hyperlinks channel clean after scrub", len(residual_links) == 0, "; ".join(residual_links[:3]))

    # 4. Word-boundary safety: MATERIAL contains 'RIA' but must survive intact.
    from app.documents.extract import extract_chunks

    masked_text = " ".join(c.text for c in extract_chunks(masked_path, content_type, filename))
    check("word-boundary safe (MATERIAL intact)", "MATERIAL" in masked_text, f"text was: {masked_text[:200]}")
    check("mask token present in masked text", "[CLIENT_1]" in masked_text, f"text was: {masked_text[:200]}")

    # 5. pptx only: orphaned media (no rels chain) must still be enumerated.
    if filename.endswith(".pptx"):
        refs = extract_images(src, content_type, filename)
        parts = {getattr(r, "partname", None) or getattr(r, "location_label", "") for r in refs}
        found = any("orphan_logo" in str(p) for p in parts) or len(refs) >= 2
        check("orphaned media enumerated (glob, not rels-walk)", found, f"only found: {sorted(str(p) for p in parts)}")
        check_near_duplicate_redaction(workdir)


def check_near_duplicate_redaction(workdir: Path) -> None:
    """The perceptual-dedup blind spot found in a real run: the same logo
    saved at two compressions is SHA-distinct but phash-near, so it merges
    into ONE cluster - and redaction must then swap BOTH media parts, not
    just the ones byte-equal to the cluster's sample."""
    from PIL import Image

    from app.agents.sanitization.image_scan import PERCEPTUAL_DEDUP_THRESHOLD
    from app.documents.image_redact import redact_images
    from app.masking.logo_reference import compute_phash, phash_distance
    from pptx import Presentation
    from pptx.util import Inches

    png = _png_bytes((10, 120, 60))
    jpg_buf = io.BytesIO()
    Image.open(io.BytesIO(png)).save(jpg_buf, format="JPEG", quality=60)
    jpg = jpg_buf.getvalue()
    check("renditions are SHA-distinct", png != jpg)
    d = phash_distance(compute_phash(png), compute_phash(jpg))
    check("renditions cluster perceptually", d is not None and d <= PERCEPTUAL_DEDUP_THRESHOLD, f"distance={d}")

    path = str(workdir / "near_dup.pptx")
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.add_picture(io.BytesIO(png), Inches(1), Inches(1))
    slide.shapes.add_picture(io.BytesIO(jpg), Inches(4), Inches(1))
    prs.save(path)

    ct = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    refs = extract_images(path, ct, "near_dup.pptx")
    both = [r for r in refs if r.locator.get("kind") == "pptx"]
    redacted, _ = redact_images(path, ct, "near_dup.pptx", both)
    check("both near-duplicate renditions redacted", redacted >= 2, f"redacted={redacted}")

    with zipfile.ZipFile(path) as z:
        media = [n for n in z.namelist() if n.startswith("ppt/media/")]
        survivors = [n for n in media if z.read(n) in (png, jpg)]
    check("no original rendition bytes survive in media", not survivors, f"survived: {survivors}")

    # Remediation double-click safety: a second pass must recognize the
    # placeholder bytes it wrote and target nothing.
    from app.documents.image_redact import is_placeholder_bytes

    refs2 = extract_images(path, ct, "near_dup.pptx")
    remaining = [r for r in refs2 if r.locator.get("kind") == "pptx" and not is_placeholder_bytes(r.image_bytes)]
    check("second remediation pass targets nothing (placeholders recognized)", not remaining,
          f"{len(remaining)} ref(s) not recognized as placeholder")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="naviknow-regression-"))
    try:
        for filename, content_type, builder in FIXTURES:
            try:
                run_format(workdir, filename, content_type, builder)
            except Exception as exc:  # a crash in one format shouldn't hide the others
                import traceback

                traceback.print_exc()
                failures.append(f"{filename}: crashed - {exc}")
        print(f"\n{'=' * 50}\n{passes} checks passed, {len(failures)} failed")
        for f in failures:
            print(f"  FAIL  {f}")
        return 1 if failures else 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
