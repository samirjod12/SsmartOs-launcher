"""
SmartOS - a TV-OS style launcher for Windows (Python 3.9+, Tkinter).

Home screen with big tiles (remote / keyboard friendly):
  * Live TV      - IPTV player for .m3u playlists, plays through VLC
  * YouTube TV   - opens youtube.com/tv in an app-style window
  * Shinchan TV  - fetches seasons + episodes from watchanimeworld.one
  * Settings     - playlist, VLC path, App Loader (shortcuts shown on Home)
  * your own app shortcuts

Optional: pip install python-vlc   (embeds VLC inside SmartOS)
Without it, SmartOS launches the installed VLC program for each channel.
VLC for Windows must be installed: https://www.videolan.org/vlc/

Keys: arrows = move, Enter = open/play, Esc = back, F11 = fullscreen.
Live TV: Left/Right = previous/next channel, Up/Down = browse list.
"""
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    import vlc  # python-vlc (optional)
except Exception:
    vlc = None

# ----------------------------------------------------------------- paths/config
APPDIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SmartOS")
os.makedirs(APPDIR, exist_ok=True)
CONFIG_FILE = os.path.join(APPDIR, "config.json")
DEFAULT_PLAYLIST = os.path.join(APPDIR, "playlist.m3u")
SHINCHAN_CACHE = os.path.join(APPDIR, "shinchan_cache.json")
SHINCHAN_URL = "https://watchanimeworld.one/series/shinchan/"

SAMPLE_PLAYLIST = """#EXTM3U
#EXTINF:-1 group-title="Kids",Sony Yay 560
https://livetv.shamon-cable.net:8443/sony_yay/index.m3u8
#EXTINF:-1 group-title="Kids",Cartoon Network
https://drk6xq0vhn.gpcdn.net/live/cn_sd_abr/index.m3u8
"""

BG, CARD, CARD_HI = "#0b0f17", "#151b28", "#2a6df4"
FG, MUTED, PANEL = "#e8ecf4", "#8a94a8", "#10151f"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_config():
    cfg = {"playlist": DEFAULT_PLAYLIST, "vlc_path": "", "apps": [], "fullscreen": False}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if not os.path.exists(DEFAULT_PLAYLIST):
        with open(DEFAULT_PLAYLIST, "w", encoding="utf-8") as f:
            f.write(SAMPLE_PLAYLIST)
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def find_vlc(cfg):
    if cfg.get("vlc_path") and os.path.exists(cfg["vlc_path"]):
        return cfg["vlc_path"]
    found = shutil.which("vlc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        p = os.path.join(base, "VideoLAN", "VLC", "vlc.exe")
        if os.path.exists(p):
            return p
    return ""


def open_target(target):
    """Open a URL, file or program with the OS."""
    try:
        if re.match(r"^https?://", target, re.I):
            webbrowser.open(target)
        elif hasattr(os, "startfile"):
            os.startfile(target)  # Windows
        else:
            subprocess.Popen([target])
    except Exception as e:
        messagebox.showerror("SmartOS", f"Could not open:\n{target}\n\n{e}")


def open_app_window(url):
    """Open a URL in an app-style (no tabs) Edge/Chrome window, else default browser."""
    pf = [os.environ.get("ProgramFiles", r"C:\Program Files"),
          os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
          os.environ.get("LOCALAPPDATA", "")]
    rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    for base in pf:
        for rel in rels:
            p = os.path.join(base, rel)
            if base and os.path.exists(p):
                subprocess.Popen([p, f"--app={url}", "--start-maximized"])
                return
    webbrowser.open(url)


# ----------------------------------------------------------------- m3u parsing
def parse_m3u(text):
    channels, info = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            grp = re.search(r'group-title="([^"]*)"', line)
            logo = re.search(r'tvg-logo="([^"]*)"', line)
            name = line.rsplit(",", 1)[-1].strip() if "," in line else "Channel"
            info = {"name": name, "group": grp.group(1) if grp else "Other",
                    "logo": logo.group(1) if logo else ""}
        elif not line.startswith("#"):
            if info is None:
                info = {"name": line.rsplit("/", 1)[-1], "group": "Other", "logo": ""}
            info["url"] = line
            channels.append(info)
            info = None
    return channels


def read_source(src):
    if re.match(r"^https?://", src, re.I):
        req = urllib.request.Request(src, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ----------------------------------------------------------------- UI helpers
class Tile(tk.Frame):
    def __init__(self, parent, icon, title, subtitle, command, w=230, h=170):
        super().__init__(parent, bg=CARD, width=w, height=h, cursor="hand2",
                         highlightthickness=3, highlightbackground=BG)
        self.pack_propagate(False)
        self.command = command
        self.i = tk.Label(self, text=icon, font=("Segoe UI Emoji", 40), bg=CARD, fg=FG)
        self.t = tk.Label(self, text=title, font=("Segoe UI", 16, "bold"), bg=CARD, fg=FG)
        self.s = tk.Label(self, text=subtitle, font=("Segoe UI", 9), bg=CARD, fg=MUTED,
                          wraplength=w - 20)
        self.i.pack(pady=(22, 2))
        self.t.pack()
        self.s.pack()
        for wdg in (self, self.i, self.t, self.s):
            wdg.bind("<Button-1>", lambda e: self.command())

    def focus_on(self, on):
        c = CARD_HI if on else CARD
        self.config(bg=c, highlightbackground=("#8fb2ff" if on else BG))
        for wdg in (self.i, self.t, self.s):
            wdg.config(bg=c)
        self.s.config(fg=("#dfe8ff" if on else MUTED))


class Screen(tk.Frame):
    def __init__(self, app):
        super().__init__(app, bg=BG)
        self.app = app

    def on_show(self):
        pass

    def on_hide(self):
        pass

    def on_key(self, e):
        pass

    def header(self, title, right_widget_factory=None):
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill="x")
        tk.Button(bar, text="⬅ Home", command=self.app.home, bg=PANEL, fg=FG,
                  activebackground=CARD_HI, activeforeground="white", bd=0,
                  font=("Segoe UI", 11), padx=12, pady=8).pack(side="left", padx=8, pady=6)
        tk.Label(bar, text=title, bg=PANEL, fg=FG, font=("Segoe UI", 16, "bold")).pack(side="left", padx=8)
        if right_widget_factory:
            right_widget_factory(bar)
        return bar


def style_listbox(lb):
    lb.config(bg=PANEL, fg=FG, selectbackground=CARD_HI, selectforeground="white",
              bd=0, highlightthickness=0, activestyle="none", font=("Segoe UI", 12))


# ----------------------------------------------------------------- Home
class HomeScreen(Screen):
    COLS = 4

    def __init__(self, app):
        super().__init__(app)
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=50, pady=(35, 10))
        tk.Label(top, text="SmartOS", font=("Segoe UI", 30, "bold"), bg=BG, fg=FG).pack(side="left")
        self.clock = tk.Label(top, font=("Segoe UI", 20), bg=BG, fg=MUTED)
        self.clock.pack(side="right")
        self.greet = tk.Label(self, text="", font=("Segoe UI", 12), bg=BG, fg=MUTED)
        self.greet.pack(anchor="w", padx=54)
        self.grid_frame = tk.Frame(self, bg=BG)
        self.grid_frame.pack(expand=True)
        tk.Label(self, text="← ↑ ↓ → move    Enter open    Esc back    F11 fullscreen",
                 font=("Segoe UI", 10), bg=BG, fg=MUTED).pack(side="bottom", pady=14)
        self.tiles, self.idx = [], 0
        self.tick()

    def tick(self):
        self.clock.config(text=time.strftime("%I:%M %p").lstrip("0"))
        h = int(time.strftime("%H"))
        self.greet.config(text=("Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening")
                          + " — " + time.strftime("%A, %d %B"))
        self.after(15000, self.tick)

    def build(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        a = self.app
        items = [
            ("📺", "Live TV", "IPTV playlist via VLC", lambda: a.show("iptv")),
            ("▶️", "YouTube TV", "youtube.com/tv", lambda: open_app_window("https://www.youtube.com/tv")),
            ("🎌", "Shinchan TV", "All seasons & episodes", lambda: a.show("shinchan")),
            ("⚙️", "Settings", "Playlist, VLC, App Loader", lambda: a.show("settings")),
        ]
        for ap in a.cfg["apps"]:
            items.append(("🚀", ap["name"], "App shortcut", lambda t=ap["target"]: open_target(t)))
        self.tiles = []
        for n, (ic, ti, su, cmd) in enumerate(items):
            t = Tile(self.grid_frame, ic, ti, su, cmd)
            t.grid(row=n // self.COLS, column=n % self.COLS, padx=14, pady=14)
            self.tiles.append(t)
        self.idx = min(self.idx, len(self.tiles) - 1)
        self.highlight()

    def highlight(self):
        for n, t in enumerate(self.tiles):
            t.focus_on(n == self.idx)

    def on_show(self):
        self.build()

    def on_key(self, e):
        n, c = len(self.tiles), self.COLS
        k = e.keysym
        if k == "Right":
            self.idx = (self.idx + 1) % n
        elif k == "Left":
            self.idx = (self.idx - 1) % n
        elif k == "Down" and self.idx + c < n:
            self.idx += c
        elif k == "Up" and self.idx - c >= 0:
            self.idx -= c
        elif k in ("Return", "space"):
            self.tiles[self.idx].command()
            return
        self.highlight()


# ----------------------------------------------------------------- Live TV
class IPTVScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        self.channels, self.visible, self.cur = [], [], -1
        self.instance = self.player = None
        self.ext_proc = None

        def right(bar):
            tk.Button(bar, text="↻ Reload", command=self.load, bg=PANEL, fg=FG, bd=0,
                      activebackground=CARD_HI, font=("Segoe UI", 10), padx=10).pack(side="right", padx=8)
            self.group_var = tk.StringVar(value="All")
            self.group_cb = ttk.Combobox(bar, textvariable=self.group_var, state="readonly", width=16)
            self.group_cb.pack(side="right", padx=6)
            self.group_cb.bind("<<ComboboxSelected>>", lambda e: self.refilter())
            self.search_var = tk.StringVar()
            self.search_var.trace_add("write", lambda *a: self.refilter())
            self.search = tk.Entry(bar, textvariable=self.search_var, width=22, bg=CARD, fg=FG,
                                   insertbackground=FG, bd=0, font=("Segoe UI", 11))
            self.search.pack(side="right", padx=6, ipady=4)
            tk.Label(bar, text="🔍", bg=PANEL, fg=MUTED).pack(side="right")

        self.header("Live TV", right)
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=PANEL, width=330)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        sb = tk.Scrollbar(left)
        sb.pack(side="right", fill="y")
        self.lb = tk.Listbox(left, yscrollcommand=sb.set, exportselection=False)
        style_listbox(self.lb)
        self.lb.pack(fill="both", expand=True)
        sb.config(command=self.lb.yview)
        self.lb.bind("<Double-Button-1>", lambda e: self.play_selected())
        self.lb.bind("<Return>", lambda e: self.play_selected())

        right_p = tk.Frame(body, bg=BG)
        right_p.pack(side="left", fill="both", expand=True)
        self.video = tk.Frame(right_p, bg="black")
        self.video.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        self.now = tk.Label(right_p, text="Select a channel", bg=BG, fg=FG, font=("Segoe UI", 14, "bold"))
        self.now.pack(anchor="w", padx=14)
        self.status = tk.Label(right_p, text="", bg=BG, fg=MUTED, font=("Segoe UI", 10))
        self.status.pack(anchor="w", padx=14)
        ctl = tk.Frame(right_p, bg=BG)
        ctl.pack(fill="x", padx=12, pady=8)
        for txt, cmd in (("⏮ Prev", self.prev), ("⏹ Stop", self.stop), ("Next ⏭", self.next)):
            tk.Button(ctl, text=txt, command=cmd, bg=CARD, fg=FG, bd=0, padx=14, pady=6,
                      activebackground=CARD_HI, activeforeground="white",
                      font=("Segoe UI", 11)).pack(side="left", padx=4)
        tk.Label(ctl, text="🔊", bg=BG, fg=MUTED).pack(side="left", padx=(20, 2))
        self.vol = tk.Scale(ctl, from_=0, to=100, orient="horizontal", showvalue=False, length=140,
                            bg=BG, fg=FG, troughcolor=CARD, highlightthickness=0,
                            command=self.set_volume)
        self.vol.set(80)
        self.vol.pack(side="left")
        self.mode_lbl = tk.Label(ctl, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.mode_lbl.pack(side="right")

    # ---- data
    def on_show(self):
        self.load()
        self.lb.focus_set()

    def load(self):
        src = self.app.cfg["playlist"]
        try:
            self.channels = parse_m3u(read_source(src))
        except Exception as e:
            self.channels = []
            messagebox.showerror("Playlist", f"Could not load playlist:\n{src}\n\n{e}")
        groups = ["All"] + sorted({c["group"] for c in self.channels})
        self.group_cb["values"] = groups
        if self.group_var.get() not in groups:
            self.group_var.set("All")
        self.refilter()
        embedded = vlc is not None
        self.mode_lbl.config(text="VLC: embedded" if embedded else
                             ("VLC: external window" if find_vlc(self.app.cfg) else "VLC not found — see Settings"))

    def refilter(self):
        q = self.search_var.get().lower().strip()
        g = self.group_var.get()
        self.visible = [c for c in self.channels
                        if (g == "All" or c["group"] == g) and q in c["name"].lower()]
        self.lb.delete(0, "end")
        for n, c in enumerate(self.visible, 1):
            self.lb.insert("end", f"{n:>3}   {c['name']}")
        self.status.config(text=f"{len(self.visible)} channels")
        if self.visible:
            self.lb.selection_set(0)

    # ---- playback
    def play_selected(self):
        sel = self.lb.curselection()
        if sel:
            self.play(sel[0])

    def play(self, i):
        if not (0 <= i < len(self.visible)):
            return
        self.cur = i
        ch = self.visible[i]
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(i)
        self.lb.see(i)
        self.now.config(text=ch["name"])
        self.status.config(text=f"{ch['group']}  •  {i + 1}/{len(self.visible)}")
        if vlc is not None:
            try:
                if self.instance is None:
                    self.instance = vlc.Instance("--network-caching=1500")
                    self.player = self.instance.media_player_new()
                    self.video.update_idletasks()
                    if sys.platform.startswith("win"):
                        self.player.set_hwnd(self.video.winfo_id())
                    else:
                        self.player.set_xwindow(self.video.winfo_id())
                self.player.set_media(self.instance.media_new(ch["url"]))
                self.player.audio_set_volume(int(self.vol.get()))
                self.player.play()
                return
            except Exception as e:
                self.status.config(text=f"Embedded VLC failed ({e}); using external VLC")
        path = find_vlc(self.app.cfg)
        if not path:
            messagebox.showwarning(
                "VLC not found",
                "Install VLC for Windows (videolan.org) or set its path in Settings.")
            return
        # --one-instance without --playlist-enqueue replaces the current item.
        subprocess.Popen([path, "--one-instance", "--no-playlist-enqueue", ch["url"]])

    def set_volume(self, v):
        if self.player is not None:
            try:
                self.player.audio_set_volume(int(float(v)))
            except Exception:
                pass

    def stop(self):
        if self.player is not None:
            self.player.stop()
        self.now.config(text="Stopped")

    def next(self):
        if self.visible:
            self.play((self.cur + 1) % len(self.visible))

    def prev(self):
        if self.visible:
            self.play((self.cur - 1) % len(self.visible))

    def on_hide(self):
        self.stop()

    def on_key(self, e):
        if e.keysym == "Right":
            self.next()
        elif e.keysym == "Left":
            self.prev()
        elif e.keysym == "Down" and self.focus_get() is self.search:
            self.lb.focus_set()


# ----------------------------------------------------------------- Shinchan scraper
EP_X = re.compile(r"(?:^|[/-])(\d{1,2})x(\d{1,4})(?:[/-]|$)", re.I)
EP_SE = re.compile(r"season-?(\d+)-?(?:episode|ep)-?(\d+)", re.I)


class _LinkParser(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.links, self.season = base, [], None
        self._href, self._text, self._ctx = None, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        blob = " ".join(str(a.get(k, "")) for k in ("id", "class", "data-tab", "aria-label"))
        m = re.search(r"season[-_ ]?(\d+)", blob, re.I)
        if m:
            self.season = int(m.group(1))
        ds = str(a.get("data-season", ""))
        if ds.isdigit():
            self.season = int(ds)
        if tag == "a" and a.get("href"):
            self._href = urllib.parse.urljoin(self.base, a["href"])
            self._text, self._ctx = [], self.season

    def handle_data(self, d):
        if self._href:
            self._text.append(d)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split()), self._ctx))
            self._href = None


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data.decode("utf-8", "replace")


def extract_episodes(html, base, default_season=None):
    p = _LinkParser(base)
    p.feed(html)
    host = urllib.parse.urlparse(base).netloc
    eps, season_pages, seen = [], [], set()
    for href, text, ctx in p.links:
        u = urllib.parse.urlparse(href)
        if u.netloc and u.netloc != host:
            continue
        path = u.path
        m = EP_SE.search(path) or EP_X.search(path)
        s = e = None
        if m:
            s, e = int(m.group(1)), int(m.group(2))
        elif "/episode" in path.lower():
            s = ctx or default_season or 1
            tail = re.search(r"(\d+)/?$", path)
            e = int(tail.group(1)) if tail else 0
        elif re.search(r"season", path, re.I) and path.rstrip("/") != urllib.parse.urlparse(base).path.rstrip("/"):
            season_pages.append(href)
            continue
        else:
            continue
        if href in seen:
            continue
        seen.add(href)
        title = text or path.strip("/").rsplit("/", 1)[-1].replace("-", " ").title()
        eps.append({"season": s, "episode": e, "title": title, "url": href})
    return eps, sorted(set(season_pages))


def fetch_all(url, html=None, progress=lambda m: None):
    html = html if html is not None else http_get(url)
    eps, season_pages = extract_episodes(html, url)
    # Season sub-pages (if the series page only links to seasons)
    for n, sp in enumerate(season_pages[:40], 1):
        progress(f"Reading season page {n}/{len(season_pages)}…")
        try:
            m = re.search(r"season-?(\d+)", sp, re.I)
            more, _ = extract_episodes(http_get(sp), sp, int(m.group(1)) if m else None)
            have = {e["url"] for e in eps}
            eps += [e for e in more if e["url"] not in have]
        except Exception:
            continue
    seasons = {}
    for e in sorted(eps, key=lambda x: (x["season"] or 1, x["episode"] or 0)):
        seasons.setdefault(e["season"] or 1, []).append(e)
    return seasons


# ----------------------------------------------------------------- Shinchan TV
class ShinchanScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        self.seasons, self.cur_season = {}, None

        def right(bar):
            tk.Button(bar, text="↻ Fetch", command=self.fetch, bg=CARD_HI, fg="white", bd=0,
                      font=("Segoe UI", 10, "bold"), padx=12, pady=4).pack(side="right", padx=8)
            tk.Button(bar, text="📂 Load saved page", command=self.load_html, bg=PANEL, fg=FG, bd=0,
                      font=("Segoe UI", 10), padx=8).pack(side="right")
            tk.Button(bar, text="🌐 Open site", command=lambda: webbrowser.open(SHINCHAN_URL),
                      bg=PANEL, fg=FG, bd=0, font=("Segoe UI", 10), padx=8).pack(side="right")

        self.header("Shinchan TV", right)
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        l = tk.Frame(body, bg=PANEL, width=200)
        l.pack(side="left", fill="y")
        l.pack_propagate(False)
        tk.Label(l, text="SEASONS", bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=12, pady=8)
        self.sl = tk.Listbox(l, exportselection=False)
        style_listbox(self.sl)
        self.sl.pack(fill="both", expand=True)
        self.sl.bind("<<ListboxSelect>>", lambda e: self.pick_season())
        r = tk.Frame(body, bg=BG)
        r.pack(side="left", fill="both", expand=True)
        tk.Label(r, text="EPISODES  (Enter / double-click opens in your browser)", bg=BG, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=12, pady=8)
        sb = tk.Scrollbar(r)
        sb.pack(side="right", fill="y")
        self.el = tk.Listbox(r, yscrollcommand=sb.set, exportselection=False)
        style_listbox(self.el)
        self.el.pack(fill="both", expand=True, padx=(12, 0))
        sb.config(command=self.el.yview)
        self.el.bind("<Return>", lambda e: self.open_ep())
        self.el.bind("<Double-Button-1>", lambda e: self.open_ep())
        self.status = tk.Label(self, text="", bg=PANEL, fg=MUTED, anchor="w", font=("Segoe UI", 10))
        self.status.pack(fill="x", side="bottom", ipady=4)

    def on_show(self):
        if not self.seasons:
            try:
                with open(SHINCHAN_CACHE, "r", encoding="utf-8") as f:
                    self.seasons = {int(k): v for k, v in json.load(f).items()}
                self.status.config(text="Loaded from cache — press Fetch to refresh")
            except Exception:
                self.fetch()
        self.refill()

    def fetch(self, html=None):
        self.status.config(text="Fetching seasons and episodes…")

        def work():
            try:
                s = fetch_all(SHINCHAN_URL, html, lambda m: self.after(0, self.status.config, {"text": m}))
                self.after(0, self.done, s, None)
            except Exception as e:
                self.after(0, self.done, {}, e)

        threading.Thread(target=work, daemon=True).start()

    def done(self, seasons, err):
        if err or not seasons:
            msg = str(err) if err else "no episode links were recognised on the page"
            self.status.config(text=f"Fetch failed: {msg}")
            messagebox.showwarning(
                "Shinchan TV",
                f"Could not read episodes automatically ({msg}).\n\n"
                "The site may block scripts. Click 'Open site', save the page "
                "(Ctrl+S, 'Webpage, HTML only') and use 'Load saved page'.")
            return
        self.seasons = seasons
        with open(SHINCHAN_CACHE, "w", encoding="utf-8") as f:
            json.dump(seasons, f)
        total = sum(len(v) for v in seasons.values())
        self.status.config(text=f"{len(seasons)} seasons, {total} episodes")
        self.refill()

    def load_html(self):
        p = filedialog.askopenfilename(filetypes=[("HTML", "*.html *.htm"), ("All", "*.*")])
        if p:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                self.fetch(f.read())

    def refill(self):
        self.sl.delete(0, "end")
        for s in sorted(self.seasons):
            self.sl.insert("end", f"Season {s}  ({len(self.seasons[s])})")
        if self.seasons:
            self.sl.selection_set(0)
            self.pick_season()

    def pick_season(self):
        sel = self.sl.curselection()
        if not sel:
            return
        self.cur_season = sorted(self.seasons)[sel[0]]
        self.el.delete(0, "end")
        for e in self.seasons[self.cur_season]:
            self.el.insert("end", f"E{e['episode']:02d}   {e['title']}")
        if self.el.size():
            self.el.selection_set(0)

    def open_ep(self):
        sel = self.el.curselection()
        if sel and self.cur_season is not None:
            webbrowser.open(self.seasons[self.cur_season][sel[0]]["url"])

    def on_key(self, e):
        f = self.focus_get()
        if e.keysym == "Right" and f is self.sl:
            self.el.focus_set()
        elif e.keysym == "Left" and f is self.el:
            self.sl.focus_set()


# ----------------------------------------------------------------- Settings
class SettingsScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        self.header("Settings")
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=30, pady=10)

        def section(t):
            tk.Label(wrap, text=t, bg=BG, fg=FG, font=("Segoe UI", 14, "bold")).pack(anchor="w", pady=(14, 4))

        def row(var, browse):
            f = tk.Frame(wrap, bg=BG)
            f.pack(fill="x")
            e = tk.Entry(f, textvariable=var, bg=CARD, fg=FG, insertbackground=FG, bd=0, font=("Segoe UI", 11))
            e.pack(side="left", fill="x", expand=True, ipady=6)
            tk.Button(f, text="Browse…", command=browse, bg=PANEL, fg=FG, bd=0, padx=10).pack(side="left", padx=6)

        self.pl, self.vp = tk.StringVar(), tk.StringVar()
        section("IPTV playlist (file path or http URL to .m3u)")
        row(self.pl, lambda: self.browse(self.pl, [("Playlist", "*.m3u *.m3u8"), ("All", "*.*")]))
        section("VLC executable (auto-detected if empty)")
        row(self.vp, lambda: self.browse(self.vp, [("vlc.exe", "*.exe")]))
        btns = tk.Frame(wrap, bg=BG)
        btns.pack(anchor="w", pady=8)
        self.fs = tk.BooleanVar()
        tk.Checkbutton(btns, text="Start fullscreen", variable=self.fs, bg=BG, fg=FG,
                       selectcolor=CARD, activebackground=BG, activeforeground=FG).pack(side="left")
        tk.Button(btns, text="💾 Save", command=self.save, bg=CARD_HI, fg="white", bd=0,
                  font=("Segoe UI", 10, "bold"), padx=14, pady=4).pack(side="left", padx=14)
        tk.Button(btns, text="Desktop shortcut for SmartOS", command=self.desktop_shortcut,
                  bg=PANEL, fg=FG, bd=0, padx=10, pady=4).pack(side="left")

        section("App Loader — shortcuts appear on the Home screen")
        af = tk.Frame(wrap, bg=BG)
        af.pack(fill="both", expand=True)
        self.al = tk.Listbox(af, height=6, exportselection=False)
        style_listbox(self.al)
        self.al.pack(side="left", fill="both", expand=True)
        ab = tk.Frame(af, bg=BG)
        ab.pack(side="left", padx=8, anchor="n")
        for txt, cmd in (("➕ Add program…", self.add_program), ("🔗 Add website…", self.add_link),
                         ("▶ Launch", self.launch), ("🗑 Remove", self.remove)):
            tk.Button(ab, text=txt, command=cmd, bg=PANEL, fg=FG, bd=0, padx=12, pady=5,
                      anchor="w", width=16).pack(pady=2)

    def on_show(self):
        c = self.app.cfg
        self.pl.set(c["playlist"])
        self.vp.set(c["vlc_path"])
        self.fs.set(c["fullscreen"])
        self.refill()

    def refill(self):
        self.al.delete(0, "end")
        for a in self.app.cfg["apps"]:
            self.al.insert("end", f"{a['name']}   —   {a['target']}")

    def browse(self, var, types):
        p = filedialog.askopenfilename(filetypes=types)
        if p:
            var.set(p)

    def save(self):
        c = self.app.cfg
        c["playlist"] = self.pl.get().strip() or DEFAULT_PLAYLIST
        c["vlc_path"] = self.vp.get().strip()
        c["fullscreen"] = bool(self.fs.get())
        save_config(c)
        messagebox.showinfo("SmartOS", "Settings saved.")

    def add_program(self):
        p = filedialog.askopenfilename(title="Choose a program or shortcut",
                                       filetypes=[("Programs / shortcuts", "*.exe *.lnk *.bat"), ("All", "*.*")])
        if p:
            name = os.path.splitext(os.path.basename(p))[0]
            self.app.cfg["apps"].append({"name": name, "target": p})
            save_config(self.app.cfg)
            self.refill()

    def add_link(self):
        url = simpledialog.askstring("Add website", "URL (https://…):", parent=self)
        if url:
            if not re.match(r"^https?://", url, re.I):
                url = "https://" + url
            name = simpledialog.askstring("Add website", "Name:", initialvalue=urllib.parse.urlparse(url).netloc,
                                          parent=self) or url
            self.app.cfg["apps"].append({"name": name, "target": url})
            save_config(self.app.cfg)
            self.refill()

    def launch(self):
        s = self.al.curselection()
        if s:
            open_target(self.app.cfg["apps"][s[0]]["target"])

    def remove(self):
        s = self.al.curselection()
        if s:
            del self.app.cfg["apps"][s[0]]
            save_config(self.app.cfg)
            self.refill()

    def desktop_shortcut(self):
        if not sys.platform.startswith("win"):
            messagebox.showinfo("SmartOS", "Desktop shortcuts are only supported on Windows.")
            return
        target = sys.executable
        args = "" if getattr(sys, "frozen", False) else f'"{os.path.abspath(__file__)}"'
        if not getattr(sys, "frozen", False):
            pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            target = pw if os.path.exists(pw) else sys.executable
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
              "[Environment]::GetFolderPath('Desktop')+'\\SmartOS.lnk');"
              f"$s.TargetPath='{target}';$s.Arguments='{args}';"
              f"$s.WorkingDirectory='{os.path.dirname(os.path.abspath(__file__))}';$s.Save()")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
            messagebox.showinfo("SmartOS", "Shortcut created on your Desktop.")
        except Exception as e:
            messagebox.showerror("SmartOS", f"Could not create shortcut:\n{e}")


# ----------------------------------------------------------------- App
class SmartOS(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SmartOS")
        self.geometry("1280x760")
        self.minsize(960, 600)
        self.configure(bg=BG)
        self.cfg = load_config()
        self.fullscreen = bool(self.cfg["fullscreen"])
        self.attributes("-fullscreen", self.fullscreen)
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("TCombobox", fieldbackground=CARD, background=PANEL, foreground=FG)
        self.screens = {"home": HomeScreen(self), "iptv": IPTVScreen(self),
                        "shinchan": ShinchanScreen(self), "settings": SettingsScreen(self)}
        self.current = None
        self.bind_all("<Key>", self.dispatch)
        self.bind("<F11>", self.toggle_fs)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.show("home")

    def show(self, name):
        if self.current:
            self.current.on_hide()
            self.current.pack_forget()
        self.current = self.screens[name]
        self.current.pack(fill="both", expand=True)
        self.current.on_show()
        self.focus_set()

    def home(self):
        self.show("home")

    def toggle_fs(self, e=None):
        self.fullscreen = not self.fullscreen
        self.attributes("-fullscreen", self.fullscreen)

    def dispatch(self, e):
        if e.keysym == "F11":
            return
        f = self.focus_get()
        typing = isinstance(f, (tk.Entry, ttk.Combobox))
        if e.keysym == "Escape":
            if self.fullscreen and self.current is self.screens["home"]:
                self.toggle_fs()
            else:
                self.home()
            return
        if typing and e.keysym not in ("Down", "Return"):
            return
        self.current.on_key(e)

    def quit_app(self):
        try:
            self.screens["iptv"].stop()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    SmartOS().mainloop()
