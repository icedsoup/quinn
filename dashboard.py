from __future__ import annotations

import math
import threading
import time
import tkinter as tk
from tkinter import font, ttk

from PIL import Image, ImageDraw, ImageTk

from telemetry import HISTORY_STAGE, LIVE

BG = '#000000'
PANEL = '#101010'
INSET = '#0b0b0b'
LINE = '#292929'
TEXT = '#f5f5f5'
MUTED = '#a6a6a6'
FAINT = '#858585'
WHITE = '#ffffff'
GREEN = '#a4c6ad'
AMBER = '#dec18d'
RED = '#f0a5a5'
FONT = 'Segoe UI'
MONO = 'Cascadia Code'
SPACE = 8
GAP = 12
PAD = 2 * SPACE
FRAME_MS = 34
RUN_LIMIT = 12

STAGES = {
    'quinn': [('intake', 'Receive'), ('context', 'Load context'), ('inference', 'Generate'),
              ('reply', 'Save reply'), ('delivery', 'Deliver')],
    'sniper': [('intake', 'Detect drop'), ('download', 'Read image'), ('ocr', 'Recognize'),
               ('match', 'Match cards'), ('wishlist', 'Check wishlist'), ('claim', 'Claim')],
}
STATES = {
    'waiting': ('', FAINT, ''),
    'active': ('Running', WHITE, ''),
    'complete': ('Done', GREEN, '✓'),
    'error': ('Failed', RED, '!'),
    'skipped': ('Skipped', FAINT, '−'),
    'stopped': ('Stopped', AMBER, '×'),
}


def measurement(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def elapsed(value):
    if not measurement(value):
        return '—'
    return f'{value:.0f} ms' if value < 1000 else f'{value / 1000:.1f} s'


def number(value):
    return f'{value:,.0f}' if measurement(value) else '—'


def cooldown(value):
    if not measurement(value):
        return '—'
    if value == 0:
        return 'Ready'
    minutes, seconds = divmod(int(value), 60)
    return f'{minutes}:{seconds:02d}'


def stage_name(stage):
    return 'OCR' if stage == 'ocr' else stage.replace('_', ' ').title()


def stage_state(event, service_active):
    if event is None:
        return 'waiting'
    state = event['status']
    if state in LIVE:
        return 'active' if service_active else 'stopped'
    return state


def stage_duration(event, service_active, now):
    if not event:
        return None
    metrics = event['metrics']
    duration = metrics.get('duration_ms')
    if measurement(duration):
        return duration
    start = event.get('started')
    if event['status'] in LIVE:
        if not service_active:
            return None
        observed = metrics.get('elapsed_ms')
        if measurement(observed):
            return observed + max(0, now - event['time']) * 1000
        end = now
    else:
        end = event.get('stopped_at', event['time'])
    return max(0, end - start) * 1000 if start is not None else None


def recent_runs(data, source):
    stage = HISTORY_STAGE[source]
    runs = [(trace, run['stages'][stage], run) for trace, run in data['traces'].items() if stage in run['stages']]
    runs.sort(key=lambda item: item[1].get('started', item[1]['time']))
    return runs[-RUN_LIMIT:]


def chart_ceiling(duration):
    magnitude = 10 ** math.floor(math.log10(max(100, duration)))
    return next(factor * magnitude for factor in (1, 2, 5, 10) if factor * magnitude >= duration)


def blend(start, end, fraction):
    return '#' + ''.join(f'{round(int(start[i:i+2], 16) * (1 - fraction) + int(end[i:i+2], 16) * fraction):02x}'
                         for i in (1, 3, 5))


def label(parent, text='', size=10, color=TEXT, bold=False, **kwargs):
    return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=color, bd=0,
                    font=(FONT, size, 'bold' if bold else 'normal'), **kwargs)


def panel(parent, background=INSET):
    return tk.Frame(parent, bg=background, highlightbackground=LINE, highlightthickness=1)


class Control(tk.Button):
    def __init__(self, parent, text, command, *, primary=False, danger=False, nav=False):
        self.pixel = tk.PhotoImage(master=parent, width=1, height=1)
        self.colors = (WHITE, '#dedede', '#bfbfbf', BG) if primary else (
            '#151515', '#262626', '#603838' if danger else '#343434', TEXT)
        self.base, self.hover, pressed, foreground = self.colors
        self.hovered = self.selected = False
        self.timer = None
        super().__init__(parent, text=text, command=command, image=self.pixel, compound='center',
                         height=(40 if nav else 36) - 2, bg=self.base, fg=foreground,
                         activebackground=pressed, activeforeground=foreground,
                         disabledforeground=FAINT, relief='flat', overrelief='flat', bd=0,
                         padx=14 if not nav else 12, pady=0, cursor='hand2', takefocus=True,
                         highlightthickness=1, highlightbackground=LINE, highlightcolor=WHITE,
                         anchor='w' if nav else 'center', font=(FONT, 10, 'bold' if primary else 'normal'))
        self.bind('<Enter>', lambda _: self.set_hover(True))
        self.bind('<Leave>', lambda _: self.set_hover(False))
        self.bind('<Destroy>', lambda _: self.cancel_hover())
        self.bind('<Unmap>', lambda _: self.finish_hover())

    def cancel_hover(self):
        if self.timer:
            self.after_cancel(self.timer)
            self.timer = None

    def target(self):
        if self.cget('state') == 'disabled':
            return '#151515'
        if self.hovered:
            return '#353535' if self.selected else self.hover
        return '#242424' if self.selected else self.base

    def finish_hover(self):
        self.cancel_hover()
        self.hovered = False
        self.configure(bg=self.target())

    def set_hover(self, hovered):
        self.hovered = hovered
        if self.cget('state') == 'disabled':
            return
        self.cancel_hover()
        origin, target = self.cget('bg'), self.target()
        start = time.monotonic()

        def paint():
            fraction = min(1, (time.monotonic() - start) / .15)
            self.configure(bg=blend(origin, target, fraction))
            self.timer = self.after(FRAME_MS, paint) if fraction < 1 and self.winfo_viewable() else None

        if origin != target:
            paint()

    def set_enabled(self, enabled):
        state = 'normal' if enabled else 'disabled'
        if (self.cget('state') == 'disabled') == enabled:
            self.cancel_hover()
            self.configure(state=state, takefocus=enabled, cursor='hand2' if enabled else 'arrow')
            self.configure(bg=self.target())

    def set_selected(self, selected):
        self.selected = selected
        self.cancel_hover()
        self.configure(bg=self.target())


def add_scrollbar(widget, parent):
    bar = ttk.Scrollbar(parent, orient='vertical', style='Room.Vertical.TScrollbar', command=widget.yview)

    def sync(first, last):
        bar.set(first, last)
        bar.state(['disabled' if float(first) == 0 and float(last) == 1 else '!disabled'])

    widget.configure(yscrollcommand=sync)
    bar.pack(side='right', fill='y')


class PipelineView(tk.Frame):
    def __init__(self, parent, source, select):
        super().__init__(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        self.source, self.select = source, select
        self.data = {'stages': {}, 'events': [], 'metrics': {}, 'traces': {}}
        self.service_active = False
        self.revision = None
        self.animated = False
        self.runs = []
        self.ceiling = 100
        self.oldest = None
        self.bar_motion = {}
        self.pointer = None
        self.hovered = self.pressed = None
        self.hover_changed = 0
        self.hit_areas = []
        self.markers = {}
        self.marker_scale = None
        self.timer = None

        header = self.header = tk.Frame(self, bg=PANEL)
        header.pack(fill='x', padx=PAD, pady=(PAD, 0))
        self.title = label(header, source.title(), 17, bold=True)
        self.title.pack(side='left')
        self.badge = label(header, 'OFFLINE', 9, MUTED, True)
        self.badge.pack(side='right')

        metrics = self.metrics_row = tk.Frame(self, bg=PANEL)
        metrics.pack(fill='x', padx=PAD, pady=SPACE)
        self.values = []
        captions = ('Output chars', 'Generation', 'First output') if source == 'quinn' else ('OCR time', 'Cooldown', 'Collected')
        for index, caption in enumerate(captions):
            metrics.columnconfigure(index, weight=1, uniform='metric')
            cell = tk.Frame(metrics, bg=PANEL)
            cell.grid(row=0, column=index, sticky='ew')
            value = label(cell, '—', 17)
            value.pack(anchor='w')
            label(cell, caption, 9, MUTED).pack(anchor='w')
            self.values.append(value)

        self.note = label(self, '', 9, MUTED, anchor='w', width=1)
        self.note.pack(side='bottom', fill='x', padx=PAD, pady=(0, SPACE))
        self.canvas = tk.Canvas(self, height=160, bg=PANEL, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=PAD, pady=(0, SPACE))
        self.canvas.bind('<Configure>', lambda _: self.redraw())
        self.canvas.bind('<Map>', lambda _: self.redraw())
        self.canvas.bind('<Unmap>', self.hide)
        self.canvas.bind('<Motion>', self.point)
        self.canvas.bind('<Leave>', self.point)
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        self.canvas.bind('<Destroy>', lambda _: self.cancel_animation())

    def set_compact(self, compact):
        self.header.pack_configure(pady=(SPACE if compact else PAD, 0))
        self.metrics_row.pack_configure(pady=4 if compact else SPACE)
        self.title.configure(font=(FONT, 15 if compact else 17, 'bold'))
        for value in self.values:
            value.configure(font=(FONT, 15 if compact else 17))
        if compact:
            self.note.pack_forget()
        else:
            self.note.pack(side='bottom', before=self.canvas, fill='x', padx=PAD, pady=(0, SPACE))

    def cancel_animation(self):
        if self.timer:
            self.after_cancel(self.timer)
            self.timer = None

    def hide(self, _):
        self.pointer = self.hovered = self.pressed = None
        self.cancel_animation()

    def point(self, event):
        self.pointer = None if event.type == tk.EventType.Leave else (event.x, event.y)
        target = self.hit()
        if target != self.hovered:
            self.hover_changed = time.monotonic()
            self.hovered = target
            self.canvas.configure(cursor='hand2' if target else 'arrow')
            self.redraw()

    def press(self, event):
        self.point(event)
        self.pressed = self.hit()
        self.redraw()

    def release(self, event):
        self.point(event)
        target = self.hit()
        pressed, self.pressed = self.pressed, None
        if target and target == pressed:
            for key, _, detail in self.hit_areas:
                if key == target:
                    self.select(detail)
                    break
        self.redraw()

    def hit(self):
        if self.pointer:
            x, y = self.pointer
            for key, (left, top, right, bottom), _ in self.hit_areas:
                if left <= x < right and top <= y < bottom:
                    return key
        return None

    def update_data(self, data, service_active, service_state=None):
        changed = data['revision'] != self.revision
        service_active = service_active and not data.get('stopped')
        active_changed = service_active != self.service_active
        if changed:
            self.revision = data['revision']
            self.data = data
            self.runs = recent_runs(data, self.source)
            self.bar_motion = {trace: self.bar_motion[trace] for trace, _, _ in self.runs if trace in self.bar_motion}
            latest = next((event for event in reversed(data['events']) if event['stage'] not in ('cooldown', 'backend')), None)
            self.note.configure(text=latest['detail'].split('\n')[0][:160] if latest else '')
        self.service_active = service_active
        stages = data['stages']
        self.animated = service_active and any(event['status'] in LIVE for event in stages.values())
        state, color = ('LISTENING', GREEN) if service_active else ('OFFLINE', FAINT)
        if self.animated:
            state, color = 'PROCESSING', WHITE
        elif any(event['status'] == 'error' for event in stages.values()):
            state, color = 'ATTENTION', AMBER
        if service_state in ('starting', 'stopping', 'reconnecting', 'failed'):
            state, color = service_state.upper(), RED if service_state == 'failed' else AMBER
        self.badge.configure(text=state, fg=color)
        self.update_metrics(time.time())
        if changed or active_changed:
            self.redraw()

    def update_metrics(self, now):
        metrics = self.data['metrics']
        stage = self.data['stages'].get(HISTORY_STAGE[self.source])
        duration = elapsed(stage_duration(stage, self.service_active, now))
        if self.source == 'quinn':
            values = (number(metrics.get('output_chars')), duration, elapsed(metrics.get('first_output_ms')))
        else:
            values = (duration, cooldown(metrics.get('grab_seconds')), number(metrics.get('collected')))
        for widget, value in zip(self.values, values):
            if widget.cget('text') != value:
                widget.configure(text=value)

    def redraw(self):
        if self.timer is None and self.winfo_viewable():
            self.timer = self.after(FRAME_MS, self.animate)

    def animate(self):
        self.timer = None
        if self.winfo_viewable():
            self.draw()

    def marker(self, size, scale, state):
        if scale != self.marker_scale:
            self.markers.clear()
        self.marker_scale = scale
        key = size, scale, state
        if key not in self.markers:
            _, color, _ = STATES[state]
            image = Image.new('RGBA', (size * 4, size * 4))
            brush = ImageDraw.Draw(image)
            brush.ellipse((4, 4, size * 4 - 5, size * 4 - 5),
                          fill=color if state == 'active' else INSET,
                          outline=LINE if state == 'waiting' else color, width=5)
            self.markers[key] = ImageTk.PhotoImage(image.resize((size, size), Image.Resampling.LANCZOS), master=self)
        return self.markers[key]

    def draw(self):
        canvas = self.canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 10 or height < 10:
            return
        now, tick = time.time(), time.monotonic()
        canvas.delete('all')
        self.hit_areas.clear()
        scale = display_scale(self)
        rail = min(round(235 * scale), max(round(185 * scale), width * .39), width * .57)
        left = rail + GAP
        self.draw_stages(rail, height, scale, now, tick)
        chart_active = self.draw_history(left, width, height, scale, now, tick)
        if self.animated:
            self.update_metrics(now)
        hovered = self.hit()
        if hovered != self.hovered:
            self.hovered, self.hover_changed = hovered, tick
            canvas.configure(cursor='hand2' if hovered else 'arrow')
        if chart_active or self.animated or (self.hovered and tick - self.hover_changed < .15):
            self.redraw()

    def draw_stages(self, rail, height, scale, now, tick):
        canvas = self.canvas
        stages = STAGES[self.source]
        step = min(36 * scale, (height - SPACE) / len(stages))
        text_size = -max(9, min(round(12 * scale), int(step * .7), int(rail / 15)))
        caption_size = -max(8, min(round(11 * scale), int(step * .65), int(rail / 18)))
        marker_size = max(10, min(round(17 * scale), round(step) - 4))
        center = marker_size / 2 + 3
        first = SPACE / 2 + step / 2
        canvas.create_line(center, first, center, first + (len(stages) - 1) * step, fill=LINE)
        for index, (stage, title) in enumerate(stages):
            y = first + index * step
            event = self.data['stages'].get(stage)
            state = stage_state(event, self.service_active)
            caption, color, symbol = STATES[state]
            key = 'stage', stage
            if event:
                self.hit_areas.append((key, (0, y - step / 2, rail, y + step / 2), event))
            if key == self.hovered:
                highlight = self.highlight_color(key, tick)
                canvas.create_rectangle(0, y - step / 2 + 1, rail, y + step / 2 - 1, fill=highlight, outline='')
                canvas.create_line(center, y - step / 2 + 1, center, y + step / 2 - 1, fill=LINE)
            if state == 'active':
                radius = marker_size / 2 + 2
                pulse = .25 + .22 * (1 + math.sin(tick * 4))
                canvas.create_oval(center - radius, y - radius, center + radius, y + radius,
                                   outline=blend(PANEL, WHITE, pulse))
            canvas.create_image(center, y, image=self.marker(marker_size, scale, state))
            if symbol:
                canvas.create_text(center, y, text=symbol, fill=color, font=(FONT, caption_size, 'bold'))
            canvas.create_text(center * 2 + 6, y, anchor='w', text=title, fill=MUTED if not event else TEXT, font=(FONT, text_size))
            duration = stage_duration(event, self.service_active, now) if state == 'complete' else None
            if caption:
                canvas.create_text(rail - 5, y, anchor='e', text=elapsed(duration) if duration is not None else caption,
                                   fill=color, font=(FONT, caption_size))

    def highlight_color(self, key, tick):
        if key == self.pressed:
            return '#333333'
        return blend(PANEL, '#242424', min(1, max(0, tick - self.hover_changed) / .15))

    def draw_history(self, left, width, height, scale, now, tick):
        canvas = self.canvas
        title = 'Generation time' if self.source == 'quinn' else 'OCR time'
        title_size = -min(round(12 * scale), max(10, int((width - left - PAD) / 8)))
        canvas.create_text(left + SPACE, 6, text=title, anchor='nw', fill=TEXT, font=(FONT, title_size, 'bold'))
        if not self.runs:
            canvas.create_text(left + SPACE, min(52, height * .4), text='No runs yet', anchor='nw', fill=MUTED, font=(FONT, 9))
            return False

        oldest = self.runs[0][0]
        durations = [stage_duration(event, self.service_active, now) for _, event, _ in self.runs]
        upper = chart_ceiling(max((duration for duration in durations if duration is not None), default=0))
        self.ceiling = upper if oldest != self.oldest else max(upper, self.ceiling)
        self.oldest = oldest
        plot_left, right = left + SPACE, width - SPACE
        top, bottom = 18 - title_size + round(13 * scale), height - round(22 * scale)
        if right - plot_left < 40 or bottom <= top:
            return False
        canvas.create_text(right, top - 2, text=elapsed(self.ceiling), anchor='se', fill=MUTED, font=(FONT, 8))
        for y in (top, (top + bottom) / 2, bottom):
            canvas.create_line(plot_left, y, right, y, fill=LINE)
        canvas.create_text(plot_left, bottom + 3, text='0 ms', anchor='nw', fill=FAINT, font=(FONT, 8))
        canvas.create_text(right, bottom + 3, text='Latest', anchor='ne', fill=FAINT, font=(FONT, 8))

        slot = (right - plot_left) / RUN_LIMIT
        active = False
        hover_details = None
        for index, ((trace, event, run), duration) in enumerate(zip(self.runs, durations), RUN_LIMIT - len(self.runs)):
            key = 'run', trace
            state = stage_state(event, self.service_active)
            left_edge = plot_left + index * slot
            active |= state == 'active'
            if duration is None:
                bar_top = bottom - 2
            else:
                fraction = duration / self.ceiling
                drawn, moving = self.bar_fraction(trace, fraction, state == 'active', tick)
                active |= moving
                bar_top = bottom - max(2, (bottom - top) * drawn)
            color = {'active': WHITE, 'complete': '#9fa7a1', 'error': RED, 'stopped': AMBER, 'skipped': FAINT}[state]
            if key == self.hovered:
                canvas.create_rectangle(left_edge, top, left_edge + slot, bottom, fill=self.highlight_color(key, tick), outline='')
                hover_details = event, run, duration
            inset = max(1, slot * .17)
            x0, x1 = left_edge + inset, left_edge + slot - inset
            canvas.create_rectangle(x0, bar_top, x1, bottom, fill=color if duration is not None else '',
                                    outline='' if duration is not None else color)
            if state in ('error', 'stopped') and bar_top > top + 9:
                canvas.create_text((x0 + x1) / 2, bar_top - 1, text=STATES[state][2], fill=color, anchor='s', font=(FONT, 8))
            self.hit_areas.append((key, (left_edge, top, left_edge + slot, bottom + 1),
                                   (event, run, duration, state)))
        if hover_details:
            self.draw_run_hover(left, right, height, *hover_details)
        return active

    def bar_fraction(self, trace, target, live, tick):
        previous, end, start = self.bar_motion.get(trace, (0, target, tick))
        fraction = min(1, (tick - start) / .2)
        visible = previous + (end - previous) * (1 - (1 - fraction) ** 3)
        if live:
            self.bar_motion[trace] = (target, target, tick)
            return target, False
        if target != end:
            self.bar_motion[trace] = (visible, target, tick)
            return visible, True
        self.bar_motion[trace] = (previous, end, start)
        return visible, fraction < 1 and previous != end

    def draw_run_hover(self, left, right, height, event, run, duration):
        state = stage_state(event, self.service_active)
        timestamp = time.strftime('%H:%M:%S', time.localtime(event.get('started', event['time'])))
        lines = [f'{timestamp}  ·  {STATES[state][0]}', elapsed(duration) if duration is not None else 'Time unavailable']
        if self.source == 'quinn':
            metrics = run['metrics']
            if measurement(metrics.get('output_chars')):
                lines.append(f'{number(metrics["output_chars"])} chars')
            if measurement(metrics.get('first_output_ms')):
                lines.append(f'First output  {elapsed(metrics["first_output_ms"])}')
        else:
            passes = [sample['metrics']['pass_ms'] for sample in run['samples'] if measurement(sample['metrics'].get('pass_ms'))]
            if passes:
                lines.extend((f'{len(passes)} passes', f'Slowest  {elapsed(max(passes))}'))
        canvas = self.canvas
        content = canvas.create_text(left + SPACE, 28, text='\n'.join(lines), fill=TEXT, anchor='nw',
                                     width=right - left - PAD, font=(FONT, 8))
        bounds = canvas.bbox(content)
        if bounds[3] > height - SPACE:
            canvas.move(content, 0, height - SPACE - bounds[3])
            bounds = canvas.bbox(content)
        background = canvas.create_rectangle(left, bounds[1] - 4, right, bounds[3] + 4, fill=INSET, outline=LINE)
        canvas.tag_lower(background, content)


def display_scale(widget):
    return round(float(widget.tk.call('tk', 'scaling')) * .75, 2)


def size_window(root):
    left, top, right, bottom = 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
    try:
        import win32api
    except ImportError:
        pass
    else:
        monitor = win32api.MonitorFromPoint(win32api.GetCursorPos(), 2)
        left, top, right, bottom = win32api.GetMonitorInfo(monitor)['Work']
    available_width, available_height = right - left - 16, bottom - top - 48
    width, height = min(1280, available_width), min(820, available_height)
    root.minsize(min(1000, width), min(700, height))
    x, y = left + (right - left - width) // 2, top + max(0, (bottom - top - height - 40) // 2)
    root.geometry(f'{width}x{height}+{x}+{y}')


class Dashboard:
    def __init__(self, root, launcher):
        self.root, self.launcher = root, launcher
        self.filter = 'overview'
        self.events = {}
        self.inspected_source = None
        self.snapshots = {}
        self.snapshot_revision = -1
        self.last_revision = None
        self.last_logs = None
        self.last_error = ''
        self.monitor = None
        self.closing = False
        self.compact = False
        self.show_logs = False
        self.wait_timer = None
        self.wait_phase = 0
        root.title('Quinn · Control room')
        root.configure(bg=BG)
        size_window(root)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.option_add('*Font', (FONT, 10))
        self.icon = tk.PhotoImage(master=root, width=32, height=32)
        self.icon.put(BG, to=(0, 0, 32, 32))
        for box in ((7, 6, 24, 9), (5, 9, 8, 23), (23, 9, 26, 23), (8, 23, 24, 26), (18, 20, 22, 24), (22, 24, 28, 28)):
            self.icon.put(WHITE, to=box)
        root.iconphoto(True, self.icon)
        self.style()
        self.layout()
        root.bind('<Configure>', self.resize)
        root.bind('<Map>', self.update_wait)
        root.bind('<Unmap>', self.update_wait)
        root.bind('<Destroy>', self.destroy)
        self.refresh_timer = root.after(80, self.refresh)

    def style(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('Treeview', background=INSET, fieldbackground=INSET, foreground=TEXT,
                        borderwidth=0, rowheight=30, font=(FONT, 9))
        style.layout('Treeview', [('Treeview.treearea', {'sticky': 'nswe'})])
        style.configure('Treeview.Heading', background=PANEL, foreground=MUTED, padding=(7, 5),
                        relief='flat', borderwidth=0, font=(FONT, 9, 'bold'))
        style.map('Treeview', background=[('selected', '#2b2b2b')], foreground=[('selected', WHITE)])
        style.map('Treeview.Heading', background=[('pressed', PANEL), ('active', PANEL)],
                  relief=[('pressed', 'flat'), ('active', 'flat')])
        scrollbar = 'Room.Vertical.TScrollbar'
        style.layout(scrollbar, [('Vertical.Scrollbar.trough', {'sticky': 'ns', 'children': [
            ('Vertical.Scrollbar.thumb', {'expand': True, 'sticky': 'nswe'})]})])
        style.configure(scrollbar, background='#3b3b3b', troughcolor=INSET, width=10,
                        arrowsize=10, borderwidth=0, gripcount=0,
                        lightcolor='#3b3b3b', darkcolor='#3b3b3b', bordercolor=INSET)
        style.map(scrollbar, background=[('disabled', INSET), ('pressed', '#858585'), ('active', '#5b5b5b')],
                  lightcolor=[('disabled', INSET)], darkcolor=[('disabled', INSET)])

    def layout(self):
        sidebar = tk.Frame(self.root, bg=INSET, width=154, highlightbackground=LINE, highlightthickness=1)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        label(sidebar, 'Quinn', 22, bold=True).pack(anchor='w', padx=PAD, pady=(PAD, PAD + SPACE))
        self.nav, self.nav_indicators = {}, {}
        for key, title in [('overview', 'Overview'), ('quinn', 'Quinn'), ('sniper', 'Sniper'), ('activity', 'Activity')]:
            row = tk.Frame(sidebar, bg=INSET)
            row.pack(fill='x', padx=SPACE, pady=(0, SPACE))
            item = Control(row, title, lambda key=key: self.navigate(key), nav=True)
            item.pack(fill='x')
            indicator = tk.Frame(row, bg=WHITE, width=2)
            self.nav[key], self.nav_indicators[key] = item, indicator

        main = self.main = tk.Frame(self.root, bg=BG)
        main.pack(side='left', fill='both', expand=True, padx=PAD + SPACE, pady=PAD)
        main.columnconfigure(0, weight=1)
        top = tk.Frame(main, bg=BG)
        top.grid(row=0, column=0, sticky='ew')
        self.heading = label(top, 'Control room', 25, bold=True)
        self.heading.pack(side='left')
        self.mode = Control(top, 'Live · next launch', lambda: self.action('d'))
        self.mode.pack(side='right')

        actions = self.actions = tk.Frame(main, bg=BG)
        actions.grid(row=1, column=0, sticky='ew', pady=(PAD, PAD))
        self.start_buttons = []
        for title, key in [('Start', '3'), ('Quinn', '2'), ('Sniper', '1'), ('SillyTavern', 't')]:
            widget = Control(actions, title, lambda key=key: self.action(key), primary=key == '3')
            widget.pack(side='left', padx=(0, SPACE))
            self.start_buttons.append(widget)
        self.stop = Control(actions, 'Stop all', lambda: self.action('s'), danger=True)
        self.stop.pack(side='right')

        services = self.service_strip = tk.Frame(main, bg=BG)
        services.grid(row=2, column=0, sticky='ew', pady=(0, PAD))
        self.services, self.service_titles = {}, []
        for index, (key, title) in enumerate([('kobold', 'Model'), ('bridge', 'Bridge'), ('tavern', 'SillyTavern'), ('discord', 'Discord'), ('sniper', 'Sniper')]):
            services.columnconfigure(index, weight=1, uniform='service')
            cell = panel(services, PANEL)
            cell.grid(row=0, column=index, sticky='ew', padx=(0, SPACE if index < 4 else 0))
            name = label(cell, title, 9, MUTED)
            name.pack(anchor='w', padx=SPACE, pady=(SPACE, 4))
            state = label(cell, '○  Stopped', 10, FAINT)
            state.pack(anchor='w', padx=SPACE, pady=(0, SPACE))
            self.services[key] = state
            self.service_titles.append(name)

        self.pipelines = tk.Frame(main, bg=BG)
        self.pipelines.grid(row=3, column=0, sticky='nsew')
        self.pipelines.rowconfigure(0, weight=1)
        self.cards = {}
        self.pipelines.columnconfigure(1, minsize=GAP)
        for index, source in enumerate(('quinn', 'sniper')):
            self.pipelines.columnconfigure(index * 2, weight=1, uniform='pipeline')
            self.cards[source] = PipelineView(self.pipelines, source, self.inspect_pipeline)

        header = self.activity_header = tk.Frame(main, bg=BG)
        header.grid(row=4, column=0, sticky='ew', pady=(PAD, SPACE))
        self.activity_title = label(header, 'Activity', 12, bold=True)
        self.activity_title.grid(row=0, column=0)
        self.event_count = label(header, 'Waiting for events.', 9, MUTED, anchor='w')
        self.event_count.grid(row=0, column=1, sticky='w', padx=GAP)
        title_font = font.Font(font=self.activity_title.cget('font'), root=self.root)
        count_font = font.Font(font=self.event_count.cget('font'), root=self.root)
        offset = (title_font.metrics('ascent') - title_font.metrics('descent') -
                  count_font.metrics('ascent') + count_font.metrics('descent'))
        self.event_count.grid_configure(pady=(max(0, offset), max(0, -offset)))
        header.columnconfigure(1, minsize=count_font.measure('Waiting for events...') + 2 * GAP, weight=1)
        self.log_toggle = Control(header, 'Service log', self.toggle_log)
        self.log_toggle.configure(width=count_font.measure('Processing events') + 40)
        self.log_toggle.grid(row=0, column=2, sticky='e')

        self.activity = tk.Frame(main, bg=BG)
        self.activity.grid(row=5, column=0, sticky='nsew')
        self.activity.columnconfigure(0, weight=3, uniform='activity')
        self.activity.columnconfigure(1, weight=2, uniform='activity')
        self.activity.rowconfigure(0, weight=1)
        table = panel(self.activity)
        table.grid(row=0, column=0, sticky='nsew', padx=(0, GAP))
        self.tree = ttk.Treeview(table, columns=('time', 'source', 'stage', 'detail'), show='headings', height=4, selectmode='browse')
        for key, title, width, stretch in [('time', 'Time', 84, False), ('source', 'Engine', 76, False),
                                           ('stage', 'Stage', 100, False), ('detail', 'Event', 150, True)]:
            self.tree.heading(key, text=title, anchor='w')
            self.tree.column(key, width=width, minwidth=50 if not stretch else 80, stretch=stretch, anchor='w')
        add_scrollbar(self.tree, table)
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.tag_configure('error', foreground=RED)
        self.tree.bind('<<TreeviewSelect>>', self.select_event)
        self.tree.bind('<FocusIn>', lambda _: table.configure(highlightbackground=FAINT))
        self.tree.bind('<FocusOut>', lambda _: table.configure(highlightbackground=LINE))

        inspector = panel(self.activity)
        inspector.grid(row=0, column=1, sticky='nsew')
        label(inspector, 'Event inspector', 9, TEXT, True).pack(anchor='w', padx=GAP, pady=(SPACE, 0))
        self.inspector = self.readout(inspector, FONT)
        self.set_inspector('Select an event, stage, or run.')
        self.log_panel = panel(main)
        self.logs = self.readout(self.log_panel, MONO)

        footer = tk.Frame(main, bg=BG)
        footer.grid(row=6, column=0, sticky='ew', pady=(SPACE, 0))
        self.status = label(footer, 'Ready to start', 9, MUTED)
        self.status.pack(side='left')
        self.navigate('overview')

    def readout(self, parent, family):
        text = tk.Text(parent, width=1, height=4, bg=INSET, fg=MUTED, relief='flat', wrap='word',
                       bd=0, highlightthickness=0, font=(family, 9), padx=GAP, pady=SPACE, state='disabled')
        add_scrollbar(text, parent)
        text.pack(side='left', fill='both', expand=True)
        return text

    def resize(self, event):
        if event.widget is self.root:
            compact = event.height < 780 * display_scale(self.root) or event.width < 1130
            if compact != self.compact:
                self.compact = compact
                self.resize_panels()

    def resize_panels(self):
        spacing = SPACE if self.compact else PAD
        scaled_compact = self.compact and display_scale(self.root) > 1.1
        self.main.pack_configure(pady=SPACE if scaled_compact else PAD)
        self.heading.configure(font=(FONT, 21 if scaled_compact else 25, 'bold'))
        self.actions.grid_configure(pady=spacing)
        self.service_strip.grid_configure(pady=(0, spacing))
        for title in self.service_titles:
            title.pack_configure(pady=(4 if scaled_compact else SPACE, 4))
        for widget in self.services.values():
            widget.pack_configure(pady=(0, 4 if scaled_compact else SPACE))
        for card in self.cards.values():
            card.set_compact(scaled_compact)
        self.activity_header.grid_configure(pady=(spacing, SPACE))
        self.tree.configure(height=7 if self.filter == 'activity' else 2 if scaled_compact else 3 if self.compact else 4)
        self.inspector.configure(height=2 if scaled_compact else 3 if self.compact else 5)
        self.logs.configure(height=3 if scaled_compact else 4 if self.compact else 7)

    def navigate(self, key):
        self.filter = key
        for name, widget in self.nav.items():
            widget.set_selected(name == key)
            indicator = self.nav_indicators[name]
            if name == key:
                indicator.place(x=1, y=6, relheight=1, height=-12)
            else:
                indicator.place_forget()
        self.heading.configure(text='Control room' if key == 'overview' else key.title())
        self.update_activity_title()
        if self.inspected_source and key in self.cards and self.inspected_source != key:
            self.inspected_source = None
            self.set_inspector('Select an event, stage, or run.')
        for index, (source, card) in enumerate(self.cards.items()):
            if key == 'overview':
                card.grid(row=0, column=index * 2, columnspan=1, sticky='nsew')
            elif key == source:
                card.grid(row=0, column=0, columnspan=3, sticky='nsew')
            else:
                card.grid_remove()
        if key == 'activity':
            self.pipelines.grid_remove()
        else:
            self.pipelines.grid()
        self.main.rowconfigure(3, weight=int(key != 'activity'))
        self.main.rowconfigure(5, weight=int(key == 'activity'))
        self.resize_panels()
        self.last_revision = None
        if self.snapshots:
            self.refresh_events(self.snapshots)

    def action(self, key):
        if not self.closing or key == 's':
            self.launcher.handle(key)

    def close(self):
        self.closing = True
        self.launcher.handle('q')
        self.status.configure(text='Finishing active work before closing…')

    def destroy(self, event):
        if event.widget is self.root:
            self.root.after_cancel(self.refresh_timer)
            if self.wait_timer:
                self.root.after_cancel(self.wait_timer)

    def toggle_log(self):
        self.show_logs = not self.show_logs
        if self.show_logs:
            self.activity.grid_remove()
            self.log_panel.grid(row=5, column=0, sticky='nsew')
        else:
            self.log_panel.grid_remove()
            self.activity.grid()
        self.log_toggle.configure(text='Processing events' if self.show_logs else 'Service log')
        self.update_activity_title()
        self.update_wait()

    def update_activity_title(self):
        self.activity_title.configure(text='Service log' if self.show_logs else 'Events' if self.filter == 'activity' else 'Activity')

    def update_wait(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if self.wait_timer:
            self.root.after_cancel(self.wait_timer)
            self.wait_timer = None
        if self.show_logs:
            self.event_count.configure(text='')
        elif self.events:
            count = len(self.events)
            self.event_count.configure(text=f'{count} recent event' + ('s' if count != 1 else ''))
        elif self.root.winfo_viewable():
            self.wait_tick()

    def wait_tick(self):
        self.wait_timer = None
        if self.root.winfo_viewable():
            self.event_count.configure(text='Waiting for events' + '.' * (self.wait_phase + 1))
            self.wait_phase = (self.wait_phase + 1) % 3
            self.wait_timer = self.root.after(450, self.wait_tick)

    def set_inspector(self, text):
        self.inspector.configure(state='normal')
        self.inspector.delete('1.0', 'end')
        self.inspector.insert('1.0', text)
        self.inspector.configure(state='disabled')

    def inspect_pipeline(self, item):
        self.tree.selection_remove(*self.tree.selection())
        self.inspect(item)

    def inspect(self, item):
        if self.show_logs:
            self.toggle_log()
        if isinstance(item, tuple):
            event, run, duration, state = item
            started = time.strftime('%H:%M:%S', time.localtime(event.get('started', event['time'])))
            lines = [f"{event['source'].title()} · {stage_name(event['stage'])}",
                     f'{started} · {STATES[state][0]}', '', event['detail'], '',
                     f'Duration  {elapsed(duration) if duration is not None else "Unavailable"}']
            if event['source'] == 'quinn':
                for key, title, formatter in [('output_chars', 'Output characters', number),
                                               ('first_output_ms', 'First output', elapsed)]:
                    value = run['metrics'].get(key)
                    if measurement(value):
                        lines.append(f'{title}  {formatter(value)}')
            else:
                for sample in run['samples']:
                    metrics = sample['metrics']
                    if measurement(metrics.get('pass_ms')):
                        lines.append(f"Card {metrics.get('card', '—')} · {metrics.get('field', 'pass')}  {elapsed(metrics['pass_ms'])}")
        else:
            event = item
            lines = [event['source'].title() + ' · ' + stage_name(event['stage']) + ' · ' + event['status'].title(), '', event['detail']]
            for key, value in event.get('metrics', {}).items():
                title = key.replace('_', ' ').capitalize()
                value = elapsed(value) if key.endswith('_ms') else str(value)
                lines.append(f'{title}  {value}')
        if event.get('trace'):
            lines.extend(['', 'Trace  ' + event['trace']])
        self.inspected_source = event['source']
        self.set_inspector('\n'.join(lines))

    def select_event(self, _):
        selected = self.tree.selection()
        if selected and selected[0] in self.events:
            self.inspect(self.events[selected[0]])

    def refresh_events(self, snapshots):
        revision = tuple(snapshot['revision'] for snapshot in snapshots.values())
        if revision == self.last_revision:
            return
        self.last_revision = revision
        events = sorted((event for source, snapshot in snapshots.items() for event in snapshot['events']
                         if self.filter not in ('quinn', 'sniper') or self.filter == source),
                        key=lambda event: (event['time'], event['sequence']), reverse=True)
        events = [event for event in events if event['stage'] not in ('cooldown', 'backend')][:240]
        wanted = {f"{event['source']}:{event['sequence']}": event for event in events}
        if wanted.keys() == self.events.keys():
            return
        was_waiting = not self.events
        for identifier in self.events.keys() - wanted.keys():
            self.tree.delete(identifier)
        for index, (identifier, event) in enumerate(wanted.items()):
            if identifier not in self.events:
                values = (time.strftime('%H:%M:%S', time.localtime(event['time'])), event['source'].title(),
                          stage_name(event['stage']), ' '.join(event['detail'].splitlines()))
                self.tree.insert('', index, iid=identifier, values=values, tags=(event['status'],))
            elif self.tree.index(identifier) != index:
                self.tree.move(identifier, '', index)
        self.events = wanted
        if not was_waiting or wanted:
            self.update_wait()

    def refresh_logs(self, logs):
        if logs == self.last_logs:
            return
        previous, self.last_logs = self.last_logs, logs
        following = not previous or self.logs.yview()[1] >= .999
        anchor = self.logs.index('@0,0')
        dropped = 0
        overlap = 0
        if previous and logs:
            overlap = next((count for count in range(min(len(previous), len(logs)), 0, -1)
                            if previous[-count:] == logs[:count]), 0)
            dropped = sum(line.count('\n') + 1 for line in previous[:len(previous) - overlap])
        self.logs.configure(state='normal')
        if overlap:
            if dropped:
                self.logs.delete('1.0', f'{dropped + 1}.0')
            if len(logs) > overlap:
                self.logs.insert('end-1c', '\n' + '\n'.join(logs[overlap:]))
        else:
            self.logs.delete('1.0', 'end')
            self.logs.insert('1.0', '\n'.join(logs) or 'No service output yet')
        self.logs.configure(state='disabled')
        if following:
            self.logs.see('end')
        elif overlap:
            line, column = anchor.split('.')
            self.logs.yview(f'{int(line) - dropped}.{column}' if int(line) > dropped else '1.0')
        else:
            self.logs.yview('1.0')

    def refresh(self):
        launcher = self.launcher
        self.closing |= launcher.quit
        if not self.monitor or not self.monitor.is_alive():
            self.monitor = threading.Thread(target=launcher.monitor, daemon=True)
            self.monitor.start()
        snapshots = launcher.telemetry.snapshot(since=self.snapshot_revision)
        if snapshots is not None:
            self.snapshots = snapshots
            self.snapshot_revision = max(snapshot['revision'] for snapshot in snapshots.values())
            self.refresh_events(snapshots)
        for name, widget in self.services.items():
            component = launcher.components[name]
            alive, state = component.alive, component.state
            if component.proc and not alive and state not in ('stopped', 'stopping'):
                state = 'failed'
            color = {'ready': GREEN if alive else FAINT, 'failed': RED, 'starting': AMBER,
                     'stopping': AMBER, 'reconnecting': AMBER}.get(state, FAINT)
            widget.configure(text=('●  ' if alive else '○  ') + state.title(), fg=color)
        for source, card in self.cards.items():
            component = launcher.components['discord' if source == 'quinn' else 'sniper']
            card.update_data(self.snapshots[source], component.alive, component.state)
        with launcher.log_lock:
            logs = tuple(launcher.logs)
        self.refresh_logs(logs)
        busy = bool(launcher.operation)
        for widget in [*self.start_buttons, self.mode]:
            widget.set_enabled(not busy and not self.closing)
        self.mode.configure(text='Dry run · next launch' if launcher.dry else 'Live · next launch')
        self.stop.configure(text='Force stop' if launcher.operation == 'stopping' else 'Stop all')
        active = sum(component.alive for component in launcher.components.values())
        if launcher.last_error != self.last_error:
            self.last_error = launcher.last_error
            if self.last_error:
                if not self.show_logs:
                    self.toggle_log()
                self.logs.see('end')
        status = ('Finishing active work before closing…' if self.closing else
                  f'{launcher.operation.title()}…' if busy else 'Attention · See service log' if self.last_error
                  else f'{active} services running' if active else 'Ready to start')
        self.status.configure(text=status, fg=AMBER if self.last_error else MUTED)
        if self.closing and not launcher.operation and not (launcher.worker and launcher.worker.is_alive()) and not active:
            self.root.destroy()
            return
        self.refresh_timer = self.root.after(300, self.refresh)


def run(launcher):
    root = tk.Tk()
    Dashboard(root, launcher)
    try:
        root.mainloop()
    finally:
        launcher.console_close()
