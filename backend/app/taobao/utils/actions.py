"""Taobao browse/search actions; activities provide list and return policies."""
import logging
import re
import time
from backend.core.models import normalize
from backend.core.errors import TaskUnavailable
logger = logging.getLogger('backend.execution')

class TaskActions:
    def refine_task_rows(self, screen):
        """Re-read at most two unclear title rows from the exact current image."""
        if (not self.config['ocr'] or screen.source != 'ocr'
                or self.reader.last_screen is not screen or not self.is_task_list(screen.nodes)):
            return screen
        progress = re.compile(r'[([]\s*\d+\s*/\s*\d+\s*[)\]]')
        def caption(text):
            return progress.sub('', normalize(text)).strip()
        def incomplete(text):
            return len(text) <= 2 or bool(re.fullmatch(r'点击去逛|点击查看|跳转得(?:奖励)?', text))
        suspect = [task for task in self.parse_tasks(screen.nodes, screen.width, screen.height)
                   if incomplete(task.title) and task.kind != 'claim'][:2]
        if not suspect:
            return screen
        regions = [(round(screen.width*.04), max(0, round(task.bounds[1]-screen.height*.065)),
                    max(1, round(task.bounds[0]-screen.width*.01)),
                    min(screen.height, round(task.bounds[3]+screen.height*.025))) for task in suspect]
        enriched = self.reader.enrich_regions(screen, regions)
        additions = enriched.nodes[len(screen.nodes):]
        # Only replace a known incomplete caption when a longer label is painted
        # on that same line. Keep full-frame authentication and safety evidence.
        replaced = []
        for node in screen.nodes:
            if not any(caption(node.text) == normalize(task.title) for task in suspect):
                continue
            if any(not incomplete(caption(fresh.text)) and re.search(r'[\u4e00-\u9fff]', fresh.text)
                   and not re.match(r'浏览|点击|跳转|奖励|\+\d', caption(fresh.text))
                   and abs(fresh.center[1]-node.center[1]) < max(
                       node.bounds[3]-node.bounds[1], fresh.bounds[3]-fresh.bounds[1])*.6
                   and fresh.bounds[0] <= node.bounds[2] and node.bounds[0] <= fresh.bounds[2]
                   for fresh in additions):
                replaced.append(node)
        if not replaced:
            self.reader.last_screen = screen
            return screen
        from dataclasses import replace
        result = replace(enriched, nodes=[n for n in enriched.nodes if n not in replaced])
        self.reader.last_screen = result
        return result

    def pause(self, seconds):
        self.control.wait(max(0.0, float(seconds)))

    def page_transition_wait(self):
        """Let the destination paint before sampling, without extra CPU OCR."""
        self.pause(1.2)

    @staticmethod
    def destination_layout(screen):
        """Compare task/header controls, not animated products or status clocks."""
        landmarks = []
        for node in screen.nodes:
            text = normalize(node.text)
            if (screen.height*.08 < node.center[1] < screen.height*.3
                    and re.search(r'^(?:搜索|浏览|再逛|还需|剩余|已得)|(?:能量|奖励).*到账', text)):
                landmarks.append((re.sub(r'\d+', '#', text),
                                  round(node.center[0]/screen.width*25),
                                  round(node.center[1]/screen.height*25)))
        return screen.package, screen.width, screen.height, tuple(sorted(landmarks))

    def record_task_failure(self, task):
        if not getattr(self, '_task_budget_counted', False):
            category = ('navigation' if getattr(self, '_attempt_nav_failure', False) else
                        'settlement' if getattr(self, '_attempt_browsed', False) else '')
            defer = getattr(self.task_policy, 'defer', None)
            if category and callable(defer) and defer(task, category):
                self._task_budget_counted = True
                logger.info('本次%s尚未核实，保留一次补查机会：%s', category, task.title)
                return self.task_policy.failures(task)
        return super().record_task_failure(task)

    def execute(self, task, before):
        self._active_task = task
        # 还没离开清单：这一屏的 × 如果是浮层，可以顺手关掉。
        self._task_page = False
        # 本次失败是不是“没打开页面”造成的，由 record_task_failure 归因用。
        self._attempt_nav_failure = False
        # 这次到底有没有真的浏览过：只有真浏览过、又没看到进度涨，才算“软失败”。
        self._attempt_browsed = False
        self.update('opening', current_task=task.title, message='打开任务页面', remaining=0)
        logger.info('执行 %s (%d/%d)，类型 %s', task.title, task.current, task.total, task.kind)
        self.control.check()
        left, top, right, bottom = task.bounds
        self.device.click((left + right) // 2, (top + bottom) // 2)
        self.page_transition_wait()
        deadline = self.control.now() + self.config['page_timeout']
        opening_hard_deadline = deadline + self.config['page_timeout'] * 2
        screen, left = before, False
        confirmations = 0
        ready_layout = None
        def skip_launch():
            reason='应用跳转未确认，今日跳过此任务；其他任务继续'
            self.unavailable_tasks['task:'+normalize(task.title)]=reason
            self.report_failure(reason,task,phase='unavailable')
            logger.warning('%s：%s',reason,task.title)
            return self.recover(),False,False
        while self.control.now() < deadline:
            observed_at = self.control.now()
            screen = self.read(force_ocr=bool(self.config['ocr']))
            # Always make the first real jump attempt. Installation evidence is
            # read from this device only, never from a bundled skip list.
            self.guard(screen)
            self.check_destination(screen)
            if (screen.package == self.origin and not self.is_task_list(screen.nodes)
                    and not any(n.center[1] > screen.height * .1 for n in screen.nodes)):
                # A fast recognizer can see only the status bar while the H5
                # destination is still loading. Do not start its browse timer.
                self.pause(.35)
                continue
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
                    or callable(ready) and not ready(screen)
                    or screen.find(r'^(?:正在加载|加载中|页面加载中|请稍候|请稍等)[.。…]*$')):
                ready_layout = None
                self.pause(.7)
                continue
            if (getattr(self.reader, 'ocr_backend', None) in ('cuda', 'dml')
                    and self.control.now() - observed_at < .6):
                # Only cheap, fast observations need a second painted frame.
                # CPU (including CUDA fallback) accepts the first ready frame
                # after the pre-capture settling delay; never double its OCR.
                layout = self.destination_layout(screen)
                if ready_layout != layout:
                    ready_layout = layout
                    self.pause(.4)
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
        return self.browse_destination(task, screen)

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
        seconds = (task.seconds or self.config['visit_seconds']) if task.kind == 'visit' else max(
            task.seconds, self.config['default_browse_seconds'])
        explicit_success, cross = self.wait_for_browse(task, screen, cross, seconds)
        self.update('returning', message='浏览结束，返回任务列表核验', remaining=0)
        self._attempt_browsed = True
        return self.recover(), explicit_success, cross

    def browse_completed(self, screen):
        counter = getattr(self, 'browse_countdown', None)
        visible = counter(screen) if callable(counter) else None
        countdown = re.search(r'(?:还需|再逛|再浏览|剩余|再看)[^\d]{0,3}(\d{1,2})\s*秒', screen.text)
        visible = visible if visible is not None else int(countdown[1]) if countdown else None
        earned = any(n.center[1] < screen.height*.2 and re.fullmatch(
            r'完成[!！。\s]*\d+能量已到账|已得\d+(?:能量|淘金币|金币)?(?:下单再得\d+(?:最高)?)?',
            normalize(n.text)) for n in screen.nodes)
        return not visible and bool(earned or screen.find(
            r'^(?:任务已完成|浏览已完成|浏览完成|完成浏览|奖励已到账|恭喜完成|任务完成啦)[!！。\s]*$'))

    def wait_for_browse(self, task, screen, cross, seconds):
        """Browse serially; CUDA checks the first deadline, CPU retries stay sparse."""
        started = self.control.now()
        hard_deadline = started + self.config['max_task_seconds']
        scrolling = self.config['scroll_browse'] and not cross and task.kind in ('browse', 'search')
        self.guard(screen)
        self.check_destination(screen)
        if self.browse_completed(screen):
            self.settle_completion(deadline=hard_deadline)
            return True, cross
        retry_needed = getattr(self.task_policy, 'retry_needed', self.task_policy.failures)
        retry = task.kind in ('browse', 'search') and bool(retry_needed(task))
        first_check = (not cross and task.kind in ('browse', 'search')
                       and getattr(self.reader, 'ocr_backend', None) in ('cuda', 'dml'))

        def browse_for(duration):
            deadline = min(self.control.now() + duration, hard_deadline)
            while self.control.now() < deadline:
                self.control.check()
                self.update('browsing', message='连续滑动浏览中' if scrolling else '停留浏览中',
                            remaining=max(0, int(deadline-self.control.now()+.9)))
                if scrolling:
                    # The adapter still checks package/user/display before input.
                    self.browse_swipe(screen)
                    interval = self.gestures.interval()
                else:
                    interval = min(.2, deadline-self.control.now())
                if interval > 0:
                    self.control.wait(min(interval,max(0,deadline-self.control.now())))
            self.control.check()

        browse_for(seconds)
        if self.control.now() >= hard_deadline:
            return False, cross
        if not (retry or first_check):
            if task.seconds:
                # 写明了时长：到点就走，不为了确认再多读一次屏。
                return False, cross
            # 描述里根本没写时长（「发现精选好物」下方只有「浏览 +40」），这里的
            # 秒数用的是默认值，到点后到底算不算做完只能看页面怎么说。CPU 推理
            # 下原先只有失败过的任务才复查，第一次就这样被记成“进度未增加”，
            # 接着把重试额度用光。只对这类“时长不明”的任务补一次确认。
            screen = self.read(force_ocr=bool(self.config['ocr']))
            self.guard(screen)
            if self.browse_completed(screen):
                self.settle_completion(deadline=hard_deadline)
                return True, cross
            return False, cross
        # Fast CUDA inference checks the real countdown at the first deadline.
        # CPU retains sparse observation. Both use bounded serial supplements.
        if retry:
            self.control.wait(min(1,max(0,hard_deadline-self.control.now())))
        for round_no in range(self.config.get('browse_recheck_rounds', 3) + 1):
            if self.control.now() >= hard_deadline:
                break
            screen = self.read(force_ocr=bool(self.config['ocr']))
            self.guard(screen)
            self.check_destination(screen)
            if self.is_task_list(screen.nodes):
                break
            if screen.package != self.origin:
                # A changed destination must be handled by the normal return path.
                break
            if self.browse_completed(screen):
                self.settle_completion(deadline=hard_deadline)
                return True, cross
            if round_no == self.config.get('browse_recheck_rounds', 3):
                break
            self.update('browsing',message=f'补滑复查 {round_no+1}/{self.config.get("browse_recheck_rounds",3)}',remaining=1)
            browse_for(1)
        return False, cross
