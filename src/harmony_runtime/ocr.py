"""Optional OCR adapter (T10). Never invents boxes when no engine is present.

When an engine is available, OCR labels are converted to visual-region
proposals and revalidated by the runtime before dispatch. When no engine is
installed the capability stays false and ocr_tap is unsupported — that is the
honest result, not a silent pass.
"""
from __future__ import annotations

from typing import Any


def detect_engine() -> str | None:
    """Return a short engine name when a local OCR engine is usable."""
    try:
        import pytesseract  # type: ignore
        pytesseract.get_tesseract_version()
        return "tesseract"
    except Exception:
        pass
    try:
        import easyocr  # type: ignore  # noqa: F401
        return "easyocr"
    except Exception:
        pass
    return None


def ocr_boxes(image_bytes: bytes) -> list[dict[str, Any]]:
    """Detect text boxes. Empty list when no engine or no hits."""
    engine = detect_engine()
    if engine is None or not image_bytes:
        return []
    if engine == "tesseract":
        try:
            import pytesseract
            from PIL import Image
            import io
            image = Image.open(io.BytesIO(image_bytes))
            data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
            boxes = []
            for i, text in enumerate(data.get("text") or []):
                text = (text or "").strip()
                if not text:
                    continue
                x, y, w, h = (data["left"][i], data["top"][i],
                              data["width"][i], data["height"][i])
                boxes.append({"label": text, "region": (x, y, x + w, y + h)})
            return boxes
        except Exception:
            return []
    return []
