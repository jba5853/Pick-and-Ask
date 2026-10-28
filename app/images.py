"""Shrink and clean photos before they go to the API."""

import io

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()  # lets Pillow open iPhone HEIC/HEIF photos

MAX_EDGE = 1568
JPEG_QUALITY = 85


def prepare_image(data: bytes) -> bytes:
    """Return a JPEG with EXIF rotation applied, long edge <= MAX_EDGE, and no metadata."""
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except Exception as e:
        raise ValueError("Could not read that image. Use a JPEG, PNG or HEIC photo.") from e
    img = img.convert("RGB")  # also drops alpha and any palette
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)  # no exif= argument, so metadata (incl. GPS) is dropped
    return out.getvalue()
