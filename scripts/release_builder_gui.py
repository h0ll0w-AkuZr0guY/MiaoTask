"""Responsive local release builder UI; all network and build work is off Tk's thread."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import traceback
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass(frozen=True)
class BuildOptions:
    source: str
    home: Path
    version_mode: str = 'patch'
    target_version: str | None = None
    summary: str = ''
    python: str | None = None
    adb_dir: str | None = None
    create_env: bool = False
    write_back_notes: bool = False
    preview: bool = False

    def arguments(self, activities):
        if not self.source.strip():
            raise ValueError('请选择源码目录、ZIP，或填写 GitHub 源码链接。')
        if self.version_mode not in ('patch', 'minor', 'keep'):
            raise ValueError('请选择有效的版本策略。')
        if self.target_version and not re.fullmatch(r'(?:MiaoTask-v)?0\.\d+\.\d+', self.target_version):
            raise ValueError('指定版本使用 0.x.y 或 MiaoTask-v0.x.y。')
        if '\n' in self.summary or '\r' in self.summary or len(self.summary) > 100:
            raise ValueError('更新摘要请使用不超过 100 字的单行文字。')
        if self.preview and self.write_back_notes:
            raise ValueError('测试构建不回写发布记录，请取消其中一个选项。')
        return dict(python=self.python, adb_dir=self.adb_dir, summary=self.summary,
                    create_env=self.create_env, write_back_notes=self.write_back_notes,
                    version_mode=self.version_mode, target_version=self.target_version,
                    preview=self.preview, activities=activities)


def selected_activities(packages, selected):
    """Retain the exact visible source revision, without silently selecting unknown rows."""
    from backend.core.plugins.contracts import FRAMEWORK_API
    selected = set(selected)
    result = []
    for item in packages:
        identity = item.get('app'), item.get('activity')
        if identity not in selected:
            continue
        commit = item.get('source_commit', '')
        if (not all(isinstance(value, str) and value for value in identity)
                or not re.fullmatch(r'[0-9a-f]{40}', commit)
                or item.get('framework_api') != FRAMEWORK_API
                or item.get('python') != f'{sys.version_info.major}.{sys.version_info.minor}'):
            raise ValueError('选中的活动与当前框架不兼容，请刷新官方目录。')
        result.append(dict(app=identity[0], activity=identity[1], source_commit=commit))
        selected.remove(identity)
    if selected:
        raise ValueError('选中的活动已不在展示目录中，请重新勾选。')
    return result


def local_source_version(source):
    """Read only a small version file; links are resolved by the build worker later."""
    if not source or '://' in source:
        return None
    path = Path(source)
    if path.is_dir():
        file = path / 'version.json'
        if not file.is_file() or file.stat().st_size > 64 * 1024:
            return None
        content = file.read_bytes()
    elif path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            candidates = [item for item in archive.infolist()
                          if item.filename == 'version.json'
                          or item.filename.count('/') == 1 and item.filename.endswith('/version.json')]
            if len(candidates) != 1 or candidates[0].file_size > 64 * 1024:
                return None
            content = archive.read(candidates[0])
    else:
        return None
    metadata = json.loads(content.decode('utf-8-sig'))
    value = metadata.get('version')
    if not isinstance(value, str) or not re.fullmatch(r'MiaoTask-v0\.\d+\.\d+', value):
        raise ValueError('源码 version.json 中的版本无效。')
    return value


def phase_for_line(line):
    lower = line.lower()
    for words, phase in ((('smoke_packaged', 'offlineocr', 'self-test'), '验证实际 exe 与运行环境'),
                         (('pyinstaller', 'building exe', 'building collect'), '构建 exe 与运行环境'),
                         (('unittest', 'ran ', '测试'), '运行回归测试'),
                         (('pip check', 'pip install', 'requirements'), '检查构建环境'),
                         (('fetch_platform_tools',), '准备 Android 工具')):
        if any(word in lower for word in words):
            return phase
    return None


class QueueWriter:
    """File-like log destination; worker threads never touch a Tk widget."""
    def __init__(self, events):
        self.events = events

    def write(self, text):
        if text:
            self.events.put(('log', str(text)))
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


class ReleaseBuilderWindow:
    def __init__(self, window, *, home=None, source='', build_callable=None, provider_factory=None):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.tk, self.ttk, self.window = tk, ttk, window
        self.events = queue.Queue()
        self.build_callable, self.provider_factory = build_callable, provider_factory
        self.packages, self.activity_vars = [], {}
        self.busy, self.result, self.closed = '', None, False
        self.controls, self.version_generation = [], 0
        self.version_after = None
        self.source = tk.StringVar(value=source or str(ROOT))
        self.home = tk.StringVar(value=str(Path(home or ROOT.parent / 'MiaoTask Release Builder').resolve()))
        self.version_mode = tk.StringVar(value='patch')
        self.target_version, self.summary = tk.StringVar(), tk.StringVar()
        self.python, self.adb_dir = tk.StringVar(), tk.StringVar()
        self.create_env, self.write_back, self.preview = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar()
        self.phase = tk.StringVar(value='选择源码，按需勾选预装活动，然后开始。')
        self.version_hint = tk.StringVar(value='默认递增补丁版本，仅修改构建副本。')
        self.activity_hint = tk.StringVar(value='不勾选时只打包框架；用户可在活动中心按需下载。')

        window.title('MiaoTask Release Builder')
        window.geometry('1040x820')
        window.minsize(920, 740)
        window.configure(bg='#f3f6f5')
        try:
            window.iconbitmap(str(ROOT / 'frontend/assets/miaotask.ico'))
        except tk.TclError:
            pass
        style = ttk.Style(window)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('TFrame', background='#f3f6f5')
        style.configure('Card.TFrame', background='#ffffff')
        style.configure('TLabel', background='#f3f6f5', foreground='#253632')
        style.configure('Card.TLabel', background='#ffffff', foreground='#253632')
        style.configure('Hint.TLabel', foreground='#61736e', background='#ffffff', font=('Microsoft YaHei UI', 9))
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 22, 'bold'))
        style.configure('Section.TLabel', background='#ffffff', font=('Microsoft YaHei UI', 11, 'bold'))
        style.configure('TButton', padding=(10, 7))
        style.configure('Accent.TButton', background='#ed7649', foreground='#ffffff', padding=(18, 10))
        style.map('Accent.TButton', background=[('active', '#d96137'), ('disabled', '#b9c2be')])
        style.configure('TCheckbutton', background='#ffffff', foreground='#263b34')
        style.configure('TRadiobutton', background='#ffffff')
        main = ttk.Frame(window, padding=22)
        main.pack(fill='both', expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(3, weight=1)
        title = ttk.Frame(main)
        title.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(0, 14))
        ttk.Label(title, text='MiaoTask', style='Title.TLabel').pack(side='left')
        ttk.Label(title, text='  Release Builder  ·  本地测试与打包', foreground='#61736e').pack(side='left', pady=(8, 0))

        source_card = ttk.Frame(main, padding=16, style='Card.TFrame')
        source_card.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(0, 12))
        source_card.columnconfigure(1, weight=1)
        ttk.Label(source_card, text='源码', style='Section.TLabel').grid(row=0, column=0, sticky='w', padx=(0, 12))
        self._entry(source_card, self.source).grid(row=0, column=1, sticky='ew', padx=(0, 8))
        self._button(source_card, '选择目录', self.choose_source_dir).grid(row=0, column=2, padx=(0, 6))
        self._button(source_card, '选择 ZIP', self.choose_source_zip).grid(row=0, column=3)
        ttk.Label(source_card, text='也可粘贴 GitHub 仓库、分支页面或源码 ZIP 链接；原始源码默认保持不变。',
                  style='Hint.TLabel').grid(row=1, column=1, columnspan=3, sticky='w', pady=(6, 12))
        ttk.Label(source_card, text='输出', style='Section.TLabel').grid(row=2, column=0, sticky='w', padx=(0, 12))
        self._entry(source_card, self.home).grid(row=2, column=1, columnspan=2, sticky='ew', padx=(0, 8))
        self._button(source_card, '选择位置', self.choose_home).grid(row=2, column=3)

        version_card = ttk.Frame(main, padding=16, style='Card.TFrame')
        version_card.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(0, 12))
        version_card.columnconfigure(1, weight=1)
        ttk.Label(version_card, text='版本', style='Section.TLabel').grid(row=0, column=0, padx=(0, 14), sticky='w')
        choices = ttk.Frame(version_card, style='Card.TFrame')
        choices.grid(row=0, column=1, sticky='w')
        for text, value in (('补丁 +1（默认）', 'patch'), ('功能 +1', 'minor'), ('保持源码版本', 'keep')):
            radio = ttk.Radiobutton(choices, text=text, value=value, variable=self.version_mode)
            radio.pack(side='left', padx=(0, 18))
            self.controls.append(radio)
        ttk.Label(version_card, text='指定版本', style='Card.TLabel').grid(row=0, column=2, padx=(16, 8))
        self._entry(version_card, self.target_version, width=19).grid(row=0, column=3)
        ttk.Label(version_card, textvariable=self.version_hint, style='Hint.TLabel').grid(row=1, column=1, columnspan=3, sticky='w', pady=(7, 12))
        ttk.Label(version_card, text='摘要', style='Section.TLabel').grid(row=2, column=0, sticky='w')
        self._entry(version_card, self.summary).grid(row=2, column=1, columnspan=3, sticky='ew')
        self.advanced_toggle = self._button(version_card, '构建环境与选项 ▸', self.toggle_advanced)
        self.advanced_toggle.grid(row=3, column=1, sticky='w', pady=(10, 0))
        self.advanced = ttk.Frame(version_card, style='Card.TFrame')
        self.advanced.columnconfigure(1, weight=1)
        for row, (text, variable) in enumerate((('Python', self.python), ('ADB 目录', self.adb_dir))):
            ttk.Label(self.advanced, text=text, style='Card.TLabel').grid(row=row, column=0, padx=(0, 10), pady=3, sticky='w')
            self._entry(self.advanced, variable).grid(row=row, column=1, sticky='ew')
        for row, (text, variable) in enumerate((('创建独立锁定依赖环境', self.create_env),
                                               ('成功后回写两份更新文档（仅源码目录）', self.write_back),
                                               ('测试构建，不记录为下次版本基线', self.preview)), start=2):
            check = ttk.Checkbutton(self.advanced, text=text, variable=variable)
            check.grid(row=row, column=1, sticky='w', pady=3)
            self.controls.append(check)

        activity_card = ttk.Frame(main, padding=16, style='Card.TFrame')
        activity_card.grid(row=3, column=1, sticky='nsew', padx=(14, 0))
        activity_card.columnconfigure(0, weight=1)
        activity_card.rowconfigure(1, weight=1)
        bar = ttk.Frame(activity_card, style='Card.TFrame')
        bar.grid(row=0, column=0, sticky='ew')
        ttk.Label(bar, text='预装活动（可选）', style='Section.TLabel').pack(anchor='w')
        activity_buttons = ttk.Frame(bar, style='Card.TFrame')
        activity_buttons.pack(fill='x', pady=(9, 0))
        self._button(activity_buttons, '加载官方 GitHub 活动', self.load_catalog).pack(side='left')
        self._button(activity_buttons, '清空勾选', self.clear_activities).pack(side='right', padx=(8, 0))
        self.activity_canvas = tk.Canvas(activity_card, bg='#ffffff', height=100, highlightthickness=0)
        self.activity_canvas.grid(row=1, column=0, sticky='nsew', pady=(10, 4))
        scrollbar = ttk.Scrollbar(activity_card, orient='vertical', command=self.activity_canvas.yview)
        scrollbar.grid(row=1, column=1, sticky='ns')
        self.activity_canvas.configure(yscrollcommand=scrollbar.set)
        self.activity_list = ttk.Frame(self.activity_canvas, style='Card.TFrame')
        self.activity_item = self.activity_canvas.create_window((0, 0), window=self.activity_list, anchor='nw')
        self.activity_list.bind('<Configure>', lambda event: self.activity_canvas.configure(scrollregion=self.activity_canvas.bbox('all')))
        self.activity_canvas.bind('<Configure>', lambda event: self.activity_canvas.itemconfigure(self.activity_item, width=event.width))
        ttk.Label(self.activity_list, text='点击右侧按钮读取官方 main，勾选本次需要预装的活动。', style='Hint.TLabel').pack(anchor='w', pady=8)
        ttk.Label(activity_card, textvariable=self.activity_hint, style='Hint.TLabel', wraplength=380).grid(row=2, column=0, sticky='w')

        log_card = ttk.Frame(main)
        log_card.grid(row=3, column=0, sticky='nsew')
        log_card.rowconfigure(1, weight=1)
        log_card.columnconfigure(0, weight=1)
        ttk.Label(log_card, textvariable=self.phase).grid(row=0, column=0, sticky='w', pady=(0, 7))
        self.log = ScrolledText(log_card, height=8, bg='#20352f', fg='#e1eee8', insertbackground='#ffffff',
                                relief='flat', font=('Consolas', 10), padx=12, pady=10, state='disabled', wrap='word')
        self.log.grid(row=1, column=0, sticky='nsew')
        self.progress = ttk.Progressbar(log_card, mode='indeterminate')
        self.progress.grid(row=2, column=0, sticky='ew', pady=(7, 0))
        footer = ttk.Frame(main)
        footer.grid(row=4, column=0, columnspan=2, sticky='ew', pady=(12, 0))
        ttk.Label(footer, text='只在本地构建，不提交、不上传或发布。', foreground='#61736e', font=('Microsoft YaHei UI', 9)).pack(side='left')
        self.result_button = ttk.Button(footer, text='打开产物目录', command=self.open_result, state='disabled')
        self.result_button.pack(side='right', padx=(10, 0))
        self.build_button = self._button(footer, '开始测试并打包', self.start_build, style='Accent.TButton')
        self.build_button.pack(side='right')
        for variable in (self.source, self.home, self.version_mode, self.target_version):
            variable.trace_add('write', self.schedule_version)
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.after(80, self.poll)
        self.schedule_version()

    def _entry(self, parent, variable, **kwargs):
        widget = self.ttk.Entry(parent, textvariable=variable, **kwargs)
        self.controls.append(widget)
        return widget

    def _button(self, parent, text, command, **kwargs):
        widget = self.ttk.Button(parent, text=text, command=command, **kwargs)
        self.controls.append(widget)
        return widget

    def choose_source_dir(self):
        from tkinter import filedialog
        value = filedialog.askdirectory(parent=self.window, title='选择完整源码目录')
        if value:
            self.source.set(value)

    def choose_source_zip(self):
        from tkinter import filedialog
        value = filedialog.askopenfilename(parent=self.window, title='选择源码 ZIP', filetypes=[('源码 ZIP', '*.zip')])
        if value:
            self.source.set(value)

    def choose_home(self):
        from tkinter import filedialog
        value = filedialog.askdirectory(parent=self.window, title='选择构建输出位置', initialdir=self.home.get())
        if value:
            self.home.set(value)

    def toggle_advanced(self):
        visible = bool(self.advanced.winfo_manager())
        if visible:
            self.advanced.grid_remove()
        else:
            self.advanced.grid(row=4, column=1, columnspan=3, sticky='ew', pady=(8, 0))
            self.window.geometry(f'{self.window.winfo_width()}x{min(self.window.winfo_screenheight() - 80, max(self.window.winfo_height(), 990))}')
        self.advanced_toggle.configure(text='构建环境与选项 ' + ('▸' if visible else '▾'))

    def schedule_version(self, *_args):
        if self.version_after:
            self.window.after_cancel(self.version_after)
        self.version_generation += 1
        self.version_after = self.window.after(250, self._preview_version)

    def _preview_version(self):
        self.version_after = None
        source, home, mode, target = self.source.get().strip(), self.home.get().strip(), self.version_mode.get(), self.target_version.get().strip() or None
        generation = self.version_generation
        def work():
            try:
                from scripts.local_release import plan_version
                current = local_source_version(source)
                hint = ('构建时读取源码版本，按所选策略生成目标版本。' if current is None else
                        f'源码 {current}  →  目标 {plan_version(current, Path(home), mode, target)}；只修改构建副本。')
            except Exception as exc:
                hint = str(exc)
            self.events.put(('version', (generation, hint)))
        threading.Thread(target=work, name='miao-builder-version', daemon=True).start()

    def set_busy(self, kind=''):
        self.busy = kind
        for control in self.controls:
            control.configure(state='disabled' if kind else 'normal')
        for control, compatible in self.activity_vars.values():
            control[1].configure(state='disabled' if kind or not compatible else 'normal')
        self.result_button.configure(state='normal' if self.result and not kind else 'disabled')
        if kind:
            self.progress.start(12)
        else:
            self.progress.stop()

    def load_catalog(self):
        if self.busy:
            return
        self.set_busy('catalog')
        self.phase.set('正在读取官方 GitHub main 活动目录…')
        def work():
            try:
                if self.provider_factory:
                    provider = self.provider_factory()
                else:
                    from backend.core.plugins.repository import GitHubSourceProvider
                    provider = GitHubSourceProvider()
                self.events.put(('catalog', provider.catalog()))
            except Exception as exc:
                self.events.put(('catalog_error', str(exc)))
        threading.Thread(target=work, name='miao-builder-catalog', daemon=True).start()

    def show_catalog(self, catalog):
        from backend.core.plugins.contracts import FRAMEWORK_API
        self.packages = list(catalog.get('packages', []))
        for child in self.activity_list.winfo_children():
            child.destroy()
        self.activity_vars.clear()
        for item in self.packages:
            identity = item['app'], item['activity']
            compatible = item.get('framework_api') == FRAMEWORK_API and item.get('python') == f'{sys.version_info.major}.{sys.version_info.minor}'
            variable = self.tk.BooleanVar(value=False)
            text = f"{item.get('app_label', identity[0])}  /  {item.get('label', identity[1])}"
            if not compatible:
                text += '  （当前框架不兼容）'
            control = self.ttk.Checkbutton(self.activity_list, text=text, variable=variable,
                                           command=self.update_activity_hint, state='normal' if compatible else 'disabled')
            control.pack(anchor='w', pady=3)
            self.activity_vars[identity] = ((variable, control), compatible)
        if not self.packages:
            self.ttk.Label(self.activity_list, text='此源码提交没有可显示的活动。', style='Hint.TLabel').pack(anchor='w', pady=8)
        self.catalog_revision = catalog.get('revision') or catalog.get('source_commit', '')[:12]
        warnings = catalog.get('warnings', [])
        if warnings:
            self.append_log('目录提示：\n' + '\n'.join(map(str, warnings)) + '\n')
        self.update_activity_hint()

    def update_activity_hint(self):
        count = sum(bool(control[0].get()) for control, compatible in self.activity_vars.values() if compatible)
        self.activity_hint.set(f'官方 main · {getattr(self, "catalog_revision", "")} · 已勾选 {count} 个；未预装的活动可由用户后续按需下载。')

    def clear_activities(self):
        for control, _compatible in self.activity_vars.values():
            control[0].set(False)
        self.update_activity_hint()

    def start_build(self):
        from tkinter import messagebox
        if self.busy:
            return
        try:
            home = self.home.get().strip()
            if not home:
                raise ValueError('请选择构建输出位置。')
            options = BuildOptions(source=self.source.get().strip().strip('"'), home=Path(home).expanduser(),
                                   version_mode=self.version_mode.get(), target_version=self.target_version.get().strip() or None,
                                   summary=self.summary.get().strip(), python=self.python.get().strip() or None,
                                   adb_dir=self.adb_dir.get().strip() or None, create_env=self.create_env.get(),
                                   write_back_notes=self.write_back.get(), preview=self.preview.get())
            selections = [key for key, (control, compatible) in self.activity_vars.items() if compatible and control[0].get()]
            arguments = options.arguments(selected_activities(self.packages, selections))
        except ValueError as exc:
            messagebox.showerror('检查构建选项', str(exc), parent=self.window)
            return
        self.result = None
        self.set_busy('build')
        self.phase.set('准备清洁源码副本，随后测试与打包…')
        self.append_log('\n开始构建：' + options.source + '\n')
        def work():
            writer = QueueWriter(self.events)
            try:
                with redirect_stdout(writer), redirect_stderr(writer):
                    if self.build_callable:
                        build = self.build_callable
                    else:
                        from scripts.local_release import build
                    result = build(options.source, options.home, **arguments)
                self.events.put(('built', result))
            except Exception as exc:
                self.events.put(('log', traceback.format_exc() + '\n'))
                self.events.put(('build_error', str(exc)))
        threading.Thread(target=work, name='miao-release-builder', daemon=False).start()

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text)
        # Keep the window bounded; the complete log remains in runs/<stamp>/build.log.
        if int(self.log.index('end-1c').split('.')[0]) > 5000:
            self.log.delete('1.0', '1001.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def poll(self):
        if self.closed:
            return
        for _index in range(180):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.append_log(value)
                phase = phase_for_line(value)
                if phase and self.busy == 'build':
                    self.phase.set(phase)
            elif kind == 'version':
                generation, hint = value
                if generation == self.version_generation:
                    self.version_hint.set(hint)
            elif kind == 'catalog':
                self.show_catalog(value)
                self.set_busy()
                self.phase.set('目录已加载。选择预装活动，或直接构建框架。')
            elif kind == 'catalog_error':
                self.set_busy()
                self.phase.set('官方目录读取失败；可以不预装活动，继续构建框架。')
                self.append_log('读取活动失败：' + value + '\n')
            elif kind == 'built':
                self.result = value
                self.set_busy()
                self.phase.set('构建完成 · ' + str(value.get('version', '')))
                self.append_log('产物：' + str(value.get('archive', '')) + '\n')
                self.schedule_version()
            elif kind == 'build_error':
                self.set_busy()
                self.phase.set('构建未完成，请查看日志并修正后重试。')
                self.append_log('构建失败：' + value + '\n')
        self.window.after(80, self.poll)

    def open_result(self):
        if self.result and self.result.get('archive'):
            folder = Path(self.result['archive']).resolve().parent
            if folder.is_dir():
                os.startfile(folder)

    def close(self):
        from tkinter import messagebox
        if self.busy == 'build' and not messagebox.askyesno('构建仍在进行',
                '关闭窗口后，本次构建会在后台继续，完整日志会保存在输出位置的 runs 目录。仍要关闭窗口吗？', parent=self.window):
            return
        self.closed = True
        self.window.destroy()


def main():
    import tkinter as tk
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', nargs='?', default='')
    parser.add_argument('--home', type=Path, default=ROOT.parent / 'MiaoTask Release Builder')
    args = parser.parse_args()
    window = tk.Tk()
    ReleaseBuilderWindow(window, home=args.home, source=args.source)
    window.mainloop()


if __name__ == '__main__':
    main()
