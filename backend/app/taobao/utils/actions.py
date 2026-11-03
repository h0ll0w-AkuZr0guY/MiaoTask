"""Taobao browse/search actions; activities provide list and return policies."""
import logging
import math
import random
import re
from contextlib import nullcontext
from backend.core.models import normalize
from backend.core.errors import TaskUnavailable
from backend.core.execution.task_policy import task_identity
logger = logging.getLogger('backend.execution')

class TaskActions:
    # 固定节拍是行为风控最容易抓的特征：同一段脚本跑两天，点与点之间的间隔
    # 是一模一样的 0.6/0.8/1.4 秒，点击落点又都压在元素几何中心那一像素上。
    # 单看每一步都没问题，攒两三天就会被判定成机器，然后开始弹滑块。
    # 所有等待都走 pause()，落点都走 click()，抖动集中在这两个入口里。
    # 幅度做成类属性：测试里置 0 就能回到可复现的精确坐标。
    PACE_JITTER = .35
    TAP_SPREAD = .22

    def pause(self, seconds, jitter=None):
        base = max(0.0, float(seconds))
        if base <= 0:
            self.control.wait(0)
            return
        jitter = self.PACE_JITTER if jitter is None else max(0.0, float(jitter))
        scaled = base * random.uniform(1 - jitter, 1 + jitter) if jitter else base
        # 偶尔多停一拍：真人做任务会走神，不是一直匀速点下去。
        if jitter and base >= .3 and random.random() < .08:
            scaled += random.uniform(1.2, 2.8)
        self.control.wait(scaled)

    def click(self, node, spread=None):
        """点元素的随机位置，不要每次都落在几何中心。

        `spread` 是相对元素尺寸的内缩比例：0.22 表示在中间 78% 的区域里随机，
        既不会点到边缘外，也不会每次命中同一像素。
        """
        spread = self.TAP_SPREAD if spread is None else max(0.0, min(.6, float(spread)))
        self.control.check()
        left, top, right, bottom = node.bounds
        if spread:
            inset_x = max(0.0, (right - left) * spread / 2)
            inset_y = max(0.0, (bottom - top) * spread / 2)
            x = round(random.uniform(left + inset_x, max(left + inset_x, right - inset_x)))
            y = round(random.uniform(top + inset_y, max(top + inset_y, bottom - inset_y)))
        else:
            x, y = node.center
        geometry = getattr(getattr(self, 'reader', None), 'last_screen', None)
        geometry = getattr(geometry, 'geometry', None)
        if geometry and not geometry.contains(x, y):
            from backend.core.errors import NeedsAttention
            raise NeedsAttention('目标位于系统栏、屏幕裁切或不可操作区域，请检查窗口布局')
        self.device.click(x, y)

    def execute(self, task, before):
        self._active_task = task
        # 每次执行前清掉上一次的到账标记：它只对当前这次浏览有效。
        self._browse_credited = None
        # 还没离开清单：这一屏的 × 如果是浮层，可以顺手关掉。
        self._task_page = False
        # 本次失败是不是“没打开页面”造成的，由 record_task_failure 归因用。
        self._attempt_nav_failure = False
        # 这次到底有没有真的浏览过：只有真浏览过、又没看到进度涨，才算“软失败”。
        self._attempt_browsed = False
        self.install_policy_hooks()
        self.update('opening', current_task=task.title, message='打开任务页面', remaining=0)
        logger.info('执行 %s (%d/%d)，类型 %s', task.title, task.current, task.total, task.kind)
        self.control.check()
        left, top, right, bottom = task.bounds
        self.device.click((left + right) // 2, (top + bottom) // 2)
        self.pause(.6)
        deadline = self.control.now() + self.config['page_timeout']
        opening_hard_deadline = deadline + self.config['page_timeout'] * 2
        screen, left = before, False
        confirmations = 0
        def skip_launch():
            reason='应用跳转未确认，今日跳过此任务；其他任务继续'
            self.unavailable_tasks['task:'+normalize(task.title)]=reason
            self.report_failure(reason,task,phase='unavailable')
            logger.warning('%s：%s',reason,task.title)
            return self.recover(),False,False
        while self.control.now() < deadline:
            screen = self.read(force_ocr=bool(self.config['ocr']))
            # Always make the first real jump attempt. Installation evidence is
            # read from this device only, never from a bundled skip list.
            self.guard(screen)
            self.check_destination(screen)
            # Scoped external-app launch confirmation; never accept privacy or login grants.
            confirm = getattr(self,'launch_confirmation',None)
            one_time = confirm(screen) if callable(confirm) else None
            if one_time:
                if not self.config['allow_cross_app']:
                    break
                if confirmations>=2:
                    return skip_launch()
                confirmations += 1
                logger.info('确认本次应用跳转：%s',normalize(one_time.text))
                self.click(one_time)
                self.pause(.8)
                deadline = min(opening_hard_deadline, self.control.now() + self.config['page_timeout'])
                continue
            pending=getattr(self,'is_launch_prompt',None)
            if callable(pending) and pending(screen):
                if self.config['ocr'] and confirmations < 2:
                    visual = self.read(force_ocr=True)
                    self.guard(visual)
                    button = confirm(visual) if callable(confirm) else None
                    if button:
                        confirmations += 1
                        self.click(button)
                        self.pause(.8)
                        deadline = min(opening_hard_deadline, self.control.now()+self.config['page_timeout'])
                        continue
                return skip_launch()
            if screen.package == self.origin and self.is_task_list(screen.nodes):
                self._task_page = False
                after = next((x for x in self.tasks(screen) if x.key == task.key), None)
                if task.kind == 'claim' and after and (after.action != task.action or after.current > task.current):
                    return screen, True, False
                if task.key.startswith('milestone-') and after is None and self.milestone_received(task, screen):
                    return screen, True, False
                self.pause(.6)
                continue
            if not callable(confirm) and screen.find(r'打开.{0,10}应用|是否.{0,12}打开|即将.{0,10}跳转|允许.{0,12}打开|尝试打开'):
                button = screen.find(r'^(打开|立即打开|继续打开|允许打开|去支付宝|去看看|允许)$')
                if button:
                    if not self.config['allow_cross_app']:
                        break
                    self.click(button)
                    self.pause(.8)
                    continue
            ready = getattr(self,'destination_ready',None)
            if (not normalize(screen.text) or len(screen.nodes) < 2
                    or callable(ready) and not ready(screen)):
                self.pause(.7)
                continue
            left = True
            # 已经站在任务落地页上了：从现在到回到清单之前，屏上的 × 都归这个
            # 页面自己，不能当弹窗关（关掉它 = 任务没打开，白记一次失败）。
            self._task_page = True
            break
        if not left:
            logger.warning('任务没有成功离开列表：%s', task.title)
            self._attempt_nav_failure = True
            reason = self.availability_reason(task, check_installed=bool(task.target_packages))
            if reason:
                raise TaskUnavailable(reason)
            return self.recover(), False, False
        self._attempt_browsed = True
        return self.browse_destination(task, screen)

    # 淘金币面板是 WebView：服务端入账后，页面上还是旧数字，要一两分钟才刷新。
    # 真机证据（11-03）：10:12 判「发现精选好物 2/6 失败」，10:19 列表上已经是 3/6；
    # 搜一搜 08:58 判失败的 4/6，09:13 已经变成 5/6。这些“失败”其实都做成了。
    # 同一条任务连续两次这样的误判就会写 exhausted，整天不再执行——用户看到的就是
    # 「已跳过：今日失败已达上限」，而他自己点一下执行又能跑。
    SOFT_MISS_LIMIT = 3

    def install_policy_hooks(self):
        """核验成功时把“软失败”计数清零。

        分发包里跑的是旧框架，改不了 TaskPolicy 的源码，只能在实例上盖一层：
        它的 ``progress()`` 只清 failures/exhausted/browse_once_attempted，
        我们自己的 ``soft`` 字段要自己收尾，否则一天里攒够 SOFT_MISS_LIMIT 次
        就会把后面本来能做的尝试按真失败扣掉。
        """
        policy = getattr(self, 'task_policy', None)
        if policy is None or getattr(policy, '_miao_hooked', False):
            return
        original_progress = getattr(policy, 'progress', None)

        def progress(task):
            result = original_progress(task) if callable(original_progress) else None
            entries = getattr(policy, 'entries', None)
            if isinstance(entries, dict):
                item = entries.get(task_identity(task))
                if isinstance(item, dict) and item.pop('soft', None) is not None:
                    save = getattr(policy, '_save', None)
                    if callable(save):
                        save()
            return result

        try:
            policy.progress = progress
            policy._miao_hooked = True
        except Exception:
            logger.warning('任务策略钩子安装失败，按框架默认判断继续', exc_info=True)

    def record_task_failure(self, task):
        """把“没做成”分成三类，只有真失败才扣当日额度。

        1) 导航层失败：点了去完成但没确认离开清单（落地页被关、跳转没确认）。
           跟任务本身能不能做无关，见 can_auto_dismiss 的说明。
        2) 软失败：页面打开了、停留时间也够了，回来核对却发现进度没涨。
           多半是面板还没刷新（上面真机证据），属于“其实做成了但没看到”。
        3) 真失败：进度、金币余额、到店证据全都对不上——这才该扣额度。

        1、2 都不写 exhausted：导航层失败本轮限额内放过；软失败只累加一个当日
        计数，超过 SOFT_MISS_LIMIT 次才按真失败算。这样一次误判不会把任务整天
        锁死，而真的做不动的任务仍然会被额度挡住。
        """
        if not getattr(self, '_attempt_nav_failure', False) and getattr(self, '_attempt_browsed', False):
            return self.record_soft_miss(task)
        if not getattr(self, '_attempt_nav_failure', False):
            return super().record_task_failure(task)
        budget = getattr(self, '_nav_failures', None)
        if budget is None:
            budget = self._nav_failures = {}
        identity = task_identity(task)
        used = budget.get(identity, 0)
        if used >= self.config['max_attempts']:
            logger.info('导航层失败本轮已达上限，按当日额度扣除：%s', task.title)
            return super().record_task_failure(task)
        budget[identity] = used + 1
        self._task_budget_counted = True
        logger.info('导航层失败不扣当日额度：%s（本轮第 %d 次）', task.title, budget[identity])
        return self.task_policy.failures(task)

    def record_soft_miss(self, task):
        """页面打开了、也浏览够了，就是面板上还没看到进度：先当“没看到”，别当失败。"""
        policy = self.task_policy
        entries = getattr(policy, 'entries', None)
        item = entries.setdefault(task_identity(task), {}) if isinstance(entries, dict) else None
        soft = int(item.get('soft', 0)) + 1 if isinstance(item, dict) else self.SOFT_MISS_LIMIT + 1
        if isinstance(item, dict):
            item['soft'] = soft
            save = getattr(policy, '_save', None)
            if callable(save):
                save()
        if soft > self.SOFT_MISS_LIMIT:
            logger.info('连续 %d 次没在面板上看到进度，按当日额度扣除：%s', soft - 1, task.title)
            return super().record_task_failure(task)
        self._task_budget_counted = True
        logger.info('这次不算失败：%s 的进度还没在面板上刷新（今日第 %d 次），继续做别的任务',
                    task.title, soft)
        return policy.failures(task)

    def page_config(self):
        page = getattr(self, 'page', None)
        return page if isinstance(page, dict) else {}

    def product_cards(self, screen, pattern):
        """把一张卡片上的标题/价格/标签都算作“这一格能点”的证据。

        「点我得淘金币」这类奖励标签贴在卡片角落，字号很小；只认价格就会
        整页找不到可点的东西。
        """
        cards = []
        for node in screen.nodes:
            text = normalize(node.text)
            if not text or not pattern.search(text):
                continue
            if not screen.height*.08 < node.center[1] < screen.height*.95:
                continue
            if not 0 < node.bounds[3]-node.bounds[1] < screen.height*.14:
                continue
            if not screen.width*.04 < node.center[0] < screen.width*.96:
                continue
            cards.append(node)
        return cards

    @staticmethod
    def same_page(before, after):
        """同一页面内的状态变化（发币动画、按钮变灰）不算跳转。"""
        if after.package != before.package:
            return False
        old = {normalize(n.text) for n in before.nodes if len(normalize(n.text)) > 1}
        new = {normalize(n.text) for n in after.nodes if len(normalize(n.text)) > 1}
        if not old or not new:
            return False
        return len(old & new) / len(old) >= .6

    def grid_scroll(self, screen):
        """页内找下一批可点卡片；滑不动就返回 None，交给停留兜底。"""
        before = {normalize(n.text) for n in screen.nodes if len(normalize(n.text)) > 1}
        self.device.swipe(round(screen.width*.5), round(screen.height*.78),
                          round(screen.width*.5), round(screen.height*.42), duration=.5)
        self.pause(.8)
        fresh = self.read()
        after = {normalize(n.text) for n in fresh.nodes if len(normalize(n.text)) > 1}
        if not before or len(before & after) / len(before) > .85:
            return None
        return fresh

    def click_products(self, screen, need, limit=8):
        """点够 need 个卡片再返回：这类任务的计数只认真正的点击。

        两种落地页并存：点开商品详情（需要退回来）和「点我得淘金币」这种
        页内直接发币的网格（退出去就前功尽弃）。所以每次点完先看页面有没有
        真的跳走，只有跳走了才按返回键。
        """
        pattern = re.compile(self.page_config().get('product_markers')
                             or r'点我(?:得|领|拿)?(?:淘)?金币|领淘金币|金币已抵|'
                                r'金币热卖价|[¥￥]\s*\d')
        # 同一张卡片上，“点我得淘金币”标签、抵扣价、价格各是一行文字；按坐标
        # 去重，否则三个任务点全落在同一张卡上。
        label = re.compile(r'点我|领淘金币|领取淘金币|立即领取')
        clicked = []
        done = 0
        retried = False
        while done < min(need, limit):
            cards = [n for n in self.product_cards(screen, pattern)
                     if not any(abs(n.center[0]-x) < screen.width*.3
                                and abs(n.center[1]-y) < screen.height*.09
                                for x, y in clicked)]
            card = (next((n for n in cards if label.search(normalize(n.text))), None)
                    or (cards[0] if cards else None))
            if card is None and not retried:
                # 落地页还在渲染时先等一拍，别把没加载完当成“没有可点的卡”。
                retried = True
                self.pause(1.2)
                screen = self.read()
                continue
            if card is None:
                fresh = self.grid_scroll(screen)
                if fresh is None:
                    break
                screen = fresh
                if screen.package != self.origin or self.is_task_list(screen.nodes):
                    break
                continue
            clicked.append(card.center)
            left, top, right, bottom = card.bounds
            self.update('browsing', message='点击第 %d 个（共 %d 个）' % (done+1, need))
            logger.info('点击第 %d 个（共 %d 个）：%s', done+1, need, normalize(card.text)[:18])
            self.device.click((left+right)//2, (top+bottom)//2)
            self.pause(1.4)
            done += 1
            after = self.read()
            if after.package != self.origin or self.is_task_list(after.nodes):
                break
            if self.same_page(screen, after):
                screen = after
                continue
            self.device.go_back()
            self.pause(1.0)
            screen = self.read()
        return done

    def tap_once(self, screen, pattern=None):
        """点掉页面上的一次性动作按钮（施肥 / 浇水）。

        这类任务要的就是那一下点击：光停留在页面上不会计数，但也不该交给人工。
        """
        pattern = pattern or self.page_config().get('tap_labels') or (
            r'^(?:去|立即|马上|一键|点击|立刻)?(?:施肥|浇水)(?:\s*\d+\s*次)?$')
        button = next((n for n in screen.nodes
                       if re.search(pattern, normalize(n.text))
                       and 0 < n.bounds[3]-n.bounds[1] < screen.height*.12), None)
        if button is None:
            return None
        logger.info('点击页面按钮：%s', normalize(button.text))
        self.update('browsing', message='点击 %s' % normalize(button.text)[:6])
        self.click(button)
        self.pause(1.4)
        return button

    # 阿里 nc 滑块。手柄的文案固定是「滑块」，轨道文案是「向右滑动验证」，
    # 这一类只要把方块拖到轨道右端就能通过。点选图片、拼图缺口对齐不在这里做。
    # 只把“动作提示”算作验证页：直播间横幅上的「导轨滑块」「窗轨滑块」是商品名，
    # 不能因为它出现就当成验证。
    SLIDER_HINT_RE = re.compile(
        r'拖动.{0,6}滑块|拖动下方滑块|按住.{0,4}滑块|向右滑动|滑动.{0,2}完成验证|'
        r'拖动.{0,4}完成验证|滑块.{0,3}完成验证|请完成(?:下方)?验证')
    SLIDER_HANDLE_RE = re.compile(r'^(?:滑块|拖动|按住|向右|»|>>|＞＞)$')
    SLIDER_FAIL_RE = re.compile(
        r'验证失败|验证未通过|验证不通过|请重试|再试一次|重试|拖动到正确位置|操作失败|验证超时')
    # 失败后手柄会消失，框体里只剩一句「验证失败，点击框体重试(error:xxxx)」，
    # 点一下框体才会重新出滑块。这句提示是唯一还能点的东西。
    SLIDER_RETRY_RE = re.compile(r'点击?框?体?重试|点我重试|验证失败|验证超时')

    def slider_hint(self, screen):
        """返回「拖动滑块完成验证」这句提示所在的节点，没有就是 None。"""
        for node in getattr(screen, 'nodes', None) or ():
            text = normalize(node.text)
            if text and self.SLIDER_HINT_RE.search(text):
                return node
        return None

    def slider_handle(self, screen):
        """找滑块手柄：文案是「滑块/拖动/»」，而且是个方块，不是整行标题。

        页面上「亲，请拖动下方滑块完成验证」这句标题也含“滑块”，所以不能按
        文案位置去配，得按形状筛：手柄占屏宽不到一半、宽高比接近 1:1；
        同一页有多个候选时取最靠下的那个（操作区在标题下方）。
        """
        best = None
        for node in getattr(screen, 'nodes', None) or ():
            text = normalize(node.text)
            if not text or not self.SLIDER_HANDLE_RE.match(text):
                continue
            left, top, right, bottom = node.bounds
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                continue
            if width > screen.width * .45 or height > screen.height * .15:
                continue
            if not .4 <= width / height <= 3.2:
                continue
            if best is None or bottom > best.bounds[3]:
                best = node
        return best

    def slider_track_right(self, screen, handle):
        """轨道右端：手柄同一行里最宽的那一块的右边；找不到就按屏宽估。"""
        row = (handle.bounds[1] + handle.bounds[3]) // 2
        right, widest = 0, 0
        for node in getattr(screen, 'nodes', None) or ():
            left, top, edge, bottom = node.bounds
            if abs((top + bottom) // 2 - row) > 60:
                continue
            span = edge - left
            if span > widest:
                right, widest = edge, span
        if right > handle.bounds[2]:
            return min(right, screen.width)
        return min(screen.width, handle.bounds[2] + round(screen.width * .8))

    @staticmethod
    def slide_path(x1, y1, x2, y2, steps=36, jitter=1.5, overshoot=5, randomize=True):
        """生成人手拖动轨迹。

        nc 这类滑块看的不只是落点，还有过程本身，所以这几件事缺一不可：

        - **速度曲线**：采样点是等时间间隔注入的，空间上密集就等于速度慢，
          所以用 smoothstep 排布——起步慢、中段快、末端减速；
        - **拖过头再回压**：真人拖到位常常冲出去几个像素再压回来，最后 15%
          的采样点专门做这一下，落点仍然是目标位置；
        - **纵向漂移**：手指不是沿直线走的，y 上叠正弦 + 随机偏移；
        - **每次都不一样**：点数、抖动幅度、过头距离都由调用方随机。
        """
        total = max(16, int(steps))
        rand = random.uniform if randomize else (lambda low, high: (low + high) / 2)
        dx, dy = x2 - x1, y2 - y1
        over = max(0, int(overshoot)) if abs(dx) > 24 else 0
        body = max(8, round(total * .85))
        tail = max(2, total - body)
        path = []
        for index in range(body):
            t = index / max(1, body - 1)
            ease = t * t * (3 - 2 * t)
            wobble = (math.sin(t * math.pi * 2.7) * jitter
                      + rand(-jitter * .4, jitter * .4)) if 0 < t < .95 else 0.0
            path.append((x1 + dx * ease + over * ease, y1 + dy * ease + wobble))
        for index in range(1, tail + 1):
            t = index / tail
            path.append((x2 + over * (1 - t), y2 + rand(-1.3, 1.3)))
        return [(round(px), round(py)) for px, py in path]

    def slide_gesture(self, path, duration):
        """把整条轨迹作为一次连续手势注入（按下 → 多次移动 → 抬起）。

        `device.swipe` 是匀速直线，做不到变速；优先用底层 uiautomator2 的
        swipe_points。取不到时退回匀速滑动，宁可成功率低也不要抛出去让整轮停。
        """
        points = [list(point) for point in path]
        if len(points) < 2:
            return None
        driver = getattr(self.device, '_device', None)
        inject = getattr(driver, 'swipe_points', None)
        if callable(inject):
            try:
                inject(points, duration=float(duration))
                return 'points'
            except Exception as exc:
                logger.warning('按轨迹注入失败，退回匀速滑动：%s', exc)
        x1, y1 = points[0]
        x2, y2 = points[-1]
        try:
            try:
                self.device.swipe(x1, y1, x2, y2, duration=float(duration))
            except TypeError:
                self.device.swipe(x1, y1, x2, y2)
        except Exception as exc:
            logger.warning('匀速滑动也失败：%s', exc)
            return None
        return 'linear'

    def slider_failed(self, screen):
        """失败态的提示节点：「验证失败，点击框体重试(error:xxxx)」。

        这一态同样是验证页：手柄被收走、标题文案也变了。只认「拖动滑块」
        的提示，就会把失败态当成“验证已通过”，然后带着一个还卡在验证页的
        界面继续去点任务，后面每一步都点在验证框上。
        """
        for node in getattr(screen, 'nodes', None) or ():
            text = normalize(node.text)
            if not text or not self.SLIDER_RETRY_RE.search(text):
                continue
            lowered = text.lower()
            if '验证' not in text and 'error' not in lowered and '重试' not in text:
                continue
            return node
        return None

    def slider_retry(self, screen):
        """验证失败后点一下框体，让滑块手柄重新出现。

        失败态的结构是：手柄消失，框体里换成一句「验证失败，点击框体重试
        (error:xxxx)」。这句提示是框体内的文字，点它等于点框体，手柄就会
        回来。只认带「验证」或 error 码的提示，避免把「点我反馈」也点了。
        """
        target = self.slider_failed(screen)
        if target is None:
            return False
        self.update(message='点击重试验证框')
        logger.info('滑块已失败，点击框体重试：%s', target.center)
        self.click(target, spread=.15)
        return True

    def slider_failures(self):
        """本轮运行里自动拖滑块失败的次数。"""
        return int(getattr(self, '_slider_fail_count', 0) or 0)

    def slider_note_failure(self):
        self._slider_fail_count = self.slider_failures() + 1

    def on_slider_page(self, screen):
        """当前是否停在验证页——包括滑失败那一态。

        只判断「拖动滑块」提示是不够的：失败后手柄被收走、文案换成
        「验证失败，点击框体重试」，页面其实还卡在原地。
        """
        return self.slider_hint(screen) is not None or self.slider_failed(screen) is not None

    def handle_slider(self, screen):
        """滑块验证的统一入口：先自动拖，拖不过去就停下来等人滑完再继续。

        真机实测（vivo / Android 13 / 无 root）：把滑块拖到轨道末端，位置是对的，
        但阿里 nc 直接回风控错误码（EnvWgGw、Fihi9w）。匀速、逐点变速、随机抖动
        加随机停顿三种轨迹结果都一样——被拒的是“这次触摸从哪来、压力多少”，
        不是轨迹形状。所以自动拖只算尝试，失败预算和冷却都在 solve_slider 里，
        拖不过去立刻退回“等人手动滑完”，不让整轮为了一个滑块白白中断。
        """
        if not self.on_slider_page(screen):
            return True
        if self.page.get('slider_auto') and self.solve_slider(screen):
            return True
        return self.wait_slider_gone()

    def wait_slider_gone(self, seconds=None):
        """停在验证页等人手动完成，页面恢复正常就返回 True。

        等待期间**不产生任何触摸**：验证页上每多点一下，都是在给这个会话的
        风险分加码，而连着点「重新验证」正是脚本最容易留下的指纹。所以先
        静默 25 秒，之后如果确实是失败态（手柄被收走），才点那一次重试框，
        全程最多一次。右上角那个 × 等于「放弃这次验证」，任何时候都不碰。
        """
        limit = float(seconds if seconds is not None
                      else (self.page.get('slider_wait_seconds') or 600))
        # 用 control.now() 而不是 time.time()：脚本被暂停时这段时间不算，
        # 否则一次暂停就会把等待窗口吃光。
        deadline = self.control.now() + limit
        started = self.control.now()
        retried = False
        self.update(message='等待手动完成滑块验证')
        logger.warning('滑块需要本人滑动：最多等 %d 秒，滑完自动继续', round(limit))
        while self.control.now() < deadline:
            self.pause(2.0)
            if self.control.now() >= deadline:
                break
            try:
                screen = self.read()
            except Exception as exc:
                logger.info('等待滑块期间读屏失败：%s', exc)
                continue
            if not self.on_slider_page(screen):
                logger.info('滑块已验证通过，继续执行')
                return True
            # 失败态：手柄被收走，框体里只剩「验证失败，点击框体重试」。
            # 静默够久再点这一次，之后无论结果如何都不再动，等超时或等人。
            if (not retried and self.control.now() - started >= 25
                    and self.slider_handle(screen) is None and self.slider_retry(screen)):
                retried = True
                logger.warning('滑块失败态已静默 25 秒，点一次重试框，之后不再自动点')
                self.pause(2.5)
        logger.warning('等了 %d 秒滑块仍未通过，停止本轮任务', round(limit))
        return False

    def solve_slider(self, screen, attempts=None):
        """把「向右拖动滑块」的验证拖过去，成功返回 True。

        拖不过去（次数用尽 / 找不到手柄 / 读屏失败）返回 False，由调用方决定
        是停下来等人还是返回上一页。这里不关弹窗：验证页右上角的 × 等于
        “放弃这次验证”，关掉只会让任务失败。
        """
        limit = max(1, int(attempts or self.page.get('slider_attempts') or 2))
        budget = int(self.page.get('slider_daily_budget') or 4)
        for attempt in range(limit):
            if self.slider_failures() >= budget:
                logger.warning('滑块自动重试已累计 %d 次，改回等本人滑动', budget)
                return False
            if not self.on_slider_page(screen):
                return True
            handle = self.slider_handle(screen)
            if handle is None:
                # 上一轮拖失败后手柄会被收走，框体里只剩「点击框体重试」，
                # 先点一下把手柄要回来再接着算坐标。
                if self.slider_retry(screen):
                    self.pause(2.2)
                    try:
                        screen = self.read()
                    except Exception as exc:
                        logger.warning('点重试后读屏失败：%s', exc)
                        return False
                    handle = self.slider_handle(screen)
                if handle is None:
                    logger.warning('识别到滑块提示，但找不到手柄节点，交给人工')
                    return False
            left, top, right, bottom = handle.bounds
            width, hy = right - left, handle.center[1]
            # 起点按在把手左侧三分之一处：真人按住时手指不落在正中心，
            # 每次都从同一个像素起手本身就是特征。
            start_x = left + max(4, width // 3)
            start_y = hy + random.randint(-2, 2)
            # 手柄右边缘对齐轨道右端，所以目标中心是轨道右边减去半个手柄宽。
            target = self.slider_track_right(screen, handle) - width // 2
            if target <= start_x + 40:
                target = max(start_x + 40, round(screen.width * .9))
            # 每次换一条轨迹：点数、时长、抖动幅度、越过距离都不一样。
            steps = 34 + attempt * 8 + random.randint(-3, 3)
            duration = .95 + attempt * .25 + random.uniform(-.08, .08)
            path = self.slide_path(start_x, start_y, target, start_y,
                                   steps=steps, jitter=1.3 + attempt * .4,
                                   overshoot=4 + attempt * 2)
            self.update(message='拖动滑块完成验证')
            logger.info('滑块验证：手柄 (%d,%d) → (%d,%d)，%d 点 / %.2fs',
                        start_x, start_y, target, start_y, len(path), duration)
            mode = self.slide_gesture(path, duration)
            self.pause(1.1 + attempt * .5)
            try:
                screen = self.read()
            except Exception as exc:
                logger.warning('滑块拖动后读屏失败：%s', exc)
                return False
            if not self.on_slider_page(screen):
                logger.info('滑块验证通过（%s 轨迹）', mode or '匀速')
                return True
            page = normalize(screen.text)
            if self.SLIDER_FAIL_RE.search(page):
                logger.info('滑块未通过，页面提示重试：第 %d/%d 次', attempt + 1, limit)
            else:
                logger.info('滑块拖动后仍停在验证页：第 %d/%d 次', attempt + 1, limit)
            self.slider_note_failure()
            # 冷却：不在同一个风控窗口里连着撞，间隔逐次拉长。连续失败会把账号
            # 风险分推高，滑块升级成点选图或人脸，代价比这次任务大得多。
            cooldown = min(120.0, 25.0 * (2 ** attempt))
            logger.warning('滑块第 %d 次未通过（%s），冷却 %d 秒再试',
                           attempt + 1, mode or '匀速', round(cooldown))
            self.pause(cooldown)
            try:
                screen = self.read()
            except Exception as exc:
                logger.warning('冷却后读屏失败：%s', exc)
                return False
        logger.warning('滑块连续 %d 次未通过，交给人工处理', limit)
        return False

    @staticmethod
    def quiz_option(screen):
        """找 A 选项。选项既可能是「A.内容」整行，也可能只有字母圆圈。"""
        full, bare = [], []
        for node in screen.nodes:
            text = normalize(node.text)
            match = re.match(r'^([A-DＡ-Ｄ])\s*[.、．:：)）]?\s*(.*)$', text)
            if not match:
                continue
            if not screen.height*.1 < node.center[1] < screen.height*.92:
                continue
            if not 0 < node.bounds[3]-node.bounds[1] < screen.height*.16:
                continue
            if match.group(2) or re.search(r'[.、．:：)）]', text):
                full.append((match.group(1), node))
            else:
                bare.append((match.group(1), node))
        if len(full) >= 2:
            return next((n for letter, n in full if letter in 'AＡ'), full[0][1])
        # 只有字母圆圈时，要求它们竖排在同一列（A/B/C/D 并排），否则多半是
        # 页面上的其它图标，不能当选项点。
        if len(bare) >= 2:
            xs = [n.center[0] for _, n in bare]
            ys = {round(n.center[1] / (screen.height*.02)) for _, n in bare}
            if max(xs) - min(xs) < screen.width*.25 and len(ys) >= 2:
                return next((n for letter, n in bare if letter in 'AＡ'), bare[0][1])
        return None

    # 交卷按钮：趣味课堂选完 A 之后必须点「我选好了提交答案」，否则答案不会
    # 被提交，下一轮又会对着同一个 A 再点一次。
    QUIZ_SUBMIT_RE = (r'^(?:我选好了?)?[\s，,。、:：]*(?:提交答案|提交选择|确认答案|确认选择|'
                      r'提交|确定|确认|交卷|完成答题|我选好了?)$')
    # 前进按钮：只在“还没选”的时候点，选完必须先交卷。
    QUIZ_ADVANCE_RE = (r'^(?:下一题|继续答题|继续|开始答题|立即答题|去答题|再答一次|'
                       r'领取奖励|开心收下|收下奖励|已成功领取奖励\s*回到主页|回到主页|'
                       r'返回首页|知道了|关闭)$')

    def quiz_submit(self, screen):
        """找交卷按钮。支持「我选好了提交答案」「我选好了」「提交答案」几种写法。"""
        return screen.find(self.QUIZ_SUBMIT_RE)

    def quiz_advance(self, screen):
        return screen.find(self.QUIZ_ADVANCE_RE)

    def quiz_scroll(self, screen):
        """交卷按钮常压在折叠线以下：往下滑一屏把它露出来，滑不动返回 None。

        滑出按钮时页面上的其它文字几乎不变（只是多出底部那一行），所以不能只
        按“重复度高”判没动——先看有没有露出交卷按钮，再退回整体相似度判断。
        """
        before = {normalize(n.text) for n in screen.nodes if len(normalize(n.text)) > 1}
        self.device.swipe(round(screen.width*.5), round(screen.height*.72),
                          round(screen.width*.5), round(screen.height*.45), duration=.45)
        self.pause(.8)
        fresh = self.read()
        after = {normalize(n.text) for n in fresh.nodes if len(normalize(n.text)) > 1}
        if not before or before == after:
            return None
        if self.quiz_submit(fresh) is not None:
            return fresh
        if len(before & after) / len(before) > .9:
            return None
        return fresh

    def answer_quiz(self, task, screen, cross=False, limit=18):
        """趣味课堂这类答题：每题选 A → 点「我选好了提交答案」交卷 → 下一题。

        页面结构不固定，所以按“有没有选过”分两步走：没选就选 A，选完必须先找到
        交卷按钮；交卷按钮读不到就先往下滑一屏，实在没有才放弃。
        """
        passed = False
        previous, repeats, answered = None, 0, False
        for _ in range(limit):
            self.control.check()
            self.guard(screen)
            if screen.package != self.origin or self.is_task_list(screen.nodes):
                break
            option = self.quiz_option(screen)
            submit = self.quiz_submit(screen)
            advance = self.quiz_advance(screen)
            # 「我选好了」点下去就出解析页：正确答案 / 你的答案 / 回答正确(错误)，
            # 底部是「已成功领取奖励 回到主页」。这时答案已经提交、金币也到账，
            # 不能再回头去点选项（选项还留在页面上，点了只会原地打转）。
            result = screen.find(r'正确答案|回答正确|回答错误|答案解析|已成功领取奖励')
            if result is not None and submit is None:
                logger.info('答题已交卷并出结果：%s', task.title)
                self.update('browsing', message='答题完成')
                passed = True
                break
            # 单题的判对提示（“答对啦”）后面还跟着“下一题”，不能当成整轮做完；
            # 只有页面上再没有可选项、没有交卷按钮，且按钮是终结类，才算结束。
            finished = screen.find(r'答题完成|答题结束|全部答完|恭喜你|答对\s*\d+\s*题|'
                                   r'奖励已到账|领取成功')
            if (finished is not None and option is None and submit is None
                    and (advance is None or re.search(
                        r'领取奖励|开心收下|收下奖励|知道了|关闭|完成',
                        normalize(advance.text)))):
                logger.info('答题完成：%s', task.title)
                self.update('browsing', message='答题完成')
                passed = True
                break
            if answered and submit is not None:
                logger.info('答题交卷：%s', normalize(submit.text))
                self.update('browsing', message='提交答案')
                self.click(submit)
                answered = False
                self.pause(1.4)
                if re.search(r'领取奖励|开心收下|收下奖励', normalize(submit.text)):
                    passed = True
                    break
                screen = self.read()
                continue
            if option is not None:
                # 页卡住不动时同一处 A 会被反复点，别把整轮预算耗在这里。
                spot = option.center
                same = bool(previous) and abs(spot[0]-previous[0]) < 2 \
                    and abs(spot[1]-previous[1]) < 2
                repeats = repeats + 1 if same else 1
                previous = spot
                if same and answered:
                    # 已经选过、选项还原地不动、又没有读到交卷按钮：多半按钮在
                    # 折叠线以下，滑一屏再判断；滑不动才认输。
                    fresh = self.quiz_scroll(screen)
                    if fresh is not None:
                        screen = fresh
                        continue
                if repeats > 3:
                    logger.warning('答题页面没有推进，放弃本次答题')
                    break
                logger.info('答题选 A：%s', normalize(option.text)[:16])
                self.update('browsing', message='选择 A 选项')
                self.click(option)
                answered = True
                self.pause(1.0)
            else:
                if advance is not None:
                    if re.search(r'回到主页|返回首页|已成功领取', normalize(advance.text)):
                        # 这条是解析页的收尾按钮，点了会跳到淘宝首页，离面板更远；
                        # 交给后面的返回键逐层退回更省事。
                        logger.info('答题已领奖：%s', task.title)
                        self.update('browsing', message='答题完成')
                        passed = True
                        break
                    logger.info('答题前进：%s', normalize(advance.text))
                    self.update('browsing', message='答题')
                    self.click(advance)
                    answered = False
                    self.pause(1.2)
                    if re.search(r'领取奖励|开心收下|收下奖励', normalize(advance.text)):
                        passed = True
                        break
                else:
                    fresh = self.quiz_scroll(screen)
                    if fresh is None:
                        logger.warning('答题页既没有选项也没有按钮，放弃本次答题')
                        break
                    screen = fresh
                    continue
            screen = self.read()
        # 逐层退回任务面板：答题页常常套在活动浮层里，盲目 recover 会连浮层一起关掉。
        for _ in range(3):
            screen = self.read()
            if self.is_task_list(screen.nodes):
                return screen, passed, cross
            try:
                self.device.go_back()
            except Exception:
                break
            self.pause(1.0)
        return self.recover(), passed, cross

    def browse_destination(self, task, screen):
        """Observe an already-open destination; callers confirm any entry first."""
        cross = screen.package != self.origin
        if cross:
            if not self.config['allow_cross_app']:
                return self.recover(), False, False
            self.stats.cross_app += 1
            logger.info('跨应用浏览：%s', screen.package)
        if (re.search(r'答题|趣味课堂|问答', normalize(task.title + ' ' + (task.detail or '')))
                and not (task.kind == 'browse' and re.search(r'浏览|逛|看', task.detail or ''))):
            return self.answer_quiz(task, screen, cross)
        # Captions rotate between browse/search promotions. The destination is
        # authoritative when a browse caption opens the activity search form.
        search_landing = (task.kind == 'browse' and screen.package == self.origin
                          and screen.find(r'^历史搜索$|^搜索发现$|^搜索有福利$|^搜索后浏览立得奖励$'))
        if task.kind == 'search' or search_landing:
            searched = self.handle_search(screen)
            if not searched:
                return self.recover(), False, cross
            screen = self.read()
        # 「点击N个商品」只按停留计时永远不会达标：计数走的是真的点开商品。
        counted = re.search(r'(?:点击|浏览|逛)\s*(\d{1,2})\s*个(?:商品|宝贝)',
                            normalize(task.detail or task.title))
        if counted and task.kind == 'browse' and screen.package == self.origin:
            if self.click_products(screen, int(counted.group(1))):
                screen = self.read()
        # 施肥 / 浇水这类任务只认页面上那一下点击，停在页面上不会计数。
        if screen.package == self.origin and self.tap_once(screen) is not None:
            screen = self.read()
        seconds = task.seconds or (self.config['visit_seconds'] if task.kind == 'visit'
                                    else self.config['default_browse_seconds'])
        if task.kind in ('browse', 'search'):
            seconds += self.config['browse_extra_seconds']
        seconds += self.config['settle_seconds']
        explicit_success, cross = self.wait_for_browse(task, screen, cross, seconds)
        return self.recover(), explicit_success, cross

    def wait_for_browse(self, task, screen, cross, seconds):
        started = self.control.now()
        hard_deadline = started + self.config['max_task_seconds']
        browse_deadline = started + min(seconds, self.config['max_task_seconds'])
        scrolling = self.config['scroll_browse'] and not cross and task.kind in ('browse', 'search')
        # Only time spent sending actual gestures counts towards scroll tasks.
        # Capture, OCR, intentional gaps and RPC overhead cannot use this budget.
        active, target = 0.0, seconds
        minimum = task.seconds or self.config['default_browse_seconds']
        next_observation, last_observation = started + 1, started
        counted_screen = None
        observer_factory = getattr(self.reader, 'visual_observer', None)
        observer_context = (observer_factory() if scrolling and self.config['ocr'] and callable(observer_factory)
                            else nullcontext(None))
        with observer_context as observer:
            while self.control.now() < hard_deadline:
                self.control.check()
                if observer:
                    fresh = observer.poll()
                    if fresh is not None:
                        screen = fresh
                        last_observation = self.control.now() - self.reader.last_observation_seconds
                        self.assert_instance(screen)
                        self.update(package=screen.package, source=screen.source,
                                    observation_ms=round(self.reader.last_observation_seconds * 1000))
                    if observer.pending and self.control.now() - last_observation > self.config['page_timeout']:
                        raise TimeoutError('浏览画面识别超时，已停止滑动，返回后重新核验')
                self.check_destination(screen)
                self.guard(screen)
                if screen.package == self.origin and self.is_task_list(screen.nodes):
                    break
                if screen.package != self.origin:
                    if not cross:
                        self.stats.cross_app += 1
                        cross = True
                    if not self.config['allow_cross_app']:
                        break
                    scrolling = False
                # An unrelated/premature banner cannot cut a 15-second scroll
                # down to just a few gestures. Completion is verified on return.
                if (not scrolling or active >= minimum) and screen.find(
                        r'^(?:任务已完成|浏览已完成|浏览完成|完成浏览|奖励已到账|恭喜完成|任务完成啦)[!！。\s]*$'):
                    logger.info('检测到任务完成提示')
                    return True, cross
                # 「已得50」是淘金币落地页自带的到账浮层：金币已经进账，只是任务
                # 面板的进度要过一会儿才刷新。单独记一个标记，交给 verify 认账；
                # 不混进上面那条“任务已完成”的提示里，免得把别的场景也放宽。
                if (not scrolling or active >= minimum) and screen.find(r'^已得\s*\d+\s*$'):
                    logger.info('检测到金币到账提示')
                    self._browse_credited = task.key
                    return True, cross
                # Consume each observed countdown once, never extend the timer
                # on every gesture using the same stale frame.
                if screen is not counted_screen:
                    counted_screen = screen
                    countdown = re.search(r'(?:还需|再逛|再浏览|剩余|再看)[^\d]{0,3}(\d{1,2})\s*秒', screen.text)
                    counter = getattr(self, 'browse_countdown', None)
                    visible = counter(screen) if callable(counter) else None
                    visible = visible if visible is not None else int(countdown[1]) if countdown else None
                    if visible is not None:
                        if scrolling:
                            target = max(target, active + visible + 1)
                        else:
                            browse_deadline = max(browse_deadline, min(self.control.now() + visible + 1, hard_deadline))
                remaining = min(target - active if scrolling else browse_deadline - self.control.now(),
                                hard_deadline - self.control.now())
                if remaining <= 0:
                    break
                self.update('browsing', message='跨应用浏览中' if cross else '持续滑动浏览中' if scrolling else '浏览中',
                            remaining=max(0, int(remaining + .9)))
                if self.control.now() >= next_observation:
                    if observer:
                        if not observer.pending:
                            observer.request()
                            captured = observer.captured
                            self.assert_instance(captured)
                            cadence = getattr(self, 'browse_observation_interval', None)
                            next_observation = self.control.now() + (cadence(scrolling) if callable(cadence) else 1)
                    else:
                        screen = self.read()
                        last_observation = self.control.now()
                        next_observation = last_observation + 1
                        continue  # Guard the fresh page before any more input.
                if observer and observer.pending and (
                        observer.captured.package != self.origin or
                        (observer.captured.width, observer.captured.height) != (screen.width, screen.height)):
                    # A foreground/rotation change needs its own checked frame;
                    # do not reuse the preceding page's swipe geometry.
                    self.control.wait(.05)
                    continue
                if scrolling:
                    before = self.control.now()
                    duration = self.browse_swipe(screen)
                    elapsed = max(0, self.control.now() - before)
                    active += min(elapsed, duration if isinstance(duration, (int, float)) else self.config['swipe_duration_max'])
                interval = self.gestures.interval() if scrolling else 1
                interval = max(0, min(interval, hard_deadline - self.control.now()))
                if interval > 0:
                    self.control.wait(interval)
        return False, cross
