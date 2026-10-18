"""Local OCR for the quote marker: RapidOCR on ONNX Runtime, plus PNG decoding by Pillow.

Everything runs on this computer: the recognition model ships inside the rapidocr package,
and the engine refuses to start without it, so that RapidOCR never downloads one. ONNX
Runtime's telemetry is turned off before it loads (ORT_DISABLE_TELEMETRY and
disable_telemetry_events). The packages are imported only when the engine is created, so
the rest of the collector works without them; OCR_REQUIREMENTS lists the pinned versions
that setup_observer installs.

The first recognition of a new engine is several times slower than the next ones, because
ONNX Runtime prepares the model then (85 to 95 ms against 7 to 10 ms, measured on Linux).
warm_up makes that first recognition on WARM_UP_SAMPLE, drawn on the spot, before any
reading of the chart, so that no quote pays for it; it also tells what the OCR read there.
"""

import io
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from pocketautonomo.quote_reader import GrayImage, RgbImage

OCR_REQUIREMENTS = ("rapidocr==3.10.0", "onnxruntime==1.31.0")
REC_MODEL = "PP-OCRv6_rec_small.onnx"
UPSCALE_HEIGHT = 48  # the recognizer's input height; smaller lines are enlarged first
WARM_UP_SAMPLE = "1.26543"  # the same sample as the check of setup_observer


@dataclass(frozen=True)
class WarmUp:
    """The first recognition of an engine: what it read in WARM_UP_SAMPLE, and how long it took."""

    text: str
    score: float
    ms: float

    @property
    def read_sample(self):
        return self.text == WARM_UP_SAMPLE


class OcrUnavailable(Exception):
    """The OCR packages are missing or did not start; the message says what to do."""


def install_hint():
    return (
        "o OCR local não está instalado neste Python. Rode de novo o preparar_windows.cmd,"
        f" que instala {' e '.join(OCR_REQUIREMENTS)}."
    )


def decode_png(data):
    """RgbImage of a PNG, decoded by Pillow."""
    try:
        from PIL import Image
    except ImportError as error:
        raise OcrUnavailable(install_hint()) from error
    with Image.open(io.BytesIO(data)) as picture:
        rgb = picture.convert("RGB")
        return RgbImage(rgb.width, rgb.height, rgb.tobytes())


class RapidOcrEngine:
    """recognize(GrayImage) -> (text, score), with RapidOCR's recognizer only."""

    def __init__(self):
        os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
        try:
            import numpy
            import onnxruntime
            from PIL import Image
            import rapidocr
        except ImportError as error:
            raise OcrUnavailable(install_hint()) from error
        if hasattr(onnxruntime, "disable_telemetry_events"):
            onnxruntime.disable_telemetry_events()
        model = Path(rapidocr.__file__).resolve().parent / "models" / REC_MODEL
        if not model.is_file():
            raise OcrUnavailable(
                f"o modelo de reconhecimento {REC_MODEL} não veio com o rapidocr, e o programa"
                " não baixa modelos. Rode de novo o preparar_windows.cmd."
            )
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        try:
            self._engine = rapidocr.RapidOCR(
                params={"Global.log_level": "warning", "Rec.model_path": str(model)}
            )
        except Exception as error:
            raise OcrUnavailable(f"o OCR não iniciou ({type(error).__name__}: {error})") from error
        self._numpy = numpy
        self._image = Image

    def warm_up(self):
        """WarmUp: the first recognition, on WARM_UP_SAMPLE drawn here, so no quote pays for it."""
        from PIL import ImageDraw, ImageFont

        picture = self._image.new("L", (180, 40), 255)
        font = ImageFont.load_default(size=24)
        ImageDraw.Draw(picture).text((10, 20), WARM_UP_SAMPLE, font=font, fill=0, anchor="lm")
        start = time.perf_counter()
        text, score = self.recognize(GrayImage(picture.width, picture.height, picture.tobytes()))
        return WarmUp("".join(text.split()), score, round((time.perf_counter() - start) * 1000, 1))

    def recognize(self, gray):
        """The text of one line and its confidence, from 0 to 1."""
        picture = self._image.frombytes("L", (gray.width, gray.height), gray.data)
        if gray.height < UPSCALE_HEIGHT:
            factor = UPSCALE_HEIGHT / gray.height
            picture = picture.resize(
                (max(1, round(gray.width * factor)), UPSCALE_HEIGHT), self._image.Resampling.BICUBIC
            )
        array = self._numpy.asarray(picture.convert("RGB"))[:, :, ::-1]  # RGB to BGR
        result = self._engine(array, use_det=False, use_cls=False, use_rec=True)
        texts, scores = list(result.txts or ()), list(result.scores or ())
        if not texts:
            return "", 0.0
        return str(texts[0]), float(scores[0]) if scores else 0.0
