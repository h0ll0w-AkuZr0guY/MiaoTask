"""Anthropic-compatible multimodal vision locator using standard-library urllib."""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import urllib.error
import urllib.request

from PIL import Image

from backend.app.damai.utils.config import BROKEN_VISION_MODEL_PREFIXES, LlmConfig
from backend.app.damai.utils.locators.base import Point, Snapshot
from backend.app.damai.utils.targets import Target

log = logging.getLogger('backend.damai.llm')

SYSTEM_PROMPT = (
    '你是手机屏幕控件定位器。给定一张安卓截图与目标控件描述，返回该控件可点击区域中心的像素坐标。'
    '只输出一行 JSON：{"found": true, "x": 整数, "y": 整数, "reason": "简短说明"}；'
    '若画面中不存在该控件则输出 {"found": false, "reason": "..."}。'
    '坐标以传入图片的左上角为 (0,0)，单位为该图片的像素，严禁归一化到 0-1000。'
)

MAX_LONG_EDGE = 1568
JSON_RE = re.compile(r'\{[^{}]*\}')


def _encode_jpeg(img: Image.Image) -> tuple[str, float]:
    width, height = img.size
    long_edge = max(width, height)
    scale = min(1.0, MAX_LONG_EDGE / long_edge) if long_edge else 1.0
    resized = (
        img.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
        if scale < 1.0 else img
    )
    buf = io.BytesIO()
    resized.convert('RGB').save(buf, format='JPEG', quality=82)
    return base64.b64encode(buf.getvalue()).decode('ascii'), scale


def extract_point(text: str) -> Point | None:
    for match in JSON_RE.finditer(text or ''):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or not obj.get('found'):
            continue
        x, y = obj.get('x'), obj.get('y')
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool) and not isinstance(y, bool):
            return (int(round(x)), int(round(y)))
    return None


def _http_json(url: str, *, method: str = 'GET', headers: dict[str, str], payload: dict | None = None, timeout_s: float = 10.0) -> dict:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=max(1.0, float(timeout_s))) as resp:
        return json.loads(resp.read().decode('utf-8', errors='replace'))


class LlmLocator:
    name = 'llm'

    def __init__(self, cfg: LlmConfig) -> None:
        if cfg.enabled and cfg.model.lower().startswith(BROKEN_VISION_MODEL_PREFIXES):
            raise ValueError(
                f'模型 {cfg.model!r} 的像素坐标偏差超过 300px，无法用于大麦校准，请改用 claude-sonnet-4-6'
            )
        self.cfg = cfg

    def locate(self, target: Target, snap: Snapshot) -> Point | None:
        if not self.cfg.enabled or snap.image is None or not self.cfg.base_url or not self.cfg.api_key:
            return None
        b64, scale = _encode_jpeg(snap.image)
        scaled_w = round(snap.width * scale)
        scaled_h = round(snap.height * scale)
        kw = f'（关键词：{" / ".join(k for k in target.keywords if k)}）' if any(target.keywords) else ''
        user_text = f'图片尺寸 {scaled_w}x{scaled_h} px。目标：{target.description}{kw}。'

        payload = {
            'model': self.cfg.model,
            'max_tokens': 160,
            'system': SYSTEM_PROMPT,
            'messages': [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': b64}},
                        {'type': 'text', 'text': user_text},
                    ],
                }
            ],
        }
        headers = {
            'x-api-key': self.cfg.api_key,
            'Authorization': f'Bearer {self.cfg.api_key}',
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        }
        url = f"{self.cfg.base_url.rstrip('/')}/v1/messages"
        try:
            data = _http_json(url, method='POST', headers=headers, payload=payload, timeout_s=self.cfg.timeout_s)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning('视觉模型请求失败 (%s): %s', target.name, exc)
            return None

        text = ''.join(
            block.get('text', '')
            for block in (data.get('content') or [])
            if isinstance(block, dict) and block.get('type') == 'text'
        )
        pt = extract_point(text)
        if pt is None:
            log.info('视觉模型未定位到 %s: %s', target.name, text.strip()[:120])
            return None
        x = round(pt[0] / scale)
        y = round(pt[1] / scale)
        if not (0 <= x <= snap.width and 0 <= y <= snap.height):
            log.warning('视觉模型返回越界坐标 %s -> (%d, %d)', target.name, x, y)
            return None
        return (x, y)


def list_models(base_url: str, api_key: str, timeout_s: float = 8.0) -> list[str]:
    clean_url = (base_url or '').strip().rstrip('/')
    clean_key = (api_key or '').strip()
    if not clean_url or not clean_key:
        raise ValueError('请先填写视觉模型接口地址 (Base URL) 与 API Key')
    headers = {
        'x-api-key': clean_key,
        'Authorization': f'Bearer {clean_key}',
        'anthropic-version': '2023-06-01',
    }
    try:
        data = _http_json(f'{clean_url}/v1/models', method='GET', headers=headers, timeout_s=timeout_s)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ValueError(f'无法获取模型列表：{exc}') from exc
    return [str(item['id']) for item in (data.get('data') or []) if isinstance(item, dict) and item.get('id')]
