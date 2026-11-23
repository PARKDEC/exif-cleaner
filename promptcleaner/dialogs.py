"""Review existing output files before starting any writes."""

import tkinter as tk
import warnings
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageOps, ImageTk

from .batch import FileConflict
from .cleaner import MAX_FILE_BYTES, MAX_PIXELS
from .resolution import ConflictChoice, available_name, validate_name


class ConflictDialog:
    def __init__(self, parent, conflicts: tuple[FileConflict, ...]):
        self.conflicts = conflicts
        self.choices = {}
        self.decisions = None
        self.photos = []
        self.window = tk.Toplevel(parent)
        self.window.title('겹치는 결과 파일 확인')
        self.window.configure(bg='#F4F6F5')
        self.window.transient(parent)
        scale = max(1.0, parent.winfo_fpixels('1i') / 96)
        self.px = lambda value: round(value * scale)
        width = min(self.px(920), parent.winfo_screenwidth() - self.px(40))
        height = min(self.px(650), parent.winfo_screenheight() - self.px(60))
        left = max(0, min(parent.winfo_rootx() + (parent.winfo_width() - width) // 2,
                          parent.winfo_screenwidth() - width))
        top = max(0, min(parent.winfo_rooty() + (parent.winfo_height() - height) // 2,
                         parent.winfo_screenheight() - height))
        self.window.geometry(f'{width}x{height}+{left}+{top}')
        self.window.minsize(min(width, self.px(820)), min(height, self.px(600)))
        self.window.protocol('WM_DELETE_WINDOW', self.cancel)
        self.window.bind('<Escape>', lambda event: self.cancel())

        shell = tk.Frame(self.window, bg='#F4F6F5')
        shell.pack(fill='both', expand=True, padx=self.px(24), pady=self.px(22))
        tk.Label(shell, text=f'같은 이름의 결과 파일이 {len(conflicts)}개 있습니다.',
                 bg='#F4F6F5', fg='#172E28', font=('맑은 고딕', 17, 'bold'), anchor='w').pack(fill='x')
        tk.Label(shell, text='목록에서 파일을 선택하면 기존 결과와 이번 원본을 비교할 수 있습니다.\n'
                            '파일마다 처리 방법을 정한 뒤 선택 적용을 누르세요. 취소하면 이 작업을 시작하지 않습니다.',
                 bg='#F4F6F5', fg='#53665E', font=('맑은 고딕', 10), justify='left', anchor='w').pack(
                     fill='x', pady=(self.px(8), self.px(18)))

        body = tk.Frame(shell, bg='#F4F6F5')
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1, minsize=self.px(220))
        body.columnconfigure(1, weight=2, minsize=self.px(410))
        body.rowconfigure(0, weight=1)
        list_frame = tk.Frame(body, bg='white', highlightbackground='#CFDCD3', highlightthickness=1)
        list_frame.grid(row=0, column=0, sticky='nsew', padx=(0, self.px(15)))
        self.table = ttk.Treeview(list_frame, columns=('name', 'action'), show='headings', selectmode='browse')
        self.table.heading('name', text='겹치는 결과 파일')
        self.table.column('name', width=self.px(180), minwidth=self.px(100))
        self.table.heading('action', text='선택')
        self.table.column('action', width=self.px(120), minwidth=self.px(90))
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.table.pack(side='left', fill='both', expand=True)
        for index, conflict in enumerate(conflicts):
            self.table.insert('', 'end', iid=str(index), values=(conflict.destination.name, '미선택'))
        self.table.bind('<<TreeviewSelect>>', self._select)

        preview = tk.Frame(body, bg='#F4F6F5')
        preview.grid(row=0, column=1, sticky='nsew')
        preview.columnconfigure((0, 1), weight=1)
        self.preview_labels = []
        self.name_vars = []
        for column, title in enumerate(('기존 결과 이미지', '이번에 처리할 원본')):
            card = tk.Frame(preview, bg='white', highlightbackground='#CFDCD3', highlightthickness=1)
            card.grid(row=0, column=column, sticky='new', padx=(0, self.px(8) if column == 0 else 0))
            tk.Label(card, text=title, bg='white', fg='#172E28', font=('맑은 고딕', 10, 'bold')).pack(
                pady=(self.px(12), self.px(8)))
            image_frame = tk.Frame(card, bg='#EAF2ED', width=self.px(190), height=self.px(175))
            image_frame.pack(fill='x', padx=self.px(8))
            image_frame.pack_propagate(False)
            label = tk.Label(image_frame, bg='#EAF2ED', fg='#53665E', font=('맑은 고딕', 9))
            label.pack(fill='both', expand=True)
            self.preview_labels.append(label)
            name = tk.StringVar()
            self.name_vars.append(name)
            ttk.Entry(card, textvariable=name, state='readonly', width=15, font=('맑은 고딕', 9)).pack(
                fill='x', padx=self.px(8), pady=self.px(10))
        tk.Label(preview, text='선택한 기존 파일의 전체 경로', bg='#F4F6F5', fg='#53665E',
                 font=('맑은 고딕', 9)).grid(row=1, column=0, columnspan=2, pady=(self.px(10), 0), sticky='w')
        self.target_text = tk.StringVar()
        ttk.Entry(preview, textvariable=self.target_text, state='readonly', font=('맑은 고딕', 9)).grid(
            row=2, column=0, columnspan=2, sticky='ew', pady=self.px(5))
        self.planned_text = tk.StringVar()
        ttk.Entry(preview, textvariable=self.planned_text, state='readonly', font=('맑은 고딕', 9)).grid(
            row=3, column=0, columnspan=2, sticky='ew', pady=self.px(5))
        tk.Label(preview, text='덮어쓰기 시 기존 결과는 휴지통으로 이동하여 복구할 수 있습니다.',
                 bg='#F4F6F5', fg='#53665E', font=('맑은 고딕', 9), wraplength=self.px(400),
                 justify='left').grid(row=4, column=0, columnspan=2, sticky='w')

        footer = tk.Frame(shell, bg='#F4F6F5')
        footer.pack(side='bottom', before=body, fill='x', pady=(self.px(16), 0))
        action_row = tk.Frame(footer, bg='#F4F6F5')
        action_row.pack(fill='x')
        self.overwrite_button = ttk.Button(action_row, text='덮어쓰기', command=lambda: self.set_choice('overwrite'),
                                           style='Secondary.TButton')
        self.overwrite_button.pack(side='left', padx=(0, self.px(6)))
        ttk.Button(action_row, text='다른 이름으로 저장', command=lambda: self.set_choice('rename'),
                   style='Secondary.TButton').pack(side='left', padx=(0, self.px(6)))
        ttk.Button(action_row, text='건너뛰기', command=lambda: self.set_choice('skip'),
                   style='Secondary.TButton').pack(side='left')
        self.cancel_button = ttk.Button(action_row, text='취소', command=self.cancel, style='Secondary.TButton')
        self.cancel_button.pack(side='right')
        apply_row = tk.Frame(footer, bg='#F4F6F5')
        apply_row.pack(fill='x', pady=(self.px(10), 0))
        self.selection_text = tk.StringVar(value=f'0 / {len(conflicts)}개 선택 완료')
        tk.Label(apply_row, textvariable=self.selection_text, bg='#F4F6F5', fg='#53665E',
                 font=('맑은 고딕', 9)).pack(side='left')
        self.apply_button = ttk.Button(apply_row, text='선택 적용', command=self.proceed,
                                       style='Primary.TButton', state='disabled')
        self.apply_button.pack(side='right')
        if conflicts:
            self.table.selection_set('0')
            self.table.focus('0')
            self._select()

    def _thumbnail(self, path: Path):
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                return None
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.width * image.height > MAX_PIXELS:
                        return None
                    image.draft('RGB', (self.px(180), self.px(150)))
                    upright = ImageOps.exif_transpose(image)
                    thumbnail = ImageOps.contain(upright, (self.px(180), self.px(150)), Image.Resampling.LANCZOS)
                    return ImageTk.PhotoImage(thumbnail, master=self.window)
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            return None

    def _select(self, event=None):
        selected = self.table.selection()
        if not selected:
            return
        conflict = self.conflicts[int(selected[0])]
        self.target_text.set(str(conflict.destination.resolve()))
        self._show_planned_path(conflict)
        self.overwrite_button.state(['!disabled'] if conflict.digest else ['disabled'])
        self.photos = []
        for index, path in enumerate((conflict.destination, conflict.source)):
            self.name_vars[index].set(path.name)
            photo = self._thumbnail(path)
            if photo:
                self.photos.append(photo)
                self.preview_labels[index].configure(image=photo, text='')
            else:
                self.preview_labels[index].configure(image='', text='미리보기를 표시할 수 없습니다.')

    def set_choice(self, action):
        selected = self.table.selection()
        if not selected:
            return
        item = selected[0]
        conflict = self.conflicts[int(item)]
        if action == 'overwrite':
            if not conflict.digest:
                messagebox.showerror('덮어쓰기 불가', '일반 결과 파일만 덮어쓸 수 있습니다.', parent=self.window)
                return
            choice = ConflictChoice('overwrite', digest=conflict.digest)
            label = '덮어쓰기'
        elif action == 'rename':
            reserved = {c.name.casefold() for c in self.choices.values() if c.name}
            suggested = available_name(conflict.destination.parent, conflict.destination.name, reserved)
            name = simpledialog.askstring('다른 이름으로 저장', '같은 폴더에 저장할 새 파일 이름을 입력하세요.',
                                          initialvalue=suggested, parent=self.window)
            if name is None:
                return
            try:
                name = validate_name(name, conflict.source.suffix)
                target = conflict.destination.parent / name
                other_names = {value.name.casefold() for key, value in self.choices.items()
                               if key != conflict.source.name and value.name}
                if target.exists() or target.is_symlink() or name.casefold() in other_names:
                    raise ValueError('이미 사용 중인 이름입니다. 다른 이름을 입력해 주세요.')
            except ValueError as exc:
                messagebox.showerror('파일 이름 확인', str(exc), parent=self.window)
                return
            choice = ConflictChoice('rename', name=name)
            label = '새 이름: ' + name
        elif action == 'skip':
            choice = ConflictChoice('skip')
            label = '건너뛰기'
        else:
            return
        self.choices[conflict.source.name] = choice
        self._show_planned_path(conflict)
        self.table.item(item, values=(conflict.destination.name, label))
        self.selection_text.set(f'{len(self.choices)} / {len(self.conflicts)}개 선택 완료')
        if len(self.choices) == len(self.conflicts):
            self.apply_button.state(['!disabled'])

    def _show_planned_path(self, conflict):
        choice = self.choices.get(conflict.source.name)
        if choice is None:
            text = '저장 예정: 처리 방법을 선택해 주세요.'
        elif choice.action == 'skip':
            text = '건너뛰기: 이 파일은 저장하지 않습니다.'
        else:
            target = conflict.destination.parent / choice.name if choice.name else conflict.destination
            text = '저장 예정: ' + str(target.resolve())
        self.planned_text.set(text)

    def proceed(self):
        if len(self.choices) == len(self.conflicts):
            self.decisions = dict(self.choices)
            self.window.destroy()

    def cancel(self):
        self.window.destroy()


def show_conflicts(parent, conflicts: tuple[FileConflict, ...]) -> dict[str, ConflictChoice] | None:
    dialog = ConflictDialog(parent, conflicts)
    dialog.window.wait_visibility()
    dialog.window.grab_set()
    dialog.cancel_button.focus_set()
    parent.wait_window(dialog.window)
    return dialog.decisions
