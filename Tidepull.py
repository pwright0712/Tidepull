import os, io, re, time, platform, threading, datetime, subprocess, sys, shutil, sqlite3, tempfile
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
import requests
import yt_dlp
from yt_dlp.utils import DownloadCancelled
from yt_dlp.version import __version__ as YTDLP_VERSION

# the exe folder may be read-only (Program Files), so never write next to it on Windows
DOWNLOAD_DIR = (os.path.join(os.path.expanduser("~"), "Downloads", "Tidepull")
                if platform.system() == "Windows" else os.path.join(os.getcwd(), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG = shutil.which("ffmpeg")

if not FFMPEG:
    print("[!] ffmpeg not found on PATH — install ffmpeg (apt install ffmpeg / brew install ffmpeg / winget install FFmpeg)")
    sys.exit(1)
FFPROBE = shutil.which("ffprobe")
if not FFPROBE:
    print("[!] ffprobe not on PATH — advanced metadata probing disabled (downloads unaffected)")
FFMPEG_LOCATION = FFMPEG

# ← a JS runtime alone is not enough: yt-dlp also needs the yt-dlp-ejs solver scripts
JS_RUNTIME = next((p for p in (shutil.which(b) for b in ("deno", "node", "bun", "quickjs")) if p), None)
if not JS_RUNTIME:
    print("[!] no JS runtime (deno/node) — YouTube's JS challenges cannot be solved, only storyboards appear")
    print("    install deno: https://docs.deno.com/runtime/getting_started/installation/  (needs >= 2.3.0)")
else:
    try:
        _dv = subprocess.run([JS_RUNTIME, "--version"], capture_output=True, text=True, timeout=5).stdout
        _m = re.search(r"deno (\d+)\.(\d+)\.(\d+)", _dv)
        if _m and tuple(map(int, _m.groups())) < (2, 3, 0):
            print("[!] deno %s is too old — yt-dlp needs >= 2.3.0" % _m.group(0))
    except Exception:
        pass

try:
    import yt_dlp_ejs  # noqa: F401
    HAS_EJS = True
except Exception:
    HAS_EJS = False
if not HAS_EJS:
    print("[!] yt-dlp-ejs solver scripts missing — every YouTube format list collapses to storyboards")
    print("    fix:  python -m pip install -U \"yt-dlp[default]\"")

# ← Linux Chromium cookies are libsecret-encrypted; without this module yt-dlp
#   loads the rows but decrypts none of them, so a "logged in" session acts like guest
try:
    import secretstorage  # noqa: F401
    HAS_SECRETSTORAGE = True
except Exception:
    HAS_SECRETSTORAGE = False
if platform.system() == "Linux" and not HAS_SECRETSTORAGE:
    print("[!] secretstorage missing — Chromium cookies cannot be decrypted; session will act as guest")
    print("    python -m pip install secretstorage")

MIN_YTDLP = (2026, 10, 21)
def _ver_tuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
    except Exception:
        return (0, 0, 0)

if _ver_tuple(YTDLP_VERSION) < MIN_YTDLP:
    print("[!] yt-dlp %s is older than %s — signed-in downloads hit 'The page needs to be reloaded'"
          % (YTDLP_VERSION, ".".join(map(str, MIN_YTDLP))))
    print("    update it:  python -m pip install -U --pre yt-dlp")

def _find_browser():
    """Return a Chromium-family binary. Edge first on Windows: it ships with the OS."""
    if platform.system() == "Windows":
        cands = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            os.path.join(os.getcwd(), "chromium.exe"),
        ]
        for c in cands:
            if c and os.path.isfile(c):
                return c
        return shutil.which("msedge") or shutil.which("chrome") or ""
    for b in ("chromium-browser", "chromium", "google-chrome-stable", "google-chrome", "microsoft-edge"):
        p = shutil.which(b)
        if p:
            return p
    return "/usr/bin/chromium-browser"

BROWSER_PATH = _find_browser()
_b = os.path.basename(BROWSER_PATH).lower()
if "msedge" in _b or "microsoft-edge" in _b:
    BROWSER_NAME = "edge"
elif "chrome" in _b:
    BROWSER_NAME = "chrome"
else:
    BROWSER_NAME = "chromium"

# profile must live somewhere writable — the exe folder may be read-only
BROWSER_DATA_DIR = os.path.join(
    os.path.expandvars(r"%LocalAppData%\Tidepull") if platform.system() == "Windows"
    else os.path.expanduser("~/.local/share/tidepull"), "profile")
os.makedirs(BROWSER_DATA_DIR, exist_ok=True)

# Chrome/Edge/Chromium all moved the cookie DB into Network/ (Chrome 96+); probe both layouts
COOKIE_DB_CANDS = [
    os.path.join(BROWSER_DATA_DIR, "Default", "Network", "Cookies"),
    os.path.join(BROWSER_DATA_DIR, "Default", "Cookies"),
]


class Downloader:
    RATE_OPTS = ["512 KB/s", "1 MB/s", "2 MB/s", "5 MB/s", "10 MB/s"]
    RATE_MAP = {"512 KB/s": "512K", "1 MB/s": "1M", "2 MB/s": "2M", "5 MB/s": "5M", "10 MB/s": "10M"}
    HEIGHTS = {"360p": 360, "480p": 480, "720p": 720, "1080p": 1080, "1440p": 1440, "2160p": 2160, "4K (2160p)": 2160}
    # ← 'tv*' clients are broken for signed-in sessions (yt-dlp#17389): they answer
    #   playabilityStatus UNPLAYABLE -> "The page needs to be reloaded".
    #   'default' is fine on >= 2026.10.21 (tv_samsung); web_embedded is the fallback.
    PLAYER_CLIENTS = ("default", "web_embedded", "web_safari")
    RETRY_CLIENTS = ("web_embedded",)

    def __init__(self, root):
        self.root = root
        self.url = ""
        self.info = None
        self.result_file = None
        self.worker = None
        self.parts_expected = 1
        self.parts_done = 0
        self.t0 = time.time()
        self.b0 = 0.0
        self.s_t = time.time()
        self.s_b = 0.0
        self.has_lang = False
        self.has_fps = False
        self.thumb_src = None
        self.authed = False
        self.cancel_flag = threading.Event()
        self.can_cancel = False
        root.title("Tidepull")
        root.geometry("860x600")
        root.resizable(True, True)
        root.minsize(720, 480)
        root.bind("<Configure>", self._on_window_resize)
        style = ttk.Style()
        style.theme_use("clam")
        root.configure(bg="#121212")
        for name in ("TLabel", "TLabelframe", "TButton", "TCombobox"):
            try:
                style.configure(name, background="#121212", foreground="#eee", fieldbackground="#1e1e1e")
            except tk.TclError:
                pass
        style.configure("white.TCombobox", fieldbackground="#ffffff", foreground="#000000", background="#ffffff")
        style.map("white.TCombobox", fieldbackground=[("disabled", "#ffffff")], foreground=[("disabled", "#000000")])
        ttk.Label(root, text="Paste a YouTube URL, hit Search, then Download.").pack(pady=(8, 0))
        bar = tk.Frame(root, bg="#1e1e1e")
        bar.pack(fill="x", padx=12, pady=8)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(bar, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.url_entry.bind("<Return>", lambda e: self.on_search())
        self.root.event_delete("<<LineStart>>", "<Control-a>")
        self.root.event_add("<<SelectAll>>", "<Control-a>")
        self.search_btn = ttk.Button(bar, text="search", command=self.on_search)
        self.search_btn.pack(side="left", padx=(6, 0))
        self.rc_menu = tk.Menu(self.url_entry, tearoff=0)
        self.rc_menu.add_command(label="Copy", command=lambda: self.url_entry.event_generate("<<Copy>>"))
        self.rc_menu.add_command(label="Cut", command=lambda: self.url_entry.event_generate("<<Cut>>"))
        self.rc_menu.add_command(label="Paste", command=lambda: self.url_entry.event_generate("<<Paste>>"))
        self.rc_menu.add_command(label="Select All", command=lambda: self.url_entry.event_generate("<<SelectAll>>"))
        self.rc_menu.add_separator()
        self.rc_menu.add_command(label="Delete", command=lambda: self.url_entry.event_generate("<<Delete>>"))
        self.url_entry.bind("<Button-3>", lambda e: self.rc_menu.tk_popup(e.x_root, e.y_root))
        info_frame = tk.Frame(root, bg="#1e1e1e")
        info_frame.pack(fill="x", padx=12, pady=(4, 8))
        self.thumb_img = ImageTk.PhotoImage(Image.new("RGB", (440, 248), "#0a0a0a"))
        self.preview = tk.Label(info_frame, image=self.thumb_img, bg="#0a0a0a", anchor="center", highlightthickness=1, highlightbackground="#121212")
        self.preview.grid(row=0, column=0, rowspan=4, sticky="ew", padx=(4, 16), pady=8)
        self.title_lbl = tk.Label(info_frame, text="Title...", bg="#1e1e1e", fg="#f0f0f0", anchor="w", justify="left", wraplength=340, padx=8, font=("TkDefaultFont", 12, "bold"))
        self.title_lbl.grid(row=0, column=1, sticky="ew", pady=(10, 2))
        self.channel_lbl = tk.Label(info_frame, text="Channel", bg="#1e1e1e", fg="#9e9e9e", anchor="w", justify="left", padx=8, pady=2, font=("TkDefaultFont", 10))
        self.channel_lbl.grid(row=1, column=1, sticky="ew")
        self.duration_lbl = tk.Label(info_frame, text="Duration", bg="#1e1e1e", fg="#9e9e9e", anchor="w", justify="left", padx=8, font=("TkDefaultFont", 10))
        self.duration_lbl.grid(row=2, column=1, sticky="ew", pady=(2, 10))
        info_frame.columnconfigure(0, weight=0)
        info_frame.columnconfigure(1, weight=1)
        info_frame.rowconfigure(0, weight=0)
        ctrl = tk.Frame(root, bg="#1e1e1e")
        ctrl.pack(fill="x", padx=12, pady=4)
        ttk.Label(ctrl, text="video:", background="#1e1e1e").pack(side="left")
        self.q_var = tk.StringVar()
        self.q_menu = ttk.Combobox(ctrl, values=[], state="disabled", width=10, textvariable=self.q_var, style="white.TCombobox")
        self.q_menu.pack(side="left", padx=(4, 10))
        ttk.Label(ctrl, text="lang:", background="#1e1e1e").pack(side="left")
        self.l_var = tk.StringVar()
        self.l_menu = ttk.Combobox(ctrl, values=[], state="disabled", width=14, textvariable=self.l_var, style="white.TCombobox")
        self.l_menu.pack(side="left", padx=(4, 10))
        ttk.Label(ctrl, text="fps:", background="#1e1e1e").pack(side="left")
        self.fps_var = tk.StringVar()
        self.fps_menu = ttk.Combobox(ctrl, values=[], state="disabled", width=9, textvariable=self.fps_var, style="white.TCombobox")
        self.fps_menu.pack(side="left", padx=(4, 10))
        ttk.Label(ctrl, text="speed:", background="#1e1e1e").pack(side="left")
        self.band_var = tk.StringVar(value="unlimited")
        self.band_menu = ttk.Combobox(ctrl, values=["unlimited"] + self.RATE_OPTS, state="readonly", width=12, textvariable=self.band_var, style="white.TCombobox")
        self.band_menu.pack(side="left", padx=(4, 10))
        self.dl_btn = ttk.Button(ctrl, text="download", command=self.on_download, state="disabled")
        self.dl_btn.pack(side="left")
        self.cancel_btn = ttk.Button(ctrl, text="cancel", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(4, 0))
        self.progress = ttk.Progressbar(root, maximum=100)
        self.progress.pack(fill="x", padx=12, pady=(6, 0))
        self.status_var = tk.StringVar(value="idle")
        ttk.Label(root, textvariable=self.status_var, anchor="w", background="#1e1e1e").pack(fill="x", padx=12)
        auth = tk.Frame(root, bg="#1e1e1e")
        auth.pack(fill="x", padx=12, pady=(6, 0))
        ttk.Label(auth, text="session:", background="#1e1e1e").pack(side="left")
        self.auth_lbl = tk.Label(auth, text="guest", bg="#1e1e1e", fg="#9e9e9e", font=("TkDefaultFont", 9, "bold"))
        self.auth_lbl.pack(side="left", padx=(2, 10))
        self.login_btn = ttk.Button(auth, text="google login", command=self._on_login)
        self.login_btn.pack(side="left")
        self.logout_btn = ttk.Button(auth, text="logout", command=self._on_logout, state="disabled")
        self.logout_btn.pack(side="left", padx=(4, 0))
        self.folder_btn = ttk.Button(root, text="open folder", command=self.open_folder, state="disabled")
        self.folder_btn.pack(pady=10, fill="x", padx=12)
        self.exit_btn = ttk.Button(root, text="Exit", command=root.destroy)
        self.exit_btn.pack(pady=(0, 10), fill="x", padx=12)

        if self._google_auth_cookie_count() > 0:
            self.authed = True
            if HAS_SECRETSTORAGE or platform.system() == "Windows":
                self.auth_lbl.config(text="authenticated ✔", fg="#4caf50")
            else:
                self.auth_lbl.config(text="authenticated (cookies unreadable)", fg="#ffb300")
            self.logout_btn.config(state="normal")
            self.status_var.set("authenticated — resumed saved google session")

    def _on_window_resize(self, event=None):
        if not self.thumb_src: return
        cw = self.preview.winfo_width();
        ch = self.preview.winfo_height()
        if cw < 50 or ch < 50: return
        sr = self.thumb_src.width / self.thumb_src.height
        bw, bh = min(cw, 440), min(ch, 248)
        if bw / sr > bh:
            h, w = bh, int(bh * sr)
        else:
            w, h = bw, int(bw / sr)
        w, h = max(w, 1), max(h, 1)
        self.thumb_img = ImageTk.PhotoImage(self.thumb_src.resize((w, h)))
        self.preview.config(image=self.thumb_img)

    @staticmethod
    def _cookie_db():
        for p in COOKIE_DB_CANDS:
            if os.path.isfile(p):
                return p
        return None

    def _google_auth_cookie_count(self):
        """Count auth cookies. The DB is copied out first: Chrome/Edge hold a WAL lock
        on the live file and reading it in place returns zero rows or 'database is locked'."""
        src = self._cookie_db()
        if not src:
            return 0
        tmp = os.path.join(tempfile.gettempdir(), "tidepull_ck_%d.db" % os.getpid())
        for _ in range(10):
            try:
                for suffix in ("", "-wal", "-shm"):
                    if os.path.isfile(src + suffix):
                        shutil.copy2(src + suffix, tmp + suffix)
                    elif os.path.isfile(tmp + suffix):
                        os.remove(tmp + suffix)
                conn = sqlite3.connect(tmp, timeout=3)
                cnt = conn.cursor().execute(
                    "SELECT count(*) FROM cookies WHERE name IN "
                    "('SID','HSID','SSID','SAPISID','__Secure-1PSID','__Secure-3PSID') "
                    "AND host_key LIKE '%google.com%'").fetchone()[0]
                conn.close()
                return cnt
            except (sqlite3.OperationalError, sqlite3.DatabaseError):
                time.sleep(0.5)
            except Exception:
                return 0
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        return 0

    def _auth_opts(self):
        if not self.authed: return {}
        return {"cookiesfrombrowser": (BROWSER_NAME, os.path.join(BROWSER_DATA_DIR, "Default"), None, None)}

    @staticmethod
    def _browser_running():
        """Is a browser actually using OUR profile? The spawned PID exiting does not
        mean the window closed — Edge/Chrome hand off to a background instance."""
        try:
            if platform.system() == "Windows":
                exe = os.path.basename(BROWSER_PATH)
                ps = ("Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
                      "Where-Object { $_.CommandLine -like '*%s*' } | "
                      "Select-Object -First 1 -ExpandProperty ProcessId"
                      % (exe, BROWSER_DATA_DIR))
                r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                   capture_output=True, text=True, timeout=20,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return bool(r.stdout.strip())
            return subprocess.run(["pgrep", "-f", BROWSER_DATA_DIR],
                                  capture_output=True).returncode == 0
        except Exception:
            return False

    def _on_login(self):
        if self.busy(): messagebox.showwarning("busy", "wait for current task"); return
        if self.authed: messagebox.showinfo("already authed", "already logged in — click logout first"); return
        if not BROWSER_PATH or not os.path.isfile(BROWSER_PATH):
            messagebox.showerror("browser not found",
                                 "No Chromium-based browser found.\n\n"
                                 "Install Microsoft Edge or Google Chrome, then retry.")
            return

        if not messagebox.askokcancel("sign in",
                "A separate %s window will open, using its own profile — your normal "
                "browsing session is not touched.\n\n"
                "1. Sign in with google and wait for YouTube to finish loading.\n"
                "2. Close that window completely — do not just minimise it.\n"
                "3. Tidepull picks the session up as soon as the window is gone.\n\n"
                "Closing it without signing in saves nothing." % BROWSER_NAME):
            return

        self.login_btn.config(state="disabled")
        self.status_var.set("opening youtube.com in %s — sign in, then close the window" % BROWSER_NAME)
        self.root.update_idletasks()
        threading.Thread(target=self._login_worker, daemon=True).start()

    def _login_worker(self):
        pre = self._google_auth_cookie_count()
        try:
            proc = subprocess.Popen([BROWSER_PATH, "--user-data-dir=%s" % BROWSER_DATA_DIR, "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-gpu", "https://www.youtube.com"])
            proc.wait()
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("browser failed", "%s: %s" % (type(e).__name__, e)))
            self.root.after(0, self._login_cleanup); return

        self.root.after(0, lambda: self.status_var.set("waiting for the browser window to close..."))
        deadline = time.time() + 300
        while time.time() < deadline:
            if not self._browser_running():
                break
            time.sleep(1.0)

        time.sleep(1.0)                                   # let the cookie DB flush
        post = 0
        for _ in range(10):
            post = self._google_auth_cookie_count()
            if post > 0:
                break
            time.sleep(0.5)

        if post <= 0:
            self.root.after(0, lambda: messagebox.showwarning("auth", "no google session detected — sign in fully, then close the window"))
            self.root.after(0, self._login_cleanup); return
        self.root.after(0, lambda: self._login_cleanup(True))

    def _login_cleanup(self, ok=False):
        # a valid session from an earlier run — or a count that landed while the cookie DB
        # was still flushing — still counts as authenticated
        ok = ok or self._google_auth_cookie_count() > 0
        self.authed = ok
        if ok:
            if HAS_SECRETSTORAGE or platform.system() == "Windows":
                self.auth_lbl.config(text="authenticated", fg="#4caf50")
            else:
                self.auth_lbl.config(text="authenticated (cookies unreadable)", fg="#ffb300")
            self.logout_btn.config(state="normal")
            if not self.busy(): self.status_var.set("authenticated — yt-dlp will now run under your google session")
        else:
            self.auth_lbl.config(text="guest", fg="#9e9e9e")
            if not self.busy(): self.status_var.set("guest session — no google login active")
        if not self.busy(): self.login_btn.config(state="normal")

    def _on_logout(self):
        if self.busy(): messagebox.showwarning("busy", "wait for current task before logout"); return

        if not messagebox.askokcancel("sign out",
                "A separate %s window will open on your saved session.\n\n"
                "1. Sign out of google in that window.\n"
                "2. Close the window completely.\n\n"
                "Tidepull only marks you as logged out once the session is actually gone. "
                "Closing the window while still signed in will ask before discarding it."
                % BROWSER_NAME):
            return

        self.logout_btn.config(state="disabled")
        self.status_var.set("opening youtube.com in %s — sign out, then close the window" % BROWSER_NAME)
        self.root.update_idletasks()
        threading.Thread(target=self._logout_worker, daemon=True).start()

    def _logout_worker(self):
        try:
            proc = subprocess.Popen([BROWSER_PATH, "--user-data-dir=%s" % BROWSER_DATA_DIR, "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-gpu", "https://www.youtube.com"])
            proc.wait()
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("browser failed", "%s: %s" % (type(e).__name__, e)))
            self.root.after(0, self._logout_cleanup); return

        self.root.after(0, lambda: self.status_var.set("waiting for the browser window to close..."))
        deadline = time.time() + 300
        while time.time() < deadline:
            if not self._browser_running():
                break
            time.sleep(1.0)

        if self._browser_running():
            self.root.after(0, lambda: messagebox.showwarning("logout", "browser still open — close it fully, then retry"))
            self.root.after(0, self._logout_cleanup); return

        # give the browser a moment to flush the cleared cookies to disk, then poll
        time.sleep(1.0)
        left = 1
        for _ in range(10):
            left = self._google_auth_cookie_count()
            if left == 0:
                break
            time.sleep(0.5)

        if left > 0:
            # window closed while still signed in — don't pretend it was a logout
            def _ask():
                if messagebox.askyesno("still signed in",
                                       "You closed the browser without signing out of google.\n\n"
                                       "Discard the saved session anyway?"):
                    self._logout_cleanup(True)
                else:
                    self._logout_cleanup(False)
            self.root.after(0, _ask)
            return

        self.root.after(0, lambda: self._logout_cleanup(True))

    def _logout_cleanup(self, ok=False):
        if ok:
            self.authed = False; self.auth_lbl.config(text="guest", fg="#9e9e9e")
            self.logout_btn.config(state="disabled"); self.login_btn.config(state="normal")
            shutil.rmtree(BROWSER_DATA_DIR, ignore_errors=True); os.makedirs(BROWSER_DATA_DIR, exist_ok=True)
            self.status_var.set("logged out")
        elif not self.busy():
            self.logout_btn.config(state="normal"); self.status_var.set("logout pending — retry")

    @staticmethod
    def _norm_video_url(u):
        vid = None
        if re.match(r"^https?://(?:www\.|m\.)?youtube\.com/watch\?.*", u, re.I):
            m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})&?", u); vid = m.group(1) if m else None
        elif re.match(r"^https?://youtu\.be/", u, re.I):
            m = re.match(r"^https?://youtu\.be/([A-Za-z0-9_-]{11})", u, re.I); vid = m.group(1) if m else None
        return "https://www.youtube.com/watch?v=%s" % vid if vid else u

    @staticmethod
    def _is_reload_error(e):
        return "needs to be reloaded" in str(e).lower() or "unplayable" in str(e).lower()

    # ── format helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _playable(f):
        if not f.get("url") or f.get("has_drm"):
            return False
        return not (f.get("protocol") or "").startswith("mhtml")

    def _splits(self, info):
        fmts = info.get("formats") or []
        vid = [f for f in fmts if self._playable(f) and f.get("vcodec") not in (None, "none")]
        aud = [f for f in fmts if self._playable(f) and f.get("vcodec") in (None, "none")]
        mux = [f for f in fmts if self._playable(f)
               and f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]
        return vid, aud, mux

    def _build_format(self, want_h, want_fps, want_lang):
        """yt-dlp's own selector plus format_sort, so protocol/source/vcodec preference
        stays the library's instead of being pinned to format ids by us."""
        sel = "bv*[height<=%d]+ba/b" % want_h
        sort = ["res:%d" % want_h]
        if want_fps:
            sort.append("fps:%d" % want_fps)
        if want_lang:
            sort.append("lang:%s" % want_lang)
        return sel, sort

    def _extract_with(self, ydl, clients):
        """extract_info with a client list, auto-retrying the reload-bug clients."""
        last = None
        for i, cl in enumerate((clients, self.RETRY_CLIENTS)):
            if not cl: continue
            ydl.params["extractor_args"] = {"youtube": {"player_client": list(cl)}}
            try:
                return ydl.extract_info(self.url, download=False)
            except Exception as e:
                last = e
                if i or not self._is_reload_error(e):
                    raise
                print("[dbg] reload-bug on %s — retrying with %s" % (",".join(cl), ",".join(self.RETRY_CLIENTS)))
        raise last

    def on_search(self):
        if self.busy(): messagebox.showwarning("busy", "wait for current task"); return
        url = self.url_var.get().strip()
        if not url: messagebox.showwarning("input", "paste a URL first"); return
        self.url = self._norm_video_url(url); self.result_file = None
        for w in (self.folder_btn, self.q_menu, self.l_menu, self.fps_menu, self.dl_btn): w.config(state="disabled")
        self.status_var.set("fetching video info..."); self.progress["value"] = 0; self.root.update_idletasks()
        self.worker = threading.Thread(target=self._search_worker, daemon=True); self.worker.start()

    def _search_worker(self):
        if self.authed: modes = (self._auth_opts(), {})
        else: modes = ({},)
        info, errs = None, []
        for extra in modes:
            ydl = None
            try:
                sopts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
                         "ffmpeg_location": FFMPEG_LOCATION, "extractor_retries": 2,
                         "remote_components": ["ejs:github"]}
                sopts.update(extra)
                ydl = yt_dlp.YoutubeDL(sopts)
                info = self._extract_with(ydl, self.PLAYER_CLIENTS)
                if not self._splits(info)[0] and not self._splits(info)[2]:
                    raise RuntimeError("no playable video stream returned (JS runtime / ejs missing?)")
                break
            except Exception as e:
                info = None
                errs.append("%s: %s" % (type(e).__name__, e))
            finally:
                if ydl:
                    try: ydl.close()
                    except Exception: pass
        if not info:
            self.root.after(0, lambda: messagebox.showerror("fetch failed", "\n\n".join(errs))); self._unlock_ui(); return
        thumb = info.get("thumbnail") or ("https://i.ytimg.com/vi/%s/hqdefault.jpg" % info.get('id'))
        try: img_bytes = requests.get(thumb, timeout=10).content
        except Exception:
            buf = io.BytesIO(); Image.new("RGB", (440, 248), "#0a0a0a").save(buf, format="JPEG"); img_bytes = buf.getvalue()
        self.root.after(0, self._on_loaded, info, img_bytes)

    def _on_loaded(self, info, img_bytes):
        self.info = info; self.thumb_src = Image.open(io.BytesIO(img_bytes))
        if self.preview.winfo_width() >= 50 and self.preview.winfo_height() >= 50: self._on_window_resize()
        else: self.thumb_img = ImageTk.PhotoImage(self.thumb_src.resize((440, 248))); self.preview.config(image=self.thumb_img)
        self.title_lbl.config(text=info.get("title", ""))
        self.channel_lbl.config(text=info.get("uploader", ""))
        self.duration_lbl.config(text=str(datetime.timedelta(seconds=int(info.get("duration") or 0))))

        vid, aud, mux = self._splits(info)
        visual = vid + mux
        heights = sorted({int(f.get("height")) for f in visual if f.get("height")}, reverse=True)
        self.q_menu.config(values=["%dp" % h for h in heights], state="readonly")
        if heights: self.q_menu.set("%dp" % heights[0])
        fps_opts = sorted({round(float(f.get("fps"))) for f in visual if f.get("fps")}, reverse=True)
        if fps_opts:
            self.fps_menu.config(values=["%d fps" % v for v in fps_opts], state="readonly"); self.fps_var.set("%d fps" % fps_opts[0]); self.has_fps = True
        else: self.fps_var.set(""); self.fps_menu.config(values=[], state="disabled"); self.has_fps = False
        langs, seen = [], set()
        for a in sorted(aud, key=lambda f: (f.get("tbr") or 0), reverse=True):
            c = a.get("language")
            if not c or c in seen: continue
            seen.add(c); langs.append((a.get("language_preference") if a.get("language_preference") is not None else 999, c, a.get("language_name") or c))
        if langs:
            langs.sort(); opts = ["%s (%s)" % (n, c) for _, c, n in langs]
            self.l_menu.config(values=opts, state="readonly"); self.l_var.set(opts[0]); self.has_lang = True
        else: self.l_var.set(""); self.l_menu.config(values=[], state="disabled"); self.has_lang = False

        self.dl_btn.config(state="normal"); self.status_var.set("ready — pick quality, then download")

    def on_download(self):
        if self.busy(): messagebox.showwarning("busy", "download already running"); return
        if not self.q_var.get(): messagebox.showwarning("pick", "choose a resolution"); return
        self._busy(True)
        self.cancel_flag.clear()
        self.can_cancel = True
        self.cancel_btn.config(state="normal")
        outtmpl = os.path.join(DOWNLOAD_DIR, re.sub(r'[\\/:*?"<>|]', '_', self.info.get("title", "video"))[:120]) + ".%(ext)s"
        self.parts_done = 0; self.status_var.set("starting..."); self.progress["value"] = 0
        self.worker = threading.Thread(target=self._dl_worker, args=(outtmpl,), daemon=True); self.worker.start()

    def on_cancel(self):
        if not self.can_cancel: return
        self.can_cancel = False
        self.cancel_flag.set()
        self.cancel_btn.config(state="disabled")
        self.status_var.set("cancelling…")

    def _dl_worker(self, outtmpl):
        base = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "retries": 10, "fragment_retries": 10, "progress_hooks": [self._hook],
                "ffmpeg_location": FFMPEG_LOCATION, "outtmpl": outtmpl,
                "merge_output_format": "mp4", "concurrent_fragment_downloads": 4,
                "extractor_retries": 2, "check_formats": False,
                "remote_components": ["ejs:github"]}
        rate = self.RATE_MAP.get(self.band_var.get().strip())
        if rate:
            base["limit_rate"] = rate

        attempts = [("guest", dict(base))]
        if self.authed:
            attempts.insert(0, ("cookie", dict(base, **self._auth_opts())))

        want_h = self.HEIGHTS.get(self.q_var.get(), 1080)
        want_fps = round(float(re.search(r"\d+", self.fps_var.get()).group())) if self.has_fps and self.fps_var.get() else 0
        want_lang = self.l_var.get().rsplit(" (", 1)[-1].rstrip(")") if self.has_lang and self.l_var.get() else ""

        last, seen, dump = None, [], None
        for tag, opts in attempts:
            ydl = yt_dlp.YoutubeDL(opts)
            try:
                self.root.after(0, lambda t=tag: self.status_var.set("resolving… " + t))
                info = self._extract_with(ydl, self.PLAYER_CLIENTS)
                if self.cancel_flag.is_set():
                    self.root.after(0, self._on_cancelled); return
                vid, aud, mux = self._splits(info)
                print("[dbg] %s — playable video/audio/muxed: %d/%d/%d (of %d raw)"
                      % (tag, len(vid), len(aud), len(mux), len(info.get("formats") or [])))
                if not vid and not mux:
                    dump = info
                    raise RuntimeError("server sent no playable video stream to the %s request "
                                       "(JS runtime / ejs missing?)" % tag)

                sel, sort = self._build_format(want_h, want_fps, want_lang)
                ydl.params["format_sort"] = sort
                print("[dbg] %s — format: %s   sort: %s" % (tag, sel, sort))

                self.parts_expected = 2 if aud else 1
                res, tried = None, []
                for s in (sel, "bv*+ba/b", "b", "w"):
                    if self.cancel_flag.is_set():
                        self.root.after(0, self._on_cancelled); return
                    if not s or s in tried:
                        continue
                    tried.append(s)
                    ydl.params["format"] = s
                    try:
                        res = ydl.process_ie_result(info, download=True)
                        break
                    except DownloadCancelled:
                        raise
                    except Exception as e:
                        if "Requested format is not available" not in str(e):
                            raise
                        print("[dbg] %s — selector %r rejected, next" % (tag, s))
                if res is None:
                    raise RuntimeError("every selector was rejected on the %s list" % tag)

                self.result_file = ((res.get("requested_downloads") or [{}])[0].get("filepath")
                                    or res.get("filepath") or outtmpl.replace("%(ext)s", "mp4"))
                self.root.after(0, self._on_done)
                return
            except DownloadCancelled:
                print("[dbg] %s — cancelled" % tag)
                self.root.after(0, self._on_cancelled)
                return
            except Exception as e:
                if self.cancel_flag.is_set():
                    print("[dbg] %s — cancelled" % tag)
                    self.root.after(0, self._on_cancelled)
                    return
                last = e
                seen.append("%s:FAIL(%s)" % (tag, str(e)[:80]))
            finally:
                try: ydl.close()
                except Exception: pass

        if dump:
            try:
                p = os.path.join(DOWNLOAD_DIR, "_formats_%s.txt" % dump.get("id", "unknown"))
                with open(p, "w") as fh:
                    fh.write("yt-dlp %s\n" % YTDLP_VERSION)
                    for f in dump.get("formats") or []:
                        fh.write("%s | h=%s fps=%s v=%s a=%s proto=%s drm=%s url=%s\n" % (
                            f.get("format_id"), f.get("height"), f.get("fps"), f.get("vcodec"),
                            f.get("acodec"), f.get("protocol"), f.get("has_drm"), bool(f.get("url"))))
                print("[dbg] format dump written to %s" % p)
            except Exception:
                pass

        def _fail():
            messagebox.showerror("download failed",
                "%s: %s\nattempts: %s\n\nyt-dlp %s   JS runtime: %s   ejs scripts: %s\n\n"
                "If it says 'Only images are available':\n"
                "  python -m pip install -U \"yt-dlp[default]\"\n"
                "  (and install deno >= 2.3.0)\n\n"
                "If it says 'The page needs to be reloaded':\n"
                "  python -m pip install -U --pre yt-dlp"
                % (type(last).__name__, last, " ".join(seen), YTDLP_VERSION,
                   JS_RUNTIME or "none", "installed" if HAS_EJS else "MISSING"))
            self._unlock_ui()
        self.root.after(0, _fail)

    def _hook(self, d):
        if self.cancel_flag.is_set():
            raise DownloadCancelled()
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        pct = ((d.get("downloaded_bytes") or 0) / total * 100) if total else 0.0
        dt, db = time.time() - self.s_t, (d.get("downloaded_bytes") or 0) - self.s_b
        spd = db / dt if dt > 0 else 0.0
        eta = ((total - (d.get("downloaded_bytes") or 0)) / spd) if (total and spd > 1) else None
        self.root.after(0, lambda: self._on_prog(pct, spd, eta, d.get("status")))
        if d.get("status") == "finished":
            self.parts_done += 1
            self.s_t, self.s_b = time.time(), d.get("downloaded_bytes") or 0
        else:
            self.s_t, self.s_b = time.time(), d.get("downloaded_bytes") or 0

    def _on_prog(self, pct, spd, eta, status):
        if status == "finished" and self.parts_done >= self.parts_expected:
            return
        self.progress["value"] = pct
        txt = "downloading… %5.1f%%   %5.2f MB/s" % (pct, spd / 1048576)
        if eta is not None:
            txt += "   ETA %s" % datetime.timedelta(seconds=int(eta))
        self.status_var.set(txt)

    def _on_done(self):
        self.progress["value"] = 100
        self.status_var.set("COMPLETED")
        self.folder_btn.config(state="normal")
        self._unlock_ui()

    def _on_cancelled(self):
        self.cancel_flag.clear()
        self.can_cancel = False
        self.status_var.set("cancelled")
        self._unlock_ui()

    def busy(self):
        return bool(self.worker and self.worker.is_alive())

    def _busy(self, on):
        self.search_btn.config(state="disabled" if on else "normal")
        self.dl_btn.config(state="disabled" if on else "normal")
        self.cancel_btn.config(state="disabled" if on else "disabled")
        self.q_menu.config(state="disabled" if on else "readonly")
        self.l_menu.config(state="disabled" if (on or not self.has_lang) else "readonly")
        self.fps_menu.config(state="disabled" if (on or not self.has_fps) else "readonly")
        self.band_menu.config(state="disabled" if on else "readonly")
        self.url_entry.config(state="disabled" if on else "normal")

    def _unlock_ui(self):
        self.can_cancel = False
        self._busy(False)

    def open_folder(self):
        if platform.system() == "Windows":
            os.startfile(DOWNLOAD_DIR)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", DOWNLOAD_DIR])
        else:
            subprocess.Popen(["xdg-open", DOWNLOAD_DIR])


if __name__ == "__main__":
    root = tk.Tk()
    Downloader(root)
    root.mainloop()