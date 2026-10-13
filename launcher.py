from __future__ import annotations

import ctypes
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    text = 'Python 缺少 Tk。请安装带有 Tcl/Tk 支持的 Python 后重试。'
    if sys.platform == 'win32':
        ctypes.windll.user32.MessageBoxW(None, text, 'VLM PDF 转 Markdown', 0x10)
    else:
        print(text, file=sys.stderr)
    raise SystemExit(1)

APP_ROOT = Path(__file__).resolve().parent
from runtime_paths import STATE_ROOT
SETTINGS = STATE_ROOT / 'settings.json'
DEFAULT_SOURCE = ''

# Modern Fluent & Studio Glass Color System
COLOR_BG = '#f4f6fb'            # 柔和科技冷白工作台底色
COLOR_CARD = '#ffffff'          # 纯白悬浮卡片
COLOR_BORDER = '#e2e8f0'        # 细腻微边框
COLOR_BORDER_FOCUS = '#6366f1'  # 聚焦外框高光
COLOR_TEXT_MAIN = '#0f172a'     # 深度石板黑 (主标题与强文字)
COLOR_TEXT_BODY = '#1e293b'     # 正文深灰
COLOR_TEXT_MUTED = '#64748b'    # 辅助灰
COLOR_TEXT_LIGHT = '#94a3b8'    # 浅灰弱化说明

# Accent Palette
COLOR_PRIMARY = '#4f46e5'       # 科技紫绀蓝 (Indigo 600)
COLOR_PRIMARY_HOVER = '#4338ca' # 悬浮深蓝紫 (Indigo 700)
COLOR_PRIMARY_ACTIVE = '#3730a3'# 激活暗蓝紫 (Indigo 800)
COLOR_PRIMARY_LIGHT = '#eef2ff' # 浅紫蓝背景 (Indigo 50)

# Functional Status Colors
COLOR_SUCCESS = '#059669'       # 翡翠绿 (Emerald 600)
COLOR_SUCCESS_BG = '#ecfdf5'    # 浅绿底
COLOR_WARNING = '#d97706'       # 琥珀金 (Amber 600)
COLOR_WARNING_BG = '#fffbeb'    # 浅琥珀底
COLOR_DANGER = '#dc2626'        # 珊瑚红 (Rose 600)
COLOR_DANGER_BG = '#fef2f2'     # 浅红底
COLOR_DANGER_BORDER = '#fecaca'

# Dark Terminal Console Palette
COLOR_TERM_BG = '#0b0f19'       # 深度黑夜极客蓝黑
COLOR_TERM_HEADER = '#131b2e'   # 终端标题栏
COLOR_TERM_BORDER = '#1e293b'   # 终端内边框
COLOR_TERM_TEXT = '#cbd5e1'     # 终端正文字
COLOR_TERM_MUTED = '#64748b'    # 终端时间戳弱化文字


class Launcher:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title('VLM PDF 转 Markdown')
        root.geometry('980x820')
        root.minsize(900, 720)
        root.configure(background=COLOR_BG)
        
        # 加载应用程序图标
        if (APP_ROOT / 'app.ico').exists():
            try:
                root.iconbitmap(str(APP_ROOT / 'app.ico'))
            except Exception:
                pass
                
        # 尝试应用 Windows 11 现代窗口圆角与深浅适配
        self.apply_dwm_attributes()

        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.active = False
        self.started_at = 0.0
        self.source = tk.StringVar(value=DEFAULT_SOURCE)
        self.destination = tk.StringVar()
        self.same_folder = tk.BooleanVar(value=True)
        self.recursive = tk.BooleanVar(value=False)
        self.retry_failed = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='选择文件夹，然后点击「开始转换」。')
        self.count = tk.StringVar(value='')
        self.detail = tk.StringVar(value='默认在每份 PDF 旁保存同名 Markdown 和配图文件夹。')
        self.rows = {}
        self.files = []
        self.last_output = None
        self.scan_timer = None
        self.logo_image = None
        
        self.load_settings()
        self._custom_destination = self.destination.get()
        self._same_previous = True
        
        self.build()
        
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Control-Return>', lambda _: self.start())
        self.source.trace_add('write', self.schedule_scan)
        self.recursive.trace_add('write', self.schedule_scan)
        self.same_folder.trace_add('write', self.update_destination)
        self.update_destination()
        self.scan()
        self.poll_timer = root.after(150, self.poll)

    def apply_dwm_attributes(self):
        """为 Windows 11/10 窗口注入现代 DWM 圆角与属性"""
        if sys.platform != 'win32':
            return
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                hwnd = self.root.winfo_id()
            # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)
            )
        except Exception:
            pass

    def build(self):
        style = ttk.Style()
        style.theme_use('clam')

        # 全局基础排版与字体
        style.configure('.', font=('Microsoft YaHei UI', 10), foreground=COLOR_TEXT_BODY)
        style.configure('TFrame', background=COLOR_BG)
        style.configure('Card.TFrame', background=COLOR_CARD)
        style.configure('TLabel', background=COLOR_BG, foreground=COLOR_TEXT_BODY)
        style.configure('Card.TLabel', background=COLOR_CARD, foreground=COLOR_TEXT_BODY)
        style.configure('Muted.TLabel', foreground=COLOR_TEXT_MUTED, font=('Microsoft YaHei UI', 9))
        style.configure('CardMuted.TLabel', background=COLOR_CARD, foreground=COLOR_TEXT_MUTED, font=('Microsoft YaHei UI', 9))
        
        # 顶栏主副标题
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 18, 'bold'), foreground=COLOR_TEXT_MAIN)
        style.configure('Subtitle.TLabel', font=('Microsoft YaHei UI', 9), foreground=COLOR_TEXT_MUTED)
        
        # 卡片分节小标题
        style.configure('Section.TLabel', background=COLOR_CARD, font=('Microsoft YaHei UI', 10, 'bold'), foreground=COLOR_TEXT_MAIN)
        style.configure('FieldLabel.TLabel', background=COLOR_CARD, font=('Microsoft YaHei UI', 9, 'bold'), foreground=COLOR_TEXT_BODY)

        # 按钮系统
        # 1. 默认/次要按钮 (Secondary Button)
        style.configure('TButton', padding=(14, 7), background='#ffffff', borderwidth=1,
                        relief='flat', font=('Microsoft YaHei UI', 9))
        style.map('TButton',
                  background=[('disabled', '#f8fafc'), ('active', '#f1f5f9')],
                  foreground=[('disabled', '#94a3b8'), ('active', COLOR_TEXT_MAIN)])

        # 2. 突出主按钮 (Primary Action Button)
        style.configure('Primary.TButton', background=COLOR_PRIMARY, foreground='#ffffff',
                        padding=(22, 10), borderwidth=0, relief='flat', font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Primary.TButton',
                  background=[('disabled', '#cbd5e1'), ('active', COLOR_PRIMARY_HOVER)],
                  foreground=[('disabled', '#f1f5f9'), ('active', '#ffffff')])

        # 3. 停止按钮 (Danger / Stop Button)
        style.configure('Stop.TButton', background=COLOR_DANGER_BG, foreground=COLOR_DANGER,
                        padding=(16, 9), borderwidth=1, relief='flat', font=('Microsoft YaHei UI', 9, 'bold'))
        style.map('Stop.TButton',
                  background=[('disabled', '#f8fafc'), ('active', '#fee2e2')],
                  foreground=[('disabled', '#94a3b8'), ('active', '#b91c1c')])

        # 4. 打开目录按钮 (Success Outline Button)
        style.configure('Open.TButton', background='#ffffff', foreground=COLOR_PRIMARY,
                        padding=(16, 9), borderwidth=1, relief='flat', font=('Microsoft YaHei UI', 9, 'bold'))
        style.map('Open.TButton',
                  background=[('disabled', '#f8fafc'), ('active', COLOR_PRIMARY_LIGHT)],
                  foreground=[('disabled', '#94a3b8'), ('active', COLOR_PRIMARY)])

        # 复选框 Checkbutton
        style.configure('TCheckbutton', background=COLOR_CARD, font=('Microsoft YaHei UI', 9))
        style.map('TCheckbutton', background=[('active', COLOR_CARD)])

        # 输入框 Entry
        style.configure('TEntry', padding=(9, 7), fieldbackground='#ffffff', borderwidth=1, relief='flat')

        # 进度条 Progressbar
        style.configure('Horizontal.TProgressbar', background=COLOR_PRIMARY, troughcolor='#e2e8f0',
                        borderwidth=0, thickness=8)

        # 列表表格 Treeview
        style.configure('Treeview', background='#ffffff', fieldbackground='#ffffff', rowheight=36,
                        borderwidth=0, font=('Microsoft YaHei UI', 10))
        style.configure('Treeview.Heading', padding=(10, 10), background='#f1f5f9', relief='flat',
                        font=('Microsoft YaHei UI', 9, 'bold'), foreground=COLOR_TEXT_MAIN)
        style.map('Treeview',
                  background=[('selected', '#e0e7ff')],
                  foreground=[('selected', '#1e1b4b')])

        # 滚动条 Scrollbar
        style.configure('Vertical.TScrollbar', gripcount=0, background='#cbd5e1', troughcolor='#f8fafc',
                        borderwidth=0, relief='flat')

        # 外层主容器
        outer = tk.Frame(self.root, bg=COLOR_BG, padx=18, pady=12)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)  # 让表格与日志区域弹性分配

        # ----------------------------------------------------
        # 1. 顶部 Header 品牌区 (带高清图标、主副标、科技徽章)
        # ----------------------------------------------------
        header_frame = tk.Frame(outer, bg=COLOR_BG)
        header_frame.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        header_frame.columnconfigure(1, weight=1)

        # 尝试加载 48x48 高保真水晶 Logo
        logo_path = APP_ROOT / 'app_logo_48.png'
        if logo_path.exists():
            try:
                self.logo_image = tk.PhotoImage(file=str(logo_path))
                logo_lbl = tk.Label(header_frame, image=self.logo_image, bg=COLOR_BG)
                logo_lbl.grid(row=0, column=0, rowspan=2, padx=(0, 12), sticky='w')
            except Exception:
                pass

        title_box = tk.Frame(header_frame, bg=COLOR_BG)
        title_box.grid(row=0, column=1, sticky='w')
        ttk.Label(title_box, text='VLM PDF 转 Markdown', style='Title.TLabel').pack(anchor='w')
        ttk.Label(title_box, text='基于本地 MinerU 视觉模型 · 识别文字、版面、表格与公式',
                  style='Subtitle.TLabel').pack(anchor='w', pady=(2, 0))

        # 右侧科技特征微药丸徽章
        badges_frame = tk.Frame(header_frame, bg=COLOR_BG)
        badges_frame.grid(row=0, column=2, sticky='e', padx=(10, 0))
        
        badge1 = tk.Label(badges_frame, text='⚡ 本地离线加速', bg='#e0f2fe', fg='#0369a1',
                          font=('Microsoft YaHei UI', 9, 'bold'), padx=8, pady=3)
        badge1.pack(side='left', padx=(0, 6))
        
        badge2 = tk.Label(badges_frame, text='🛡️ 零数据上传', bg='#ecfdf5', fg='#047857',
                          font=('Microsoft YaHei UI', 9, 'bold'), padx=8, pady=3)
        badge2.pack(side='left')

        # ----------------------------------------------------
        # 2. 配置卡片 (路径与选项 Card)
        # ----------------------------------------------------
        card_config = tk.Frame(outer, bg=COLOR_CARD, bd=1, relief='solid',
                               highlightbackground=COLOR_BORDER, highlightthickness=1)
        card_config.grid(row=1, column=0, sticky='ew', pady=(0, 10))
        card_config.columnconfigure(1, weight=1)

        cfg_inner = tk.Frame(card_config, bg=COLOR_CARD, padx=16, pady=11)
        cfg_inner.pack(fill='both', expand=True)
        cfg_inner.columnconfigure(1, weight=1)

        # 源目录
        ttk.Label(cfg_inner, text='PDF 文件夹', style='FieldLabel.TLabel').grid(row=0, column=0, sticky='w', padx=(0, 12))
        self.source_entry = ttk.Entry(cfg_inner, textvariable=self.source)
        self.source_entry.grid(row=0, column=1, sticky='ew')
        self.source_button = ttk.Button(cfg_inner, text='浏览文件夹…', command=self.choose_source)
        self.source_button.grid(row=0, column=2, padx=(8, 0))

        # 默认保存选项
        self.same_check = ttk.Checkbutton(cfg_inner, text='输出到源文件目录（推荐，保持同名排版与配图同级）',
                                          variable=self.same_folder)
        self.same_check.grid(row=1, column=1, sticky='w', pady=(6, 4))

        # 自选输出位置
        ttk.Label(cfg_inner, text='自选输出位置', style='FieldLabel.TLabel').grid(row=2, column=0, sticky='w', padx=(0, 12))
        self.dest_entry = ttk.Entry(cfg_inner, textvariable=self.destination)
        self.dest_entry.grid(row=2, column=1, sticky='ew')
        self.dest_button = ttk.Button(cfg_inner, text='自选位置…', command=self.choose_output)
        self.dest_button.grid(row=2, column=2, padx=(8, 0))

        ttk.Label(cfg_inner, textvariable=self.detail, style='CardMuted.TLabel', wraplength=760).grid(
            row=3, column=1, columnspan=2, sticky='w', pady=(3, 6))

        # 分割线
        sep = tk.Frame(cfg_inner, height=1, bg=COLOR_BORDER)
        sep.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(2, 8))

        # 选项开关与统计计数
        options_row = tk.Frame(cfg_inner, bg=COLOR_CARD)
        options_row.grid(row=5, column=0, columnspan=3, sticky='ew')
        
        self.recursive_check = ttk.Checkbutton(options_row, text='包含子文件夹', variable=self.recursive)
        self.recursive_check.pack(side='left')
        
        self.retry_check = ttk.Checkbutton(options_row, text='重试上次失败的文件', variable=self.retry_failed)
        self.retry_check.pack(side='left', padx=(18, 0))

        # 文件统计胶囊
        self.count_badge = tk.Label(options_row, textvariable=self.count, bg='#f1f5f9', fg=COLOR_TEXT_MAIN,
                                    font=('Microsoft YaHei UI', 9, 'bold'), padx=9, pady=2)
        self.count_badge.pack(side='right')

        # ----------------------------------------------------
        # 3. 控制与进度指示区 (Action Bar & Progress HUD)
        # ----------------------------------------------------
        card_action = tk.Frame(outer, bg=COLOR_CARD, bd=1, relief='solid',
                               highlightbackground=COLOR_BORDER, highlightthickness=1)
        card_action.grid(row=2, column=0, sticky='ew', pady=(0, 10))

        act_inner = tk.Frame(card_action, bg=COLOR_CARD, padx=16, pady=10)
        act_inner.pack(fill='both', expand=True)
        act_inner.columnconfigure(0, weight=1)

        # 按钮栏
        btn_bar = tk.Frame(act_inner, bg=COLOR_CARD)
        btn_bar.pack(fill='x', pady=(0, 8))

        self.start_button = ttk.Button(btn_bar, text='🚀 开始转换', style='Primary.TButton', command=self.start)
        self.start_button.pack(side='left')

        self.stop_button = ttk.Button(btn_bar, text='⏹ 完成当前文件后停止', style='Stop.TButton',
                                      command=self.request_stop, state='disabled')
        self.stop_button.pack(side='left', padx=(10, 0))

        self.open_button = ttk.Button(btn_bar, text='📂 打开结果目录', style='Open.TButton',
                                      command=self.open_output, state='disabled')
        self.open_button.pack(side='right')

        # 进度指示与状态
        hud_bar = tk.Frame(act_inner, bg=COLOR_CARD)
        hud_bar.pack(fill='x', pady=(0, 4))
        
        self.status_label = ttk.Label(hud_bar, textvariable=self.status, style='Card.TLabel',
                                      font=('Microsoft YaHei UI', 9), wraplength=730)
        self.status_label.pack(side='left')

        self.timer_label = ttk.Label(hud_bar, text='', style='CardMuted.TLabel',
                                     font=('Microsoft YaHei UI', 9, 'bold'))
        self.timer_label.pack(side='right')

        # 饱满现代进度条
        self.progress = ttk.Progressbar(act_inner, mode='determinate')
        self.progress.pack(fill='x')

        # ----------------------------------------------------
        # 4. 任务表格与实时控制台分层 (Queue & Terminal)
        # ----------------------------------------------------
        main_split = tk.Frame(outer, bg=COLOR_BG)
        main_split.grid(row=3, column=0, sticky='nsew')
        main_split.columnconfigure(0, weight=1)
        main_split.rowconfigure(0, weight=3)  # 表格卡片
        main_split.rowconfigure(1, weight=2)  # 终端卡片

        # 表格卡片
        table_card = tk.Frame(main_split, bg=COLOR_CARD, bd=1, relief='solid',
                              highlightbackground=COLOR_BORDER, highlightthickness=1)
        table_card.grid(row=0, column=0, sticky='nsew', pady=(0, 8))
        table_card.columnconfigure(0, weight=1)
        table_card.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(table_card, columns=('name', 'status', 'result'), show='headings',
                                 selectmode='browse', height=5)
        for key, label, width in [('name', '📄 PDF 文件列表', 420), ('status', '状态', 130), ('result', '输出产物 / 说明', 320)]:
            is_status = (key == 'status')
            self.tree.heading(key, text=label, anchor='center' if is_status else 'w')
            self.tree.column(key, width=width, minwidth=80, stretch=key != 'status',
                             anchor='center' if is_status else 'w')

        # 彩色徽章标签配置
        self.tree.tag_configure('failed', foreground=COLOR_DANGER, background=COLOR_DANGER_BG)
        self.tree.tag_configure('completed', foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_BG)
        self.tree.tag_configure('processing', foreground=COLOR_PRIMARY, background=COLOR_PRIMARY_LIGHT)
        self.tree.tag_configure('skipped', foreground=COLOR_WARNING, background=COLOR_WARNING_BG)
        self.tree.tag_configure('even', background='#ffffff')
        self.tree.tag_configure('odd', background='#f8fafc')

        self.tree.grid(row=0, column=0, sticky='nsew')
        
        scrollbar = ttk.Scrollbar(table_card, orient='vertical', command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scrollbar.set)

        # ----------------------------------------------------
        # 5. 极客暗夜终端 (Inference Engine Console)
        # ----------------------------------------------------
        term_frame = tk.Frame(main_split, bg=COLOR_TERM_BG, bd=1, relief='solid',
                              highlightbackground=COLOR_TERM_BORDER, highlightthickness=1)
        term_frame.grid(row=1, column=0, sticky='nsew')
        term_frame.columnconfigure(0, weight=1)
        term_frame.rowconfigure(1, weight=1)

        # 终端标题栏
        term_header = tk.Frame(term_frame, bg=COLOR_TERM_HEADER, padx=10, pady=5)
        term_header.grid(row=0, column=0, sticky='ew')
        
        term_dot = tk.Label(term_header, text='●', fg='#34d399', bg=COLOR_TERM_HEADER, font=('Segoe UI', 8))
        term_dot.pack(side='left', padx=(0, 6))

        term_title = tk.Label(term_header, text='转换日志 (MinerU Engine Logs)',
                              fg='#94a3b8', bg=COLOR_TERM_HEADER, font=('Microsoft YaHei UI', 9, 'bold'))
        term_title.pack(side='left')

        clear_btn = tk.Button(term_header, text='清空日志', bg='#1e293b', fg='#94a3b8',
                              activebackground='#334155', activeforeground='#f8fafc',
                              bd=0, font=('Microsoft YaHei UI', 8), padx=6, pady=1,
                              cursor='hand2', command=self.clear_logs)
        clear_btn.pack(side='right')

        # 终端输出框
        self.logbox = tk.Text(term_frame, height=4, background=COLOR_TERM_BG, foreground=COLOR_TERM_TEXT,
                              relief='flat', font=('Consolas', 10), padx=10, pady=6,
                              wrap='word', state='disabled', insertbackground='white')
        self.logbox.grid(row=1, column=0, sticky='nsew')

        # 终端日志语法高亮色彩配置
        self.logbox.tag_configure('info', foreground='#38bdf8')      # 亮天青
        self.logbox.tag_configure('success', foreground='#34d399')   # 亮薄荷绿
        self.logbox.tag_configure('warning', foreground='#fbbf24')   # 亮金黄
        self.logbox.tag_configure('error', foreground='#f87171')     # 亮珊瑚红
        self.logbox.tag_configure('time', foreground=COLOR_TERM_MUTED)

        self.append_log('界面已准备就绪。开始转换时加载本地模型。')
        self.append_log('系统准备完毕。已有完整结果自动跳过；公式和表格数值请核对原 PDF。')
        
        self.controls = [self.source_entry, self.source_button, self.same_check,
                         self.recursive_check, self.retry_check]

    def clear_logs(self):
        self.logbox.configure(state='normal')
        self.logbox.delete('1.0', 'end')
        self.logbox.configure(state='disabled')

    def load_settings(self):
        try:
            values = json.loads(SETTINGS.read_text(encoding='utf-8'))
            if not isinstance(values, dict):
                return
            self.source.set(values.get('source', DEFAULT_SOURCE))
            self.destination.set(values.get('destination', ''))
            self.recursive.set(bool(values.get('recursive', False)))
        except (OSError, ValueError, TypeError):
            pass

    def save_settings(self):
        values = {
            'source': self.source.get(),
            'destination': self._custom_destination if self.same_folder.get() else self.destination.get(),
            'recursive': self.recursive.get()
        }
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS.with_suffix('.tmp')
            tmp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(SETTINGS)
        except OSError as exc:
            logging.warning('Cannot save preferences: %s', exc)

    def choose_source(self):
        chosen = filedialog.askdirectory(title='选择 PDF 所在文件夹', initialdir=self.source.get() or str(Path.home()))
        if chosen:
            self.source.set(chosen)

    def choose_output(self):
        chosen = filedialog.askdirectory(title='选择 Markdown 输出文件夹',
                                         initialdir=self.destination.get() or self.source.get() or str(Path.home()))
        if chosen:
            self.destination.set(chosen)

    def update_destination(self, *_):
        if self.same_folder.get():
            if not self._same_previous:
                self._custom_destination = self.destination.get()
            self.destination.set(self.source.get())
            self.dest_entry.configure(state='disabled')
            self.dest_button.configure(state='disabled')
            self.detail.set('在每份 PDF 旁保存同名 .md 和配图文件夹。')
        else:
            if self._same_previous:
                self.destination.set(self._custom_destination or self.source.get())
            if not self.destination.get().strip():
                self.destination.set(self.source.get())
            state = 'disabled' if self.active else 'normal'
            self.dest_entry.configure(state=state)
            self.dest_button.configure(state=state)
            self.detail.set('保存到自选目录；包含子文件夹时保留原目录层级。')
        self._same_previous = self.same_folder.get()

    def schedule_scan(self, *_):
        if self.active:
            return
        if self.same_folder.get():
            self.destination.set(self.source.get())
        if self.scan_timer:
            self.root.after_cancel(self.scan_timer)
        self.scan_timer = self.root.after(250, self.scan)

    def scan(self):
        if self.scan_timer:
            self.root.after_cancel(self.scan_timer)
        self.scan_timer = None
        if self.active:
            return
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.files = []
        if not self.source.get().strip():
            self.count.set('未选择文件夹')
            self.status.set('请先选择 PDF 所在文件夹。')
            self.start_button.configure(state='disabled')
            return
        try:
            from backend import discover_pdfs
            self.files = discover_pdfs(self.source.get().strip(), self.recursive.get())
            for idx, pdf in enumerate(self.files):
                label = str(pdf.relative_to(Path(self.source.get().strip()).resolve()))
                tag = 'even' if idx % 2 == 0 else 'odd'
                self.rows[str(pdf.resolve())] = self.tree.insert('', 'end', values=(label, '待处理', ''),
                                                                 tags=(tag,))
            self.count.set(f'{len(self.files)} 个 PDF' if self.files else '0 个 PDF')
            self.status.set('准备就绪，点击「开始转换」。' if self.files else '此文件夹没有 PDF，请选择其他文件夹。')
            self.start_button.configure(state='normal' if self.files else 'disabled')
        except Exception as exc:
            self.count.set('读取异常')
            self.status.set(f'无法读取文件夹：{exc}')
            self.start_button.configure(state='disabled')

    def start(self):
        if self.active:
            return
        self.scan()
        if not self.files:
            return
        source = self.source.get().strip()
        output = None if self.same_folder.get() else self.destination.get().strip()
        if output == '':
            self.status.set('请先选择输出位置。')
            self.dest_entry.focus_set()
            return
        self.active = True
        self.stop_event.clear()
        self.started_at = time.monotonic()
        self.last_output = None
        self.progress.configure(maximum=len(self.files), value=0)
        self.start_button.configure(state='disabled', text='正在转换…')
        self.stop_button.configure(state='normal', text='完成当前文件后停止')
        self.open_button.configure(state='disabled')
        for widget in self.controls + [self.dest_entry, self.dest_button]:
            widget.configure(state='disabled')
        self.save_settings()
        self.status.set('正在启动本地 VLM，首次加载模型需要一些时间。')
        self.append_log(f'开始转换：{source}', 'info')
        recursive = self.recursive.get()
        retry = self.retry_failed.get()
        threading.Thread(target=self.worker, args=(source, output, recursive, retry), daemon=False).start()

    def worker(self, source, output, recursive, retry):
        try:
            from backend import run_conversion
            summary = run_conversion(source, output, recursive, retry, self.events.put, self.stop_event)
            self.events.put({'kind': 'finished', 'summary': summary})
        except Exception as exc:
            logging.exception('Conversion failed')
            self.events.put({'kind': 'fatal', 'error': str(exc)})

    def request_stop(self):
        if self.active:
            self.stop_event.set()
            self.stop_button.configure(state='disabled', text='等待当前文件完成…')
            self.status.set('将完成当前 PDF 后停止，其余文件可下次继续转换。')

    def append_log(self, text, level='normal'):
        logging.info('%s', text)
        now_str = datetime.now().strftime('%H:%M:%S ')
        
        # 自动识别日志级别
        if level == 'normal':
            lower = text.lower()
            if 'error' in lower or '失败' in text or 'fatal' in lower:
                level = 'error'
            elif '完成' in text or '成功' in text or 'ready' in lower or '结束' in text:
                level = 'success'
            elif '跳过' in text or 'warn' in lower:
                level = 'warning'
            elif '转换' in text or 'info' in lower or '启动' in text:
                level = 'info'

        self.logbox.configure(state='normal')
        self.logbox.insert('end', now_str, 'time')
        self.logbox.insert('end', text.rstrip() + '\n', level)
        
        if int(self.logbox.index('end-1c').split('.')[0]) > 600:
            self.logbox.delete('1.0', '100.0')
        self.logbox.see('end')
        self.logbox.configure(state='disabled')

    def poll(self):
        try:
            while True:
                self.handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.active:
            elapsed = int(time.monotonic() - self.started_at)
            self.timer_label.configure(text=f'⏱ 已用 {elapsed // 60:02d}:{elapsed % 60:02d}')
        self.poll_timer = self.root.after(150, self.poll)

    def handle_event(self, event):
        kind = event.get('kind')
        if kind == 'scan':
            self.progress.configure(maximum=max(1, event['total']))
        elif kind == 'file_start':
            path = str(Path(event['path']).resolve())
            row = self.rows.get(path)
            if row:
                self.tree.set(row, 'status', '正在转换')
                self.tree.item(row, tags=('processing',))
                self.tree.see(row)
            self.status.set(f"正在转换 {event['index']}/{event['total']}：{Path(path).name}")
            self.append_log(self.status.get(), 'info')
        elif kind in ('log', 'activity'):
            text = event.get('text', '')
            if text:
                self.append_log(text)
        elif kind == 'file_done':
            row = self.rows.get(str(Path(event['path']).resolve()))
            status = event.get('status')
            label = {'completed': '已完成', 'skipped': '已跳过', 'failed': '失败'}.get(status, status)
            info = event.get('error') or event.get('markdown') or ''
            if row:
                self.tree.set(row, 'status', label)
                self.tree.set(row, 'result', info)
                self.tree.item(row, tags=(status,))
            self.progress.configure(value=event['index'])
            err_msg = f'；{event["error"]}' if event.get('error') else ''
            self.append_log(f'{Path(event["path"]).name}：{label}{err_msg}',
                            'error' if status == 'failed' else ('warning' if status == 'skipped' else 'success'))
        elif kind == 'finished':
            summary = event['summary']
            self.last_output = summary.get('output_root') or self.source.get()
            prefix = '已停止' if summary.get('stopped') else '转换结束'
            self.status.set(f"{prefix}：完成 {summary['completed']}，跳过 {summary['skipped']}，失败 {summary['failed']}。")
            self.append_log(self.status.get(), 'success')
            self.finish()
        elif kind == 'fatal':
            self.status.set(f'无法开始转换：{event["error"]}')
            self.append_log(self.status.get(), 'error')
            self.finish()

    def finish(self):
        self.active = False
        self.start_button.configure(state='normal', text='🚀 再次转换')
        self.stop_button.configure(state='disabled', text='⏹ 完成当前文件后停止')
        for widget in self.controls:
            widget.configure(state='normal')
        self.update_destination()
        self.open_button.configure(state='normal' if self.last_output else 'disabled')

    def open_output(self):
        if self.last_output:
            try:
                os.startfile(str(Path(self.last_output).resolve()))
            except OSError as exc:
                self.status.set(f'无法打开目录：{exc}')

    def close(self):
        if self.active:
            if messagebox.askyesno('正在转换', '当前 PDF 仍在转换。是否完成当前文件后停止？窗口将在任务结束后手动关闭。'):
                self.request_stop()
            return
        self.save_settings()
        self.cancel_timers()
        self.root.destroy()

    def cancel_timers(self):
        for timer in (self.scan_timer, self.poll_timer):
            if timer:
                self.root.after_cancel(timer)


def main():
    mutex = None
    root = None
    try:
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=STATE_ROOT / 'launcher.log', encoding='utf-8', level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s')
        if sys.platform == 'win32':
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
            kernel = ctypes.windll.kernel32
            kernel.CreateMutexW.restype = ctypes.c_void_p
            mutex = kernel.CreateMutexW(None, False, 'Local\\VlmPdfToMarkdownPublicLauncher')
            if kernel.GetLastError() == 183:
                ctypes.windll.user32.MessageBoxW(None, '程序已打开，请在任务栏中切换到转换窗口。', 'VLM PDF 转 Markdown', 0)
                return
        root = tk.Tk()
        Launcher(root)
        root.mainloop()
    except Exception as exc:
        logging.exception('Launcher startup failed')
        text = f'程序启动失败：{exc}\n请检查 Python 环境与运行数据目录。\n日志位置：{STATE_ROOT / "launcher.log"}'
        if root is not None:
            messagebox.showerror('无法打开转换入口', text)
        elif sys.platform == 'win32':
            ctypes.windll.user32.MessageBoxW(None, text, '无法打开转换入口', 0x10)
        else:
            print(text, file=sys.stderr)
    finally:
        if mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))


if __name__ == '__main__':
    main()
