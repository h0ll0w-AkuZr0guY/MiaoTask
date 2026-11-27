"""Uniform locator protocol and screen snapshot container for Damai."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from xml.etree import ElementTree as ET

from PIL import Image

from backend.app.damai.utils.targets import Target

Point = tuple[int, int]


@dataclass(frozen=True)
class Snapshot:
    xml: str
    root: ET.Element | None
    image: Image.Image | None
    width: int
    height: int
    activity: str = ''


class Locator(Protocol):
    name: str

    def locate(self, target: Target, snap: Snapshot) -> Point | None: ...
