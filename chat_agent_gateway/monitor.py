"""A read-only, local Tk window for the gateway worker and its task history."""

import sqlite3
import time
import tkinter as tk
from datetime import datetime
from math import cos, tau
from tkinter import font, ttk

from .paths import STATE_DIR
from .progress import MAX_EVENTS, MAX_TASKS, snapshot, status_text

BACKGROUND = "#0b101c"
SURFACE = "#121b2b"
RECESSED = "#0e1624"
BORDER = "#25344c"
TEXT = "#edf4ff"
MUTED = "#a0afc8"
CYAN = "#72dcff"
VIOLET = "#bbadff"
MINT = "#76edbd"
AMBER = "#ffc68a"
ONLINE_BG = "#14372f"
WARNING_BG = "#392c28"
PULSE_DIM = (49, 86, 116)
PULSE_BRIGHT = (114, 220, 255)
SCAN_COLORS = (
    "#203651",
    "#294762",
    "#38627b",
    "#4a91ad",
    CYAN,
    "#c8f2ff",
    "#68bedb",
    "#345671",
)


def clock_text(stamp):
    return datetime.fromtimestamp(stamp).strftime("%H:%M:%S")


def draw_metric_icon(icon, key, color):
    icon.create_oval(1, 1, 23, 23, fill=RECESSED, outline=BORDER)
    match key:
        case "active":
            icon.create_polygon(9, 6, 17, 12, 9, 18, fill="", outline=color, width=1.5)
        case "queued":
            for y in (8, 12, 16):
                icon.create_line(7, y, 9, y, fill=color, width=1.5)
                icon.create_line(11, y, 17, y, fill=color, width=1.5)
        case "notices":
            icon.create_polygon(
                6,
                7,
                18,
                7,
                18,
                15,
                12,
                15,
                9,
                18,
                9,
                15,
                6,
                15,
                fill="",
                outline=color,
                width=1.5,
            )
            icon.create_line(9, 11, 15, 11, fill=color, width=1.5)
        case "interrupted":
            icon.create_line(10, 8, 10, 16, fill=color, width=2)
            icon.create_line(15, 8, 15, 16, fill=color, width=2)


class Monitor:
    def __init__(self, window, state):
        self.window, self.state = window, state
        installed = set(font.families(window))
        self.sans = "Inter" if "Inter" in installed else "Helvetica Neue"
        self.mono = "IBM Plex Mono" if "IBM Plex Mono" in installed else "Menlo"
        self.title_font = font.Font(window, family=self.sans, size=19, weight="bold")
        self.selected = None
        self.ids = []
        self.task_options = None
        self.task_title = ""
        self.event_view = None
        self.online = False
        self.running = False
        self.scan_started = 0.0

        window.title("Chat Agent Gateway")
        window.geometry("580x560")
        window.minsize(448, 468)
        window.configure(bg=BACKGROUND)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(3, weight=1)
        self.configure_theme()
        self.build_header()
        self.build_metrics()
        self.build_task_panel()
        self.build_events()
        self.build_footer()
        self.refresh()
        self.animate()

    def configure_theme(self):
        style = ttk.Style(self.window)
        style.theme_use("clam")
        style.configure(
            "Monitor.TCombobox",
            fieldbackground=RECESSED,
            background=RECESSED,
            foreground=TEXT,
            arrowcolor=CYAN,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            arrowsize=12,
            padding=(8, 4),
        )
        style.map(
            "Monitor.TCombobox",
            fieldbackground=[("readonly", RECESSED)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", RECESSED)],
            selectforeground=[("readonly", TEXT)],
            bordercolor=[("focus", CYAN)],
        )
        for option, color in (
            ("background", SURFACE),
            ("foreground", TEXT),
            ("selectBackground", "#294462"),
            ("selectForeground", TEXT),
        ):
            self.window.option_add(f"*TCombobox*Listbox.{option}", color)
        style.configure(
            "Monitor.Vertical.TScrollbar",
            background=BORDER,
            troughcolor=RECESSED,
            bordercolor=RECESSED,
            darkcolor=RECESSED,
            lightcolor=RECESSED,
            arrowcolor=MUTED,
            arrowsize=9,
            width=10,
        )
        style.map("Monitor.Vertical.TScrollbar", background=[("active", "#435775")])
        style.configure(
            "Monitor.TCheckbutton",
            background=BACKGROUND,
            foreground=MUTED,
            indicatorbackground=RECESSED,
            bordercolor=BORDER,
            font=(self.sans, 11),
            padding=(4, 2),
        )
        style.map(
            "Monitor.TCheckbutton",
            background=[("active", BACKGROUND)],
            foreground=[("selected", CYAN), ("active", TEXT)],
            indicatorbackground=[("selected", CYAN), ("active", BORDER)],
        )

    def build_header(self):
        header = tk.Frame(self.window, bg=BACKGROUND)
        header.grid(row=0, sticky="ew", padx=18, pady=(16, 13))
        self.mark = tk.Canvas(
            header, width=40, height=40, bg=BACKGROUND, highlightthickness=0
        )
        self.mark.pack(side="left", padx=(0, 11))
        self.mark_outline = self.mark.create_polygon(
            20, 1, 39, 20, 20, 39, 1, 20, fill="#122941", outline="#2e5876"
        )
        self.mark_wave = self.mark.create_line(
            9, 21, 15, 21, 19, 13, 23, 27, 26, 21, 31, 21, fill=CYAN, width=2
        )
        tk.Label(
            header,
            text="运行监视器",
            bg=BACKGROUND,
            fg=TEXT,
            font=(self.sans, 21, "bold"),
        ).pack(side="left")
        self.health = tk.Label(header, font=(self.sans, 11, "bold"), padx=9, pady=5)
        self.health.pack(side="right")

    def build_metrics(self):
        metrics = tk.Frame(self.window, bg=BACKGROUND)
        metrics.grid(row=1, sticky="ew", padx=18)
        self.metrics = {}
        for index, (key, label, accent) in enumerate(
            (
                ("active", "执行中", CYAN),
                ("queued", "排队", VIOLET),
                ("notices", "待回复提醒", MINT),
                ("interrupted", "已中断", AMBER),
            )
        ):
            metrics.columnconfigure(index, weight=1, uniform="metric")
            tile = tk.Frame(
                metrics, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1
            )
            tile.grid(row=0, column=index, sticky="ew", padx=(0, 8 if index < 3 else 0))
            caption = tk.Frame(tile, bg=SURFACE)
            caption.pack(fill="x", padx=8, pady=(6, 0))
            tk.Label(
                caption, text=label, bg=SURFACE, fg=MUTED, font=(self.sans, 10)
            ).pack(side="left")
            icon = tk.Canvas(
                caption, width=24, height=24, bg=SURFACE, highlightthickness=0
            )
            icon.pack(side="right")
            draw_metric_icon(icon, key, accent)
            value = tk.Label(tile, bg=SURFACE, fg=accent, font=(self.mono, 23, "bold"))
            value.pack(anchor="w", padx=10, pady=(0, 5))
            self.metrics[key] = value

    def build_task_panel(self):
        panel = tk.Frame(
            self.window, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1
        )
        panel.grid(row=2, sticky="ew", padx=18, pady=(11, 10))
        panel.columnconfigure(0, weight=1)
        toolbar = tk.Frame(panel, bg=SURFACE)
        toolbar.grid(row=0, sticky="ew", padx=12, pady=(7, 0))
        tk.Label(toolbar, text="任务", bg=SURFACE, fg=MUTED, font=(self.sans, 11)).pack(
            side="left"
        )
        self.tasks = ttk.Combobox(
            toolbar,
            state="readonly",
            style="Monitor.TCombobox",
            font=(self.sans, 11),
            width=18,
            height=10,
        )
        self.tasks.pack(side="right", fill="x", expand=True, padx=(78, 0))
        self.tasks.bind("<<ComboboxSelected>>", self.choose)
        self.title = tk.Label(
            panel,
            anchor="w",
            bg=SURFACE,
            fg=TEXT,
            font=self.title_font,
        )
        self.title.grid(row=1, sticky="ew", padx=12, pady=(2, 3))
        self.title.bind("<Configure>", self.fit_title)

        status = tk.Frame(panel, bg=SURFACE)
        status.grid(row=2, sticky="ew", padx=12, pady=(0, 9))
        self.detail = tk.Label(
            status, bg=SURFACE, fg=MUTED, font=(self.sans, 11, "bold")
        )
        self.detail.pack(side="left")
        self.elapsed = tk.Label(
            status, bg=SURFACE, fg=CYAN, font=(self.mono, 16, "bold")
        )
        self.elapsed.pack(side="right")
        self.scan = tk.Canvas(panel, height=3, bg="#1a283c", highlightthickness=0)
        self.scan.grid(row=3, sticky="ew")
        self.scan_segments = tuple(
            self.scan.create_rectangle(
                0, 0, 0, 3, fill=color, width=0, state="hidden", tags="sweep"
            )
            for color in SCAN_COLORS
        )

    def build_events(self):
        panel = tk.Frame(
            self.window, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1
        )
        panel.grid(row=3, sticky="nsew", padx=18)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        heading = tk.Frame(panel, bg=SURFACE)
        heading.grid(row=0, sticky="ew", padx=12, pady=(7, 5))
        tk.Label(
            heading, text="执行记录", bg=SURFACE, fg=TEXT, font=(self.sans, 12, "bold")
        ).pack(side="left")
        self.steps = tk.Label(heading, bg=SURFACE, fg=MUTED, font=(self.sans, 10))
        self.steps.pack(side="right")

        log = tk.Frame(panel, bg=RECESSED)
        log.grid(row=1, sticky="nsew", padx=7, pady=(0, 7))
        self.log = tk.Text(
            log,
            wrap="word",
            state="disabled",
            bg=RECESSED,
            fg=TEXT,
            selectbackground="#294462",
            selectforeground=TEXT,
            relief="flat",
            highlightthickness=0,
            padx=8,
            pady=7,
            height=4,
            width=12,
            font=(self.sans, 12),
            spacing2=2,
            spacing3=6,
        )
        bar = ttk.Scrollbar(
            log,
            orient="vertical",
            style="Monitor.Vertical.TScrollbar",
            command=self.log.yview,
        )
        self.log.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("event", lmargin2=91)
        self.log.tag_configure("time", foreground=MUTED, font=(self.mono, 10))
        self.log.tag_configure("separator", foreground="#547597", font=(self.mono, 10))
        self.log.tag_configure("latest", foreground=CYAN)
        self.log.tag_configure(
            "event_detail",
            foreground=MUTED,
            font=(self.sans, 11),
            lmargin1=91,
            lmargin2=91,
        )
        self.log.tag_configure(
            "reply_heading", foreground=VIOLET, font=(self.sans, 11, "bold")
        )
        self.log.tag_configure("reply_preview", foreground=MUTED, font=(self.sans, 11))
        self.log.tag_configure("empty", foreground=MUTED)

    def build_footer(self):
        footer = tk.Frame(self.window, bg=BACKGROUND)
        footer.grid(row=4, sticky="ew", padx=18, pady=(6, 7))
        self.updated = tk.Label(footer, bg=BACKGROUND, fg=MUTED, font=(self.sans, 10))
        self.updated.pack(side="left")
        self.pin = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            footer,
            text="窗口置顶",
            style="Monitor.TCheckbutton",
            variable=self.pin,
            command=lambda: self.window.attributes("-topmost", self.pin.get()),
        ).pack(side="right")

    def fit_title(self, event=None):
        available = (event.width if event else self.title.winfo_width()) - 4
        text = self.task_title
        if available > 0 and self.title_font.measure(text) > available:
            ellipsis = "…"
            end = next(
                (
                    size
                    for size in range(len(text) - 1, -1, -1)
                    if self.title_font.measure(text[:size] + ellipsis) <= available
                ),
                0,
            )
            text = text[:end].rstrip() + ellipsis
        self.title.configure(text=text)

    def set_title(self, title):
        if title != self.task_title:
            self.task_title = title
            self.fit_title()

    def choose(self, _event):
        index = self.tasks.current()
        self.selected = self.ids[index - 1] if 0 < index <= len(self.ids) else None
        self.update_view()

    def refresh(self):
        self.update_view()
        self.window.after(1200, self.refresh)

    def animate(self):
        visible = self.window.winfo_viewable() and self.window.state() == "normal"
        if visible:
            tick = time.monotonic()
            if self.online:
                pulse = (1 - cos(tick * tau / 3.2)) / 2
                glow = "#" + "".join(
                    f"{round(dim + (bright - dim) * pulse):02x}"
                    for dim, bright in zip(PULSE_DIM, PULSE_BRIGHT)
                )
                self.mark.itemconfigure(self.mark_outline, outline=glow)
                self.mark.itemconfigure(self.mark_wave, fill=glow)
            else:
                self.mark.itemconfigure(self.mark_outline, outline=BORDER)
                self.mark.itemconfigure(self.mark_wave, fill=MUTED)
            if self.running:
                width = self.scan.winfo_width()
                sweep = max(64, width / 6)
                segment = sweep / len(self.scan_segments)
                phase = ((tick - self.scan_started) % 2.0) / 2.0
                start = phase * (width + sweep) - sweep
                for index, item in enumerate(self.scan_segments):
                    left = start + index * segment
                    self.scan.coords(item, left, 0, left + segment + 1, 3)
        self.window.after(50 if visible and self.online else 250, self.animate)

    def set_running(self, running):
        if running != self.running:
            self.running = running
            self.scan_started = time.monotonic()
            self.scan.itemconfigure("sweep", state="normal" if running else "hidden")

    def update_view(self):
        try:
            view = snapshot(self.state, self.selected)
        except (OSError, sqlite3.Error):
            self.online = False
            self.set_running(False)
            self.health.configure(text="●  读取失败", bg=WARNING_BG, fg=AMBER)
            self.set_metrics(None)
            self.set_task_options(())
            self.set_title("暂时无法读取运行记录")
            self.detail.configure(text="稍后自动重试", fg=AMBER)
            self.elapsed.configure(text="")
            self.steps.configure(text="")
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
            self.event_view = None
            self.updated.configure(text="读取失败于 " + clock_text(time.time()))
        else:
            self.render(view)

    def set_metrics(self, view):
        for key, label in self.metrics.items():
            label.configure(text=str(view[key]) if view is not None else "—")

    def set_task_options(self, options):
        if options != self.task_options:
            self.task_options = options
            self.tasks.configure(
                values=options, state="readonly" if options else "disabled"
            )
        if not options:
            self.tasks.set("")

    def render(self, view):
        online = view["online"]
        self.online = online
        now = time.time()
        self.health.configure(
            text="●  服务在线" if online else "○  服务离线",
            bg=ONLINE_BG if online else WARNING_BG,
            fg=MINT if online else AMBER,
        )
        self.set_metrics(view)
        tasks = view["tasks"]
        self.ids = [row["id"] for row in tasks]
        if self.selected not in self.ids:
            self.selected = None
        index = self.ids.index(self.selected) if self.selected is not None else 0
        values = tuple(
            f"#{row['id']}  {' '.join((row['title'] or '').split())}" for row in tasks
        )
        self.set_task_options(("跟随当前任务", *values) if tasks else ())
        selected_task = tasks[index] if tasks else None
        if selected_task:
            menu_index = index + 1 if self.selected is not None else 0
            if self.tasks.current() != menu_index:
                self.tasks.current(menu_index)
        self.render_task(selected_task, online, now)
        self.render_events(view, selected_task, online)
        self.updated.configure(
            text=f"更新于 {clock_text(now)}   ·   最近 {MAX_TASKS} 个任务"
        )

    def render_task(self, task, online, now):
        elapsed = None
        color = MUTED
        self.set_running(bool(task and online and task["status"] == "running"))
        if task:
            title = " ".join((task["title"] or "").split()) or "未命名任务"
            detail = f"#{task['id']}  ·  {status_text(task, online)}"
            if task["is_blocked"] or (
                not online and task["status"] in ("running", "sending")
            ):
                color = AMBER
            elif task["status"] == "running":
                color = CYAN
            elif task["status"] == "sent":
                color = MINT
            start, end = task["started"], task["ended"]
            if start is not None and (
                end is not None or (online and task["status"] == "running")
            ):
                elapsed = max(0, int((now if end is None else end) - start))
        else:
            title = "等待你的消息" if online else "服务未运行"
            detail = "暂无任务"
        self.set_title(title)
        self.detail.configure(text=detail, fg=color)
        self.elapsed.configure(
            text=f"{elapsed // 60:02d}:{elapsed % 60:02d}"
            if elapsed is not None
            else ""
        )

    def render_events(self, view, task, online):
        total = view["total"]
        self.steps.configure(
            text=f"最近 {MAX_EVENTS} / {total} 步"
            if total > MAX_EVENTS
            else f"{total} 步"
        )
        events = tuple(view["events"])
        task_id = task["id"] if task else None
        preview = view["result_preview"]
        content = (events, task_id, online, preview)
        if content == self.event_view:
            return
        same_task = self.event_view is not None and self.event_view[1] == task_id
        position, bottom = self.log.yview()
        self.event_view = content
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        for index, (at, label, detail) in enumerate(events):
            latest = index == len(events) - 1
            self.log.insert("end", clock_text(at), ("time", "event"))
            self.log.insert(
                "end", "   ◆   " if latest else "   │   ", ("separator", "event")
            )
            self.log.insert(
                "end", label + "\n", ("latest", "event") if latest else ("event",)
            )
            if detail:
                self.log.insert("end", detail + "\n", "event_detail")
        if not events:
            if task:
                empty = "这条任务没有步骤记录。"
            elif online:
                empty = "在 Lark 机器人私聊发任务，执行步骤会显示在这里。"
            else:
                empty = "网关启动后，这里会显示执行步骤。"
            self.log.insert("end", empty + "\n", "empty")
        if preview:
            self.log.insert("end", "\n回复预览\n", "reply_heading")
            self.log.insert("end", preview, "reply_preview")
        if same_task and bottom < 0.99:
            self.log.yview_moveto(position)
        else:
            self.log.see("end")
        self.log.configure(state="disabled")


def main():
    root = tk.Tk()
    Monitor(root, STATE_DIR / "lark")
    root.mainloop()


if __name__ == "__main__":
    main()
