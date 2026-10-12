"""Aero's own on-screen cursor, its "is using" banner and the outline around the app being used.

The cursor is a small blue pointer with a label that glides to wherever the agent clicks or types, ripples on clicks
and fades when idle. It is click-through and never takes focus, so it can't get in the way of the real mouse.

The banner sits at the top of the app being used (or the top of the screen) while a model drives it:
"<model> is using <app>" with a Stop button that stops every running task, and a blue outline goes around that app's
window (1.2.3). Both follow the window when it moves, onto any monitor, and the outline goes away while it is
minimized. Neither takes focus or clicks; both are left out of screenshots (so the model never sees or clicks
them), and the banner moves to the bottom when the agent is about to click where it sits.

Hidden pieces are parked outside every monitor (1.2.1 used -500,-500, which is on screen when a monitor sits left of
or above the main one). Runs as its own process (Tk needs its own main thread) and exits with the Aero that started
it; tools talk to it over UDP."""
import os
import socket
import subprocess
import sys
import time

PORT = int(os.environ.get("AERO_OVERLAY_PORT", "8185"))
_proc = {"p": None, "t": 0, "fresh": False}


def _running():
    """An overlay from an earlier Aero run may still be up: it owns the UDP port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", PORT))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _ensure():
    """Start the overlay process if it isn't running. False when it can't run (not Windows, or Tk is missing)."""
    if sys.platform != "win32":
        return False
    p = _proc["p"]
    if not _proc["fresh"]:
        _proc["fresh"] = True
        if p is None and _running():               # left by an earlier Aero run, maybe an older version: replace it
            _send_raw("quit\t\t\t")
            time.sleep(0.4)
    if (p is None or p.poll() is not None) and not _running():
        if time.time() - _proc["t"] < 10:          # don't respawn in a tight loop if Tk is missing
            return False
        _proc["t"] = time.time()
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        _proc["p"] = subprocess.Popen([exe if os.path.exists(exe) else sys.executable, "-m", "aero.overlay",
                                       "--parent", str(os.getpid())],
                                      cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      creationflags=0x08000000)
        time.sleep(0.6)
    return True


def _send_raw(msg):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(msg.encode("utf-8")[:500], ("127.0.0.1", PORT))
    s.close()


def send(action, x, y, label=""):
    """Fire-and-forget; starts the overlay process on first use."""
    if not _ensure():
        return
    _send_raw(f"{action}\t{int(x)}\t{int(y)}\t{label}")
    time.sleep(0.25 if action == "click" else 0.12)   # let the glide land before the action happens


def banner(text, rect=None, hwnd=None):
    """Show "<text>" with a Stop button at the top of rect (left, top, right, bottom; None = the screen), or hide it
    with text=None. With hwnd, a blue outline goes around that window and both follow it."""
    if sys.platform != "win32":
        return
    if text is None:
        if _running():
            _send_raw("unbanner\t\t\t")
        return
    if not _ensure():
        return
    r = ",".join(str(int(v)) for v in rect) if rect else ""
    msg = f"banner\t{r}\t{os.environ.get('AERO_PORT', '8180')}\t{text}\t{int(hwnd or 0)}"
    _send_raw(msg)
    if time.time() - _proc["t"] < 3:                # the overlay just started: Tk may not be listening yet
        import threading
        threading.Timer(1.5, _send_raw, args=(msg,)).start()


def banner_pos(rect, size, screen, low=False):
    """Where the banner goes: centred at the top of rect (or of the screen), inside the screen; at the bottom
    instead when low (the agent needed the spot it covered). screen: (width, height) of the main monitor, or
    (left, top, right, bottom) of the monitor the window is on (negative left of / above the main one)."""
    bw, bh = size
    sl, st, sr, sb = (0, 0, *screen) if len(screen) == 2 else screen
    left, top, right, bottom = rect if rect and rect[2] - rect[0] > 120 and rect[3] - rect[1] > 80 else (sl, st, sr, sb)
    x = int((left + right) / 2 - bw / 2)
    y = int(bottom - bh - (56 if bottom >= sb - 2 else 8)) if low else int(top + 8)
    return max(sl, min(x, sr - bw)), max(st, min(y, sb - bh))


def parked(virtual):
    """A position outside every monitor: virtual = (left, top, width, height) of the whole desktop."""
    return f"+{virtual[0] - 4000}+{virtual[1] - 4000}"


def outline_rects(r, t=4):
    """The four edges (width, height, x, y) of an outline drawn just inside rect r."""
    left, top, right, bottom = r
    w, h = right - left, bottom - top
    return [(w, t, left, top), (w, t, left, bottom - t), (t, h, left, top), (t, h, right - t, top)]


def covers(pos, size, point, margin=14):
    """True when point is on (or within margin of) the banner at pos."""
    (x, y), (bw, bh), (px, py) = pos, size, point
    return x - margin <= px <= x + bw + margin and y - margin <= py <= y + bh + margin


def main():
    import ctypes
    import tkinter as tk
    from ctypes import wintypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # same physical pixels as the tools
    except Exception:
        pass
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.MonitorFromRect.restype = u32.MonitorFromWindow.restype = ctypes.c_void_p
    u32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
    u32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    u32.GetForegroundWindow.restype = ctypes.c_void_p
    for f in (u32.IsWindow, u32.IsWindowVisible, u32.IsIconic):
        f.argtypes = [ctypes.c_void_p]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    u32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
    PARK = parked([u32.GetSystemMetrics(i) for i in (76, 77, 78, 79)])     # SM_X/YVIRTUALSCREEN, SM_CX/CYVIRTUALSCREEN

    def monitor(rect):
        """(left, top, right, bottom) of the work area of the monitor showing rect (None: the window in front)."""
        try:
            if rect:
                r = wintypes.RECT(*rect)
                m = u32.MonitorFromRect(ctypes.byref(r), 2)                  # MONITOR_DEFAULTTONEAREST
            else:
                m = u32.MonitorFromWindow(u32.GetForegroundWindow(), 2)
            mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
            if m and u32.GetMonitorInfoW(m, ctypes.byref(mi)):
                w = mi.rcWork
                return (w.left, w.top, w.right, w.bottom)
        except Exception:
            pass
        return (0, 0, u32.GetSystemMetrics(0), u32.GetSystemMetrics(1))

    def frame(h):
        """The window's visible bounds, or None while it is closed, hidden or minimized."""
        try:
            if not h or not u32.IsWindow(h) or not u32.IsWindowVisible(h) or u32.IsIconic(h):
                return None
            r = wintypes.RECT()
            if ctypes.windll.dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(h), 9, ctypes.byref(r), ctypes.sizeof(r)):
                u32.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(r))     # DWMWA_EXTENDED_FRAME_BOUNDS failed
            return (r.left, r.top, r.right, r.bottom) if r.right - r.left > 40 and r.bottom - r.top > 40 else None
        except Exception:
            return None

    parent = None
    if "--parent" in sys.argv:
        try:
            parent = k32.OpenProcess(0x00100000, False, int(sys.argv[sys.argv.index("--parent") + 1]))  # SYNCHRONIZE
        except Exception:
            parent = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", PORT))
    sock.setblocking(False)

    KEY = "#010203"                                       # transparent colour
    W, H = 240, 64
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", KEY)
    root.configure(bg=KEY)
    cv = tk.Canvas(root, width=W, height=H, bg=KEY, highlightthickness=0)
    cv.pack()
    root.geometry(f"{W}x{H}{PARK}")
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    GWL_EXSTYLE = -20
    ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    # layered + click-through + no taskbar button + never activates
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)

    blue, dark = "#2f8cff", "#0b1a33"
    st = {"x": -500.0, "y": -500.0, "tx": -500.0, "ty": -500.0, "label": "", "last": 0.0, "ripple": 0, "shown": False}

    def draw():
        cv.delete("all")
        r = st["ripple"]
        if r:
            rr = 4 + (12 - r) * 2
            cv.create_oval(8 - rr, 8 - rr, 8 + rr, 8 + rr, outline=blue, width=2)
        pts = [8, 4, 8, 30, 14, 24, 19, 35, 23, 33, 18, 22, 27, 22]       # arrow pointer, tip at (8, 4)
        cv.create_polygon(pts, fill=blue, outline="white", width=1.5)
        txt = "Aero" + (f" · {st['label']}" if st["label"] else "")
        tw = min(W - 34, 7 * len(txt) + 14)
        cv.create_rectangle(30, 26, 30 + tw, 46, fill=dark, outline=blue)
        cv.create_text(37, 36, text=txt[:30], fill="white", anchor="w", font=("Segoe UI", 9, "bold"))

    def tick():
        try:
            while True:
                data, _ = sock.recvfrom(1024)
                parts = data.decode("utf-8", "replace").split("\t") + ["", "", "", ""]
                action, x, y, label = parts[:4]
                if action == "quit":
                    root.destroy()
                    return
                if action in ("banner", "unbanner"):
                    st["banner"](action, x, y, label, parts[4])
                    continue
                st["avoid"](float(x), float(y))
                st.update(tx=float(x), ty=float(y), label=label, last=time.time())
                if not st["shown"]:
                    st.update(x=float(x) - 60, y=float(y) - 40, shown=True)
                if action == "click":
                    st["ripple"] = 12
        except BlockingIOError:
            pass
        except (OSError, ValueError):
            pass
        st["x"] += (st["tx"] - st["x"]) * 0.35
        st["y"] += (st["ty"] - st["y"]) * 0.35
        if st["ripple"]:
            st["ripple"] -= 1
        idle = time.time() - st["last"]
        if st["shown"] and idle > 6:
            st["shown"] = False
            root.attributes("-alpha", 0.0)
            root.geometry(PARK)
        elif st["shown"]:
            root.geometry(f"+{int(st['x']) - 8}+{int(st['y']) - 4}")
            root.attributes("-alpha", 1.0 if idle < 4 else max(0.15, 1 - (idle - 4) / 2))
            draw()
        root.after(16, tick)

    # ---- the "is using" banner
    bn = tk.Toplevel(root)
    bn.overrideredirect(True)
    bn.attributes("-topmost", True)
    bn.configure(bg=blue)
    inner = tk.Frame(bn, bg=dark, padx=12, pady=6)
    inner.pack(padx=1, pady=1)
    dot = tk.Canvas(inner, width=12, height=12, bg=dark, highlightthickness=0)
    dot.pack(side="left", padx=(0, 8))
    dot_id = dot.create_oval(1, 1, 11, 11, fill="#ff5a6e", outline="")
    msg = tk.Label(inner, text="", bg=dark, fg="white", font=("Segoe UI", 10, "bold"))
    msg.pack(side="left")
    stop_btn = tk.Label(inner, text="Stop", bg="#d8344a", fg="white", font=("Segoe UI", 9, "bold"), padx=14, pady=2,
                        cursor="hand2")
    stop_btn.pack(side="left", padx=(14, 0))
    # shown once, off-screen, and moved on and off screen from then on: showing a window again (deiconify) can
    # activate it and pull focus away from the app the model is typing into
    OFF = PARK
    bn.geometry(OFF)
    bn.update_idletasks()
    bh_wnd = ctypes.windll.user32.GetParent(bn.winfo_id())
    ex = ctypes.windll.user32.GetWindowLongW(bh_wnd, GWL_EXSTYLE)
    # no taskbar button, never activates (clicking Stop doesn't take focus from the app being controlled)
    ctypes.windll.user32.SetWindowLongW(bh_wnd, GWL_EXSTYLE, ex | 0x80 | 0x08000000)
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(bh_wnd, 0x11)     # WDA_EXCLUDEFROMCAPTURE: not in screenshots
    except Exception:
        pass
    bs = {"on": False, "rect": None, "low": False, "port": "8180", "last": 0.0, "pos": (0, 0), "blink": 0,
          "hwnd": 0, "frame": None, "ticks": 0}

    # ---- the outline around the window being used: four thin click-through edges
    edges = []
    for _ in range(4):
        e = tk.Toplevel(root)
        e.overrideredirect(True)
        e.attributes("-topmost", True)
        e.configure(bg=blue)
        e.geometry("1x1" + PARK)
        e.attributes("-alpha", 0.9)
        e.update_idletasks()
        eh = ctypes.windll.user32.GetParent(e.winfo_id())
        ex = ctypes.windll.user32.GetWindowLongW(eh, GWL_EXSTYLE)
        # layered + click-through + no taskbar button + never activates; not in screenshots
        ctypes.windll.user32.SetWindowLongW(eh, GWL_EXSTYLE, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(eh, 0x11)
        except Exception:
            pass
        edges.append(e)

    def outline(r):
        if r is None:
            for e in edges:
                e.geometry("1x1" + PARK)
            return
        for e, (gw, gh, gx, gy) in zip(edges, outline_rects(r)):
            e.geometry(f"{max(1, gw)}x{max(1, gh)}+{gx}+{gy}")
        bn.lift()

    def place():
        bn.update_idletasks()
        size = (bn.winfo_reqwidth(), bn.winfo_reqheight())
        pos = banner_pos(bs["rect"], size, monitor(bs["rect"]), bs["low"])
        bs["pos"] = pos
        bn.geometry(f"+{pos[0]}+{pos[1]}")

    def stop(_e=None):
        import threading
        import urllib.request

        def post():
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{bs['port']}/api/stop_all", data=b"{}", method="POST",
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).close()
            except Exception:
                pass
        threading.Thread(target=post, daemon=True).start()
        msg.configure(text="Stopping…")
        place()

    stop_btn.bind("<Button-1>", stop)

    def handle_banner(action, rect, port, text, hwnd=""):
        if action == "unbanner":
            bs.update(on=False, low=False, hwnd=0, frame=None)
            bn.geometry(OFF)
            outline(None)
            return
        try:
            r = tuple(int(v) for v in rect.split(",")) if rect else None
        except ValueError:
            r = None
        try:
            h = int(hwnd or 0)
        except ValueError:
            h = 0
        f = frame(h) if h else None
        r = f or r
        if r != bs["rect"] or h != bs["hwnd"]:
            bs["low"] = False
        bs.update(on=True, rect=r, port=port or "8180", last=time.time(), hwnd=h, frame=f)
        msg.configure(text=text[:90])
        place()
        outline(f if h else r)

    def banner_tick():
        bs["ticks"] += 1
        if parent and bs["ticks"] % 25 == 0 and k32.WaitForSingleObject(parent, 0) == 0:
            root.destroy()                              # the Aero that started this overlay has exited
            return
        if bs["on"]:
            if time.time() - bs["last"] > 600:          # Aero went away without hiding it
                bs.update(on=False, hwnd=0, frame=None)
                bn.geometry(OFF)
                outline(None)
            else:
                bs["blink"] = (bs["blink"] + 1) % 20
                dot.itemconfigure(dot_id, fill="#ff5a6e" if bs["blink"] < 12 else "#7a2030")
                if bs["hwnd"]:                          # follow the window: moved, resized, minimized, closed
                    f = frame(bs["hwnd"])
                    if f != bs["frame"]:
                        bs["frame"] = f
                        outline(f)
                        bs["rect"] = f                  # None while minimized: the top of the screen
                        place()
        root.after(80, banner_tick)

    def avoid(x, y):
        if bs["on"] and not bs["low"] and covers(bs["pos"], (bn.winfo_width(), bn.winfo_height()), (x, y)):
            bs["low"] = True
            place()

    st["banner"] = handle_banner
    st["avoid"] = avoid
    draw()
    root.after(16, tick)
    root.after(80, banner_tick)
    root.mainloop()


if __name__ == "__main__":
    main()
