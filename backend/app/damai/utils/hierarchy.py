"""Parse Android uiautomator XML and pixel selection state for Damai pages.

Resource IDs and layout structures verified against real Damai 9.x captures:
- Detail buy bar: cn.damai:id/trade_project_detail_purchase_status_bar_container_fl
- Detail sell time: cn.damai:id/id_project_count_sell_time
- SKU rows (sessions + prices share ID): cn.damai:id/ll_perform_item
- SKU price section header: cn.damai:id/tv_price_name
- SKU presale/non-seat confirm: cn.damai:id/btn_buy_view
- SKU live-sale quick order ("一键下单"): cn.damai:id/id_quick_seat_selection_bottom_left_action_layout
- SKU live-sale seat map ("去选座"): cn.damai:id/id_quick_seat_selection_bottom_right_action_layout
- Order confirmation viewer checkbox: cn.damai:id/checkbox + cn.damai:id/text_name
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from xml.etree import ElementTree as ET

BOUNDS_RE = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')

ID_BUY_BAR = 'cn.damai:id/trade_project_detail_purchase_status_bar_container_fl'
ID_SELL_TIME = 'cn.damai:id/id_project_count_sell_time'
ID_ROW = 'cn.damai:id/ll_perform_item'
ID_ROW_TAG = 'cn.damai:id/tv_tag'
ID_PRICE_TITLE = 'cn.damai:id/tv_price_name'
ID_CONFIRM = 'cn.damai:id/btn_buy_view'
ID_QUICK_ORDER = 'cn.damai:id/id_quick_seat_selection_bottom_left_action_layout'
ID_QUICK_ORDER_TEXT = 'cn.damai:id/id_quick_seat_selection_bottom_left_action_tv'
ID_SEAT_MAP = 'cn.damai:id/id_quick_seat_selection_bottom_right_action_layout'
ID_SEAT_MAP_TEXT = 'cn.damai:id/id_quick_seat_selection_bottom_right_action_tv'
ID_ORDER_TITLE = 'cn.damai:id/order_activity_title'
ID_VIEWER_CHECKBOX = 'cn.damai:id/checkbox'
ID_VIEWER_NAME = 'cn.damai:id/text_name'
ID_SEAT_CONTAINER = 'cn.damai:id/seat_view_container'
ID_SEAT_CONFIRM = 'cn.damai:id/seat_confirm_order'

SOLD_OUT_WORDS = ('缺货登记', '无票', '已售罄', '售罄', '暂不可售')
PRICE_WORDS = ('元', '票', '看台', '内场', 'VIP', '套票', '单日', '双日', '早鸟', '正价', '学生')


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom


@dataclass(frozen=True)
class Row:
    index: int
    box: Box
    texts: tuple[str, ...]
    selected: bool
    sold_out: bool

    @property
    def label(self) -> str:
        return ' '.join(self.texts)

    @property
    def has_price(self) -> bool:
        joined = self.label
        return any(w in joined for w in PRICE_WORDS) or bool(re.search(r'\d+\s*元', joined))


@dataclass(frozen=True)
class ViewerItem:
    index: int
    name: str
    box: Box
    checked: bool


def parse_bounds(raw: str) -> Box | None:
    match = BOUNDS_RE.search(raw or '')
    if not match:
        return None
    l, t, r, b = (int(v) for v in match.groups())
    return Box(l, t, r, b) if r > l and b > t else None


def parse_xml(xml_text: str) -> ET.Element | None:
    if not xml_text:
        return None
    start = xml_text.find('<hierarchy')
    if start > 0:
        xml_text = xml_text[start:]
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError:
        return None


def first_box(root: ET.Element | None, resource_id: str) -> Box | None:
    if root is None:
        return None
    for el in root.iter():
        if el.attrib.get('resource-id') == resource_id:
            box = parse_bounds(el.attrib.get('bounds', ''))
            if box:
                return box
    return None


def first_text(root: ET.Element | None, resource_id: str) -> str:
    if root is None:
        return ''
    for el in root.iter():
        if el.attrib.get('resource-id') == resource_id:
            txt = (el.attrib.get('text') or '').strip()
            if txt:
                return txt
            for sub in el.iter():
                txt = (sub.attrib.get('text') or '').strip()
                if txt:
                    return txt
    return ''


def all_texts(el: ET.Element) -> list[str]:
    out: list[str] = []
    for sub in el.iter():
        txt = (sub.attrib.get('text') or '').strip()
        if txt:
            out.append(txt)
    return out


def _is_selected_xml(el: ET.Element) -> bool:
    """Presale SKU renders an ImageView inside cn.damai:id/image_xin when selected."""
    if el.attrib.get('selected') == 'true':
        return True
    for sub in el.iter():
        if sub.attrib.get('resource-id') == 'cn.damai:id/image_xin':
            for leaf in sub.iter():
                if leaf is not sub and leaf.attrib.get('class', '').endswith('ImageView'):
                    return True
    return False


def _is_warm_border_pixel(rgb: tuple[int, ...]) -> bool:
    r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
    return r >= 190 and (r - g) >= 45 and (r - b) >= 60


def has_selected_border(image, box: Box) -> bool:
    """Detect Damai live-sale selected SKU cell border via pixel RGB sampling.

    In live-sale mode, `image_xin` is absent from XML; selected rows draw a
    ~3-4px orange/pink border (`RGB ~(235, 88, 11)`) while unselected rows use
    neutral gray (`RGB ~(225, 225, 225)`). Sampling the bottom inner border
    avoids top-corner badges such as `预售` / `无票` / `缺货登记`.
    """
    if image is None or box.width < 24 or box.height < 20:
        return False
    img_w, img_h = image.size
    if box.right > img_w or box.bottom > img_h or box.left < 0 or box.top < 0:
        return False
    rgb_img = image if image.mode in ('RGB', 'RGBA') else image.convert('RGB')
    x_start = box.left + max(6, int(box.width * 0.2))
    x_end = box.right - max(6, int(box.width * 0.2))
    if x_end <= x_start:
        return False
    step = max(1, (x_end - x_start) // 24)
    xs = list(range(x_start, x_end, step))
    if not xs:
        return False
    max_inset = min(14, max(3, box.height // 4))
    for dy in range(1, max_inset + 1):
        y = box.bottom - 1 - dy
        if not (0 <= y < img_h):
            continue
        warm = sum(1 for x in xs if _is_warm_border_pixel(rgb_img.getpixel((x, y))))
        if warm / len(xs) >= 0.45:
            return True
    return False


def _is_sold_out(el: ET.Element, texts: list[str]) -> bool:
    for sub in el.iter():
        if sub.attrib.get('resource-id') == ID_ROW_TAG:
            tag = (sub.attrib.get('text') or '').strip()
            if any(w in tag for w in SOLD_OUT_WORDS):
                return True
    return any(w in ' '.join(texts) for w in SOLD_OUT_WORDS)


def split_rows(root: ET.Element | None, image=None) -> tuple[list[Row], list[Row]]:
    """Return `(sessions, prices)` sorted in visual reading order (row-major)."""
    if root is None:
        return [], []
    divider_box = first_box(root, ID_PRICE_TITLE)
    divider_y = divider_box.top if divider_box else None

    raw: list[tuple[Box, list[str], bool, bool]] = []
    for el in root.iter():
        if el.attrib.get('resource-id') != ID_ROW:
            continue
        box = parse_bounds(el.attrib.get('bounds', ''))
        if not box:
            continue
        texts = all_texts(el)
        if not texts:
            continue
        selected = _is_selected_xml(el) or (image is not None and has_selected_border(image, box))
        raw.append((box, texts, selected, _is_sold_out(el, texts)))

    raw.sort(key=lambda item: (item[0].top // 40, item[0].left))

    sessions_raw: list[tuple[Box, list[str], bool, bool]] = []
    prices_raw: list[tuple[Box, list[str], bool, bool]] = []
    for item in raw:
        box, texts, _, _ = item
        if divider_y is not None:
            (sessions_raw if box.center[1] < divider_y else prices_raw).append(item)
        else:
            joined = ' '.join(texts)
            is_price = any(w in joined for w in PRICE_WORDS) or bool(re.search(r'\d+\s*元', joined))
            (prices_raw if is_price else sessions_raw).append(item)

    sessions = [Row(i, b, tuple(t), sel, so) for i, (b, t, sel, so) in enumerate(sessions_raw)]
    prices = [Row(i, b, tuple(t), sel, so) for i, (b, t, sel, so) in enumerate(prices_raw)]
    return sessions, prices


def viewer_checkboxes(root: ET.Element | None) -> list[ViewerItem]:
    """Extract real-name viewer rows and checkbox states on `DmOrderActivity`."""
    if root is None:
        return []
    names: list[tuple[Box, str]] = []
    checkboxes: list[tuple[Box, bool]] = []
    for el in root.iter():
        rid = el.attrib.get('resource-id', '')
        cls = el.attrib.get('class', '')
        box = parse_bounds(el.attrib.get('bounds', ''))
        if not box:
            continue
        if rid == ID_VIEWER_NAME:
            txt = (el.attrib.get('text') or '').strip()
            if txt:
                names.append((box, txt))
        elif rid == ID_VIEWER_CHECKBOX or cls == 'android.widget.CheckBox':
            checked = el.attrib.get('checked') == 'true' or el.attrib.get('selected') == 'true'
            checkboxes.append((box, checked))

    checkboxes.sort(key=lambda item: (item[0].top, item[0].left))
    result: list[ViewerItem] = []
    for idx, (cb_box, checked) in enumerate(checkboxes):
        cy = cb_box.center[1]
        matched_name = ''
        best_dy = 120
        for name_box, txt in names:
            dy = abs(name_box.center[1] - cy)
            if dy < best_dy:
                best_dy = dy
                matched_name = txt
        result.append(ViewerItem(index=idx, name=matched_name, box=cb_box, checked=checked))
    return result


def clickable_text_box(
    root: ET.Element | None,
    keywords: tuple[str, ...],
    *,
    min_top_ratio: float = 0.0,
    screen_height: int = 0,
) -> Box | None:
    """Find a clickable control (or its nearest clickable container) matching text."""
    if root is None or not keywords:
        return None
    parent_map = {child: parent for parent in root.iter() for child in parent}
    min_y = int(screen_height * min_top_ratio) if screen_height > 0 and min_top_ratio > 0 else 0

    for kw in keywords:
        if not kw:
            continue
        for el in root.iter():
            txt = (el.attrib.get('text') or el.attrib.get('content-desc') or '').strip()
            if not txt or (txt != kw and kw not in txt):
                continue
            node: ET.Element | None = el
            chosen_box: Box | None = None
            for _ in range(5):
                if node is None:
                    break
                box = parse_bounds(node.attrib.get('bounds', ''))
                if box and node.attrib.get('clickable') == 'true':
                    chosen_box = box
                    break
                if chosen_box is None and box:
                    chosen_box = box
                node = parent_map.get(node)
            if chosen_box and chosen_box.center[1] >= min_y:
                return chosen_box
    return None
