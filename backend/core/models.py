"""Driver-independent text geometry and task contracts."""
from dataclasses import asdict, dataclass
import re
import unicodedata

def normalize(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text)))


def bounds_of(value):
    if isinstance(value, dict):
        return tuple(int(value.get(k, 0)) for k in ("left", "top", "right", "bottom"))
    if isinstance(value, (tuple, list)) and len(value) == 4:
        return tuple(map(int, value))
    numbers = re.findall(r"-?\d+", str(value or ""))
    return tuple(map(int, numbers[:4])) if len(numbers) >= 4 else (0, 0, 0, 0)


@dataclass
class TextNode:
    text: str
    bounds: tuple
    source: str = "ui"

    @property
    def center(self):
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


@dataclass
class Task:
    key: str
    title: str
    detail: str
    action: str
    bounds: tuple
    current: int = 0
    total: int = 1
    seconds: int = 0
    kind: str = "browse"
    skip_reason: str = ""
    target_app: str = ""
    target_packages: tuple = ()
    browse_once: bool = False

    def to_dict(self):
        result = asdict(self)
        if not result['browse_once']:
            result.pop('browse_once')
        return result
