"""Aero's own on-screen cursor: a small blue pointer with a label that glides to wherever the agent clicks or
types, ripples on clicks and fades when idle. It is click-through and never takes focus, so it can't get in the
way of the real mouse. Runs as its own process (Tk needs its own main thread); tools talk to it over UDP."""
import os
import socket
import subprocess
import sys
import time

PORT = int(os.environ.get("AERO_OVERLAY_PORT", "8185"))
_proc = {"p": None, "t": 0}


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


def send(action, x, y, label=""):
    """Fire-and-forget; starts the overlay process on first use."""
    if sys.platform != "win32":
        return
    p = _proc["p"]
    if (p is None or p.poll() is not None) and not _running():
        if time.time() - _proc["t"] < 10:          # don't respawn in a tight loop if Tk is missing
            return
        _proc["t"] = time.time()
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        _proc["p"] = subprocess.Popen([exe if os.path.exists(exe) else sys.executable, "-m", "aero.overlay"],
                                      cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                      creationflags=0x08000000)
        time.sleep(0.6)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(f"{action}\t{int(x)}\t{int(y)}\t{label}".encode("utf-8")[:500], ("127.0.0.1", PORT))
    s.close()
    time.sleep(0.25 if action == "click" else 0.12)   # let the glide land before the action happens


def main():
    import ctypes
    import tkinter as tk
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # same physical pixels as the tools
    except Exception:
        pass
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
    root.geometry(f"{W}x{H}+-500+-500")
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
                action, x, y, label = (data.decode("utf-8", "replace").split("\t") + [""])[:4]
                if action == "quit":
                    root.destroy()
                    return
                st.update(tx=float(x), ty=float(y), label=label, last=time.time())
                if not st["shown"]:
                    st.update(x=float(x) - 60, y=float(y) - 40, shown=True)
                if action == "click":
                    st["ripple"] = 12
        except BlockingIOError:
            pass
        except OSError:
            pass
        st["x"] += (st["tx"] - st["x"]) * 0.35
        st["y"] += (st["ty"] - st["y"]) * 0.35
        if st["ripple"]:
            st["ripple"] -= 1
        idle = time.time() - st["last"]
        if st["shown"] and idle > 6:
            st["shown"] = False
            root.geometry("+-500+-500")
        elif st["shown"]:
            root.geometry(f"+{int(st['x']) - 8}+{int(st['y']) - 4}")
            root.attributes("-alpha", 1.0 if idle < 4 else max(0.15, 1 - (idle - 4) / 2))
            draw()
        root.after(16, tick)

    draw()
    root.after(16, tick)
    root.mainloop()


if __name__ == "__main__":
    main()
