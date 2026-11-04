from backend.core.execution.gestures import validate_swipes
import re


def keyword_list(value, name):
    """Accept the settings editor's separators or an explicit JSON word list."""
    if isinstance(value, str):
        if len(value) > 4000:
            raise ValueError(f'{name} 内容过长')
        value = [word for word in re.split(r'[;；\s]+', value.strip()) if word]
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError(f'{name} 必须为最多100项的词表')
    words = []
    for word in value:
        if (not isinstance(word, str) or not 1 <= len(word.strip()) <= 40
                or re.search(r'[;；\s\x00-\x1f]', word.strip())):
            raise ValueError(f'{name} 每项须为1～40个字符，以分号或空白分隔')
        word = word.strip()
        if word not in words:
            words.append(word)
    return words

def validate_config(config):
    validate_swipes(config)
    for name in ('task_search_keywords', 'skip_task_keywords'):
        config[name] = keyword_list(config.get(name, []), name)
    config.setdefault('browse_check_interval', 1)
    config.setdefault('screen_timeout_minutes', 10)
    rounds = config.setdefault('browse_recheck_rounds', 3)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 0 <= rounds <= 10:
        raise ValueError('browse_recheck_rounds 必须为0～10的整数')
    delay = config.setdefault('completion_return_ms', 0)
    if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= 10000:
        raise ValueError('completion_return_ms 必须为0～10000的整数')
    quality = config.setdefault('screenshot_quality', 70)
    if isinstance(quality, bool) or not isinstance(quality, int) or not 30 <= quality <= 95:
        raise ValueError('screenshot_quality 必须为30～95的整数')
    limits = {
        'default_browse_seconds': (1, 120), 'visit_seconds': (1, 120),
        'browse_extra_seconds': (0, 60),
        'max_task_seconds': (5, 180), 'settle_seconds': (0, 10),
        'page_timeout': (3, 60), 'return_timeout': (5, 90),
        'browse_check_interval': (.5, 10),
        'screen_timeout_minutes': (1, 1440),
        'max_back_steps': (1, 8), 'max_attempts': (1, 3),
        'max_tasks': (1, 300), 'max_scroll_pages': (1, 20),
    }
    for key, (low, high) in limits.items():
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not low <= value <= high:
            raise ValueError(f'{key} 必须在 {low}～{high} 之间')
        if key.startswith('max_') and key not in ('max_task_seconds',) and not isinstance(value, int):
            raise ValueError(f'{key} 必须是整数')
    for key in ('allow_cross_app', 'ocr', 'scroll_browse', 'claim_rewards'):
        if not isinstance(config.get(key), bool):
            raise ValueError(f'{key} 必须为布尔值')
    for key in ('minimum_volume', 'minimum_brightness', 'reset_after_activity', 'keep_screen_on', 'compressed_screenshots'):
        if not isinstance(config.setdefault(key, False), bool):
            raise ValueError(f'{key} 必须为布尔值')
    if not isinstance(config.setdefault('clear_clipboard', True), bool):
        raise ValueError('clear_clipboard 必须为布尔值')
    kinds = config.get('enabled_task_kinds', ['browse', 'search', 'visit', 'claim'])
    if (not isinstance(kinds, list) or not kinds
            or any(not isinstance(kind, str) or kind not in {'browse', 'search', 'visit', 'claim'} for kind in kinds)
            or len(kinds) != len(set(kinds))):
        raise ValueError('本轮任务类型至少选择一项，且只能包含浏览、搜索、跳转和领奖')
    config['enabled_task_kinds'] = kinds
    if not isinstance(config.get('search_keyword'), str) or len(config['search_keyword']) > 120:
        raise ValueError('活动搜索词过长或格式错误')
    query = config.setdefault('task_search_keyword', '手机壳')
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 40:
        raise ValueError('任务搜索词必须为 1～40 个字符')
    for key in ('entry_labels', 'task_entry_labels'):
        labels = config.get(key)
        if not isinstance(labels, list) or not labels or not all(isinstance(v, str) and 0 < len(v) < 80 for v in labels):
            raise ValueError(f'{key} 必须为非空文字列表')
    return config
