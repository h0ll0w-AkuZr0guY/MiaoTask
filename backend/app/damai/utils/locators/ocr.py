"""macOS Apple Silicon Vision OCR locator using horizontal strips to preserve small text."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Callable

from PIL import Image

from backend.app.damai.utils.config import OcrConfig
from backend.app.damai.utils.hierarchy import Box, split_rows
from backend.app.damai.utils.locators.base import Point, Snapshot
from backend.app.damai.utils.targets import Target
from backend.core.device.screen import local_ocr_engine


@dataclass(frozen=True)
class OcrSpan:
    text: str
    confidence: float
    box: Box


def _norm(value: str) -> str:
    return re.sub(r'\s+', '', value or '').lower()


def text_matches(keyword: str, candidate: str) -> bool:
    """Match keyword inside candidate while preventing integer substring collisions ('580' vs '1580')."""
    kw, cand = _norm(keyword), _norm(candidate)
    if not kw or not cand:
        return False
    if kw.isdigit():
        return bool(re.search(rf'(?<!\d){re.escape(kw)}(?!\d)', cand))
    return kw in cand


def _poly_to_box(poly, y_offset: int = 0) -> Box | None:
    try:
        xs = [float(pt[0]) for pt in poly]
        ys = [float(pt[1]) + y_offset for pt in poly]
    except (TypeError, ValueError, IndexError):
        return None
    left, top, right, bottom = int(round(min(xs))), int(round(min(ys))), int(round(max(xs))), int(round(max(ys)))
    return Box(left, top, right, bottom) if right > left and bottom > top else None


def recognize_strips(
    image: Image.Image,
    *,
    min_conf: float = 0.3,
    strip_h: int = 700,
    overlap: int = 150,
    engine: Callable | None = None,
) -> list[OcrSpan]:
    """Recognize full phone screenshot in horizontal strips to prevent Vision downscaling loss."""
    width, height = image.size
    if width <= 0 or height <= 0:
        return []
    ocr_engine = engine if engine is not None else local_ocr_engine()
    spans: list[OcrSpan] = []
    top = 0
    while top < height:
        bottom = min(height, top + strip_h)
        strip_img = image.crop((0, top, width, bottom))
        try:
            res = ocr_engine(strip_img, language_correction=False)
        except TypeError:
            res = ocr_engine(strip_img)
        boxes = res.boxes or ()
        txts = res.txts or ()
        scores = res.scores or ()
        for poly, txt, score in zip(boxes, txts, scores):
            conf = float(score)
            if conf < min_conf or not txt:
                continue
            box = _poly_to_box(poly, y_offset=top)
            if box is None:
                continue
            cx, cy = box.center
            if any(s.text == txt and math.hypot(s.box.center[0] - cx, s.box.center[1] - cy) < 24 for s in spans):
                continue
            spans.append(OcrSpan(text=str(txt), confidence=conf, box=box))
        if bottom >= height:
            break
        top += max(100, strip_h - overlap)
    return spans


class OcrLocator:
    name = 'ocr'

    def __init__(self, cfg: OcrConfig, *, engine: Callable | None = None) -> None:
        self.cfg = cfg
        self._engine = engine
        self._cached_image_id: int | None = None
        self._cached_spans: list[OcrSpan] = []

    def spans(self, image: Image.Image) -> list[OcrSpan]:
        if id(image) != self._cached_image_id:
            self._cached_spans = recognize_strips(
                image,
                min_conf=self.cfg.min_conf,
                engine=self._engine,
            )
            self._cached_image_id = id(image)
        return self._cached_spans

    def locate(self, target: Target, snap: Snapshot) -> Point | None:
        if not self.cfg.enabled or snap.image is None:
            return None
        keywords = [kw for kw in target.keywords if kw]
        if not keywords:
            return None
        all_spans = self.spans(snap.image)

        bottom_bar = target.name in ('buy_bar', 'confirm', 'submit')
        min_y = int(snap.height * 0.75) if bottom_bar else 0

        for kw in keywords:
            hits = [s for s in all_spans if s.box.center[1] >= min_y and text_matches(kw, s.text)]
            if hits:
                hits.sort(key=lambda s: (-s.box.center[1] if bottom_bar else s.box.center[1], -s.confidence))
                pt = hits[0].box.center
                return self._snap_to_row(target, snap, pt)
        return None

    @staticmethod
    def _snap_to_row(target: Target, snap: Snapshot, pt: Point) -> Point:
        if snap.root is None or target.name not in ('session', 'price'):
            return pt
        sessions, prices = split_rows(snap.root)
        rows = sessions if target.name == 'session' else prices
        for row in rows:
            if row.box.contains(*pt):
                return row.box.center
        return pt
