"""Typed configuration and validation for Damai ticket grabbing and Web UI options."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import os
import re

BROKEN_VISION_MODEL_PREFIXES = ('gemini-',)

DEFAULT_ACTIVITY_OPTIONS: dict[str, object] = {
    'session_text': '',
    'session_index': None,
    'price_text': '',
    'price_index': None,
    'sell_time': '',
    'lead_ms': 80,
    'order_mode': 'quick_order',
    'viewer_count': 1,
    'viewer_names': [],
    'submit_order': False,
    'jitter_min_ms': 120,
    'jitter_max_ms': 280,
    'max_attempts': 40,
    'page_timeout_s': 2.5,
    'agree_tolerance_px': 45,
    'auto_resume_verify': True,
    'verify_wait_s': 90.0,
    'auto_select_session_on_calibrate': True,
    'llm_enabled': False,
    'llm_base_url': '',
    'llm_model': 'claude-sonnet-4-6',
    'llm_timeout_s': 15.0,
    'ocr_min_conf': 0.3,
}


@dataclass(frozen=True)
class Choice:
    text: str = ''
    index: int | None = None


@dataclass(frozen=True)
class LlmConfig:
    enabled: bool = False
    base_url: str = ''
    api_key: str = ''
    model: str = 'claude-sonnet-4-6'
    timeout_s: float = 15.0


@dataclass(frozen=True)
class OcrConfig:
    enabled: bool = True
    min_conf: float = 0.3


@dataclass(frozen=True)
class DamaiConfig:
    session: Choice = Choice()
    price: Choice = Choice()
    sell_time: str = ''
    lead_ms: int = 80
    order_mode: str = 'quick_order'
    viewer_count: int = 1
    viewer_names: tuple[str, ...] = ()
    submit_order: bool = False
    jitter_ms: tuple[int, int] = (120, 280)
    max_attempts: int = 40
    page_timeout_s: float = 2.5
    agree_tolerance_px: int = 45
    auto_resume_verify: bool = True
    verify_wait_s: float = 90.0
    auto_select_session_on_calibrate: bool = True
    llm: LlmConfig = field(default_factory=LlmConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)


def _optional_index(value, label: str) -> int | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValueError(f'{label} 必须为非负整数或留空')
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if not re.fullmatch(r'\d+', value):
            raise ValueError(f'{label} 必须为非负整数或留空')
        value = int(value)
    if not isinstance(value, int) or not 0 <= value <= 50:
        raise ValueError(f'{label} 必须为 0～50 的整数或留空')
    return value


def _bounded_int(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f'{label} 必须为 {low}～{high} 的整数')
    if isinstance(value, str) and re.fullmatch(r'-?\d+', value.strip()):
        value = int(value.strip())
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{label} 必须为 {low}～{high} 的整数')
    return value


def _bounded_float(value, label: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f'{label} 必须在 {low}～{high} 之间')
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError as exc:
            raise ValueError(f'{label} 必须在 {low}～{high} 之间') from exc
    if not isinstance(value, (int, float)) or not low <= float(value) <= high:
        raise ValueError(f'{label} 必须在 {low}～{high} 之间')
    return round(float(value), 3)


def _check_sell_time_format(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ''
    candidates = ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M',
                  '%H:%M:%S.%f', '%H:%M:%S', '%H:%M')
    normalized = cleaned.replace('T', ' ')
    for fmt in candidates:
        try:
            datetime.strptime(normalized, fmt)
            return normalized
        except ValueError:
            continue
    raise ValueError('开售时间格式须为 YYYY-MM-DD HH:MM:SS（或 HH:MM:SS），留空则自动读取详情页或立即开抢')


def _parse_viewer_names(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r'[,，;；\s]+', value.strip()) if part.strip()]
    elif isinstance(value, list):
        items = [str(part).strip() for part in value if isinstance(part, str) and part.strip()]
    else:
        raise ValueError('指定观演人姓名格式无效')
    if len(items) > 8 or any(len(name) > 32 for name in items):
        raise ValueError('指定观演人最多 8 人，每项不超过 32 个字符')
    deduped: list[str] = []
    for name in items:
        if name not in deduped:
            deduped.append(name)
    return deduped


def validate_damai_options(raw: dict | None) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError('大麦抢票参数必须为对象')
    unknown = set(raw) - set(DEFAULT_ACTIVITY_OPTIONS)
    if unknown:
        raise ValueError('未知的大麦抢票配置项：' + ', '.join(sorted(unknown)))
    merged = dict(DEFAULT_ACTIVITY_OPTIONS, **raw)

    session_text = str(merged.get('session_text') or '').strip()
    price_text = str(merged.get('price_text') or '').strip()
    if len(session_text) > 80 or len(price_text) > 80:
        raise ValueError('场次或票档关键词不能超过 80 个字符')

    session_index = _optional_index(merged.get('session_index'), '场次序号')
    price_index = _optional_index(merged.get('price_index'), '票档序号')
    sell_time = _check_sell_time_format(str(merged.get('sell_time') or ''))
    lead_ms = _bounded_int(merged.get('lead_ms', 80), '提前点击毫秒数 (lead_ms)', -2000, 5000)

    order_mode = str(merged.get('order_mode') or 'quick_order').strip()
    if order_mode not in ('quick_order', 'seat_map'):
        raise ValueError('下单模式只能是 quick_order（一键下单/确定）或 seat_map（去选座）')

    viewer_count = _bounded_int(merged.get('viewer_count', 1), '观演人数量', 0, 8)
    viewer_names = _parse_viewer_names(merged.get('viewer_names'))
    if viewer_names and viewer_count < len(viewer_names):
        viewer_count = len(viewer_names)

    for bool_key, label in (
        ('submit_order', '自动提交订单'),
        ('auto_resume_verify', '滑块验证后自动恢复'),
        ('auto_select_session_on_calibrate', '校准时自动点选场次展开票档'),
        ('llm_enabled', '启用视觉大模型校准'),
    ):
        if not isinstance(merged.get(bool_key), bool):
            raise ValueError(f'{label} 必须为布尔值')

    jitter_min = _bounded_int(merged.get('jitter_min_ms', 120), '最小点击抖动 (ms)', 0, 2000)
    jitter_max = _bounded_int(merged.get('jitter_max_ms', 280), '最大点击抖动 (ms)', 0, 2000)
    if jitter_min > jitter_max:
        raise ValueError('最小点击抖动不能大于最大点击抖动')

    max_attempts = _bounded_int(merged.get('max_attempts', 40), '最大抢票轮数', 1, 500)
    page_timeout_s = _bounded_float(merged.get('page_timeout_s', 2.5), '页面跳转超时 (秒)', 0.5, 30.0)
    agree_tolerance_px = _bounded_int(merged.get('agree_tolerance_px', 45), '坐标一致容差 (px)', 5, 200)
    verify_wait_s = _bounded_float(merged.get('verify_wait_s', 90.0), '滑块验证等待上限 (秒)', 5.0, 300.0)
    llm_timeout_s = _bounded_float(merged.get('llm_timeout_s', 15.0), '视觉模型超时 (秒)', 2.0, 60.0)
    ocr_min_conf = _bounded_float(merged.get('ocr_min_conf', 0.3), 'Vision OCR 置信度阈值', 0.05, 0.95)

    llm_base_url = str(merged.get('llm_base_url') or '').strip()
    if len(llm_base_url) > 300 or (llm_base_url and not re.match(r'^https?://', llm_base_url, re.I)):
        raise ValueError('视觉模型接口地址必须以 http:// 或 https:// 开头（或留空）')
    llm_model = str(merged.get('llm_model') or 'claude-sonnet-4-6').strip()
    if not llm_model or len(llm_model) > 120:
        raise ValueError('视觉模型名称长度须在 1～120 个字符之间')
    if llm_model.lower().startswith(BROKEN_VISION_MODEL_PREFIXES) and merged['llm_enabled']:
        raise ValueError(
            f'模型 {llm_model!r} 的像素坐标偏差超过 300px，无法用于大麦抢票校准；'
            '请改用 claude-sonnet-4-6 / claude-opus-4-7 或关闭视觉大模型校验'
        )

    return {
        'session_text': session_text,
        'session_index': session_index,
        'price_text': price_text,
        'price_index': price_index,
        'sell_time': sell_time,
        'lead_ms': lead_ms,
        'order_mode': order_mode,
        'viewer_count': viewer_count,
        'viewer_names': viewer_names,
        'submit_order': bool(merged['submit_order']),
        'jitter_min_ms': jitter_min,
        'jitter_max_ms': jitter_max,
        'max_attempts': max_attempts,
        'page_timeout_s': page_timeout_s,
        'agree_tolerance_px': agree_tolerance_px,
        'auto_resume_verify': bool(merged['auto_resume_verify']),
        'verify_wait_s': verify_wait_s,
        'auto_select_session_on_calibrate': bool(merged['auto_select_session_on_calibrate']),
        'llm_enabled': bool(merged['llm_enabled']),
        'llm_base_url': llm_base_url.rstrip('/'),
        'llm_model': llm_model,
        'llm_timeout_s': llm_timeout_s,
        'ocr_min_conf': ocr_min_conf,
    }


def damai_config_from_options(
    options: dict | None,
    *,
    api_key: str = '',
    ocr_enabled: bool = True,
) -> DamaiConfig:
    cleaned = validate_damai_options(options or {})
    resolved_url = str(cleaned['llm_base_url']) or os.environ.get('ANTHROPIC_BASE_URL', '').rstrip('/')
    resolved_key = api_key or os.environ.get('ANTHROPIC_API_KEY', '')
    llm_enabled = bool(cleaned['llm_enabled'] and resolved_url and resolved_key)
    return DamaiConfig(
        session=Choice(text=str(cleaned['session_text']), index=cleaned['session_index']),
        price=Choice(text=str(cleaned['price_text']), index=cleaned['price_index']),
        sell_time=str(cleaned['sell_time']),
        lead_ms=int(cleaned['lead_ms']),
        order_mode=str(cleaned['order_mode']),
        viewer_count=int(cleaned['viewer_count']),
        viewer_names=tuple(cleaned['viewer_names']),
        submit_order=bool(cleaned['submit_order']),
        jitter_ms=(int(cleaned['jitter_min_ms']), int(cleaned['jitter_max_ms'])),
        max_attempts=int(cleaned['max_attempts']),
        page_timeout_s=float(cleaned['page_timeout_s']),
        agree_tolerance_px=int(cleaned['agree_tolerance_px']),
        auto_resume_verify=bool(cleaned['auto_resume_verify']),
        verify_wait_s=float(cleaned['verify_wait_s']),
        auto_select_session_on_calibrate=bool(cleaned['auto_select_session_on_calibrate']),
        llm=LlmConfig(
            enabled=llm_enabled,
            base_url=resolved_url,
            api_key=resolved_key,
            model=str(cleaned['llm_model']),
            timeout_s=float(cleaned['llm_timeout_s']),
        ),
        ocr=OcrConfig(enabled=bool(ocr_enabled), min_conf=float(cleaned['ocr_min_conf'])),
    )
