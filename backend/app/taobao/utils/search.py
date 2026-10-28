"""Native field submission and result-page policies."""
import logging
import re
from backend.core.models import TextNode, normalize, bounds_of
from backend.core.errors import NeedsAttention
logger = logging.getLogger('backend.execution')

class SearchPolicy:
    def check_search_context(self, screen, *, live=False):
        expected = self.origin or self.application.package_for(self.device.device_type)
        valid = screen.package == expected
        current = getattr(self.device, 'current_app', None)
        if valid and live and callable(current):
            foreground = current()
            valid = isinstance(foreground, dict) and foreground.get('package') == expected
        if not valid:
            reason = '搜索过程中已离开所选应用，已停止输入与提交，请确认前台后重新开始'
            saved = self.report_failure(reason, phase='searching')
            raise NeedsAttention(reason, evidence_saved=saved)

    def search_edit(self, screen, edits):
        return next((e for e in edits if e.get('focused') in (True, 'true')),
                    edits[0] if len(edits) == 1 else None)

    def search_tree(self):
        return self.device.dump_hierarchy()

    def fill_search_field(self, query, edit):
        self.device.input_text(query)

    @staticmethod
    def search_results_ready(screen):
        if screen.find(r'^历史搜索$|^搜索发现$|^猜你想搜$|^搜索推荐$'):
            return False
        filters = sum(bool(screen.find(pattern)) for pattern in
                      (r'^综合', r'^销量', r'^筛选', r'^店铺', r'^价格'))
        if filters >= 2 and len(screen.nodes) >= 8:
            return True
        # Double 11's embedded search results use an orange browse countdown
        # and a product grid instead of the normal sort/filter toolbar.
        banner = screen.find(r'浏览\d+[秒sS](?:可领|可得|得)|浏览完成.*(?:奖励|能量)')
        search = next((n for n in screen.nodes if normalize(n.text) == '搜索'
                       and n.center[1] < screen.height * .25), None)
        products = [n for n in screen.nodes if search and n.center[1] > search.bounds[3]
                    and re.search(r'[¥￥]\s*\d|金币热卖价|金币已抵', n.text)]
        return bool(banner and search and len(products) >= 2 and len(screen.nodes) >= 8)


    @staticmethod
    def search_controls(tree):
        edits, buttons = [], []
        def walk(item):
            if isinstance(item, list):
                for child in item:
                    walk(child)
            elif isinstance(item, dict):
                attrs = item.get('attributes', item)
                kind = attrs.get('type', attrs.get('class', ''))
                if 'EditText' in kind or 'TextInput' in kind:
                    edits.append(attrs)
                elif (normalize(attrs.get('text') or attrs.get('description') or '') == '搜索'
                      and ('Button' in kind or 'searchbtn' in attrs.get('id', '').lower())):
                    buttons.append(TextNode('搜索', bounds_of(attrs.get('bounds', attrs.get('rect')))))
                for key in ('children', 'hierarchy'):
                    if isinstance(item.get(key), (list, dict)):
                        walk(item[key])
        walk(tree)
        return edits, buttons


    @staticmethod
    def _submit_buttons(screen, buttons=()):
        # Input hints may also say 搜索. Only the narrow upper-right action counts.
        return [n for n in [*buttons, *screen.nodes] if normalize(n.text) == '搜索'
                and n.center[0] > screen.width * .7 and 0 < n.center[1] < screen.height * .25
                and 0 < n.bounds[2] - n.bounds[0] < screen.width * .3]


    def open_home_search(self, screen):
        """Wait for an actual editable field after the homepage search tap."""
        self.guard(screen)
        self.check_search_context(screen, live=True)
        entry = screen.find(r'^搜索栏$|^搜索框$')
        if not entry:
            return False
        self.click(entry)
        deadline = self.control.now() + self.config['page_timeout']
        while self.control.now() < deadline:
            self.control.wait(.35)
            screen = self.read()
            self.guard(screen)
            if screen.package != self.origin:
                return False
            edits, _ = self.search_controls(screen.tree)
            if edits:
                return self.handle_search(screen, query=self.config['search_keyword'], activity_search=True)
        # A popup may have consumed the tap. Let the shared navigation unwind
        # it instead of throwing an input error or changing input methods.
        return False


    def handle_search(self, screen, query=None, activity_search=False):
        """Fill the native field, relocate Submit, retry then fall back to Enter."""
        def result_ready(value):
            return bool(self.official_activity_entry(value)) if activity_search else self.search_results_ready(value)

        self.check_search_context(screen)
        if result_ready(screen):
            return True
        tree = screen.tree if screen.tree is not None else self.search_tree()
        edits, _ = self.search_controls(tree)
        edit = self.search_edit(screen, edits)
        if not edit:
            logger.warning('没有可确认的输入框，不开始浏览计时')
            self.report_failure('搜索页没有可确认的输入框', phase='searching')
            return False
        query = query if query is not None else self.config.get('task_search_keyword', '手机壳')
        if normalize(edit.get('text', '')) != normalize(query):
            if self.device.device_type == 'hmdriver2':
                self.click(TextNode('', bounds_of(edit.get('bounds', edit.get('rect')))))
            self.control.check()
            self.check_search_context(screen, live=True)
            # set_text replaces the field without installing/switching the IME.
            self.fill_search_field(query, edit)
            self.control.wait(.2)
        self.update('searching', message='提交搜索并确认活动入口' if activity_search else '提交搜索并确认商品结果页')
        deadline = self.control.now() + self.config['page_timeout']
        submitted, next_submit = 0, self.control.now()
        result = screen
        while self.control.now() < deadline:
            self.control.check()
            if self.control.now() >= next_submit and submitted < 3:
                # Always relocate after input or a failed click; the reward header
                # can move the button and a landing overlay can replace the page.
                fresh_tree = self.search_tree()
                _, native_buttons = self.search_controls(fresh_tree)
                native = [n for n in self._submit_buttons(result, native_buttons)
                          if n in native_buttons]
                if not native:
                    result = self.read(force_ocr=bool(self.config['ocr']))
                    self.guard(result)
                    self.check_search_context(result)
                    if result_ready(result):
                        return True
                buttons = native or self._submit_buttons(result)
                self.check_search_context(result, live=True)
                if submitted < 2 and buttons:
                    # Prefer the native position, then the rightmost visible action.
                    button = max(buttons, key=lambda n: n.center[0])
                    self.click(button)
                else:
                    # Hardware Enter does not invoke the driver's IME helper.
                    self.device.press('enter')
                    submitted = 2
                submitted += 1
                next_submit = self.control.now() + max(1, min(2, self.config['page_timeout'] / 4))
            self.control.wait(.35)
            result = self.read(force_ocr=bool(self.config['ocr']))
            self.guard(result)
            self.check_search_context(result)
            self.check_destination(result)
            if result_ready(result):
                logger.info('已确认搜索结果中的官方活动入口' if activity_search else '已确认进入搜索结果页，开始浏览计时')
                return True
            if self.is_task_list(result.nodes):
                break
        self.report_failure('搜索提交后未识别到官方活动入口' if activity_search else '搜索提交后未识别到商品结果页', phase='searching')
        logger.warning('搜索按钮重试及回车后仍未确认结果页，结束本次尝试')
        return False
