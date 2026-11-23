"""A local desktop window with sequential file and folder jobs."""

import os
import queue
import threading
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import COPY, DND_FILES

from . import __version__
from .batch import FileResult, find_conflicts, run_input
from .dialogs import show_conflicts

BG = '#F4F6F5'
INK = '#172E28'
MUTED = '#53665E'
GREEN = '#255C48'
PALE = '#EAF2ED'
LINE = '#CFDCD3'


@dataclass(frozen=True)
class QueuedJob:
    number: int
    source: Path
    output_folder: Path | None


class CleanerApp:
    def __init__(self, root):
        self.root = root
        self.scale = max(1.0, root.winfo_fpixels('1i') / 96)
        self.busy = False
        self.preflighting = False
        self.closing = False
        self.last_result = None
        self.completed_results = []
        self.pending = deque()
        self.queue_paused = False
        self.current_job = None
        self.worker = None
        self.next_job_number = 1
        self.output_folder = None
        self.messages = queue.Queue()
        self.stop_event = threading.Event()
        self.status_text = tk.StringVar(value='파일이나 폴더를 넣으면 자동으로 시작합니다.')
        self.folder_text = tk.StringVar(value='아직 추가한 작업이 없습니다.')
        self.queue_text = tk.StringVar(value='대기 0건')
        self.output_text = tk.StringVar(value='기본: 원본 폴더 안에 새 결과 폴더')
        self.count_text = tk.StringVar(value='PNG · JPG   /   하위 폴더 제외')
        root.title('Exif Cleaner · 프롬프트 제거')
        root.configure(bg=BG)
        width = min(self.px(840), root.winfo_screenwidth() - self.px(40))
        height = min(self.px(820), root.winfo_screenheight() - self.px(60))
        left = max(0, (root.winfo_screenwidth() - width) // 2)
        top = max(0, (root.winfo_screenheight() - height) // 3)
        root.geometry(f'{width}x{height}+{left}+{top}')
        root.minsize(min(self.px(760), width), min(self.px(760), height))
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        self._styles()
        self._build()
        root.update_idletasks()
        self.drop_canvas.configure(height=self.drop_frame.winfo_reqheight() + self.px(20))
        icon = Path(__file__).resolve().parent / 'assets' / 'icon.png'
        self.icon = None
        if icon.exists():
            try:
                self.icon = tk.PhotoImage(master=root, data=icon.read_bytes(), format='png')
                root.iconphoto(True, self.icon)
            except (OSError, tk.TclError):
                # A decorative icon must never prevent image processing.
                self.icon = None
        self._poll_id = root.after(60, self._poll)

    def px(self, value):
        return round(value * self.scale)

    def _styles(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=('맑은 고딕', 10))
        style.configure('Primary.TButton', background=GREEN, foreground='white',
                        padding=(self.px(24), self.px(11)), borderwidth=0, font=('맑은 고딕', 11, 'bold'))
        style.map('Primary.TButton', background=[('disabled', '#CFD8D2'), ('active', '#184533')],
                  foreground=[('disabled', '#64756B')])
        style.configure('Secondary.TButton', background='white', foreground=INK,
                        padding=(self.px(16), self.px(10)), borderwidth=1, bordercolor=LINE)
        style.map('Secondary.TButton', background=[('active', PALE)],
                  foreground=[('disabled', '#8B9790')])
        style.configure('Compact.TButton', background='white', foreground=INK,
                        padding=(self.px(10), self.px(4)), font=('맑은 고딕', 9), borderwidth=1)
        style.map('Compact.TButton', foreground=[('disabled', '#8B9790')])
        style.configure('Clean.Horizontal.TProgressbar', background=GREEN,
                        troughcolor='#E0E8E2', borderwidth=0, lightcolor=GREEN, darkcolor=GREEN)
        style.configure('Treeview', background='white', fieldbackground='white',
                        foreground=INK, rowheight=self.px(31), borderwidth=0, font=('맑은 고딕', 9))
        style.configure('Treeview.Heading', background=BG, foreground=MUTED,
                        padding=(8, 8), font=('맑은 고딕', 9, 'bold'), relief='flat')
        style.map('Treeview', background=[('selected', '#DDECE2')], foreground=[('selected', INK)])

    def _label(self, parent, text=None, **kwargs):
        return tk.Label(parent, text=text, bg=kwargs.pop('bg', BG),
                        fg=kwargs.pop('fg', INK), font=kwargs.pop('font', ('맑은 고딕', 10)), **kwargs)

    def _build(self):
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill='both', expand=True, padx=self.px(32), pady=self.px(24))
        header = tk.Frame(shell, bg=BG)
        header.pack(fill='x')
        self._label(header, 'PC', bg=GREEN, fg='white', font=('Segoe UI', 12, 'bold'),
                    width=3, pady=5).pack(side='left')
        self._label(header, '  Exif Cleaner', font=('Segoe UI', 14, 'bold')).pack(side='left')
        self._label(header, f'LOCAL ONLY  ·  v{__version__}', fg=MUTED,
                    font=('Segoe UI', 9)).pack(side='right')
        self._label(shell, '공유할 이미지의 프롬프트를 정리하세요.',
                    font=('맑은 고딕', 21, 'bold'), anchor='w').pack(fill='x', pady=(23, 5))
        self._label(shell, '원본은 그대로 두고, 생성 정보를 제거한 사본을 만듭니다.',
                    fg=MUTED, anchor='w').pack(fill='x', pady=(0, 18))

        output_row = tk.Frame(shell, bg=BG)
        output_row.pack(fill='x', pady=(0, 7))
        self._label(output_row, '저장 위치', font=('맑은 고딕', 10, 'bold')).pack(side='left')
        self.reset_output_button = ttk.Button(output_row, text='기본 위치', style='Secondary.TButton',
                                              command=self.reset_output_folder)
        self.reset_output_button.pack(side='right', padx=(self.px(8), 0))
        self.output_button = ttk.Button(output_row, text='저장 폴더 선택', style='Secondary.TButton',
                                        command=self.choose_output_folder)
        self.output_button.pack(side='right')
        self.output_entry = ttk.Entry(shell, textvariable=self.output_text, state='readonly',
                                       font=('맑은 고딕', 9))
        self.output_entry.pack(fill='x', ipady=self.px(5), pady=(0, self.px(14)))

        self.drop_canvas = tk.Canvas(shell, height=self.px(204), bg=BG, highlightthickness=0)
        self.drop_canvas.pack(fill='x')
        self.drop_frame = tk.Frame(self.drop_canvas, bg=PALE)
        self.drop_window = self.drop_canvas.create_window(self.px(10), self.px(10), window=self.drop_frame, anchor='nw')
        self.drop_border = self.drop_canvas.create_rectangle(2, 2, 100, 201, outline='#8BA997', dash=(6, 4), width=2)
        self.drop_canvas.bind('<Configure>', self._resize_drop)
        self._label(self.drop_frame, '↓', bg=PALE, fg=GREEN, font=('Segoe UI', 24)).pack(pady=(5, 0))
        self.drop_title = self._label(self.drop_frame, '파일이나 폴더를 끌어다 놓으세요',
                                      bg=PALE, font=('맑은 고딕', 14, 'bold'))
        self.drop_title.pack(pady=(0, 3))
        self._label(self.drop_frame, 'PNG·JPG 여러 파일·폴더 · 처리 중에는 대기열에 추가', bg=PALE, fg=MUTED,
                    font=('맑은 고딕', 9)).pack(pady=(0, 10))
        input_buttons = tk.Frame(self.drop_frame, bg=PALE)
        input_buttons.pack(pady=(0, 13))
        self.choose_button = ttk.Button(input_buttons, text='폴더 추가', style='Primary.TButton',
                                        command=self.choose_folder, takefocus=True)
        self.choose_button.pack(side='left', padx=(0, self.px(8)))
        self.file_button = ttk.Button(input_buttons, text='파일 추가', style='Primary.TButton', command=self.choose_files)
        self.file_button.pack(side='left')
        self._register_drop(self.drop_canvas)

        queue_header = tk.Frame(shell, bg=BG)
        queue_header.pack(fill='x', pady=(self.px(10), self.px(5)))
        self._label(queue_header, '작업 대기열', font=('맑은 고딕', 10, 'bold')).pack(side='left')
        self._label(queue_header, textvariable=self.queue_text, fg=MUTED,
                    font=('맑은 고딕', 9)).pack(side='left', padx=self.px(10))
        self.clear_queue_button = ttk.Button(queue_header, text='대기열 비우기', style='Compact.TButton',
                                             command=self.clear_queue, state='disabled')
        self.clear_queue_button.pack(side='right')
        self.resume_button = ttk.Button(queue_header, text='대기열 계속', style='Compact.TButton',
                                        command=self.resume_queue, state='disabled')
        self.resume_button.pack(side='right', padx=(0, self.px(6)))
        queue_frame = tk.Frame(shell, bg='white', highlightbackground=LINE, highlightthickness=1)
        queue_frame.pack(fill='x')
        self.queue_table = ttk.Treeview(queue_frame, columns=('source', 'output'), show='headings', height=2)
        self.queue_table.heading('source', text='대기 중인 파일·폴더')
        self.queue_table.heading('output', text='저장 위치')
        self.queue_table.column('source', width=self.px(445), minwidth=self.px(150))
        self.queue_table.column('output', width=self.px(280), minwidth=self.px(100))
        queue_scroll = ttk.Scrollbar(queue_frame, orient='vertical', command=self.queue_table.yview)
        self.queue_table.configure(yscrollcommand=queue_scroll.set)
        queue_scroll.pack(side='right', fill='y')
        self.queue_table.pack(side='left', fill='both', expand=True)

        self.folder_entry = ttk.Entry(shell, textvariable=self.folder_text, state='readonly',
                                       font=('맑은 고딕', 9))
        self.folder_entry.pack(fill='x', ipady=self.px(3), pady=(self.px(10), self.px(7)))
        status_line = tk.Frame(shell, bg=BG)
        status_line.pack(fill='x', pady=(2, 7))
        self.status_label = self._label(status_line, textvariable=self.status_text,
                                        anchor='w', font=('맑은 고딕', 10, 'bold'))
        self.status_label.pack(side='left')
        self._label(status_line, textvariable=self.count_text, fg=MUTED,
                    font=('맑은 고딕', 9)).pack(side='right')
        self.progress = ttk.Progressbar(shell, style='Clean.Horizontal.TProgressbar', maximum=1, value=0)
        self.progress.pack(fill='x', ipady=2, pady=(0, 14))

        table_frame = tk.Frame(shell, bg='white', highlightbackground=LINE, highlightthickness=1)
        self.table = ttk.Treeview(table_frame, columns=('name', 'status', 'detail'), show='headings', height=5)
        self.table.heading('name', text='이미지')
        self.table.heading('status', text='결과')
        self.table.heading('detail', text='처리 내용')
        self.table.column('name', width=self.px(230), minwidth=self.px(100))
        self.table.column('status', width=self.px(65), minwidth=self.px(55), stretch=False, anchor='center')
        self.table.column('detail', width=self.px(400), minwidth=self.px(170))
        self.table.tag_configure('error', foreground='#A23629')
        self.table.tag_configure('success', foreground=GREEN)
        self.table.tag_configure('skipped', foreground=MUTED)
        scroll = ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.table.pack(side='left', fill='both', expand=True)
        self.table.bind('<Double-1>', self._show_detail)
        self.table.bind('<Return>', self._show_detail)

        actions = tk.Frame(shell, bg=BG)
        self.cancel_button = ttk.Button(actions, text='현재 작업 중지', style='Secondary.TButton',
                                        command=self.cancel, state='disabled')
        self.cancel_button.pack(side='left')
        self.open_button = ttk.Button(actions, text='결과 폴더 열기', style='Primary.TButton',
                                      command=self.open_output, state='disabled')
        self.open_button.pack(side='right')
        self._label(shell, '원본 보존  ·  하위 폴더 제외  ·  모든 처리는 내 PC에서',
                    fg=MUTED, font=('맑은 고딕', 9), anchor='w').pack(side='bottom', fill='x')
        # Reserve the bottom controls before allocating space to the log.
        actions.pack(side='bottom', fill='x', pady=(14, 12))
        table_frame.pack(fill='both', expand=True)

    def _resize_drop(self, event):
        width = event.width
        height = self.drop_frame.winfo_reqheight() + self.px(20)
        self.drop_canvas.coords(self.drop_border, 2, 2, width - 3, height - 3)
        self.drop_canvas.itemconfigure(self.drop_window, width=max(100, width - self.px(20)),
                                       height=height - self.px(20))

    def _register_drop(self, widget):
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind('<<Drop>>', self.on_drop)
        widget.dnd_bind('<<DropEnter>>', self._drop_enter)
        widget.dnd_bind('<<DropLeave>>', self._drop_leave)
        for child in widget.winfo_children():
            self._register_drop(child)

    def _drop_enter(self, event):
        if self.closing:
            return 'refuse_drop'
        self.drop_canvas.itemconfigure(self.drop_border, outline=GREEN, width=3)
        self.drop_title.configure(text='놓으면 대기열에 추가합니다' if self.busy else '놓으면 프롬프트 정리를 시작합니다')
        return COPY

    def _drop_leave(self, event=None):
        self.drop_canvas.itemconfigure(self.drop_border, outline='#8BA997', width=2)
        self.drop_title.configure(text='처리 중에도 파일·폴더를 추가할 수 있습니다' if self.busy
                                  else '파일이나 폴더를 끌어다 놓으세요')

    def on_drop(self, event):
        self._drop_leave()
        if self.closing:
            return 'refuse_drop'
        try:
            paths = self.root.tk.splitlist(event.data)
        except tk.TclError:
            self._error('입력 경로를 읽지 못했습니다. 파일·폴더 추가 버튼을 사용해 주세요.')
            return 'refuse_drop'
        return COPY if self.enqueue_paths([Path(path) for path in paths]) else 'refuse_drop'

    def choose_folder(self):
        if self.closing or self.preflighting:
            return
        selected = filedialog.askdirectory(parent=self.root, title='프롬프트를 제거할 이미지 폴더 선택', mustexist=True)
        if selected:
            self.enqueue_paths([Path(selected)])

    def choose_files(self):
        if self.closing or self.preflighting:
            return
        selected = filedialog.askopenfilenames(parent=self.root, title='처리할 이미지 파일 선택 (여러 개 가능)',
                                               filetypes=[('PNG·JPG 이미지', '*.png *.jpg *.jpeg')])
        if selected:
            self.enqueue_paths([Path(path) for path in selected])

    def _validate_input(self, source, output_folder):
        if source.is_file():
            if source.is_symlink():
                raise ValueError('바로 가기 대신 원본 이미지 파일을 선택해 주세요.')
            if source.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
                raise ValueError('PNG와 JPG 이미지 파일 또는 폴더를 넣어 주세요.')
            original_folder = source.resolve().parent
        elif source.is_dir():
            original_folder = source.resolve()
        else:
            raise ValueError('입력한 파일이나 폴더를 찾을 수 없습니다.')
        if output_folder is not None:
            if not output_folder.is_dir():
                raise ValueError('지정한 저장 폴더가 없습니다. 저장 위치를 다시 선택해 주세요.')
            if output_folder.resolve() == original_folder:
                raise ValueError('원본 폴더와 다른 저장 폴더를 선택해 주세요.')

    def enqueue_paths(self, paths):
        if self.closing:
            return 0
        output_folder = self.output_folder.resolve() if self.output_folder is not None else None
        added = 0
        errors = []
        seen = set()
        for path in paths:
            source = Path(path)
            try:
                self._validate_input(source, output_folder)
                source = source.resolve()
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
                continue
            if source in seen:
                continue
            seen.add(source)
            job = QueuedJob(self.next_job_number, source, output_folder)
            self.next_job_number += 1
            self.pending.append(job)
            kind = '폴더' if source.is_dir() else '파일'
            self.queue_table.insert('', 'end', iid=str(job.number), values=(
                f'{job.number}. [{kind}] {source}', str(output_folder) if output_folder else '원본 옆 새 결과 폴더'))
            added += 1
        self._refresh_queue()
        if errors:
            self._error(errors[0] + (f' (정상 입력 {added}건은 대기열에 추가됨)' if added else ''))
        self._start_next()
        return added

    def _refresh_queue(self):
        prefix = '일시중지 · ' if self.queue_paused else ''
        self.queue_text.set(f'{prefix}대기 {len(self.pending)}건')
        self.clear_queue_button.state(['!disabled'] if self.pending else ['disabled'])
        resumable = self.pending and self.queue_paused and not self.busy and not self.preflighting
        self.resume_button.state(['!disabled'] if resumable else ['disabled'])

    def resume_queue(self):
        if self.busy or self.preflighting or self.closing:
            return
        self.queue_paused = False
        self._refresh_queue()
        self._start_next()

    def clear_queue(self):
        self.pending.clear()
        # Remove pending UI rows only; no file is removed or changed.
        for item in self.queue_table.get_children():
            self.queue_table.delete(item)
        self.queue_paused = False
        self._refresh_queue()
        if not self.busy:
            self.status_text.set('대기열을 비웠습니다. 파일이나 폴더를 추가할 수 있습니다.')
            self.status_label.configure(fg=INK)

    def choose_output_folder(self):
        if self.busy or self.preflighting:
            return
        options = dict(parent=self.root, title='결과 이미지를 저장할 폴더 선택', mustexist=True)
        if self.output_folder and self.output_folder.is_dir():
            options['initialdir'] = str(self.output_folder)
        selected = filedialog.askdirectory(**options)
        if selected:
            folder = Path(selected).resolve()
            if not folder.is_dir():
                self._error('저장할 폴더를 선택해 주세요.')
                return
            self.output_folder = folder
            self.output_text.set(str(folder))
            self.status_text.set('지정한 폴더에 바로 저장합니다. 처리할 파일이나 폴더를 넣어 주세요.')
            self.status_label.configure(fg=INK)

    def reset_output_folder(self):
        if not self.busy and not self.preflighting:
            self.output_folder = None
            self.output_text.set('기본: 원본 폴더 안에 새 결과 폴더')
            self.status_text.set('파일이나 폴더를 넣으면 자동으로 시작합니다.')
            self.status_label.configure(fg=INK)

    def _start_next(self):
        if self.busy or self.preflighting or self.queue_paused or self.closing:
            return
        while self.pending:
            job = self.pending.popleft()
            self.queue_table.delete(str(job.number))
            self._refresh_queue()
            try:
                self._validate_input(job.source, job.output_folder)
                conflicts = find_conflicts(job.source, job.output_folder) if job.output_folder else ()
            except (OSError, ValueError) as exc:
                self._append_result(FileResult(job.source.name, False, str(exc)))
                self._error('작업을 시작할 수 없습니다: ' + str(exc))
                continue
            resolutions = {}
            if conflicts:
                self.preflighting = True
                self._refresh_queue()
                try:
                    resolutions = show_conflicts(self.root, conflicts)
                finally:
                    self.preflighting = False
                if resolutions is None:
                    self.queue_paused = bool(self.pending)
                    self.status_text.set('중복 작업을 취소했습니다. 남은 대기열은 일시중지됩니다.'
                                         if self.pending else '중복 파일 확인을 취소했습니다. 저장 위치를 바꿀 수 있습니다.')
                    self._refresh_queue()
                    return
            self._begin_job(job, resolutions)
            return

    def _begin_job(self, job, resolutions):
        self.busy = True
        self.current_job = job
        self.last_result = None
        self.stop_event.clear()
        self.folder_text.set(f'작업 {job.number} 진행 중: {job.source}')
        self.status_text.set('이미지를 확인하고 있습니다…')
        self.status_label.configure(fg=INK)
        self.count_text.set('준비 중')
        self.output_button.state(['disabled'])
        self.reset_output_button.state(['disabled'])
        self.open_button.state(['disabled'])
        self.cancel_button.state(['!disabled'])
        self._drop_leave()
        self._refresh_queue()
        self.progress.configure(value=0, maximum=1)
        self.worker = threading.Thread(target=self._worker, args=(job.source, job.output_folder, resolutions),
                                       name=f'ExifCleaner-job-{job.number}', daemon=False)
        self.worker.start()

    def _append_result(self, row):
        status = '중복 제외' if row.skipped else '성공' if row.success else '실패'
        tag = 'skipped' if row.skipped else 'success' if row.success else 'error'
        name = row.name
        detail = row.detail
        if row.destination:
            if row.destination.name != 'ex_' + row.name:
                name += ' → ' + row.destination.name
            detail += ' · 저장: ' + str(row.destination)
        item = self.table.insert('', 'end', values=(name, status, detail), tags=(tag,))
        self.table.see(item)

    def _worker(self, source, output_folder, resolutions):
        try:
            result = run_input(source, on_progress=lambda done, total, row: self.messages.put(('progress', done, total, row)),
                                cancel_event=self.stop_event, output_folder=output_folder,
                                resolutions=resolutions)
            self.messages.put(('done', result))
        except Exception as exc:
            self.messages.put(('error', str(exc)))

    def _poll(self):
        try:
            while True:
                message = self.messages.get_nowait()
                if message[0] == 'progress':
                    _, done, total, row = message
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self.count_text.set(f'{done} / {total}')
                    self.status_text.set('이미지 사본을 처리하고 검증하고 있습니다…')
                    if row:
                        self._append_result(row)
                elif message[0] == 'done':
                    self.last_result = message[1]
                    self._finish()
                    self._start_next()
                elif message[0] == 'error':
                    if self.current_job:
                        self._append_result(FileResult(self.current_job.source.name, False, message[1]))
                    self._unlock()
                    self._error('처리할 수 없습니다: ' + message[1])
                    self._start_next()
        except queue.Empty:
            pass
        if self.closing and not self.busy:
            self.root.destroy()
            return
        self._poll_id = self.root.after(60, self._poll)

    def _unlock(self):
        self.busy = False
        self.current_job = None
        self.output_button.state(['!disabled'])
        self.reset_output_button.state(['!disabled'])
        self.cancel_button.state(['disabled'])
        self._drop_leave()
        self._refresh_queue()

    def _finish(self):
        result = self.last_result
        self.completed_results.append(result)
        if result.safety_stopped:
            self.queue_paused = True
        self._unlock()
        if not result.total:
            self.status_text.set('입력에 처리할 PNG·JPG 이미지가 없습니다.')
            self.count_text.set('0개')
            return
        label = '안전 중지' if result.safety_stopped else '중지됨' if result.cancelled else '완료'
        self.status_text.set(f'{label} · 성공 {result.success}개 / 중복 제외 {result.skipped}개 / 실패 {result.failed}개')
        if result.failed:
            self.status_label.configure(fg='#A23629')
        self.count_text.set(f'{len(result.files)} / {result.total}')
        if result.output_folder:
            self.open_button.state(['!disabled'])
            self.folder_text.set('결과: ' + str(result.output_folder))
        if result.report_error:
            messagebox.showwarning('결과 보고서 저장 실패', '이미지 처리 결과는 표를 확인해 주세요.\n' + result.report_error,
                                   parent=self.root)

    def _error(self, text):
        self.status_text.set(text)
        self.status_label.configure(fg='#A23629')

    def cancel(self):
        if self.busy:
            self.queue_paused = True
            self.stop_event.set()
            self.status_text.set('현재 파일을 마친 뒤 중지합니다. 대기열은 유지됩니다.')
            self.cancel_button.state(['disabled'])
            self._refresh_queue()

    def open_output(self):
        if self.last_result and self.last_result.output_folder:
            try:
                os.startfile(self.last_result.output_folder)
            except OSError as exc:
                messagebox.showerror('폴더 열기 실패', str(exc), parent=self.root)

    def _show_detail(self, event=None):
        selected = self.table.selection()
        if selected:
            name, status, detail = self.table.item(selected[0], 'values')
            messagebox.showinfo(name, f'{status}\n\n{detail}', parent=self.root)

    def on_close(self):
        if self.preflighting:
            return
        self.closing = True
        self.pending.clear()
        if self.busy:
            self.cancel()
            self.status_text.set('현재 파일을 안전하게 마친 뒤 닫습니다…')
        else:
            self.root.after_cancel(self._poll_id)
            self.root.destroy()
