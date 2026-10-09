"""A tiny Windows program for Aero's live background-control tests (tests/test_app_background.py). Plain Win32 through
ctypes, no dependencies: a text field, a "Press me" button that changes a label, a "Remove field" button that deletes
the text field, and a custom-drawn panel that ignores keyboard and mouse messages (like many games and GPU apps).
The window opens without taking focus and closes itself after 120 seconds.

    python tests/toy_win32_app.py "Aero toy app 1234"
"""
import ctypes
import sys
from ctypes import wintypes as wt

TITLE = sys.argv[1] if len(sys.argv) > 1 else "Aero toy app"
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32")
gdi32 = ctypes.WinDLL("gdi32")
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


WS_OVERLAPPEDWINDOW, WS_CHILD, WS_VISIBLE, WS_BORDER, WS_TABSTOP = 0x00CF0000, 0x40000000, 0x10000000, 0x00800000, 0x10000
WM_COMMAND, WM_DESTROY, WM_TIMER, WM_CHAR, WM_LBUTTONDOWN, WM_KEYDOWN = 0x0111, 0x0002, 0x0113, 0x0102, 0x0201, 0x0100
ES_AUTOHSCROLL, BS_PUSHBUTTON = 0x80, 0
ID_EDIT, ID_PRESS, ID_LABEL, ID_REMOVE = 101, 102, 103, 104
hinst = kernel32.GetModuleHandleW(None)
state = {"presses": 0, "edit": None, "label": None}


@WNDPROC
def main_proc(hwnd, msg, wp, lp):
    if msg == WM_COMMAND:
        cid = wp & 0xFFFF
        if cid == ID_PRESS:
            state["presses"] += 1
            user32.SetWindowTextW(state["label"], f"Pressed {state['presses']}")
        elif cid == ID_REMOVE and state["edit"]:
            user32.DestroyWindow(state["edit"])
            state["edit"] = None
            user32.SetWindowTextW(state["label"], "Field removed")
        return 0
    if msg == WM_TIMER:
        user32.DestroyWindow(hwnd)
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


@WNDPROC
def deaf_proc(hwnd, msg, wp, lp):
    """A custom-drawn panel that ignores keys and clicks (as games and GPU-drawn apps often do)."""
    if msg in (WM_CHAR, WM_KEYDOWN, WM_LBUTTONDOWN, 0x0202, 0x0203):
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def main():
    for name, proc in (("AeroToyMain", main_proc), ("AeroToyDeaf", deaf_proc)):
        wc = WNDCLASSW(lpfnWndProc=proc, hInstance=hinst, lpszClassName=name, hbrBackground=6)   # COLOR_WINDOW + 1
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
    hwnd = user32.CreateWindowExW(0, "AeroToyMain", TITLE, WS_OVERLAPPEDWINDOW, 40, 40, 460, 300, None, None, hinst, None)
    state["edit"] = user32.CreateWindowExW(0, "Edit", "", WS_CHILD | WS_VISIBLE | WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL,
                                           16, 16, 300, 26, hwnd, ID_EDIT, hinst, None)
    user32.CreateWindowExW(0, "Button", "Press me", WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON, 16, 52, 120, 30,
                           hwnd, ID_PRESS, hinst, None)
    user32.CreateWindowExW(0, "Button", "Remove field", WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_PUSHBUTTON, 150, 52, 120,
                           30, hwnd, ID_REMOVE, hinst, None)
    state["label"] = user32.CreateWindowExW(0, "Static", "Not pressed", WS_CHILD | WS_VISIBLE, 16, 92, 300, 22, hwnd,
                                            ID_LABEL, hinst, None)
    user32.CreateWindowExW(0, "AeroToyDeaf", "", WS_CHILD | WS_VISIBLE | WS_BORDER, 16, 124, 300, 100, hwnd, None, hinst,
                           None)
    user32.ShowWindow(hwnd, 4)                          # SW_SHOWNOACTIVATE: appear without taking focus
    user32.SetTimer(hwnd, 1, 120000, None)
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
