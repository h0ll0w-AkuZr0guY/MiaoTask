"""Damai coordinate locators: cached, native XML, Vision OCR, and Anthropic Vision LLM."""
from backend.app.damai.utils.locators.base import Locator, Point, Snapshot
from backend.app.damai.utils.locators.cached import CachedLocator, CoordCache, cache_path_for_device, target_signature
from backend.app.damai.utils.locators.llm import LlmLocator, extract_point, list_models
from backend.app.damai.utils.locators.native import NativeLocator
from backend.app.damai.utils.locators.ocr import OcrLocator, OcrSpan, text_matches

__all__ = [
    'CachedLocator',
    'CoordCache',
    'LlmLocator',
    'Locator',
    'NativeLocator',
    'OcrLocator',
    'OcrSpan',
    'Point',
    'Snapshot',
    'cache_path_for_device',
    'extract_point',
    'list_models',
    'target_signature',
    'text_matches',
]
