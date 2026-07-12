"""LibreOffice-headless conversion to PDF, for true-fidelity in-browser preview
(images, layout, everything) without building a bespoke DOCX/PPTX renderer.
Converted PDFs are cached next to the source file.
"""

import os
import shutil
import subprocess
import tempfile

from app.config import get_settings

settings = get_settings()

_COMMON_MAC_PATHS = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]


class ConversionUnavailableError(Exception):
    pass


def _find_soffice() -> str:
    if settings.SOFFICE_PATH and os.path.exists(settings.SOFFICE_PATH):
        return settings.SOFFICE_PATH
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for path in _COMMON_MAC_PATHS:
        if os.path.exists(path):
            return path
    raise ConversionUnavailableError(
        "LibreOffice ('soffice') was not found. Install it (e.g. `brew install --cask libreoffice` on "
        "Mac) or set SOFFICE_PATH in backend/.env to the binary's full path."
    )


def to_pdf_cached(src_path: str) -> str:
    """Convert src_path to PDF if it isn't one already, caching the result next
    to the source (<src>.preview.pdf). Returns the path to a PDF file."""
    if src_path.lower().endswith(".pdf"):
        return src_path

    cache_path = f"{src_path}.preview.pdf"
    if os.path.exists(cache_path) and os.path.getmtime(cache_path) >= os.path.getmtime(src_path):
        return cache_path

    soffice = _find_soffice()
    out_dir = os.path.dirname(os.path.abspath(src_path))
    try:
        subprocess.run(
            [soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", out_dir, src_path],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        raise ConversionUnavailableError(f"LibreOffice conversion failed: {exc.stderr.decode(errors='replace')}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConversionUnavailableError("LibreOffice conversion timed out") from exc

    stem = os.path.splitext(os.path.basename(src_path))[0]
    produced = os.path.join(out_dir, f"{stem}.pdf")
    if not os.path.exists(produced):
        raise ConversionUnavailableError("LibreOffice did not produce the expected output file")

    if produced != cache_path:
        shutil.move(produced, cache_path)
    return cache_path


def rasterize_image_to_png(image_bytes: bytes, ext: str) -> bytes:
    """Convert a vector image (svg/emf/wmf/emz/wmz) to PNG bytes via LibreOffice,
    so it can be sent to Bedrock vision (raster-only) or hashed with imagehash
    (Pillow can't open these formats directly). One document per unique image -
    callers should sha256-dedupe before calling this, same as scan_document_images
    already does for the vision call itself."""
    soffice = _find_soffice()
    with tempfile.TemporaryDirectory() as tmp_dir:
        src_path = os.path.join(tmp_dir, f"logo.{ext.lstrip('.')}")
        with open(src_path, "wb") as f:
            f.write(image_bytes)
        try:
            subprocess.run(
                [soffice, "--headless", "--norestore", "--convert-to", "png", "--outdir", tmp_dir, src_path],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as exc:
            raise ConversionUnavailableError(f"LibreOffice image conversion failed: {exc.stderr.decode(errors='replace')}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ConversionUnavailableError("LibreOffice image conversion timed out") from exc

        produced = os.path.join(tmp_dir, "logo.png")
        if not os.path.exists(produced):
            raise ConversionUnavailableError("LibreOffice did not produce the expected PNG output")
        with open(produced, "rb") as f:
            return f.read()
