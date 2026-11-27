"""Page classification for Damai ticket-grabbing state machine."""
from __future__ import annotations

from enum import Enum
from xml.etree import ElementTree as ET

from backend.app.damai.utils.hierarchy import (
    ID_BUY_BAR,
    ID_CONFIRM,
    ID_ORDER_TITLE,
    ID_PRICE_TITLE,
    ID_QUICK_ORDER,
    ID_ROW,
    ID_SEAT_CONFIRM,
    ID_SEAT_CONTAINER,
    ID_SELL_TIME,
    ID_VIEWER_CHECKBOX,
    first_box,
)


class Page(str, Enum):
    DETAIL = 'detail'
    SKU = 'sku'
    ORDER = 'order'
    SEAT = 'seat'
    PAY = 'pay'
    VERIFY = 'verify'
    UNKNOWN = 'unknown'


PAGE_LABELS: dict[Page, str] = {
    Page.DETAIL: '商品详情页',
    Page.SKU: '场次票档选择页',
    Page.ORDER: '订单确认页',
    Page.SEAT: '在线选座页',
    Page.PAY: '支付收银台',
    Page.VERIFY: '滑块安全验证',
    Page.UNKNOWN: '未知页面',
}

VERIFY_WORDS = ('完成验证', '拖动滑块', '向右滑动', '操作过于频繁', '请按住滑块', '安全验证')
VERIFY_RESOURCE_HINTS = ('baxia', 'nocaptcha', 'puzzle-captcha', 'nc_1_')
PAY_WORDS = ('支付宝付款', '确认付款', '付款详情', '请输入支付密码')


def detect(activity: str, root: ET.Element | None) -> Page:
    act = activity or ''
    act_lower = act.lower()

    if any(k in act for k in ('ContainerActivity', 'Captcha', 'Verify')) or 'middletier.fc' in act_lower:
        return Page.VERIFY
    if root is not None:
        for el in root.iter():
            rid = (el.attrib.get('resource-id') or '').lower()
            if rid and any(h in rid for h in VERIFY_RESOURCE_HINTS):
                return Page.VERIFY
            txt = el.attrib.get('text') or el.attrib.get('content-desc') or ''
            if txt and any(w in txt for w in VERIFY_WORDS):
                return Page.VERIFY

    if any(k in act for k in ('H5PayActivity', 'PayActivity', 'Alipay', 'Cashier', 'MSPContainerActivity')):
        return Page.PAY

    if any(k in act for k in ('DmOrderActivity', 'OrderConfirm', 'TradeOrder', 'UltronOrder')):
        return Page.ORDER
    if root is not None:
        if first_box(root, ID_ORDER_TITLE) or first_box(root, ID_VIEWER_CHECKBOX):
            return Page.ORDER
        joined_order_hints = set()
        for el in root.iter():
            txt = (el.attrib.get('text') or '').strip()
            if txt in ('确认购买', '立即提交', '提交订单', '实名观演人'):
                joined_order_hints.add(txt)
            if txt and any(w in txt for w in PAY_WORDS):
                return Page.PAY
        if '确认购买' in joined_order_hints or ('立即提交' in joined_order_hints and '实名观演人' in joined_order_hints):
            return Page.ORDER

    if any(k in act for k in ('TSeatActivity', 'SeatActivity', 'SeatSelect')):
        return Page.SEAT
    if root is not None and (first_box(root, ID_SEAT_CONTAINER) or first_box(root, ID_SEAT_CONFIRM)):
        return Page.SEAT

    if any(k in act for k in ('NcovSkuActivity', 'SkuActivity')):
        return Page.SKU
    if root is not None:
        if first_box(root, ID_PRICE_TITLE) or first_box(root, ID_CONFIRM) or first_box(root, ID_QUICK_ORDER):
            return Page.SKU
        if any(el.attrib.get('resource-id') == ID_ROW for el in root.iter()):
            return Page.SKU

    if any(k in act for k in ('ProjectDetailActivity', 'PerformDetail')):
        return Page.DETAIL
    if root is not None and (first_box(root, ID_BUY_BAR) or first_box(root, ID_SELL_TIME)):
        return Page.DETAIL

    return Page.UNKNOWN
