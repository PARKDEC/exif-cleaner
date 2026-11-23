import ctypes
import sys
from pathlib import Path
from tkinter import messagebox

from tkinterdnd2 import TkinterDnD

from promptcleaner.ui import CleanerApp


def main():
    if sys.platform == 'win32':
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('PromptCleaner.Desktop.1')
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = TkinterDnD.Tk()
    app = CleanerApp(root)

    def show_error(exception_type, value, traceback):
        messagebox.showerror('오류', '처리 중 오류가 발생했습니다.\n' + str(value), parent=root)

    root.report_callback_exception = show_error
    if len(sys.argv) > 1:
        root.after(250, lambda: app.enqueue_paths([Path(path) for path in sys.argv[1:]]))
    root.mainloop()


if __name__ == '__main__':
    main()
