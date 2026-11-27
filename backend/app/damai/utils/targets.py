"""Semantic click targets for Damai pages, decoupled from coordinates."""
from __future__ import annotations

from dataclasses import dataclass

from backend.app.damai.utils.config import DamaiConfig
from backend.app.damai.utils.pages import Page


@dataclass(frozen=True)
class Target:
    name: str
    page: Page
    description: str
    keywords: tuple[str, ...] = ()
    index: int | None = None


TARGET_LABELS: dict[str, str] = {
    'buy_bar': '详情页底部购票栏',
    'session': '目标场次按钮',
    'price': '目标票档按钮',
    'confirm': 'SKU 底部下单按钮',
    'submit': '订单页立即提交按钮',
}


def build(cfg: DamaiConfig) -> dict[str, Target]:
    sess_desc = f'场次按钮（包含「{cfg.session.text}」的场次卡片）' if cfg.session.text else '第一个可选场次卡片'
    price_desc = f'票档按钮（包含「{cfg.price.text}」的票档卡片）' if cfg.price.text else '第一个可售票档卡片'
    if cfg.order_mode == 'seat_map':
        confirm_desc = '底部右侧「去选座 / 选座购买」按钮'
        confirm_kw = ('去选座', '选座购买', '确定')
    else:
        confirm_desc = '底部橙粉渐变「一键下单 / 确定 / 立即购买」按钮'
        confirm_kw = ('一键下单', '确定', '立即购买', '确认')

    return {
        'buy_bar': Target(
            name='buy_bar',
            page=Page.DETAIL,
            description='底部右侧粉红渐变主购票按钮（立即预订 / 立即购买 / 选座购买）',
            keywords=('立即预订', '立即购买', '选座购买', '缺货登记'),
        ),
        'session': Target(
            name='session',
            page=Page.SKU,
            description=sess_desc,
            keywords=(cfg.session.text,) if cfg.session.text else (),
            index=cfg.session.index,
        ),
        'price': Target(
            name='price',
            page=Page.SKU,
            description=price_desc,
            keywords=(cfg.price.text,) if cfg.price.text else (),
            index=cfg.price.index,
        ),
        'confirm': Target(
            name='confirm',
            page=Page.SKU,
            description=confirm_desc,
            keywords=confirm_kw,
        ),
        'submit': Target(
            name='submit',
            page=Page.ORDER,
            description='订单确认页底部右下角「立即提交 / 提交订单」按钮',
            keywords=('立即提交', '提交订单', '确认并支付'),
        ),
    }
