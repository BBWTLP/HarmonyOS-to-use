"""Image evidence derived only from the matching current UI-tree catalog."""
import base64
import io
from PIL import ImageDraw, ImageFont


def encode_image(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {"mime_type": "image/png", "base64": base64.b64encode(buffer.getvalue()).decode("ascii")}


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
