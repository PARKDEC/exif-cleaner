import ctypes
import gc
import sys

import pytest
from tkinterdnd2 import TkinterDnD


@pytest.fixture(scope='session')
def tk_host():
    # Match the application's one-interpreter model. Isolate test windows with
    # Toplevels instead of repeatedly creating/destroying Tcl interpreters.
    if sys.platform == 'win32':
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = TkinterDnD.Tk()
    root.withdraw()
    yield root
    gc.collect()
    root.destroy()
