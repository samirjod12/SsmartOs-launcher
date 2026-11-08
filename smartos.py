"""
SmartOS - TV-OS style launcher for Windows (PyQt6).

  * Live TV      - IPTV (.m3u) player, VLC engine embedded, channel keys, fullscreen
  * YouTube TV   - runs INSIDE the app with a built-in ad blocker
  * Shinchan TV  - S1..S15 x E1..E52 list, stream is extracted in the background and
                   played natively; if that fails the page opens in the in-app browser
  * Settings     - playlist, referer/user-agent, ad blocker, episode limits, App Loader

Needs: pip install PyQt6 PyQt6-WebEngine python-vlc   and VLC (64-bit) installed.
Keys: arrows move, Enter open/play, Esc back, F11 fullscreen, F = video fullscreen,
      Live TV: Left/Right previous/next channel.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import gzip

# ================================================================ PURE HELPERS (no Qt)
APPDIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SmartOS")
os.makedirs(APPDIR, exist_ok=True)
CONFIG_FILE = os.path.join(APPDIR, "config.json")
DEFAULT_PLAYLIST = os.path.join(APPDIR, "playlist.m3u")
CACHE_FILE = os.path.join(APPDIR, "shinchan_media_cache.json")
EXTRA_ADS_FILE = os.path.join(APPDIR, "adblock_extra.txt")
SHINCHAN_BASE = "https://watchanimeworld.one/episode/shinchan-{s}x{e}/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SAMPLE_PLAYLIST = """#EXTM3U
#EXTINF:-1 group-title="Kids",Sony Yay 560
https://livetv.shamon-cable.net:8443/sony_yay/index.m3u8
#EXTINF:-1 group-title="Kids",Cartoon Network
https://drk6xq0vhn.gpcdn.net/live/cn_sd_abr/index.m3u8
"""

AD_DOMAINS = set("""
doubleclick.net googlesyndication.com googleadservices.com adservice.google.com
pagead2.googlesyndication.com google-analytics.com googletagservices.com 2mdn.net
adnxs.com taboola.com outbrain.com popads.net popcash.net propellerads.com exoclick.com
juicyads.com trafficjunky.com adsterra.com hilltopads.net clickadu.com onclickads.net
admaven.com ad-maven.com mgid.com revcontent.com adcash.com yllix.com a-ads.com
histats.com betteradsystem.com richpush.co monetag.com profitableratecpm.com
highperformanceformat.com effectivegatecpm.com adskeeper.com zedo.com criteo.com
criteo.net scorecardresearch.com amazon-adsystem.com adsafeprotected.com moatads.com
rubiconproject.com pubmatic.com openx.net casalemedia.com smartadserver.com
tsyndicate.com trafficstars.com adform.net adsrvr.org bidswitch.net contextweb.com
facebook.net connect.facebook.net ads.youtube.com yandex.ru mc.yandex.ru
popunderjs.com pushame.com notifpush.com pushprofit.net clksite.com
""".split())
AD_PATH_HINTS = ("/pagead/", "/api/stats/ads", "/get_midroll_", "/ptracking")


def load_config():
    cfg = {"playlist": DEFAULT_PLAYLIST, "vlc_path": "", "apps": [], "fullscreen": False,
           "referer": "", "user_agent": "", "adblock": True,
           "shinchan_seasons": 15, "shinchan_eps": 52, "shinchan_overrides": ""}
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


def is_url(s):
    return bool(re.match(r"^https?://", s or "", re.I))


def host_is_ad(host, extra=frozenset()):
    parts = host.lower().split(".")
    for i in range(len(parts) - 1):
        d = ".".join(parts[i:])
        if d in AD_DOMAINS or d in extra:
            return True
    return False


def load_extra_ads():
    try:
        with open(EXTRA_ADS_FILE, "r", encoding="utf-8") as f:
            return frozenset(l.strip().lower() for l in f
                             if l.strip() and not l.startswith("#"))
    except Exception:
        return frozenset()


_OPT_MAP = {"referer": "http-referrer", "referrer": "http-referrer",
            "user-agent": "http-user-agent", "useragent": "http-user-agent"}


def parse_m3u(text):
    channels, info, opts = [], None, {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            grp = re.search(r'group-title="([^"]*)"', line)
            name = line.rsplit(",", 1)[-1].strip() if "," in line else "Channel"
            info = {"name": name, "group": grp.group(1) if grp else "Other", "opts": {}}
        elif line.upper().startswith("#EXTVLCOPT:"):
            k, _, v = line[11:].partition("=")
            opts[k.strip().lower()] = v.strip()
        elif not line.startswith("#"):
            if info is None:
                info = {"name": line.rsplit("/", 1)[-1], "group": "Other", "opts": {}}
            url = line
            if "|" in url:  # kodi style: url|Referer=..&User-Agent=..
                url, _, tail = url.partition("|")
                for pair in tail.split("&"):
                    k, _, v = pair.partition("=")
                    k = _OPT_MAP.get(k.strip().lower())
                    if k:
                        opts[k] = urllib.parse.unquote(v)
            info["url"] = url
            info["opts"].update(opts)
            channels.append(info)
            info, opts = None, {}
    return channels


def read_source(src):
    if is_url(src):
        req = urllib.request.Request(src, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def http_get(url, referer=None, timeout=20):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        h["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data.decode("utf-8", "replace")


def probe(url, referer=None, ua=None, timeout=8):
    """Explain why a stream does not open (shown in the player)."""
    h = {"User-Agent": ua or UA}
    if referer:
        h["Referer"] = referer
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return f"The server answered HTTP {r.status}, so it is reachable; VLC may not support the stream format."
    except urllib.error.HTTPError as e:
        hint = {401: "needs a login/token", 403: "refuses this app (needs a Referer/User-Agent, a token, or only works on the provider's own network)",
                404: "the stream address no longer exists"}.get(e.code, "the server rejected the request")
        return f"The server answered HTTP {e.code}: {hint}."
    except Exception as e:
        return f"Could not reach the server ({e}). It may be offline, or only reachable from the provider's own network."


MEDIA_RE = re.compile(r'https?:(?:\\?/){2}(?:[^"\'\s<>\\]|\\/)+?\.(?:m3u8|mp4)(?:\?(?:[^"\'\s<>\\]|\\/)*)?', re.I)
IFRAME_RE = re.compile(r'<iframe[^>]+?(?:data-src|src)=["\']([^"\']+)', re.I)


def find_media(html):
    out = []
    for m in MEDIA_RE.finditer(html):
        u = m.group(0).replace("\\/", "/")
        if not host_is_ad(urllib.parse.urlparse(u).netloc) and u not in out:
            out.append(u)
    return out


def scan_page(url):
    """Stage A: look for a media URL in the page or its embedded players (plain HTTP)."""
    html = http_get(url)
    media = find_media(html)
    if media:
        return media[0], url
    frames = []
    for src in IFRAME_RE.findall(html):
        full = urllib.parse.urljoin(url, src.replace("&amp;", "&"))
        if is_url(full) and not host_is_ad(urllib.parse.urlparse(full).netloc) and full not in frames:
            frames.append(full)
    for fr in frames[:6]:
        try:
            media = find_media(http_get(fr, referer=url))
        except Exception:
            continue
        if media:
            return media[0], fr
    return None


def parse_overrides(text):
    out = {}
    for part in re.split(r"[,\s;]+", text or ""):
        m = re.match(r"^(\d+)=(\d+)$", part)
        if m:
            out[int(m.group(1))] = int(m.group(2))
    return out


def ep_count(cfg, season):
    return parse_overrides(cfg.get("shinchan_overrides", "")).get(season, int(cfg["shinchan_eps"]))


# ================================================================ QT
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--autoplay-policy=no-user-gesture-required")

from PyQt6.QtCore import Qt, QUrl, QTimer, pyqtSignal, QObject, QEvent
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QStackedWidget, QStackedLayout,
                             QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton, QListWidget,
                             QLineEdit, QComboBox, QSlider, QCheckBox, QFileDialog, QMessageBox,
                             QInputDialog, QSpinBox, QFrame, QScrollArea)
from PyQt6.QtWebEngineCore import (QWebEngineUrlRequestInterceptor, QWebEngineProfile, QWebEnginePage,
                                   QWebEngineSettings, QWebEngineScript)
from PyQt6.QtWebEngineWidgets import QWebEngineView

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PyQt6.QtMultimediaWidgets import QVideoWidget
    HAVE_QTMM = True
except Exception:
    HAVE_QTMM = False

try:
    import vlc
except Exception:
    vlc = None

QSS = """
QWidget{background:#0b0f17;color:#e8ecf4;font-family:'Segoe UI';font-size:14px}
QPushButton{background:#151b28;border:0;padding:8px 14px;border-radius:8px}
QPushButton:hover,QPushButton:focus{background:#2a6df4}
QLineEdit,QComboBox,QSpinBox{background:#151b28;border:0;padding:6px;border-radius:6px}
QListWidget{background:#10151f;border:0;outline:0}
QListWidget::item{padding:8px}
QListWidget::item:selected{background:#2a6df4;color:white}
QSlider::groove:horizontal{height:4px;background:#2a3142}
QSlider::handle:horizontal{background:#2a6df4;width:12px;margin:-5px 0;border-radius:6px}
QLabel#muted{color:#8a94a8}
QWidget#bar{background:#10151f}
QFrame#tile{background:#151b28;border-radius:14px;border:3px solid transparent}
QFrame#tile:focus{background:#2a6df4;border:3px solid #8fb2ff}
QFrame#tile QLabel{background:transparent}
QWidget#video{background:black}
QLabel#status{background:black;color:#e8ecf4;font-size:16px;padding:30px}
"""

# ---------------------------------------------------------------- ad blocker / scripts
class AdBlocker(QWebEngineUrlRequestInterceptor):
    media_seen = pyqtSignal(str, str)   # media url, first-party page url

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled = True
        self.capture = False
        self.blocked = 0
        self.extra = load_extra_ads()

    def interceptRequest(self, info):
        url = info.requestUrl()
        s = url.toString()
        if self.enabled and (host_is_ad(url.host(), self.extra) or any(h in s for h in AD_PATH_HINTS)):
            info.block(True)
            self.blocked += 1
            return
        if self.capture:
            path = url.path().lower()
            if path.endswith((".m3u8", ".mp4", ".mpd")) or ".m3u8" in path:
                self.media_seen.emit(s, info.firstPartyUrl().toString())


COSMETIC_JS = r"""
(function(){
 try{
  var css='ins.adsbygoogle,.adsbygoogle,div[id^="google_ads"],iframe[src*="doubleclick"],iframe[src*="googlesyndication"],'
   +'.ytp-ad-module,.ytp-ad-overlay-container,ytd-ad-slot-renderer,ytd-promoted-sparkles-web-renderer,#player-ads,.video-ads,'
   +'a[href*="popads"],a[href*="exoclick"],div[class*="popunder" i]{display:none!important}';
  var st=document.createElement('style'); st.textContent=css;
  (document.head||document.documentElement).appendChild(st);
 }catch(e){}
 if(/youtube\.com$/.test(location.hostname)){
  setInterval(function(){
   try{
    var v=document.querySelector('video');
    var ad=document.querySelector('.ad-showing,.ytp-ad-player-overlay,.ytp-ad-text');
    if(v&&ad){v.__sm=1;v.muted=true;v.playbackRate=16;
      if(isFinite(v.duration)&&v.duration>0){v.currentTime=v.duration;}}
    else if(v&&v.__sm){v.__sm=0;v.muted=false;v.playbackRate=1;}
    var sk=document.querySelector('.ytp-ad-skip-button,.ytp-ad-skip-button-modern,.ytp-skip-ad-button');
    if(sk)sk.click();
   }catch(e){}
  },300);
 }
})();
"""

AUTOPLAY_JS = r"""
(function(){
 setInterval(function(){
  try{
   document.querySelectorAll('video').forEach(function(v){v.muted=true;var p=v.play();if(p&&p.catch)p.catch(function(){});});
   ['.jw-icon-display','.vjs-big-play-button','.plyr__control--overlaid','.play-button','.btn-play',
    '#play','.fp-play','button[aria-label*="Play" i]','[class*="play-btn" i]','[class*="PlayButton" i]']
   .forEach(function(s){var e=document.querySelector(s);if(e)e.click();});
  }catch(e){}
 },1200);
})();
"""


def make_script(name, src):
    s = QWebEngineScript()
    s.setName(name)
    s.setSourceCode(src)
    s.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
    s.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    s.setRunsOnSubFrames(True)
    return s


class SmartPage(QWebEnginePage):
    """Page that blocks pop-up windows and injects the cosmetic ad filter."""

    def __init__(self, profile, parent=None, autoplay=False):
        super().__init__(profile, parent)
        self.scripts().insert(make_script("cosmetic", COSMETIC_JS))
        if autoplay:
            self.scripts().insert(make_script("autoplay", AUTOPLAY_JS))
        st = self.settings()
        st.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        st.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)

    def createWindow(self, _type):
        return None  # no pop-ups / new tabs

    def javaScriptConsoleMessage(self, *a):
        pass


# ---------------------------------------------------------------- shared widgets
class Screen(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def on_show(self): pass
    def on_hide(self): pass
    def on_key(self, ev): return False
    def handle_escape(self): return False


def make_header(app, title, extras=None):
    bar = QWidget()
    bar.setObjectName("bar")
    lay = QHBoxLayout(bar)
    lay.setContentsMargins(10, 6, 10, 6)
    home = QPushButton("⬅ Home")
    home.clicked.connect(app.go_home)
    lay.addWidget(home)
    t = QLabel(title)
    t.setStyleSheet("font-size:20px;font-weight:bold;background:transparent")
    lay.addWidget(t)
    lay.addStretch(1)
    for w in extras or []:
        lay.addWidget(w)
    return bar, lay


class Tile(QFrame):
    def __init__(self, icon, title, sub, command):
        super().__init__()
        self.setObjectName("tile")
        self.setFixedSize(230, 170)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.command = command
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for text, css in ((icon, "font-size:40px"), (title, "font-size:17px;font-weight:bold"),
                          (sub, "font-size:11px;color:#c3cbe0")):
            lb = QLabel(text)
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lb.setWordWrap(True)
            lb.setStyleSheet(css)
            lay.addWidget(lb)

    def mousePressEvent(self, e):
        self.command()


# ---------------------------------------------------------------- video player widget
class VideoPlayer(QWidget):
    prev_clicked = pyqtSignal()
    next_clicked = pyqtSignal()
    fullscreen_clicked = pyqtSignal()
    failed = pyqtSignal(str)
    started = pyqtSignal()
    _probe_done = pyqtSignal(str)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.backend = None
        self.inst = self.player = self.qplayer = self.qaudio = self.qvideo = None
        self.url, self.opts = None, {}
        self._t0, self._playing, self._failed = 0, False, False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        holder = QWidget()
        self.stack = QStackedLayout(holder)
        self.surface = QWidget()
        self.surface.setObjectName("video")
        self.surface.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        self.stack.addWidget(self.surface)   # 0
        self.stack.addWidget(self.status)    # 1
        self.stack.setCurrentIndex(1)
        lay.addWidget(holder, 1)

        self.controls = QWidget()
        self.controls.setObjectName("bar")
        c = QHBoxLayout(self.controls)
        c.setContentsMargins(8, 4, 8, 4)

        def btn(text, fn, tip=""):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            c.addWidget(b)
            return b

        btn("⏮", self.prev_clicked.emit, "Previous")
        self.pp = btn("⏯", self.toggle_pause, "Play / pause")
        btn("⏭", self.next_clicked.emit, "Next")
        btn("⏹", self.stop_playback, "Stop")
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.setEnabled(False)
        self.seek.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.seek.sliderReleased.connect(self._seek_to)
        c.addWidget(self.seek, 1)
        self.time_lbl = QLabel("LIVE")
        self.time_lbl.setObjectName("muted")
        c.addWidget(self.time_lbl)
        c.addWidget(QLabel("🔊"))
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(110)
        self.vol.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.vol.valueChanged.connect(self._set_vol)
        c.addWidget(self.vol)
        btn("⛶", self.fullscreen_clicked.emit, "Fullscreen (F)")
        btn("VLC ↗", self.open_external, "Open this stream in the VLC program")
        lay.addWidget(self.controls)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        self.dots = 0
        self.anim = QTimer(self)
        self.anim.timeout.connect(self._animate)
        self._base = ""
        self._probe_done.connect(self._on_probe)

    # ---- status page
    def show_status(self, text, animate=False):
        self._base = text
        self.status.setText(text)
        self.stack.setCurrentIndex(1)
        if animate:
            self.anim.start(400)
        else:
            self.anim.stop()

    def _animate(self):
        self.dots = (self.dots + 1) % 4
        self.status.setText(self._base + "\n\n" + "●" * self.dots + "○" * (3 - self.dots))

    # ---- engine
    def _ensure_backend(self):
        if self.backend:
            return True
        if vlc is not None:
            try:
                self.inst = vlc.Instance(["--no-video-title-show", "--network-caching=1500", "--http-reconnect"])
                if self.inst:
                    self.player = self.inst.media_player_new()
                    wid = int(self.surface.winId())
                    if sys.platform.startswith("win"):
                        self.player.set_hwnd(wid)
                    elif sys.platform == "darwin":
                        self.player.set_nsobject(wid)
                    else:
                        self.player.set_xwindow(wid)
                    self.player.video_set_mouse_input(False)
                    self.player.video_set_key_input(False)
                    self.backend = "vlc"
                    return True
            except Exception:
                pass
        if HAVE_QTMM:
            self.qplayer = QMediaPlayer(self)
            self.qaudio = QAudioOutput(self)
            self.qplayer.setAudioOutput(self.qaudio)
            self.qvideo = QVideoWidget()
            self.qplayer.setVideoOutput(self.qvideo)
            self.stack.addWidget(self.qvideo)  # 2
            self.qplayer.errorOccurred.connect(lambda _e, s: self._fail(s or "Playback error"))
            self.backend = "qt"
            return True
        return False

    def play(self, url, opts=None):
        self.stop_playback(clear=False)
        self.url, self.opts = url, dict(opts or {})
        self._t0, self._playing, self._failed = time.time(), False, False
        self.show_status("Loading stream…", animate=True)
        if not self._ensure_backend():
            self._fail("No video engine found. Install VLC (64-bit, videolan.org) and restart SmartOS.")
            return
        self._set_vol(self.vol.value())
        if self.backend == "vlc":
            m = self.inst.media_new(url)
            for k, v in self.opts.items():
                m.add_option(f":{k}={v}")
            self.player.set_media(m)
            self.player.play()
        else:
            self.qplayer.setSource(QUrl(url))
            self.qplayer.play()

    def stop_playback(self, clear=True):
        try:
            if self.backend == "vlc":
                self.player.stop()
            elif self.backend == "qt":
                self.qplayer.stop()
        except Exception:
            pass
        self._playing = False
        if clear:
            self.url = None
            self.show_status("Stopped")

    def toggle_pause(self):
        try:
            if self.backend == "vlc":
                self.player.pause()
            elif self.backend == "qt":
                if self.qplayer.isPlaying():
                    self.qplayer.pause()
                else:
                    self.qplayer.play()
        except Exception:
            pass

    def _set_vol(self, v):
        try:
            if self.backend == "vlc":
                self.player.audio_set_volume(int(v))
            elif self.backend == "qt":
                self.qaudio.setVolume(v / 100)
        except Exception:
            pass

    def _seek_to(self):
        v = self.seek.value() / 1000
        try:
            if self.backend == "vlc":
                self.player.set_position(v)
            elif self.backend == "qt":
                self.qplayer.setPosition(int(v * self.qplayer.duration()))
        except Exception:
            pass

    def _tick(self):
        if not self.url or self._failed:
            return
        pos = length = 0
        state = None
        try:
            if self.backend == "vlc":
                st = self.player.get_state()
                state = ("playing" if st == vlc.State.Playing else
                         "error" if st == vlc.State.Error else
                         "ended" if st == vlc.State.Ended else
                         "paused" if st == vlc.State.Paused else "opening")
                length = self.player.get_length()
                pos = self.player.get_time()
            elif self.backend == "qt":
                st = self.qplayer.playbackState()
                state = "playing" if st == QMediaPlayer.PlaybackState.PlayingState else "opening"
                length, pos = self.qplayer.duration(), self.qplayer.position()
        except Exception:
            return
        if state == "playing" and not self._playing:
            self._playing = True
            self.anim.stop()
            self.stack.setCurrentIndex(0 if self.backend == "vlc" else 2)
            self.started.emit()
        if state == "error":
            self._fail("The video engine could not play this stream.")
        elif not self._playing and state == "opening" and time.time() - self._t0 > 25:
            self._fail("No response from the stream after 25 seconds.")
        elif state == "ended" and length > 0:
            self.next_clicked.emit()
        if self._playing and length > 0:
            self.seek.setEnabled(True)
            if not self.seek.isSliderDown():
                self.seek.setValue(int(pos / length * 1000))
            self.time_lbl.setText(f"{self._fmt(pos)} / {self._fmt(length)}")
        elif self._playing:
            self.seek.setEnabled(False)
            self.time_lbl.setText("LIVE")

    @staticmethod
    def _fmt(ms):
        s = max(0, int(ms // 1000))
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

    def _fail(self, msg):
        if self._failed:
            return
        self._failed = True
        self.stop_playback(clear=False)
        self.show_status(f"⚠  {msg}")
        if self.url and is_url(self.url):
            url, ref, ua = self.url, self.opts.get("http-referrer"), self.opts.get("http-user-agent")
            threading.Thread(target=lambda: self._probe_done.emit(probe(url, ref, ua)), daemon=True).start()
        self.failed.emit(msg)

    def _on_probe(self, text):
        if self._failed:
            self.status.setText(self._base + "\n\n" + text)

    def open_external(self):
        path = find_vlc(self.app.cfg)
        if not (self.url and path):
            QMessageBox.information(self, "SmartOS", "VLC program not found or nothing is loaded.")
            return
        cmd = [path, self.url]
        for k, v in self.opts.items():
            cmd.append(f":{k}={v}")
        subprocess.Popen(cmd)

    def shutdown(self):
        self.stop_playback(clear=False)


# ---------------------------------------------------------------- home
class HomeScreen(Screen):
    COLS = 4

    def __init__(self, app):
        super().__init__(app)
        root = QVBoxLayout(self)
        root.setContentsMargins(50, 30, 50, 20)
        top = QHBoxLayout()
        title = QLabel("SmartOS")
        title.setStyleSheet("font-size:34px;font-weight:bold")
        self.clock = QLabel("")
        self.clock.setStyleSheet("font-size:22px;color:#8a94a8")
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(self.clock)
        root.addLayout(top)
        self.greet = QLabel("")
        self.greet.setObjectName("muted")
        root.addWidget(self.greet)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(18)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.grid_host, 1)
        hint = QLabel("← ↑ ↓ → move     Enter open     Esc back     F11 fullscreen")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(hint)
        self.tiles = []
        t = QTimer(self)
        t.timeout.connect(self.tick)
        t.start(15000)
        self.tick()

    def tick(self):
        self.clock.setText(time.strftime("%I:%M %p").lstrip("0"))
        h = int(time.strftime("%H"))
        self.greet.setText(("Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening")
                           + "  —  " + time.strftime("%A, %d %B"))

    def on_show(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        a = self.app
        items = [("📺", "Live TV", "IPTV playlist", lambda: a.show_screen("iptv")),
                 ("▶️", "YouTube TV", "In-app, ad blocker on",
                  lambda: a.open_web("https://www.youtube.com/tv", "YouTube TV", "home")),
                 ("🎌", "Shinchan TV", "15 seasons × 52 episodes", lambda: a.show_screen("shinchan")),
                 ("⚙️", "Settings", "Playlist, ad blocker, App Loader", lambda: a.show_screen("settings"))]
        for ap in a.cfg["apps"]:
            items.append(("🚀", ap["name"], "App shortcut", lambda t=ap["target"], n=ap["name"]: a.launch_app(n, t)))
        self.tiles = []
        for n, (ic, ti, su, cmd) in enumerate(items):
            tile = Tile(ic, ti, su, cmd)
            self.grid.addWidget(tile, n // self.COLS, n % self.COLS)
            self.tiles.append(tile)
        if self.tiles:
            self.tiles[0].setFocus()

    def on_key(self, ev):
        if not self.tiles:
            return False
        fw = QApplication.focusWidget()
        idx = self.tiles.index(fw) if fw in self.tiles else 0
        n, c, k = len(self.tiles), self.COLS, ev.key()
        if k == Qt.Key.Key_Right:
            idx = (idx + 1) % n
        elif k == Qt.Key.Key_Left:
            idx = (idx - 1) % n
        elif k == Qt.Key.Key_Down and idx + c < n:
            idx += c
        elif k == Qt.Key.Key_Up and idx - c >= 0:
            idx -= c
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.tiles[idx].command()
            return True
        else:
            return False
        self.tiles[idx].setFocus()
        return True


# ---------------------------------------------------------------- in-app browser
class WebScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        self.back_to = "home"
        self.view = QWebEngineView()
        self.page = SmartPage(app.profile, self.view)
        self.view.setPage(self.page)
        self.page.fullScreenRequested.connect(self._fs)
        self.shield = QLabel("🛡 0")
        self.shield.setObjectName("muted")
        self.title_lbl = QLabel("")
        back, fwd, rel = QPushButton("◀"), QPushButton("▶"), QPushButton("↻")
        back.clicked.connect(self.view.back)
        fwd.clicked.connect(self.view.forward)
        rel.clicked.connect(self.view.reload)
        self.hdr, lay = make_header(app, "", [back, fwd, rel, self.shield])
        self.title_lbl = self.hdr.findChild(QLabel)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.hdr)
        root.addWidget(self.view, 1)
        self.fs_on = False
        t = QTimer(self)
        t.timeout.connect(lambda: self.shield.setText(f"🛡 {app.adblock.blocked} blocked"))
        t.start(1500)

    def open(self, url, title, back_to):
        self.back_to = back_to
        self.title_lbl.setText(title)
        self.view.load(QUrl(url))

    def on_hide(self):
        self.view.load(QUrl("about:blank"))

    def _fs(self, req):
        req.accept()
        self.fs_on = req.toggleOn()
        self.hdr.setVisible(not self.fs_on)
        self.app.set_fullscreen(self.fs_on or self.app.cfg["fullscreen"])

    def handle_escape(self):
        if self.fs_on:
            self.page.triggerAction(QWebEnginePage.WebAction.ExitFullScreen)
            self.hdr.setVisible(True)
            self.fs_on = False
            self.app.set_fullscreen(self.app.cfg["fullscreen"])
            return True
        self.app.show_screen(self.back_to)
        return True


# ---------------------------------------------------------------- Live TV
class IPTVScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        self.channels, self.visible, self.cur, self.fs = [], [], -1, False
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 Search channels")
        self.search.setFixedWidth(220)
        self.search.textChanged.connect(self.refilter)
        self.group = QComboBox()
        self.group.setFixedWidth(150)
        self.group.currentIndexChanged.connect(self.refilter)
        reload_b = QPushButton("↻ Reload")
        reload_b.clicked.connect(self.load)
        self.hdr, _ = make_header(app, "Live TV", [self.search, self.group, reload_b])
        self.list = QListWidget()
        self.list.setFixedWidth(330)
        self.list.itemActivated.connect(lambda it: self.play(self.list.row(it)))
        self.list.itemDoubleClicked.connect(lambda it: self.play(self.list.row(it)))
        self.player = VideoPlayer(app)
        self.player.prev_clicked.connect(self.prev)
        self.player.next_clicked.connect(self.next)
        self.player.fullscreen_clicked.connect(self.toggle_fs)
        self.now = QLabel("Select a channel")
        self.now.setStyleSheet("font-size:16px;font-weight:bold")
        self.info = QLabel("")
        self.info.setObjectName("muted")
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(10, 8, 10, 8)
        rl.addWidget(self.player, 1)
        rl.addWidget(self.now)
        rl.addWidget(self.info)
        self.right_meta = [self.now, self.info]
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.list)
        body.addWidget(right, 1)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.hdr)
        root.addLayout(body, 1)

    def on_show(self):
        self.load()
        self.list.setFocus()

    def on_hide(self):
        self.player.stop_playback()
        if self.fs:
            self.toggle_fs()

    def load(self):
        src = self.app.cfg["playlist"]
        try:
            self.channels = parse_m3u(read_source(src))
        except Exception as e:
            self.channels = []
            QMessageBox.warning(self, "Playlist", f"Could not load playlist:\n{src}\n\n{e}")
        self.group.blockSignals(True)
        self.group.clear()
        self.group.addItem("All")
        for g in sorted({c["group"] for c in self.channels}):
            self.group.addItem(g)
        self.group.blockSignals(False)
        self.refilter()

    def refilter(self):
        q, g = self.search.text().lower().strip(), self.group.currentText()
        self.visible = [c for c in self.channels if (g in ("All", "") or c["group"] == g) and q in c["name"].lower()]
        self.list.clear()
        for n, c in enumerate(self.visible, 1):
            self.list.addItem(f"{n:>3}   {c['name']}")
        self.info.setText(f"{len(self.visible)} channels")
        if self.visible:
            self.list.setCurrentRow(0)

    def play(self, i):
        if not (0 <= i < len(self.visible)):
            return
        self.cur = i
        ch = self.visible[i]
        self.list.setCurrentRow(i)
        self.now.setText(ch["name"])
        self.info.setText(f"{ch['group']}  •  {i + 1}/{len(self.visible)}")
        opts = dict(ch.get("opts", {}))
        if self.app.cfg["referer"] and "http-referrer" not in opts:
            opts["http-referrer"] = self.app.cfg["referer"]
        if self.app.cfg["user_agent"] and "http-user-agent" not in opts:
            opts["http-user-agent"] = self.app.cfg["user_agent"]
        self.player.play(ch["url"], opts)

    def next(self):
        if self.visible:
            self.play((self.cur + 1) % len(self.visible))

    def prev(self):
        if self.visible:
            self.play((self.cur - 1) % len(self.visible))

    def toggle_fs(self):
        self.fs = not self.fs
        self.hdr.setVisible(not self.fs)
        self.list.setVisible(not self.fs)
        for w in self.right_meta:
            w.setVisible(not self.fs)
        self.app.set_fullscreen(self.fs or self.app.cfg["fullscreen"])

    def handle_escape(self):
        if self.fs:
            self.toggle_fs()
            return True
        return False

    def on_key(self, ev):
        k = ev.key()
        if k == Qt.Key.Key_F:
            self.toggle_fs()
        elif k == Qt.Key.Key_Right:
            self.next()
        elif k == Qt.Key.Key_Left:
            self.prev()
        elif k == Qt.Key.Key_Down and QApplication.focusWidget() is self.search:
            self.list.setFocus()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and QApplication.focusWidget() is self.list:
            self.play(self.list.currentRow())
        elif k == Qt.Key.Key_Space:
            self.player.toggle_pause()
        else:
            return False
        return True


# ---------------------------------------------------------------- stream extractor
class Extractor(QObject):
    progress = pyqtSignal(str)
    found = pyqtSignal(str, str, str)    # page url, media url, referer
    failed = pyqtSignal(str, str)        # page url, reason
    _stage_a = pyqtSignal(str, object)

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.page_url = None
        self.active = False
        self.hits = []
        self.view = None
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self._timed_out)
        self.pick_timer = QTimer(self)
        self.pick_timer.setSingleShot(True)
        self.pick_timer.timeout.connect(self._pick)
        self._stage_a.connect(self._after_a)
        app.adblock.media_seen.connect(self._seen)

    def start(self, url):
        self.cancel()
        self.page_url, self.active, self.hits = url, True, []
        self.progress.emit("Looking for the video stream…")

        def work():
            try:
                res = scan_page(url)
            except Exception:
                res = None
            self._stage_a.emit(url, res)

        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        self.active = False
        self.app.adblock.capture = False
        self.timeout.stop()
        self.pick_timer.stop()
        if self.view:
            self.view.load(QUrl("about:blank"))

    def _after_a(self, url, res):
        if not self.active or url != self.page_url:
            return
        if res:
            self._finish(res[0], res[1])
            return
        self.progress.emit("Loading the page in the background…")
        if self.view is None:
            self.view = QWebEngineView()
            self.view.setPage(SmartPage(self.app.profile, self.view, autoplay=True))
            self.view.resize(640, 360)
            self.view.move(-10000, -10000)
            self.view.show()
        self.app.adblock.capture = True
        self.view.load(QUrl(url))
        self.timeout.start(40000)

    def _seen(self, media, first_party):
        if not self.active:
            return
        self.hits.append((media, first_party or self.page_url))
        if not self.pick_timer.isActive():
            self.pick_timer.start(2500)

    def _pick(self):
        if not self.active or not self.hits:
            return
        best = next((h for h in self.hits if ".m3u8" in h[0].lower()), self.hits[0])
        self._finish(best[0], best[1])

    def _finish(self, media, referer):
        url = self.page_url
        self.cancel()
        self.found.emit(url, media, referer)

    def _timed_out(self):
        if self.active:
            url = self.page_url
            self.cancel()
            self.failed.emit(url, "No stream found in time")


# ---------------------------------------------------------------- Shinchan TV
class ShinchanScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        self.cur = None
        self.fell_back = False
        self.extracted = False
        self.cache = {}
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                self.cache = json.load(f)
        except Exception:
            pass
        self.extractor = Extractor(app)
        self.extractor.progress.connect(lambda t: self.player.show_status(self._title() + "\n\n" + t, True))
        self.extractor.found.connect(self.on_found)
        self.extractor.failed.connect(self.on_extract_failed)

        self.pages = QStackedWidget()
        # --- page 0: lists
        lists = QWidget()
        site = QPushButton("🌐 Open site")
        site.clicked.connect(lambda: app.open_web("https://watchanimeworld.one/series/shinchan/",
                                                  "Shinchan", "shinchan"))
        hdr, _ = make_header(app, "Shinchan TV", [site])
        self.seasons = QListWidget()
        self.seasons.setFixedWidth(220)
        self.seasons.currentRowChanged.connect(self.fill_eps)
        self.eps = QListWidget()
        self.eps.itemActivated.connect(lambda it: self.play_row())
        self.eps.itemDoubleClicked.connect(lambda it: self.play_row())
        hint = QLabel("Enter / double-click an episode. The stream is found in the background and "
                      "plays here; if that fails the page opens in the in-app browser.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.seasons)
        body.addWidget(self.eps, 1)
        ll = QVBoxLayout(lists)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        ll.addWidget(hdr)
        ll.addLayout(body, 1)
        ll.addWidget(hint)
        # --- page 1: player
        pw = QWidget()
        self.player = VideoPlayer(app)
        self.player.prev_clicked.connect(lambda: self.step(-1))
        self.player.next_clicked.connect(lambda: self.step(1))
        self.player.fullscreen_clicked.connect(self.toggle_fs)
        self.player.failed.connect(self.on_player_failed)
        back = QPushButton("⬅ Episodes")
        back.clicked.connect(self.show_lists)
        alt = QPushButton("Open page in browser")
        alt.clicked.connect(self.open_in_browser)
        self.p_hdr, _ = make_header(app, "", [back, alt])
        self.p_title = self.p_hdr.findChild(QLabel)
        pl = QVBoxLayout(pw)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        pl.addWidget(self.p_hdr)
        pl.addWidget(self.player, 1)
        self.pages.addWidget(lists)
        self.pages.addWidget(pw)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.pages)
        self.fs = False

    def _title(self):
        return f"Season {self.cur[0]} · Episode {self.cur[1]}" if self.cur else ""

    def on_show(self):
        row = max(0, self.seasons.currentRow())
        self.seasons.clear()
        for s in range(1, int(self.app.cfg["shinchan_seasons"]) + 1):
            self.seasons.addItem(f"Season {s}   ({ep_count(self.app.cfg, s)} eps)")
        self.seasons.setCurrentRow(min(row, self.seasons.count() - 1))
        if self.pages.currentIndex() == 0:
            self.seasons.setFocus()

    def on_hide(self):
        self.extractor.cancel()
        self.player.stop_playback()
        if self.fs:
            self.toggle_fs()

    def fill_eps(self, row):
        if row < 0:
            return
        s = row + 1
        self.eps.clear()
        for e in range(1, ep_count(self.app.cfg, s) + 1):
            self.eps.addItem(f"S{s} · E{e:02d}   —   shinchan-{s}x{e}")
        self.eps.setCurrentRow(0)

    def play_row(self):
        s, e = self.seasons.currentRow() + 1, self.eps.currentRow() + 1
        if s > 0 and e > 0:
            self.play_ep(s, e)

    def url_for(self, s, e):
        return SHINCHAN_BASE.format(s=s, e=e)

    def play_ep(self, s, e):
        self.cur = (s, e)
        self.fell_back, self.extracted = False, False
        self.pages.setCurrentIndex(1)
        self.p_title.setText(f"Shinchan  ·  S{s} E{e}")
        self.player.stop_playback(clear=False)
        key = f"{s}x{e}"
        c = self.cache.get(key)
        if c and time.time() - c["ts"] < 6 * 3600:
            self.extracted = True
            self.player.play(c["media"], {"http-referrer": c["ref"], "http-user-agent": UA})
            return
        self.player.show_status(self._title() + "\n\nStarting…", True)
        self.extractor.start(self.url_for(s, e))

    def on_found(self, page_url, media, ref):
        if not self.cur or page_url != self.url_for(*self.cur):
            return
        self.extracted = True
        self.cache[f"{self.cur[0]}x{self.cur[1]}"] = {"media": media, "ref": ref, "ts": time.time()}
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cache, f)
        except Exception:
            pass
        self.player.play(media, {"http-referrer": ref, "http-user-agent": UA})

    def on_extract_failed(self, page_url, reason):
        self.fallback(reason)

    def on_player_failed(self, msg):
        if self.extracted and self.cur:
            self.cache.pop(f"{self.cur[0]}x{self.cur[1]}", None)
        self.fallback(msg)

    def fallback(self, reason):
        if self.fell_back or not self.cur:
            return
        self.fell_back = True
        self.player.show_status(f"{self._title()}\n\nCouldn't play it natively ({reason}).\n"
                                "Opening the page in the in-app browser…")
        url = self.url_for(*self.cur)
        QTimer.singleShot(1800, lambda: self.app.open_web(url, "Shinchan " + self._title(), "shinchan")
                          if self.pages.currentIndex() == 1 and self.app.current is self else None)

    def open_in_browser(self):
        if self.cur:
            self.extractor.cancel()
            self.fell_back = True
            self.app.open_web(self.url_for(*self.cur), "Shinchan " + self._title(), "shinchan")

    def step(self, d):
        if not self.cur:
            return
        s, e = self.cur
        e += d
        if e < 1:
            if s <= 1:
                return
            s, e = s - 1, ep_count(self.app.cfg, s - 1)
        elif e > ep_count(self.app.cfg, s):
            if s >= int(self.app.cfg["shinchan_seasons"]):
                return
            s, e = s + 1, 1
        self.play_ep(s, e)

    def show_lists(self):
        self.extractor.cancel()
        self.player.stop_playback()
        if self.fs:
            self.toggle_fs()
        self.pages.setCurrentIndex(0)
        self.eps.setFocus()

    def toggle_fs(self):
        self.fs = not self.fs
        self.p_hdr.setVisible(not self.fs)
        self.app.set_fullscreen(self.fs or self.app.cfg["fullscreen"])

    def handle_escape(self):
        if self.fs:
            self.toggle_fs()
            return True
        if self.pages.currentIndex() == 1:
            self.show_lists()
            return True
        return False

    def on_key(self, ev):
        k = ev.key()
        if self.pages.currentIndex() == 1:
            if k == Qt.Key.Key_F:
                self.toggle_fs()
            elif k == Qt.Key.Key_Right:
                self.step(1)
            elif k == Qt.Key.Key_Left:
                self.step(-1)
            elif k == Qt.Key.Key_Space:
                self.player.toggle_pause()
            else:
                return False
            return True
        fw = QApplication.focusWidget()
        if k == Qt.Key.Key_Right and fw is self.seasons:
            self.eps.setFocus()
        elif k == Qt.Key.Key_Left and fw is self.eps:
            self.seasons.setFocus()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and fw is self.eps:
            self.play_row()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and fw is self.seasons:
            self.eps.setFocus()
        else:
            return False
        return True


# ---------------------------------------------------------------- Settings
class SettingsScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        hdr, _ = make_header(app, "Settings")
        inner = QWidget()
        form = QVBoxLayout(inner)
        form.setContentsMargins(30, 10, 30, 20)

        def section(t):
            lb = QLabel(t)
            lb.setStyleSheet("font-size:16px;font-weight:bold;margin-top:12px")
            form.addWidget(lb)

        def path_row(edit, filt):
            r = QHBoxLayout()
            r.addWidget(edit, 1)
            b = QPushButton("Browse…")
            b.clicked.connect(lambda: self._browse(edit, filt))
            r.addWidget(b)
            form.addLayout(r)

        self.pl, self.vp = QLineEdit(), QLineEdit()
        self.ref, self.ua = QLineEdit(), QLineEdit()
        self.ref.setPlaceholderText("e.g. https://example.com/   (only if your streams need it)")
        self.ua.setPlaceholderText("empty = VLC default")
        section("IPTV playlist (file or http URL, .m3u)")
        path_row(self.pl, "Playlists (*.m3u *.m3u8);;All (*.*)")
        section("VLC program (only for the 'VLC ↗' button; auto-detected)")
        path_row(self.vp, "vlc.exe (*.exe)")
        section("IPTV extra request headers (applied to every channel)")
        form.addWidget(self.ref)
        form.addWidget(self.ua)
        section("Ad blocker")
        self.adb = QCheckBox("Block ads, trackers and pop-ups in the in-app browser")
        form.addWidget(self.adb)
        form.addWidget(QLabel(f"Extra domains to block: one per line in  {EXTRA_ADS_FILE}"))
        section("Shinchan TV limits")
        r = QHBoxLayout()
        self.sn, self.en = QSpinBox(), QSpinBox()
        self.sn.setRange(1, 60)
        self.en.setRange(1, 500)
        r.addWidget(QLabel("Seasons"))
        r.addWidget(self.sn)
        r.addWidget(QLabel("Episodes per season"))
        r.addWidget(self.en)
        r.addStretch(1)
        form.addLayout(r)
        self.ov = QLineEdit()
        self.ov.setPlaceholderText("Seasons with a different count, e.g.  3=48, 9=50")
        form.addWidget(self.ov)
        section("General")
        self.fsb = QCheckBox("Start fullscreen")
        form.addWidget(self.fsb)
        r = QHBoxLayout()
        save = QPushButton("💾 Save")
        save.clicked.connect(self.save)
        sc = QPushButton("Desktop shortcut for SmartOS")
        sc.clicked.connect(self.desktop_shortcut)
        r.addWidget(save)
        r.addWidget(sc)
        r.addStretch(1)
        form.addLayout(r)
        section("App Loader — websites open inside SmartOS, programs launch normally")
        self.apps = QListWidget()
        self.apps.setFixedHeight(150)
        form.addWidget(self.apps)
        r = QHBoxLayout()
        for txt, fn in (("➕ Add program…", self.add_program), ("🔗 Add website…", self.add_link),
                        ("▶ Launch", self.launch), ("🗑 Remove", self.remove)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            r.addWidget(b)
        r.addStretch(1)
        form.addLayout(r)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(hdr)
        root.addWidget(scroll, 1)

    def on_show(self):
        c = self.app.cfg
        self.pl.setText(c["playlist"])
        self.vp.setText(c["vlc_path"])
        self.ref.setText(c["referer"])
        self.ua.setText(c["user_agent"])
        self.adb.setChecked(c["adblock"])
        self.sn.setValue(int(c["shinchan_seasons"]))
        self.en.setValue(int(c["shinchan_eps"]))
        self.ov.setText(c["shinchan_overrides"])
        self.fsb.setChecked(c["fullscreen"])
        self.refill()

    def refill(self):
        self.apps.clear()
        for a in self.app.cfg["apps"]:
            self.apps.addItem(f"{a['name']}   —   {a['target']}")

    def _browse(self, edit, filt):
        p, _ = QFileDialog.getOpenFileName(self, "Choose file", "", filt)
        if p:
            edit.setText(p)

    def save(self):
        c = self.app.cfg
        c["playlist"] = self.pl.text().strip() or DEFAULT_PLAYLIST
        c["vlc_path"] = self.vp.text().strip()
        c["referer"], c["user_agent"] = self.ref.text().strip(), self.ua.text().strip()
        c["adblock"] = self.adb.isChecked()
        c["shinchan_seasons"], c["shinchan_eps"] = self.sn.value(), self.en.value()
        c["shinchan_overrides"] = self.ov.text().strip()
        c["fullscreen"] = self.fsb.isChecked()
        save_config(c)
        self.app.adblock.enabled = c["adblock"]
        self.app.adblock.extra = load_extra_ads()
        self.app.set_fullscreen(c["fullscreen"])
        QMessageBox.information(self, "SmartOS", "Settings saved.")

    def add_program(self):
        p, _ = QFileDialog.getOpenFileName(self, "Choose a program", "", "Programs (*.exe *.lnk *.bat);;All (*.*)")
        if p:
            self.app.cfg["apps"].append({"name": os.path.splitext(os.path.basename(p))[0], "target": p})
            save_config(self.app.cfg)
            self.refill()

    def add_link(self):
        url, ok = QInputDialog.getText(self, "Add website", "URL:")
        if ok and url.strip():
            url = url.strip()
            if not is_url(url):
                url = "https://" + url
            name, ok2 = QInputDialog.getText(self, "Add website", "Name:", text=urllib.parse.urlparse(url).netloc)
            self.app.cfg["apps"].append({"name": name if ok2 and name else url, "target": url})
            save_config(self.app.cfg)
            self.refill()

    def launch(self):
        r = self.apps.currentRow()
        if r >= 0:
            a = self.app.cfg["apps"][r]
            self.app.launch_app(a["name"], a["target"])

    def remove(self):
        r = self.apps.currentRow()
        if r >= 0:
            del self.app.cfg["apps"][r]
            save_config(self.app.cfg)
            self.refill()

    def desktop_shortcut(self):
        if not sys.platform.startswith("win"):
            QMessageBox.information(self, "SmartOS", "Desktop shortcuts are only supported on Windows.")
            return
        frozen = getattr(sys, "frozen", False)
        target = sys.executable
        args = "" if frozen else os.path.abspath(sys.argv[0])
        if not frozen:
            pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            target = pw if os.path.exists(pw) else sys.executable
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
              "[Environment]::GetFolderPath('Desktop')+'\\SmartOS.lnk');"
              f"$s.TargetPath='{target}';$s.Arguments='{args}';$s.Save()")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
            QMessageBox.information(self, "SmartOS", "Shortcut created on your Desktop.")
        except Exception as e:
            QMessageBox.warning(self, "SmartOS", f"Could not create shortcut:\n{e}")


# ---------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self, cfg, profile, adblock):
        super().__init__()
        self.cfg, self.profile, self.adblock = cfg, profile, adblock
        self.setWindowTitle("SmartOS")
        self.resize(1280, 780)
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.screens = {"home": HomeScreen(self), "iptv": IPTVScreen(self), "web": WebScreen(self),
                        "shinchan": ShinchanScreen(self), "settings": SettingsScreen(self)}
        for s in self.screens.values():
            self.stack.addWidget(s)
        self.current = None
        self.show_screen("home")
        self.set_fullscreen(cfg["fullscreen"])
        QApplication.instance().installEventFilter(self)

    def show_screen(self, name):
        if self.current:
            self.current.on_hide()
        self.current = self.screens[name]
        self.stack.setCurrentWidget(self.current)
        self.current.on_show()

    def go_home(self):
        self.show_screen("home")

    def open_web(self, url, title, back_to="home"):
        self.show_screen("web")
        self.screens["web"].open(url, title, back_to)

    def launch_app(self, name, target):
        if is_url(target):
            self.open_web(target, name, "home")
        else:
            try:
                os.startfile(target) if hasattr(os, "startfile") else subprocess.Popen([target])
            except Exception as e:
                QMessageBox.warning(self, "SmartOS", f"Could not open:\n{target}\n\n{e}")

    def set_fullscreen(self, on):
        if on and not self.isFullScreen():
            self.showFullScreen()
        elif not on and self.isFullScreen():
            self.showNormal()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.KeyPress and QApplication.activeWindow() is self:
            if self.handle_key(ev):
                return True
        return False

    def handle_key(self, ev):
        k = ev.key()
        if k == Qt.Key.Key_F11:
            self.cfg["fullscreen"] = not self.isFullScreen()
            self.set_fullscreen(self.cfg["fullscreen"])
            return True
        if k == Qt.Key.Key_Escape:
            if self.current.handle_escape():
                return True
            if self.current is not self.screens["home"]:
                self.go_home()
            elif self.isFullScreen():
                self.cfg["fullscreen"] = False
                self.set_fullscreen(False)
            return True
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QSpinBox, QComboBox)) and k not in (Qt.Key.Key_Down, Qt.Key.Key_Return):
            return False
        if self.current is self.screens["web"]:
            return False  # arrows etc. belong to the web page (YouTube TV remote keys)
        return bool(self.current.on_key(ev))

    def closeEvent(self, e):
        for name in ("iptv", "shinchan"):
            try:
                scr = self.screens[name]
                scr.player.shutdown()
            except Exception:
                pass
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    cfg = load_config()
    profile = QWebEngineProfile("SmartOS", app)
    profile.setPersistentStoragePath(os.path.join(APPDIR, "web"))
    profile.setCachePath(os.path.join(APPDIR, "web_cache"))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile.setHttpUserAgent(UA)
    adblock = AdBlocker(app)
    adblock.enabled = cfg["adblock"]
    profile.setUrlRequestInterceptor(adblock)
    win = MainWindow(cfg, profile, adblock)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
