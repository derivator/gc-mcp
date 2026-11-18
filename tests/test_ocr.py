"""Tests for optional OCR on screenshots."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from gc_mcp import ocr, server
from gc_mcp.errors import OcrError


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    """Keep cached engines from leaking between tests."""
    ocr._ENGINES.clear()
    yield
    ocr._ENGINES.clear()


def _output(*, txts=None, scores=None, boxes=None, word_results=(), elapse=0.0):
    """Build a stand-in for RapidOCR's ``RapidOCROutput``."""
    return types.SimpleNamespace(
        txts=txts,
        scores=scores,
        boxes=boxes,
        word_results=word_results,
        elapse=elapse,
    )


def _fake_rapidocr(*, output=None, error=None, init_error=None):
    """Return a fake ``RapidOCR`` class plus a list of created instances."""
    created: list = []

    class FakeRapidOCR:
        def __init__(self, params=None):
            if init_error is not None:
                raise init_error
            self.params = params
            self.calls: list = []
            created.append(self)

        def __call__(self, image, return_word_box=False):
            self.calls.append((image, return_word_box))
            if error is not None:
                raise error
            return output

    return FakeRapidOCR, created


# ---------------------------------------------------------------------------
# ocr.extract_text / ocr.status
# ---------------------------------------------------------------------------
class TestExtractText:
    def test_parses_lines_and_words(self, monkeypatch):
        output = _output(
            txts=("PRESS START", "CONTINUE"),
            scores=(0.99, 0.98),
            boxes=[
                [[10, 5], [60, 5], [60, 15], [10, 15]],
                [[20, 40], [100, 40], [100, 52], [20, 52]],
            ],
            word_results=(
                (
                    ("PRESS", 0.95, [[10, 5], [40, 5], [40, 15], [10, 15]]),
                    ("START", 0.9, [[45, 5], [60, 5], [60, 15], [45, 15]]),
                ),
                (("CONTINUE", 0.97, [[20, 40], [100, 40], [100, 52], [20, 52]]),),
            ),
            elapse=0.1234,
        )
        fake, _ = _fake_rapidocr(output=output)
        monkeypatch.setattr(ocr, "_import_rapidocr", lambda: fake)

        result = ocr.extract_text("frame.png", lang="en")

        assert result["available"] is True
        assert result["engine"] == "rapidocr"
        assert result["lang"] == "en"
        assert result["text"] == "PRESS START\nCONTINUE"
        assert [line["text"] for line in result["lines"]] == [
            "PRESS START", "CONTINUE",
        ]
        assert result["lines"][1]["left"] == 20
        assert result["lines"][1]["top"] == 40
        assert result["lines"][1]["width"] == 80
        assert result["lines"][1]["height"] == 12
        assert result["lines"][1]["box"] == [[20, 40], [100, 40], [100, 52], [20, 52]]
        assert [w["text"] for w in result["words"]] == [
            "PRESS", "START", "CONTINUE",
        ]
        assert result["words"][0]["confidence"] == pytest.approx(0.95)
        assert result["words"][0]["left"] == 10
        assert result["words"][1]["width"] == 15
        assert result["words"][2]["top"] == 40
        assert result["elapse"] == pytest.approx(0.1234)

    def test_blank_frame(self, monkeypatch):
        # RapidOCR returns None fields (and a single empty word entry) when no
        # text is detected.
        output = _output(txts=None, scores=None, boxes=None,
                         word_results=(("", 1.0, None),))
        fake, _ = _fake_rapidocr(output=output)
        monkeypatch.setattr(ocr, "_import_rapidocr", lambda: fake)

        result = ocr.extract_text("blank.png")

        assert result["available"] is True
        assert result["text"] == ""
        assert result["lines"] == []
        assert result["words"] == []

    def test_normalizes_tesseract_codes(self, monkeypatch):
        output = _output(txts=("HALLO",), scores=(0.9,), boxes=None)
        fake, created = _fake_rapidocr(output=output)
        monkeypatch.setattr(ocr, "_import_rapidocr", lambda: fake)

        result = ocr.extract_text("frame.png", lang="deu")

        assert result["lang"] == "de"
        assert created[0].params == {
            "Global.log_level": "error",
            "Rec.lang_type": "de",
        }

    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            ("eng", "en"),
            ("en", "en"),
            ("ger", "de"),
            ("chi_tra", "chinese_cht"),
            ("zh-TW", "chinese_cht"),
            ("jpn", "japan"),
            ("japan", "japan"),
        ],
    )
    def test_language_aliases(self, requested, expected):
        assert ocr._normalize_lang(requested) == expected

    def test_reuses_engine_for_same_language(self, monkeypatch):
        output = _output(txts=("READY",), scores=(0.9,), boxes=None)
        fake, created = _fake_rapidocr(output=output)
        monkeypatch.setattr(ocr, "_import_rapidocr", lambda: fake)

        ocr.extract_text("a.png", lang="en")
        ocr.extract_text("b.png", lang="en")

        assert len(created) == 1
        assert created[0].calls == [("a.png", True), ("b.png", True)]

    def test_missing_package_raises(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "rapidocr", None)
        with pytest.raises(OcrError) as exc:
            ocr.extract_text("frame.png")
        assert "rapidocr" in str(exc.value)

    def test_init_error_wrapped(self, monkeypatch):
        fake, _ = _fake_rapidocr(
            init_error=ValueError("Unsupported rec.lang_type='xx'")
        )
        monkeypatch.setattr(ocr, "_import_rapidocr", lambda: fake)
        with pytest.raises(OcrError) as exc:
            ocr.extract_text("frame.png", lang="xx")
        assert "Unsupported" in str(exc.value)

    def test_read_error_wrapped(self, monkeypatch):
        fake, _ = _fake_rapidocr(error=RuntimeError("boom"))
        monkeypatch.setattr(ocr, "_import_rapidocr", lambda: fake)
        with pytest.raises(OcrError) as exc:
            ocr.extract_text("frame.png")
        assert "boom" in str(exc.value)


class TestStatus:
    def test_available(self, monkeypatch):
        monkeypatch.setattr(ocr, "_module_available", lambda name: True)
        assert ocr.status() == {
            "available": True,
            "rapidocr": True,
            "onnxruntime": True,
        }

    def test_missing_backend(self, monkeypatch):
        monkeypatch.setattr(
            ocr, "_module_available", lambda name: name == "rapidocr"
        )
        assert ocr.status() == {
            "available": False,
            "rapidocr": True,
            "onnxruntime": False,
        }

    def test_missing_package(self, monkeypatch):
        monkeypatch.setattr(ocr, "_module_available", lambda name: False)
        assert ocr.status() == {
            "available": False,
            "rapidocr": False,
            "onnxruntime": False,
        }


# ---------------------------------------------------------------------------
# End-to-end (skipped when the optional extra is not installed)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not ocr.status()["available"], reason="rapidocr extra not installed"
)
def test_extracts_text_from_rendered_image(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    ImageFont = pytest.importorskip("PIL.ImageFont")

    image = Image.new("RGB", (480, 160), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 40)
    except OSError:
        font = ImageFont.load_default(size=40)
    draw.text((20, 20), "PRESS START", fill=(255, 255, 255), font=font)
    draw.text((20, 90), "CONTINUE", fill=(255, 220, 0), font=font)
    path = tmp_path / "frame.png"
    image.save(path)

    result = ocr.extract_text(path, lang="en")

    assert result["available"] is True
    assert result["engine"] == "rapidocr"
    assert "PRESS" in result["text"].upper()
    assert result["lines"]
    assert result["words"]


# ---------------------------------------------------------------------------
# gc_capture_screenshot(ocr=...)
# ---------------------------------------------------------------------------
class _FakeInstance:
    def capture_screenshot(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x89PNG\r\n\x1a\n")
        return dest


@pytest.fixture
def fake_capture(monkeypatch, tmp_path):
    """Point the screenshot tool at a fake instance and a temp captures dir."""
    monkeypatch.setattr(server, "_instance", lambda instance_id: _FakeInstance())
    monkeypatch.setattr(server, "_captures_dir", lambda: tmp_path)
    return tmp_path


class TestScreenshotOcr:
    def test_no_ocr_by_default(self, fake_capture):
        result = server.gc_capture_screenshot("game-1")
        assert "ocr" not in result
        assert Path(result["path"]).is_file()

    def test_ocr_adds_text(self, fake_capture, monkeypatch):
        monkeypatch.setattr(
            server,
            "extract_text",
            lambda path, lang=None: {
                "available": True,
                "engine": "rapidocr",
                "lang": lang or "en",
                "text": "HELLO",
                "lines": [],
                "words": [],
            },
        )
        result = server.gc_capture_screenshot("game-1", ocr=True, ocr_lang="de")
        assert result["ocr"]["text"] == "HELLO"
        assert result["ocr"]["lang"] == "de"
        assert Path(result["path"]).is_file()

    def test_ocr_uses_config_lang(self, fake_capture, monkeypatch):
        monkeypatch.setattr(server.config, "ocr_lang", "fr")
        monkeypatch.setattr(
            server, "extract_text",
            lambda path, lang=None: {"text": "", "lang": lang},
        )
        result = server.gc_capture_screenshot("game-1", ocr=True)
        assert result["ocr"]["lang"] == "fr"

    def test_ocr_unavailable_reports_error(self, fake_capture, monkeypatch):
        def boom(path, lang=None):
            raise OcrError("OCR requires the optional 'rapidocr' package")

        monkeypatch.setattr(server, "extract_text", boom)
        result = server.gc_capture_screenshot("game-1", ocr=True)
        # The screenshot is still saved and the OCR failure is reported inline.
        assert result["ocr"]["available"] is False
        assert "rapidocr" in result["ocr"]["error"]
        assert Path(result["path"]).is_file()


# ---------------------------------------------------------------------------
# gc_server_info includes OCR readiness
# ---------------------------------------------------------------------------
class _Count:
    def __len__(self) -> int:
        return 0


class TestServerInfoOcr:
    def test_reports_ocr_status_and_lang(self, monkeypatch):
        monkeypatch.setattr(ocr, "_module_available", lambda name: False)
        monkeypatch.setattr(server, "hardware", _Count())
        monkeypatch.setattr(server, "manager", _Count())
        info = server.gc_server_info()
        assert info["ocr"]["available"] is False
        assert info["ocr_lang"] == server.config.ocr_lang
