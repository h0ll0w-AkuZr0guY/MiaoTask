"""月月赚浏览任务执行。

真机验证(2026-10-26)确认的流程：
    点「去看看」 → 落地浏览页 → 停留数秒 → 返回
    → 弹出「恭喜获得」浮层（奖励已自动到账，不需要再点领取）
    → 浮层下方有一个白色圆圈 ✕ 关闭按钮（真机实测：控件树里是一个
      空文本、非 clickable 的 TextView，约 198x198，水平正居中、
      位于下半屏 cy≈0.89H），点它可关闭浮层且不退出活动页；
      找不到它时按返回键兜底（会退出活动页，由框架重新导航）

一律用 self.read()：让框架自动在「UI 控件树」和「OCR」之间选，
优先保证兼容性与效率（实测该 H5 页两种来源都能读到按钮与文案）。
"""
import logging

from backend.core.models import TextNode, bounds_of, normalize

logger = logging.getLogger('backend.execution')


class TaskActions:
    def _button_node(self, screen, task):
        """按标题在列表中匹配任务按钮，返回带 center 的 TextNode。"""
        for t in self.tasks(screen):
            if t.title == task.title or t.key == task.key:
                return TextNode('', t.bounds)
        return None

    def _reward_popup(self, screen):
        """奖励到账确认浮层。"""
        label = normalize(self.config.get('reward_popup_text', '恭喜获得'))
        return next((n for n in screen.nodes if label in normalize(n.text)), None)

    def _popup_closer(self, screen):
        """弹窗的关闭控件（下半屏的 ✕ / 关闭 / 取消）。

        优先找带文案的关闭按钮；找不到再按几何特征找 H5 渲染的
        白色圆圈 ✕（空文本的近正方形小节点，水平居中、位于下半屏）。
        都找不到则由调用方按返回键兜底。
        """
        patterns = self.config.get('popup_close_labels') or ['关闭', '取消', '✕', '×']
        for n in screen.nodes:
            if n.center[1] <= screen.height * 0.5:
                continue
            w, h = n.bounds[2] - n.bounds[0], n.bounds[3] - n.bounds[1]
            if w > screen.width * 0.25 or h > screen.height * 0.12:
                continue  # 太大，不是图标按钮
            text = normalize(n.text)
            if any(normalize(p) in text or text == normalize(p) for p in patterns):
                return n
        return self._graphic_close(screen)

    def _graphic_close(self, screen):
        """H5 图形关闭按钮（白色圆圈 ✕）。

        screen.nodes 只保留有文本的节点，空文本的图形 X 会被丢掉，
        所以遍历原始层级 screen.tree 按几何特征找：
        空文本 + 近正方形小节点 + 水平居中 + 下半屏。
        真机实测（2032x3048）：bounds (917,2617)-(1115,2815)，即
        198x198、cx 正好等于屏幕中轴、cy≈0.89H；点它即可关闭弹窗
        且不退出活动页。
        """
        if not getattr(screen, 'tree', None):
            return None
        w0, h0 = screen.width, screen.height
        cands = []

        def visit(item):
            if isinstance(item, list):
                for child in item:
                    visit(child)
            elif isinstance(item, dict):
                attrs = item.get('attributes', item)
                text = (attrs.get('text') or attrs.get('description')
                        or attrs.get('content-desc') or '')
                if not normalize(text):
                    b = bounds_of(attrs.get('bounds', attrs.get('rect')))
                    w, h = b[2] - b[0], b[3] - b[1]
                    if (0.03 * w0 <= w <= 0.18 * w0
                            and 0.03 * h0 <= h <= 0.12 * h0
                            and 0.6 <= w / max(h, 1) <= 1.7):
                        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                        if cy > h0 * 0.5:
                            cands.append((abs(cx - w0 / 2),
                                          abs(cy - h0 * 0.85), b))
                for key in ('children', 'hierarchy'):
                    for child in item.get(key, ()) or ():
                        visit(child)

        visit(screen.tree)
        return TextNode('', min(cands)[2]) if cands else None

    def execute(self, task, before):
        self._active_task = task
        self.update('opening', message='打开任务：' + task.title, remaining=0)
        screen = self.read()
        target = self._button_node(screen, task)
        if not target:
            logger.info('未在当前列表找到任务按钮：%s', task.title)
            return self.recover(), False, False
        self.click(target)
        self.control.wait(0.8)
        # 捷径：浏览页停留数秒后直接返回即可完成（无需真正等待倒计时）
        shortcut = float(self.config.get('browse_shortcut_seconds', 5))
        self.control.wait(shortcut)
        screen = self.recover()
        # 防呆：上面 recover 只保证"像任务列表"，第三方活动任务列表（青春特权…）
        # 也曾被当成月月赚。这里再确认一次是否真的回到了月月赚列表，
        # 不是就继续返回，必要时退出到首页重新搜索进入。
        screen = self.ensure_task_list(screen)

        # 奖励到账弹窗：视为成功，并用返回键关闭（点击与等待都无效）
        popup = self._reward_popup(screen)
        if popup:
            self.update('browsing', message='奖励已到账，关闭提示浮层')
            closer = self._popup_closer(screen)
            if closer:
                # 优先点关闭按钮：不会退出活动页，可以紧接着做下一个任务
                logger.info('关闭奖励浮层：命中关闭控件 bounds=' + str(closer.bounds))
                self.click(closer)
                self.control.wait(1.2)
                screen = self.read()
            if self._reward_popup(screen):
                # 读不到关闭按钮（H5 图形元素）时，返回键是已验证可行的兜底，
                # 代价是会退出活动页，下一个任务由框架重新导航进入。
                self.device.go_back()
                self.control.wait(1.2)
                screen = self.read()
            return screen, True, False

        # 少数活动需要手动领取
        claim_label = self.config.get('claim_label', '领取')
        claim = next((n for n in screen.nodes if claim_label in normalize(n.text)), None)
        if claim:
            self.click(claim)
            self.control.wait(0.8)
            screen = self.read()
            if self._reward_popup(screen):
                self.device.go_back()
                self.control.wait(1.2)
                screen = self.read()
        return screen, bool(claim), False
