"""Image evidence helpers.

`mark_targets` derives labels only from the matching current UI-tree catalog.
`region_digest`/`clip_region` are the shared revalidation primitives for visual
targets: the same function computes the digest when a region is proposed and
when the runtime re-checks it immediately before dispatch, so a proposed region
can never be trusted just because the proposer said so.
"""
import base64
import hashlib
import io
from PIL import Image, ImageDraw, ImageFont


def encode_image(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {"mime_type": "image/png", "base64": base64.b64encode(buffer.getvalue()).decode("ascii")}


def decode_image(image):
    """Decode an observation's `image` field; returns None when there is none."""
    if not isinstance(image, dict) or not image.get("base64"):
        return None
    try:
        return base64.b64decode(image["base64"], validate=True)
    except Exception:
        return None


def clip_region(region, size):
    """Clip a (left, top, right, bottom) region to image/display bounds.

    Returns None when the region has no area inside the bounds, so a region that
    is entirely off-screen can never be dispatched as a point.
    """
    if region is None or size is None:
        return None
    try:
        left, top, right, bottom = (int(value) for value in region)
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def region_digest(image_bytes, region, size=None):
    """Content digest of one image region, or None when it cannot be computed.

    The crop is decoded to plain RGB bytes so the digest does not depend on the
    container format of the screenshot.
    """
    if not image_bytes:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            box = clip_region(region, size or image.size)
            if box is None:
                return None
            return hashlib.sha256(image.convert("RGB").crop(box).tobytes()).hexdigest()
    except Exception:
        return None


def mark_targets(image, catalog):
    # Never alter the original evidence. Labels are ASCII action IDs; no platform
    # font dependency, and no guesses about text hidden behind sibling windows.
    marked = image.convert("RGB")
    draw = ImageDraw.Draw(marked)
    font = ImageFont.load_default(size=max(12, min(32, image.width // 45)))
    labels = []
    for node in catalog:
        if not node["enabled"]:
            continue
        x1, y1, x2, y2 = node["hit_bounds"]
        draw.rectangle((x1, y1, x2 - 1, y2 - 1), outline="#ff3b30", width=2)
        label = node["action_id"]
        box = draw.textbbox((0, 0), label, font=font)
        width, height = box[2] - box[0] + 6, box[3] - box[1] + 4
        left = max(0, min(x1, image.width - width))
        top = max(0, min(y1, image.height - height))
        draw.rectangle((left, top, left + width - 1, top + height - 1), fill="#a40000")
        draw.text((left + 3 - box[0], top + 2 - box[1]), label, fill="white", font=font)
        labels.append({"action_id": label, "hit_bounds": list(node["hit_bounds"])})
    return marked, labels
