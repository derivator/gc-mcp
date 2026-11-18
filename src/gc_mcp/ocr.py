"""Optional OCR (text extraction) for captured screenshots.

The screenshot tool can optionally run RapidOCR over the saved PNG so an agent
can read on-screen text (menus, dialogue, HUD values). RapidOCR runs the PP-OCR
detection/recognition models on ONNX Runtime, and the models ship inside the
wheel, so it works offline once installed.

This is an opt-in extra: neither ``rapidocr`` nor ``onnxruntime`` is required for
the server to run. When they are missing, :func:`extract_text` raises
:class:`~gc_mcp.errors.OcrError` with install hints; the server turns that into a
structured ``ocr`` result instead of failing the whole capture.
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from typing import Any

from .errors import OcrError

#: Common Tesseract / ISO-639 codes mapped to RapidOCR ``Rec.lang_type`` values.
#: Anything not listed here is passed to RapidOCR unchanged (e.g. ``"en"``).
_LANG_ALIASES = {
    # Latin-script languages with alternate Tesseract codes.
    "eng": "en",
    "deu": "de",
    "ger": "de",
    "fra": "fr",
    "fre": "fr",
    "spa": "es",
    "ita": "it",
    "por": "pt",
    "nld": "nl",
    "dut": "nl",
    "pol": "pl",
    "swe": "sv",
    "dan": "da",
    "nor": "no",
    "fin": "fi",
    "ces": "cs",
    "cze": "cs",
    "tur": "tr",
    "ron": "ro",
    "rum": "ro",
    "hun": "hu",
    # Non-Latin scripts.
    "jpn": "japan",
    "ja": "japan",
    "kor": "korean",
    "ko": "korean",
    "chi_sim": "ch",
    "chi": "ch",
    "zho": "ch",
    "zh": "ch",
    "zh_cn": "ch",
    "chi_tra": "chinese_cht",
    "zh_tw": "chinese_cht",
}

#: Instantiated engines, keyed by resolved language. Constructing one loads the
#: ONNX models, so reuse it across calls.
_ENGINES: dict[str, Any] = {}
_ENGINE_LOCK = threading.Lock()


def _import_rapidocr() -> Any:
    """Import and return the ``RapidOCR`` class, or raise :class:`OcrError`."""
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise OcrError(
            "OCR requires the optional 'rapidocr' package; install it with "
            "'uv sync --extra ocr'"
        ) from exc
    return RapidOCR


def _module_available(name: str) -> bool:
    """Return whether ``name`` can be imported."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def status() -> dict[str, Any]:
    """Report whether OCR can run on this host.

    ``available`` is true only when both the ``rapidocr`` package and its
    ONNX Runtime backend are installed.
    """
    rapidocr = _module_available("rapidocr")
    onnxruntime = _module_available("onnxruntime")
    return {
        "available": rapidocr and onnxruntime,
        "rapidocr": rapidocr,
        "onnxruntime": onnxruntime,
    }


def _normalize_lang(lang: str) -> str:
    """Map a Tesseract/ISO-639 code to a RapidOCR recognition language."""
    return _LANG_ALIASES.get(lang.strip().lower().replace("-", "_"), lang.strip())


def _load_engine(lang: str) -> Any:
    """Return a cached RapidOCR engine configured for ``lang``."""
    engine = _ENGINES.get(lang)
    if engine is not None:
        return engine

    rapidocr = _import_rapidocr()
    with _ENGINE_LOCK:
        engine = _ENGINES.get(lang)
        if engine is None:
            try:
                # 'Global.log_level' keeps RapidOCR's INFO chatter (model paths,
                # engine selection) out of the server's stderr log.
                engine = rapidocr(params={
                    "Global.log_level": "error",
                    "Rec.lang_type": lang,
                })
            except Exception as exc:
                raise OcrError(
                    f"failed to initialise RapidOCR for language {lang!r}: {exc}"
                ) from exc
            _ENGINES[lang] = engine
    return engine


def extract_text(image_path: str | Path, *, lang: str = "en") -> dict[str, Any]:
    """Extract text from ``image_path`` with RapidOCR.

    ``lang`` accepts a RapidOCR recognition language (``en``, ``de``,
    ``japan``, ...) or a common Tesseract code (``eng``, ``deu``, ...), which is
    mapped automatically. Returns the recognised text (one line per detected
    text box) plus per-line and per-word boxes and confidences::

        {"available": True, "engine": "rapidocr", "lang": "en",
         "text": "...",
         "lines": [{"text", "confidence", "left", "top", "width", "height",
                    "box": [[x, y], ...]}, ...],
         "words": [{"text", "confidence", "left", "top", "width", "height"},
                   ...]}

    Raises :class:`~gc_mcp.errors.OcrError` when the optional package is
    missing, the language is unsupported, or RapidOCR cannot read the image.
    """
    resolved = _normalize_lang(lang)
    engine = _load_engine(resolved)
    try:
        output = engine(str(image_path), return_word_box=True)
    except Exception as exc:
        raise OcrError(f"RapidOCR failed to read {image_path}: {exc}") from exc

    texts = list(getattr(output, "txts", None) or ())
    scores = list(getattr(output, "scores", None) or ())
    boxes = getattr(output, "boxes", None)
    word_results = getattr(output, "word_results", None) or ()

    lines: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    for index, raw in enumerate(texts):
        text = (raw or "").strip()
        if not text:
            continue
        line: dict[str, Any] = {
            "text": text,
            "confidence": round(_float(scores, index, default=0.0), 4),
        }
        if boxes is not None and index < len(boxes):
            line.update(_bbox(boxes[index]))
            line["box"] = _points(boxes[index])
        lines.append(line)

        for word in word_results[index] if index < len(word_results) else ():
            word_text = (word[0] or "").strip()
            if not word_text:
                continue
            entry: dict[str, Any] = {
                "text": word_text,
                "confidence": round(float(word[1]), 4),
            }
            if word[2]:
                entry.update(_bbox(word[2]))
            words.append(entry)

    return {
        "available": True,
        "engine": "rapidocr",
        "lang": resolved,
        "text": "\n".join(line["text"] for line in lines),
        "lines": lines,
        "words": words,
        "elapse": round(float(getattr(output, "elapse", 0.0) or 0.0), 4),
    }


def _float(values: list[Any], index: int, *, default: float) -> float:
    """Return ``values[index]`` as a float, or ``default`` if unusable."""
    try:
        return float(values[index])
    except (IndexError, TypeError, ValueError):
        return default


def _bbox(points: Any) -> dict[str, int]:
    """Return the axis-aligned bounding box of a quad of ``[x, y]`` points."""
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    left = round(min(xs))
    top = round(min(ys))
    return {
        "left": left,
        "top": top,
        "width": round(max(xs)) - left,
        "height": round(max(ys)) - top,
    }


def _points(points: Any) -> list[list[int]]:
    """Convert a quad of ``[x, y]`` points to rounded integer pairs."""
    return [[round(float(point[0])), round(float(point[1]))] for point in points]
