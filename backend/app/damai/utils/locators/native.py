"""Native Android XML locator reading bounds directly from uiautomator hierarchy."""
from __future__ import annotations

from backend.app.damai.utils.hierarchy import (
    ID_BUY_BAR,
    ID_CONFIRM,
    ID_QUICK_ORDER,
    ID_SEAT_MAP,
    Row,
    clickable_text_box,
    first_box,
    split_rows,
)
from backend.app.damai.utils.locators.base import Point, Snapshot
from backend.app.damai.utils.locators.ocr import text_matches
from backend.app.damai.utils.targets import Target


def pick_row(rows: list[Row], target: Target, *, prefer_available: bool = False) -> Row | None:
    if not rows:
        return None
    if target.index is not None:
        return rows[target.index] if 0 <= target.index < len(rows) else None
    keywords = [kw for kw in target.keywords if kw]
    if keywords:
        matches = [row for row in rows if any(text_matches(kw, row.label) for kw in keywords)]
        if not matches:
            return None
        if prefer_available:
            available = [row for row in matches if not row.sold_out]
            if available:
                return available[0]
        return matches[0]
    if prefer_available:
        available = [row for row in rows if not row.sold_out]
        if available:
            return available[0]
    return rows[0]


class NativeLocator:
    name = 'native'

    def locate(self, target: Target, snap: Snapshot) -> Point | None:
        if snap.root is None:
            return None
        if target.name == 'buy_bar':
            box = first_box(snap.root, ID_BUY_BAR)
            if box is None:
                box = clickable_text_box(
                    snap.root,
                    ('立即预订', '立即购买', '选座购买', '缺货登记'),
                    min_top_ratio=0.75,
                    screen_height=snap.height,
                )
            return box.center if box else None

        if target.name == 'confirm':
            seat_mode = bool(target.keywords and target.keywords[0] == '去选座')
            if seat_mode:
                box = (
                    first_box(snap.root, ID_SEAT_MAP)
                    or first_box(snap.root, ID_CONFIRM)
                    or clickable_text_box(
                        snap.root,
                        ('去选座', '选座购买', '确定'),
                        min_top_ratio=0.75,
                        screen_height=snap.height,
                    )
                )
            else:
                # Live-sale quick-seat mode replaces btn_buy_view with 一键下单 (ID_QUICK_ORDER).
                box = (
                    first_box(snap.root, ID_QUICK_ORDER)
                    or first_box(snap.root, ID_CONFIRM)
                    or clickable_text_box(
                        snap.root,
                        ('一键下单', '确定', '立即购买', '确认'),
                        min_top_ratio=0.75,
                        screen_height=snap.height,
                    )
                )
            return box.center if box else None

        if target.name == 'submit':
            box = clickable_text_box(
                snap.root,
                ('立即提交', '提交订单', '确认并支付'),
                min_top_ratio=0.75,
                screen_height=snap.height,
            )
            return box.center if box else None

        sessions, prices = split_rows(snap.root, snap.image)
        if target.name == 'session':
            chosen = pick_row(sessions, target, prefer_available=False)
            return chosen.box.center if chosen else None
        if target.name == 'price':
            chosen = pick_row(prices, target, prefer_available=True)
            return chosen.box.center if chosen else None
        return None
