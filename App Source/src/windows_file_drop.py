"""Windows Explorer file drops on a Tk widget, without third-party packages.

    https://learn.microsoft.com/windows/win32/shell/wm-dropfiles
    https://learn.microsoft.com/windows/win32/api/commctrl/nf-commctrl-setwindowsubclass
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import queue
from pathlib import Path
from typing import Callable


class WindowsFileDrop:
    """All native callbacks only enqueue data; Tk dispatch happens in a poll."""

    def __init__(self, widget, on_files: Callable[[list[Path]], None]) -> None:
        self.widget = widget
        self.on_files = on_files
        self.available = False
        self._events: queue.SimpleQueue[list[Path]] = queue.SimpleQueue()
        self._hwnd = 0
        self._after = None
        if os.name != "nt":
            return
        self._shell = ctypes.WinDLL("shell32", use_last_error=True)
        self._controls = ctypes.WinDLL("comctl32", use_last_error=True)
        uint_ptr = ctypes.c_size_t
        long_ptr = ctypes.c_ssize_t
        self._callback_type = ctypes.WINFUNCTYPE(
            long_ptr, wintypes.HWND, wintypes.UINT, uint_ptr, long_ptr, uint_ptr, uint_ptr,
        )
        self._shell.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        self._shell.DragAcceptFiles.restype = None
        self._shell.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        self._shell.DragQueryFileW.restype = wintypes.UINT
        self._shell.DragFinish.argtypes = [wintypes.HANDLE]
        self._shell.DragFinish.restype = None
        self._controls.SetWindowSubclass.argtypes = [wintypes.HWND, self._callback_type, uint_ptr, uint_ptr]
        self._controls.SetWindowSubclass.restype = wintypes.BOOL
        self._controls.RemoveWindowSubclass.argtypes = [wintypes.HWND, self._callback_type, uint_ptr]
        self._controls.RemoveWindowSubclass.restype = wintypes.BOOL
        self._controls.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, uint_ptr, long_ptr]
        self._controls.DefSubclassProc.restype = long_ptr
        self._callback = self._callback_type(self._window_message)
        self._id = id(self)
        widget.update_idletasks()
        hwnd = widget.winfo_id()
        if not self._controls.SetWindowSubclass(hwnd, self._callback, self._id, 0):
            return
        self._hwnd = hwnd
        self._shell.DragAcceptFiles(hwnd, True)
        self.available = True
        widget.bind("<Destroy>", self._destroyed, add="+")
        self._after = widget.after(80, self._poll)

    def _window_message(self, hwnd, message, wparam, lparam, subclass_id, ref_data):
        if message == 0x0233:  # WM_DROPFILES
            try:
                paths = []
                count = self._shell.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                for index in range(count):
                    length = self._shell.DragQueryFileW(wparam, index, None, 0)
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    self._shell.DragQueryFileW(wparam, index, buffer, length + 1)
                    paths.append(Path(buffer.value))
                self._events.put(paths)
            finally:
                self._shell.DragFinish(wparam)
            return 0
        if message == 0x0082:  # WM_NCDESTROY
            self._shell.DragAcceptFiles(hwnd, False)
            self._controls.RemoveWindowSubclass(hwnd, self._callback, self._id)
            self._hwnd = 0
            self.available = False
        return self._controls.DefSubclassProc(hwnd, message, wparam, lparam)

    def _poll(self) -> None:
        self._after = None
        try:
            while True:
                self.on_files(self._events.get_nowait())
        except queue.Empty:
            pass
        if self._hwnd:
            self._after = self.widget.after(80, self._poll)

    def _destroyed(self, event) -> None:
        if event.widget is self.widget:
            self.close()

    def close(self) -> None:
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None
        if self._hwnd:
            self._shell.DragAcceptFiles(self._hwnd, False)
            self._controls.RemoveWindowSubclass(self._hwnd, self._callback, self._id)
            self._hwnd = 0
        self.available = False
