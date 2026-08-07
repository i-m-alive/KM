"""EXIF/metadata stripping for embedded images (Phase 2) - runs on EVERY
embedded image in the ALREADY-RENDERED masked file, not only ones flagged for
redaction. A non-logo photo (e.g. a site visit picture) can carry GPS
coordinates, device serial numbers, or a timestamp in its EXIF block that
image_redact.py never touches - redaction only replaces images the reviewer
flagged as client-identifying; this closes the remaining, unconditional
metadata leak on the images that stay exactly as they are.

Same "render, then clean the rest" sequencing as metadata_scrub.py - operates
on the rendered file in place, after text masking and image redaction have
already happened, so an image that WAS redacted (now our own synthetic
placeholder PNG) is simply re-scanned and found to have no EXIF to strip.

Deliberately scoped to OOXML (DOCX/PPTX/XLSX) for this phase: a PDF's
embedded images are XObject streams with no standalone EXIF container in the
same sense, and rewriting them safely needs a different, PDF-stream-level
approach - out of scope here. PDF calls return 0, not an error.
"""

import zipfile

_MEDIA_PREFIXES = ("word/media/", "ppt/media/", "xl/media/")
# EXIF (and, for PNG, the analogous eXIf chunk) only ever lives in these
# formats in practice - skipping everything else avoids opening/re-encoding
# images that could never have carried it, which is most of a typical deck.
_EXIF_CAPABLE_FORMATS = {"jpeg", "tiff", "png"}


def _strip_one(data: bytes) -> bytes | None:
    """Returns re-encoded bytes with EXIF removed, or None if this image had
    no EXIF to strip (caller leaves the original bytes untouched) or
    couldn't be opened (degrade gracefully, same contract as
    image_redact.is_placeholder_bytes)."""
    import io

    from PIL import Image

    try:
        im = Image.open(io.BytesIO(data))
        has_exif = bool(im.getexif())
        if not has_exif:
            return None
        fmt = (im.format or "").upper()
        im.load()
        out = io.BytesIO()
        save_kwargs = {}
        if fmt == "JPEG":
            save_kwargs["quality"] = 95
        # Not passing exif= is what actually strips it - PIL only embeds EXIF
        # in the output when the caller explicitly hands it back in.
        im.save(out, format=fmt or im.format, **save_kwargs)
        return out.getvalue()
    except Exception:
        return None


def _strip_ooxml(path: str) -> int:
    import shutil

    from app.documents.images import guess_image_format

    tmp_path = path + ".exiftmp"
    stripped = 0
    with zipfile.ZipFile(path, "r") as zin:
        replacements: dict[str, bytes] = {}
        for name in zin.namelist():
            if not name.startswith(_MEDIA_PREFIXES):
                continue
            data = zin.read(name)
            fmt = guess_image_format(data, fallback=None)
            if fmt not in _EXIF_CAPABLE_FORMATS:
                continue
            new_bytes = _strip_one(data)
            if new_bytes is not None:
                replacements[name] = new_bytes
                stripped += 1
        if not replacements:
            return 0
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename in replacements:
                    zout.writestr(item, replacements[item.filename])
                else:
                    zout.writestr(item, zin.read(item.filename))
    shutil.move(tmp_path, path)
    return stripped


def strip_exif(path: str, content_type: str, filename: str) -> int:
    """In-place. Returns the number of images whose EXIF was actually
    removed (0 if none carried any, or the format isn't supported by this
    phase - see module docstring for the PDF scoping decision)."""
    lower = filename.lower()
    if content_type == "application/pdf" or lower.endswith(".pdf"):
        return 0
    if (
        lower.endswith((".docx", ".pptx", ".xlsx"))
        or "wordprocessingml" in content_type
        or "presentationml" in content_type
        or "spreadsheetml" in content_type
    ):
        return _strip_ooxml(path)
    return 0
