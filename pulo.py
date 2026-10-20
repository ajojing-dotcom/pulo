"""Pulo: pulau fokus di tepi atas layar Windows.

Timer Pomodoro, rehat ngopi, kontrol musik yang sedang diputar
(Spotify, YouTube di browser, dll.) dan musik fokus, dalam satu pil
hitam yang menempel di tengah atas layar seperti notch.
"""

import array
import asyncio
import ctypes
import datetime
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from bisect import bisect_right
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QEvent, QFileInfo, QLockFile, QMimeData,
                            QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer,
                            QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QFont, QFontMetrics,
                           QGuiApplication, QIcon, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QDialog, QDialogButtonBox, QFileDialog,
                               QFileIconProvider, QFormLayout,
                               QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QPushButton, QSizePolicy, QSlider, QSpinBox,
                               QStackedWidget,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QSoundEffect
    HAS_AUDIO = True
except Exception:
    HAS_AUDIO = False

APP = "Pulo"
IS_WIN = sys.platform == "win32"
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / APP
SETTINGS_FILE = DATA_DIR / "pengaturan.json"
SOUND_DIR = DATA_DIR / "suara"
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".wma", ".opus"}

# Pil hitam pekat supaya menyatu dengan bingkai layar, seperti notch.
INK = QColor(0, 0, 0)
TEXT = QColor(245, 244, 240)
DIM = QColor(176, 174, 168)          # 9.6:1 di atas hitam
LINE = QColor(255, 255, 255, 34)
ACCENT = QColor(255, 106, 61)        # satu aksen: warna tomat, hanya untuk fokus

EAR = 8                               # lengkung "telinga" di sudut atas pil
COLLAPSED = QSize(196, 30)
EXPANDED = QSize(420, 252)

TABS = [
    ("focus", "timer", "Fokus"),
    ("music", "note", "Musik & lirik"),
    ("todo", "todo", "Daftar tugas"),
    ("shelf", "shelf", "Rak berkas"),
    ("clip", "clip", "Riwayat salinan"),
]

PHASE_NAME = {
    "idle": "Siap fokus",
    "focus": "Fokus",
    "short": "Rehat sebentar",
    "long": "Rehat panjang",
    "coffee": "Ngopi dulu",
}

DEFAULTS = {
    "focus": 25, "short": 5, "long": 15, "coffee": 15, "cycles": 4,
    "auto_next": False, "music_follows": True, "chime": True,
    "open_on": "hover", "volume": 40, "focus_music": "off",
    "music_folder": "", "autostart": False,
    "show_timer": True, "pos_x": 0.5, "hint_close": False,
    "position": "top", "tab": "focus", "todos": [], "shelf": [],
    "clip_monitor": True, "lyrics": True, "lyric_offset": 0.0,
    "stats": {"date": "", "sessions": 0, "minutes": 0},
}


# ---------------------------------------------------------------- pengaturan

def load_settings():
    data = json.loads(json.dumps(DEFAULTS))
    try:
        stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return data


def save_settings(data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
        return True
    except OSError:
        return False


def today():
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------- Windows

def send_media_key(name):
    if not IS_WIN:
        return
    vk = {"toggle": 0xB3, "next": 0xB0, "prev": 0xB1}[name]
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}"'


def set_autostart(on):
    if not IS_WIN:
        return False
    import winreg
    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, APP)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def foreground_is_fullscreen(own_hwnd):
    """True kalau jendela aktif menutupi seluruh monitor (film, game, presentasi)."""
    if not IS_WIN:
        return False
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd == own_hwnd:
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return False

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    mon = user32.MonitorFromWindow(hwnd, 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(mon, ctypes.byref(info))
    m = info.rcMonitor
    return (rect.left <= m.left and rect.top <= m.top
            and rect.right >= m.right and rect.bottom >= m.bottom)


def keep_on_top(hwnd):
    if IS_WIN:
        # HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)


# ---------------------------------------------------------------- suara

def _write_wav(path, samples, rate):
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = array.array("h", (int(max(-1.0, min(1.0, s)) * 32000) for s in samples))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def ensure_noise(kind):
    """Bikin file derau 30 detik yang bisa diulang tanpa bunyi klik di sambungan."""
    path = SOUND_DIR / f"derau-{kind}.wav"
    if path.exists():
        return path
    rate, seconds, fade = 22050, 30, 22050
    n = rate * seconds + fade
    rnd = random.Random(7)
    out = [0.0] * n
    if kind == "coklat":
        b = 0.0
        for i in range(n):
            b = (b + 0.02 * rnd.uniform(-1, 1)) / 1.02
            out[i] = b * 3.5 * 0.55
    else:  # pink, filter Paul Kellet
        b0 = b1 = b2 = 0.0
        for i in range(n):
            wv = rnd.uniform(-1, 1)
            b0 = 0.99765 * b0 + wv * 0.0990460
            b1 = 0.96300 * b1 + wv * 0.2965164
            b2 = 0.57000 * b2 + wv * 1.0526913
            out[i] = (b0 + b1 + b2 + wv * 0.1848) * 0.05 * 0.55
    body = out[:n - fade]
    for i in range(fade):
        k = i / fade
        body[i] = body[i] * k + out[n - fade + i] * (1 - k)
    _write_wav(path, body, rate)
    return path


def ensure_chime():
    path = SOUND_DIR / "penanda.wav"
    if path.exists():
        return path
    rate = 22050
    samples = []
    for freq in (659.25, 880.0):
        for i in range(int(rate * 0.22)):
            t = i / rate
            samples.append(0.45 * math.sin(2 * math.pi * freq * t) * math.exp(-t * 9))
    _write_wav(path, samples, rate)
    return path


# ---------------------------------------------------------------- timer

class FocusTimer(QObject):
    changed = Signal()
    finished = Signal(str, str)  # fase yang selesai, fase berikutnya

    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self.phase = "idle"
        self.running = False
        self.done_in_cycle = 0
        self.total = self.minutes("focus") * 60
        self.remaining = float(self.total)
        self._end = 0.0
        self._tick = QTimer(self)
        self._tick.setInterval(250)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

    def minutes(self, phase):
        return max(1, int(self.s.get(phase, DEFAULTS[phase])))

    def progress(self):
        if self.phase == "idle" or self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, 1 - self.remaining / self.total))

    def display(self):
        sec = max(0, math.ceil(self.remaining))
        return f"{sec // 60:02d}:{sec % 60:02d}"

    def _set_phase(self, phase, run):
        self.phase = phase
        key = "focus" if phase == "idle" else phase
        self.total = self.minutes(key) * 60
        self.remaining = float(self.total)
        self.running = run
        self._end = time.monotonic() + self.remaining

    def toggle(self):
        if self.phase == "idle":
            self._set_phase("focus", True)
        elif self.running:
            self.remaining = max(0.0, self._end - time.monotonic())
            self.running = False
        else:
            self._end = time.monotonic() + self.remaining
            self.running = True
        self.changed.emit()

    def reset(self):
        self.done_in_cycle = 0
        self._set_phase("idle", False)
        self.changed.emit()

    def skip(self):
        if self.phase == "idle":
            return
        self._advance(count=False)

    def coffee(self):
        self._set_phase("coffee", True)
        self.changed.emit()

    def refresh_durations(self):
        if not self.running and self.remaining == self.total:
            self._set_phase(self.phase, False)
        self.changed.emit()

    def _on_tick(self):
        if not self.running:
            return
        self.remaining = max(0.0, self._end - time.monotonic())
        if self.remaining <= 0:
            self._advance(count=True)
        else:
            self.changed.emit()

    def _advance(self, count):
        ended = self.phase
        if ended == "focus":
            if count:
                stats = self.s["stats"]
                if stats.get("date") != today():
                    stats.update(date=today(), sessions=0, minutes=0)
                stats["sessions"] += 1
                stats["minutes"] += self.minutes("focus")
                save_settings(self.s)
            self.done_in_cycle += 1
            cycles = max(1, int(self.s.get("cycles", 4)))
            nxt = "long" if self.done_in_cycle % cycles == 0 else "short"
        else:
            if ended == "long":
                self.done_in_cycle = 0
            nxt = "focus"
        self._set_phase(nxt, bool(self.s.get("auto_next")))
        self.finished.emit(ended, nxt)
        self.changed.emit()


# ---------------------------------------------------------------- musik

class MediaBridge(QObject):
    """Membaca lagu yang sedang diputar lewat Windows Media Controls."""

    updated = Signal(object)

    def __init__(self):
        super().__init__()
        self.available = False
        self._mgr = None
        self._loop = None
        if IS_WIN:
            try:
                from winrt.windows.media.control import \
                    GlobalSystemMediaTransportControlsSessionManager as Manager
                self._Manager = Manager
                self.available = True
            except Exception:
                self.available = False
        if self.available:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._poll())
        self._loop.run_forever()

    async def _session(self):
        if self._mgr is None:
            self._mgr = await self._Manager.request_async()
        return self._mgr.get_current_session()

    async def _read(self):
        info = {"title": "", "artist": "", "playing": False, "app": "", "pos": None, "end": 0}
        try:
            s = await self._session()
            if s:
                props = await s.try_get_media_properties_async()
                status = s.get_playback_info().playback_status
                playing = int(status) == 4
                info = {
                    "title": props.title or "",
                    "artist": props.artist or "",
                    "playing": playing,
                    "app": s.source_app_user_model_id or "",
                    "pos": None, "end": 0,
                }
                try:
                    tl = s.get_timeline_properties()
                    end = _secs(tl.end_time) - _secs(tl.start_time)
                    if end > 0:
                        pos = _secs(tl.position)
                        age = _age(tl.last_updated_time)
                        if playing and 0 <= age < 3600:
                            pos += age
                        info["pos"], info["end"] = max(0.0, min(pos, end)), end
                except Exception:
                    pass
        except Exception:
            self._mgr = None
        return info

    async def _poll(self):
        while True:
            self.updated.emit(await self._read())
            await asyncio.sleep(1.5)

    def command(self, name, value=None):
        if self.available:
            asyncio.run_coroutine_threadsafe(self._command(name, value), self._loop)
        elif name != "seek":
            send_media_key(name)

    async def _command(self, name, value=None):
        try:
            s = await self._session()
            if not s:
                if name != "seek":
                    send_media_key(name)
                return
            if name == "seek":
                await s.try_change_playback_position_async(int(max(0.0, value) * 10_000_000))
                await asyncio.sleep(0.3)
                self.updated.emit(await self._read())
                return
            action = {"toggle": s.try_toggle_play_pause_async,
                      "next": s.try_skip_next_async,
                      "prev": s.try_skip_previous_async}[name]
            await action()
            await asyncio.sleep(0.35)
            self.updated.emit(await self._read())
        except Exception:
            send_media_key(name)


def _secs(v):
    if v is None:
        return 0.0
    if hasattr(v, "total_seconds"):
        return float(v.total_seconds())
    if hasattr(v, "duration"):
        return v.duration / 10_000_000
    return float(v)


def _age(stamp):
    """Detik sejak Windows terakhir memperbarui posisi lagu."""
    if isinstance(stamp, datetime.datetime):
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds()
    ticks = getattr(stamp, "universal_time", None)
    if ticks:
        return time.time() - (ticks - 116444736000000000) / 10_000_000
    return 0.0


def ago(stamp):
    d = max(0, int(time.time() - stamp))
    if d < 10:
        return "baru saja"
    if d < 60:
        return f"{d} dtk lalu"
    if d < 3600:
        return f"{d // 60} mnt lalu"
    return f"{d // 3600} jam lalu"


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


# ---------------------------------------------------------------- lirik

LRC_TAG = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def parse_lrc(text):
    lines = []
    for raw in text.splitlines():
        tags = LRC_TAG.findall(raw)
        if not tags:
            continue
        words = LRC_TAG.sub("", raw).strip()
        for m, sec in tags:
            lines.append((int(m) * 60 + float(sec.replace(":", ".")), words))
    lines.sort(key=lambda x: x[0])
    return lines


def lyric_candidates(artist, title):
    def clean(t):
        t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t)
        t = t.split("|")[0]
        return t.strip(" -")
    artist = re.sub(r"\s*-\s*Topic$", "", artist or "").replace("VEVO", "").strip()
    out = []
    if " - " in title:
        a, t = title.split(" - ", 1)
        out.append((a.strip(), clean(t)))
    out.append((artist, clean(title)))
    seen, result = set(), []
    for a, t in out:
        if t and (a, t) not in seen:
            seen.add((a, t))
            result.append((a, t))
    return result


class LyricsFinder(QObject):
    """Mencari lirik bersinkron di lrclib.net, di thread terpisah."""

    ready = Signal(object, object, str)
    API = "https://lrclib.net/api/"

    def __init__(self):
        super().__init__()
        self.cache = {}

    def fetch(self, key, artist, title, duration):
        if key in self.cache:
            lines, status = self.cache[key]
            QTimer.singleShot(0, lambda: self.ready.emit(key, lines, status))
            return
        threading.Thread(target=self._work, args=(key, artist, title, duration), daemon=True).start()

    def _get(self, path, params):
        url = self.API + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Pulo/1.0 (aplikasi pribadi)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))

    def _lookup(self, artist, title, duration):
        params = {"track_name": title}
        if artist:
            params["artist_name"] = artist
        if duration and duration > 0:
            try:
                return self._get("get", dict(params, duration=int(round(duration))))
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
        found = self._get("search", params)
        if not isinstance(found, list) or not found:
            return None
        for item in found:
            if item.get("syncedLyrics"):
                return item
        return found[0]

    def _work(self, key, artist, title, duration):
        lines, status = None, "Lirik tidak ditemukan"
        cache = True
        try:
            for a, t in lyric_candidates(artist, title):
                data = self._lookup(a, t, duration)
                if not data:
                    continue
                if data.get("instrumental"):
                    status = "Lagu instrumental"
                    break
                if data.get("syncedLyrics"):
                    lines = parse_lrc(data["syncedLyrics"])
                    status = ""
                    break
                if data.get("plainLyrics"):
                    status = "Lirik ada, tapi tanpa penanda waktu"
        except (OSError, ValueError):
            status = "Tidak bisa terhubung ke lrclib.net"
            cache = False
        if cache:
            self.cache[key] = (lines, status)
        self.ready.emit(key, lines, status)


def app_label(app_id):
    if not app_id:
        return ""
    name = app_id.split("!")[-1].split("\\")[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    known = {"msedge": "Edge", "chrome": "Chrome", "firefox": "Firefox",
             "spotify": "Spotify", "brave": "Brave", "opera": "Opera"}
    return known.get(name.lower(), name)


class FocusMusic(QObject):
    """Derau coklat/pink buatan sendiri, atau lagu dari folder pilihan."""

    status = Signal(str)

    def __init__(self, settings):
        super().__init__()
        self.s = settings
        self.queue = []
        self.ok = HAS_AUDIO
        if not self.ok:
            return
        try:
            self.out = QAudioOutput(self)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.out)
            self.player.mediaStatusChanged.connect(self._on_status)
            self.player.errorOccurred.connect(lambda *_: self.status.emit("File musik tidak bisa diputar"))
        except Exception:
            self.ok = False
            return
        self.set_volume(self.s.get("volume", 40))
        self.loaded = None

    def set_volume(self, v):
        if self.ok:
            self.out.setVolume(max(0, min(100, int(v))) / 100)

    def is_playing(self):
        return self.ok and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def _load(self):
        source = self.s.get("focus_music", "off")
        if source == self.loaded:
            return True
        self.loaded = None
        if source in ("coklat", "pink"):
            self.player.setLoops(-1)
            self.player.setSource(QUrl.fromLocalFile(str(ensure_noise(source))))
        elif source == "folder":
            folder = Path(self.s.get("music_folder") or "")
            if not self.s.get("music_folder") or not folder.is_dir():
                self.status.emit("Folder musik belum dipilih")
                return False
            self.queue = [p for p in sorted(folder.iterdir()) if p.suffix.lower() in AUDIO_EXT]
            if not self.queue:
                self.status.emit("Folder musik kosong")
                return False
            random.shuffle(self.queue)
            self.player.setLoops(1)
            self.player.setSource(QUrl.fromLocalFile(str(self.queue[0])))
        else:
            return False
        self.loaded = source
        self.status.emit("")
        return True

    def reload(self):
        if not self.ok:
            return
        self.player.stop()
        self.loaded = None

    def play(self):
        if self.ok and self._load() and not self.is_playing():
            self.player.play()

    def pause(self):
        if self.ok and self.is_playing():
            self.player.pause()

    def _on_status(self, st):
        if st == QMediaPlayer.MediaStatus.EndOfMedia and self.loaded == "folder" and self.queue:
            self.queue.append(self.queue.pop(0))
            self.player.setSource(QUrl.fromLocalFile(str(self.queue[0])))
            self.player.play()


# ---------------------------------------------------------------- widget kecil

def glyph_path(name, r):
    """Ikon digambar sendiri, garis tebal supaya terbaca di ukuran kecil."""
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    p = QPainterPath()
    if name == "play":
        p.moveTo(x + w * .30, y + h * .20)
        p.lineTo(x + w * .82, y + h * .50)
        p.lineTo(x + w * .30, y + h * .80)
        p.closeSubpath()
    elif name == "pause":
        p.addRoundedRect(QRectF(x + w * .26, y + h * .2, w * .16, h * .6), 1.5, 1.5)
        p.addRoundedRect(QRectF(x + w * .58, y + h * .2, w * .16, h * .6), 1.5, 1.5)
    elif name in ("next", "prev"):
        p.moveTo(x + w * .22, y + h * .22)
        p.lineTo(x + w * .66, y + h * .50)
        p.lineTo(x + w * .22, y + h * .78)
        p.closeSubpath()
        p.addRoundedRect(QRectF(x + w * .68, y + h * .22, w * .12, h * .56), 1, 1)
    return p


class GlyphButton(QPushButton):
    def __init__(self, glyph, tip, size=30, primary=False, parent=None):
        super().__init__(parent)
        self.glyph = glyph
        self.primary = primary
        self.setFixedSize(size, size)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def paintEvent(self, e):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        if self.primary:
            qp.setPen(Qt.NoPen)
            qp.setBrush(ACCENT.lighter(112) if self._hover else ACCENT)
            qp.drawEllipse(r)
            fg = INK
        else:
            if self._hover or self.isDown():
                qp.setPen(Qt.NoPen)
                qp.setBrush(QColor(255, 255, 255, 30))
                qp.drawEllipse(r)
            fg = TEXT
        if self.hasFocus():
            qp.setBrush(Qt.NoBrush)
            qp.setPen(QPen(TEXT if self.primary else ACCENT, 1.5))
            qp.drawEllipse(r)
        inner = r.adjusted(r.width() * .2, r.height() * .2, -r.width() * .2, -r.height() * .2)
        if self.glyph == "prev":
            qp.translate(self.width(), 0)
            qp.scale(-1, 1)
        self._draw(qp, self.glyph, inner, fg)

    def _draw(self, qp, name, r, fg):
        if name in ("play", "pause", "next", "prev"):
            qp.setPen(Qt.NoPen)
            qp.setBrush(fg)
            qp.drawPath(glyph_path(name, r))
            return
        pen = QPen(fg, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        qp.setPen(pen)
        qp.setBrush(Qt.NoBrush)
        x, y, w, h = r.x(), r.y(), r.width(), r.height()
        if name == "reset":
            box = QRectF(x + w * .12, y + h * .12, w * .76, h * .76)
            qp.drawArc(box, 100 * 16, 290 * 16)
            tip = QPointF(x + w * .40, y + h * .13)
            qp.drawLine(tip, tip + QPointF(w * .20, -h * .10))
            qp.drawLine(tip, tip + QPointF(w * .16, h * .16))
        elif name == "cup":
            body = QPainterPath()
            body.moveTo(x + w * .10, y + h * .38)
            body.lineTo(x + w * .70, y + h * .38)
            body.lineTo(x + w * .64, y + h * .86)
            body.lineTo(x + w * .16, y + h * .86)
            body.closeSubpath()
            qp.drawPath(body)
            qp.drawArc(QRectF(x + w * .58, y + h * .46, w * .30, h * .26), -90 * 16, 180 * 16)
            qp.drawLine(QPointF(x + w * .30, y + h * .06), QPointF(x + w * .30, y + h * .22))
            qp.drawLine(QPointF(x + w * .50, y + h * .06), QPointF(x + w * .50, y + h * .22))
        elif name == "timer":
            qp.drawEllipse(QRectF(x + w * .14, y + h * .18, w * .72, h * .72))
            c = QPointF(x + w * .5, y + h * .54)
            qp.drawLine(c, QPointF(x + w * .5, y + h * .34))
            qp.drawLine(c, QPointF(x + w * .64, y + h * .62))
            qp.drawLine(QPointF(x + w * .40, y + h * .04), QPointF(x + w * .60, y + h * .04))
        elif name == "note":
            qp.drawLine(QPointF(x + w * .40, y + h * .74), QPointF(x + w * .40, y + h * .12))
            qp.drawLine(QPointF(x + w * .40, y + h * .12), QPointF(x + w * .82, y + h * .22))
            qp.drawLine(QPointF(x + w * .82, y + h * .22), QPointF(x + w * .82, y + h * .62))
            qp.setBrush(fg)
            qp.drawEllipse(QPointF(x + w * .28, y + h * .76), w * .13, h * .11)
            qp.drawEllipse(QPointF(x + w * .70, y + h * .64), w * .13, h * .11)
        elif name == "todo":
            for i, yy in enumerate((.22, .50, .78)):
                qp.drawLine(QPointF(x + w * .40, y + h * yy), QPointF(x + w * .94, y + h * yy))
            qp.drawPolyline([QPointF(x + w * .04, y + h * .22), QPointF(x + w * .13, y + h * .31),
                             QPointF(x + w * .28, y + h * .12)])
            qp.drawRect(QRectF(x + w * .05, y + h * .42, w * .20, h * .17))
            qp.drawRect(QRectF(x + w * .05, y + h * .70, w * .20, h * .17))
        elif name == "shelf":
            tray = QPainterPath()
            tray.moveTo(x + w * .06, y + h * .56)
            tray.lineTo(x + w * .06, y + h * .88)
            tray.lineTo(x + w * .94, y + h * .88)
            tray.lineTo(x + w * .94, y + h * .56)
            qp.drawPath(tray)
            qp.drawLine(QPointF(x + w * .5, y + h * .06), QPointF(x + w * .5, y + h * .62))
            qp.drawPolyline([QPointF(x + w * .30, y + h * .44), QPointF(x + w * .5, y + h * .64),
                             QPointF(x + w * .70, y + h * .44)])
        elif name == "clip":
            qp.drawRoundedRect(QRectF(x + w * .14, y + h * .14, w * .72, h * .80), 2, 2)
            qp.drawRoundedRect(QRectF(x + w * .34, y + h * .04, w * .32, h * .18), 1.5, 1.5)
            qp.drawLine(QPointF(x + w * .30, y + h * .48), QPointF(x + w * .70, y + h * .48))
            qp.drawLine(QPointF(x + w * .30, y + h * .68), QPointF(x + w * .58, y + h * .68))
        elif name == "plus":
            qp.drawLine(QPointF(x + w * .5, y + h * .14), QPointF(x + w * .5, y + h * .86))
            qp.drawLine(QPointF(x + w * .14, y + h * .5), QPointF(x + w * .86, y + h * .5))
        elif name == "close":
            qp.drawLine(QPointF(x + w * .2, y + h * .2), QPointF(x + w * .8, y + h * .8))
            qp.drawLine(QPointF(x + w * .8, y + h * .2), QPointF(x + w * .2, y + h * .8))
        elif name == "tune":
            for i, k in enumerate((.3, .7)):
                yy = y + h * (.30 + .40 * i)
                qp.drawLine(QPointF(x + w * .06, yy), QPointF(x + w * .94, yy))
                qp.setBrush(INK)
                qp.drawEllipse(QPointF(x + w * k, yy), w * .12, w * .12)
                qp.setBrush(Qt.NoBrush)


class TabButton(QPushButton):
    def __init__(self, glyph, tip, parent=None):
        super().__init__(parent)
        self.glyph = glyph
        self.setCheckable(True)
        self.setFixedSize(34, 28)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def paintEvent(self, e):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.isChecked() or self._hover:
            qp.setPen(Qt.NoPen)
            qp.setBrush(QColor(255, 255, 255, 36 if self.isChecked() else 18))
            qp.drawRoundedRect(r, 8, 8)
        if self.hasFocus():
            qp.setBrush(Qt.NoBrush)
            qp.setPen(QPen(ACCENT, 1.5))
            qp.drawRoundedRect(r, 8, 8)
        g = QRectF(r.center().x() - 8, r.center().y() - 8, 16, 16)
        GlyphButton._draw(None, qp, self.glyph, g, TEXT if self.isChecked() else DIM)


class ShelfList(QListWidget):
    """Daftar berkas di rak; item bisa diseret keluar ke aplikasi lain."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setIconSize(QSize(18, 18))

    def mimeTypes(self):
        return ["text/uri-list"]

    def mimeData(self, items):
        m = QMimeData()
        m.setUrls([QUrl.fromLocalFile(i.data(Qt.UserRole)) for i in items])
        return m

    def supportedDropActions(self):
        return Qt.CopyAction


class CycleDots(QWidget):
    def __init__(self, timer, parent=None):
        super().__init__(parent)
        self.timer = timer
        self.setFixedHeight(12)
        self.setToolTip("Sesi fokus dalam satu putaran")

    def sizeHint(self):
        n = max(1, int(self.timer.s.get("cycles", 4)))
        return QSize(n * 13, 12)

    def paintEvent(self, e):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        n = max(1, int(self.timer.s.get("cycles", 4)))
        done = self.timer.done_in_cycle % n
        if self.timer.phase == "long":
            done = n
        x0 = self.width() - n * 13
        for i in range(n):
            c = QPointF(x0 + i * 13 + 6.5, 6)
            if i < done:
                qp.setPen(Qt.NoPen)
                qp.setBrush(TEXT)
            elif i == done and self.timer.phase == "focus":
                qp.setPen(QPen(ACCENT, 1.6))
                qp.setBrush(Qt.NoBrush)
            else:
                qp.setPen(QPen(QColor(255, 255, 255, 70), 1.2))
                qp.setBrush(Qt.NoBrush)
            qp.drawEllipse(c, 3.6, 3.6)


class ElidedLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text
        self.setMinimumWidth(10)

    def setFullText(self, text):
        self._full = text
        self._apply()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply()

    def _apply(self):
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.ElideRight, max(10, self.width())))


def ui_font(size, weight=QFont.Normal):
    f = QFont()
    f.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Inter", "Arial"])
    f.setPixelSize(size)
    f.setWeight(weight)
    return f


PANEL_CSS = f"""
QLabel {{ color: {TEXT.name()}; background: transparent; }}
QLabel#dim {{ color: {DIM.name()}; }}
QComboBox {{
    color: {TEXT.name()}; background: rgba(255,255,255,0.09);
    border: 1px solid transparent; border-radius: 6px; padding: 2px 8px; min-height: 22px;
}}
QComboBox:hover {{ background: rgba(255,255,255,0.14); }}
QComboBox:focus {{ border: 1px solid {ACCENT.name()}; }}
QComboBox::drop-down {{ border: none; width: 16px; }}
QComboBox QAbstractItemView {{
    color: {TEXT.name()}; background: #161616; border: 1px solid #333;
    selection-background-color: #333; outline: none;
}}
QSlider {{ background: transparent; min-height: 22px; }}
QSlider::groove:horizontal {{ height: 3px; background: rgba(255,255,255,0.22); border: none; border-radius: 1px; }}
QSlider::sub-page:horizontal {{ height: 3px; background: {TEXT.name()}; border-radius: 1px; }}
QSlider::add-page:horizontal {{ height: 3px; background: rgba(255,255,255,0.22); border-radius: 1px; }}
QSlider::handle:horizontal {{
    background: {TEXT.name()}; width: 12px; height: 12px; margin: -5px 0; border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QSlider:focus {{ border-bottom: 1px solid {ACCENT.name()}; }}
QToolTip {{ color: #111; background: #f5f4f0; border: none; padding: 4px 6px; }}
QLineEdit {{
    color: {TEXT.name()}; background: rgba(255,255,255,0.09); border: 1px solid transparent;
    border-radius: 7px; padding: 4px 8px; selection-background-color: #5a5a5a;
}}
QLineEdit:focus {{ border: 1px solid {ACCENT.name()}; }}
QListWidget {{ background: transparent; border: none; color: {TEXT.name()}; outline: none; }}
QListWidget::item {{ padding: 4px 4px; border-radius: 6px; }}
QListWidget::item:hover {{ background: rgba(255,255,255,0.07); }}
QListWidget::item:selected {{ background: rgba(255,255,255,0.15); color: {TEXT.name()}; }}
QListWidget::indicator {{
    width: 13px; height: 13px; border: 1px solid rgba(255,255,255,0.6);
    border-radius: 4px; background: transparent;
}}
QListWidget::indicator:checked {{ background: {TEXT.name()}; border-color: {TEXT.name()}; image: url(@CHECK@); }}
QPushButton#chip {{
    color: {TEXT.name()}; background: rgba(255,255,255,0.09); border: 1px solid transparent;
    border-radius: 6px; padding: 2px 9px; min-height: 20px;
}}
QPushButton#chip:hover {{ background: rgba(255,255,255,0.16); }}
QPushButton#chip:focus {{ border: 1px solid {ACCENT.name()}; }}
QPushButton#chip:disabled {{ color: #6f6e6a; }}
QScrollBar:vertical {{ width: 6px; background: transparent; margin: 0; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.25); border-radius: 3px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def panel_css():
    check = SOUND_DIR.parent / "centang.svg"
    try:
        if not check.exists():
            check.parent.mkdir(parents=True, exist_ok=True)
            check.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14">'
                             '<path d="M3 7.3l2.6 2.6L11 4.3" fill="none" stroke="#000" '
                             'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
                             encoding="utf-8")
    except OSError:
        pass
    return PANEL_CSS.replace("@CHECK@", check.as_posix())


def chip(text, tip=None):
    b = QPushButton(text)
    b.setObjectName("chip")
    b.setFont(ui_font(11))
    b.setCursor(Qt.PointingHandCursor)
    b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    if tip:
        b.setToolTip(tip)
    return b


# ---------------------------------------------------------------- pulau

class Island(QWidget):
    hotkey = Signal(int)
    open_settings = Signal()
    close_requested = Signal()

    def __init__(self, settings, timer, media, music):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.s = settings
        self.timer = timer
        self.media = media
        self.music = music
        self.t = 0.0
        self.pinned = False
        self.flashing = False
        self.user_visible = True
        self.now = {"title": "", "artist": "", "playing": False, "app": ""}
        self.flash_text = ""
        self.bars = [0.4, 0.8, 0.55]
        self.exp = QSize(EXPANDED)
        self._press = None
        self.dragging = False
        self.drop_hover = False
        self.mpos = (0.0, time.monotonic())
        self.mend = 0.0
        self.song_key = None
        self.ly_lines = None
        self.ly_state = "Putar lagu untuk melihat lirik"
        self.clips = []
        self._own_clip = None
        self.icons = QFileIconProvider()
        self.lyrics = LyricsFinder()
        self.lyrics.ready.connect(self._on_lyrics)
        self.music_timer = QTimer(self, interval=250, timeout=self._tick_music)
        self.setAcceptDrops(True)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle(APP)
        self.setMouseTracking(True)

        self.anim = QVariantAnimation(self)
        self.anim.valueChanged.connect(self._on_anim)

        self.hover_timer = QTimer(self, singleShot=True, interval=180, timeout=self.expand)
        self.collapse_timer = QTimer(self, singleShot=True, interval=380, timeout=self._maybe_collapse)
        self.flash_timer = QTimer(self, singleShot=True, interval=5000, timeout=self._end_flash)
        self.eq_timer = QTimer(self, interval=140, timeout=self._eq_step)

        self._build_panel()
        self.apply_feature()
        self._apply_geometry()

        timer.changed.connect(self._refresh)
        media.updated.connect(self._on_media)
        music.status.connect(self._on_music_status)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard)
        self._refresh()

    # ---- susunan panel
    def _build_panel(self):
        p = QWidget(self)
        p.setStyleSheet(panel_css())
        p.setGeometry(EAR, 0, EXPANDED.width(), EXPANDED.height())
        self.panel = p
        self.fade = QGraphicsOpacityEffect(p)
        self.fade.setOpacity(0)
        p.setGraphicsEffect(self.fade)
        p.hide()

        root = QVBoxLayout(p)
        root.setContentsMargins(16, 8, 14, 14)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(2)
        self.tabs = {}
        for key, glyph, tip in TABS:
            b = TabButton(glyph, tip)
            b.clicked.connect(lambda _=False, k=key: self.show_tab(k))
            bar.addWidget(b)
            self.tabs[key] = b
        bar.addStretch(1)
        self.btn_settings = GlyphButton("tune", "Pengaturan", 26)
        self.btn_settings.clicked.connect(self.open_settings.emit)
        bar.addWidget(self.btn_settings)
        self.btn_close = GlyphButton("close", "Tutup pulau (buka lagi lewat ikon tray)", 26)
        self.btn_close.clicked.connect(self.close_requested.emit)
        bar.addWidget(self.btn_close)
        root.addLayout(bar)

        self.stack = QStackedWidget()
        self.pages = {}
        for key, build in (("focus", self._page_focus), ("music", self._page_music),
                           ("todo", self._page_todo), ("shelf", self._page_shelf),
                           ("clip", self._page_clip)):
            w = build()
            self.stack.addWidget(w)
            self.pages[key] = w
        root.addWidget(self.stack, 1)

    @staticmethod
    def _dim(text="", size=11, shrink=False):
        lab = QLabel(text)
        lab.setObjectName("dim")
        lab.setFont(ui_font(size))
        if shrink:
            lab.setMinimumWidth(1)
            lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        return lab

    def _page_focus(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 4, 0)
        v.setSpacing(4)

        top = QHBoxLayout()
        self.phase_label = ElidedLabel()
        self.phase_label.setFont(ui_font(12, QFont.DemiBold))
        top.addWidget(self.phase_label, 1)
        self.dots = CycleDots(self.timer)
        top.addWidget(self.dots)
        v.addLayout(top)

        self.timer_box = QWidget()
        mid = QHBoxLayout(self.timer_box)
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(6)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.time_label = QLabel()
        self.time_label.setFont(ui_font(40, QFont.DemiBold))
        col.addWidget(self.time_label)
        self.stats_label = self._dim()
        col.addWidget(self.stats_label)
        mid.addLayout(col, 1)
        self.btn_reset = GlyphButton("reset", "Ulang dari awal", 32)
        self.btn_play = GlyphButton("play", "Mulai fokus  (Ctrl+Alt+Spasi)", 44, primary=True)
        self.btn_skip = GlyphButton("next", "Lewati ke fase berikutnya", 32)
        self.btn_coffee = GlyphButton("cup", "Rehat ngopi", 32)
        for b in (self.btn_reset, self.btn_play, self.btn_skip, self.btn_coffee):
            mid.addWidget(b, 0, Qt.AlignVCenter)
        self.btn_play.clicked.connect(self.timer.toggle)
        self.btn_reset.clicked.connect(self.timer.reset)
        self.btn_skip.clicked.connect(self.timer.skip)
        self.btn_coffee.clicked.connect(self.timer.coffee)
        v.addWidget(self.timer_box)
        v.addSpacing(8)

        now = QHBoxLayout()
        now.setSpacing(4)
        txt = QVBoxLayout()
        txt.setSpacing(0)
        self.song_label = ElidedLabel()
        self.song_label.setFont(ui_font(12, QFont.DemiBold))
        self.artist_label = ElidedLabel()
        self.artist_label.setObjectName("dim")
        self.artist_label.setFont(ui_font(11))
        txt.addWidget(self.song_label)
        txt.addWidget(self.artist_label)
        now.addLayout(txt, 1)
        self.btn_prev = GlyphButton("prev", "Lagu sebelumnya", 28)
        self.btn_media = GlyphButton("play", "Putar / jeda musik", 28)
        self.btn_next = GlyphButton("next", "Lagu berikutnya", 28)
        self.btn_prev.clicked.connect(lambda: self.media.command("prev"))
        self.btn_media.clicked.connect(lambda: self.media.command("toggle"))
        self.btn_next.clicked.connect(lambda: self.media.command("next"))
        for b in (self.btn_prev, self.btn_media, self.btn_next):
            now.addWidget(b)
        v.addLayout(now)

        fm = QHBoxLayout()
        fm.setSpacing(8)
        fm.addWidget(self._dim("Musik fokus"))
        self.combo = QComboBox()
        self.combo.setFont(ui_font(11))
        self.combo.setAccessibleName("Musik fokus")
        for key, text in (("off", "Mati"), ("coklat", "Derau coklat"),
                          ("pink", "Derau pink"), ("folder", "Folder saya")):
            self.combo.addItem(text, key)
        idx = self.combo.findData(self.s.get("focus_music", "off"))
        self.combo.setCurrentIndex(max(0, idx))
        self.combo.currentIndexChanged.connect(self._on_music_choice)
        self.combo.setFixedWidth(128)
        fm.addWidget(self.combo)
        self.music_note = self._dim()
        self.music_note.hide()
        fm.addWidget(self.music_note, 1)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(self.s.get("volume", 40)))
        self.volume.setAccessibleName("Volume musik fokus")
        self.volume.setToolTip("Volume musik fokus")
        self.volume.valueChanged.connect(self._on_volume)
        fm.addWidget(self.volume, 1)
        fm.addStretch(0)
        v.addLayout(fm)
        v.addStretch(1)

        if not self.music.ok:
            self.combo.setEnabled(False)
            self.volume.hide()
            self.music_note.setText("Audio tidak tersedia")
            self.music_note.show()
        return w

    def _page_music(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 4, 0)
        v.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(4)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.m_title = ElidedLabel()
        self.m_title.setFont(ui_font(15, QFont.DemiBold))
        self.m_artist = ElidedLabel()
        self.m_artist.setObjectName("dim")
        self.m_artist.setFont(ui_font(11))
        col.addWidget(self.m_title)
        col.addWidget(self.m_artist)
        head.addLayout(col, 1)
        self.m_prev = GlyphButton("prev", "Lagu sebelumnya", 30)
        self.m_play = GlyphButton("play", "Putar / jeda musik", 34)
        self.m_next = GlyphButton("next", "Lagu berikutnya", 30)
        self.m_prev.clicked.connect(lambda: self.media.command("prev"))
        self.m_play.clicked.connect(lambda: self.media.command("toggle"))
        self.m_next.clicked.connect(lambda: self.media.command("next"))
        for b in (self.m_prev, self.m_play, self.m_next):
            head.addWidget(b, 0, Qt.AlignVCenter)
        v.addLayout(head)

        self.seek_row = QWidget()
        sr = QHBoxLayout(self.seek_row)
        sr.setContentsMargins(0, 2, 0, 0)
        sr.setSpacing(8)
        self.m_elapsed = self._dim("0:00", 10)
        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 1000)
        self.seek.setAccessibleName("Posisi lagu")
        self.seek.sliderReleased.connect(self._on_seek)
        self.m_total = self._dim("0:00", 10)
        sr.addWidget(self.m_elapsed)
        sr.addWidget(self.seek, 1)
        sr.addWidget(self.m_total)
        v.addWidget(self.seek_row)
        v.addSpacing(8)

        self.ly_prev = ElidedLabel()
        self.ly_prev.setObjectName("dim")
        self.ly_prev.setFont(ui_font(12))
        self.ly_cur = QLabel()
        self.ly_cur.setFont(ui_font(17, QFont.DemiBold))
        self.ly_cur.setWordWrap(True)
        self.ly_cur.setMinimumHeight(48)
        self.ly_cur.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.ly_next = ElidedLabel()
        self.ly_next.setObjectName("dim")
        self.ly_next.setFont(ui_font(12))
        for lab in (self.ly_prev, self.ly_cur, self.ly_next):
            v.addWidget(lab)
        v.addStretch(1)

        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.ly_status = self._dim("", 10, shrink=True)
        foot.addWidget(self.ly_status, 1)
        self.ly_early = chip("Lirik lebih awal", "Lirik terlambat dari suara? Majukan 0,2 detik")
        self.ly_late = chip("Lebih lambat", "Lirik mendahului suara? Mundurkan 0,2 detik")
        self.ly_early.clicked.connect(lambda: self._nudge_lyrics(0.2))
        self.ly_late.clicked.connect(lambda: self._nudge_lyrics(-0.2))
        foot.addWidget(self.ly_early)
        foot.addWidget(self.ly_late)
        v.addLayout(foot)
        return w

    def _page_todo(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 4, 0)
        v.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.todo_in = QLineEdit()
        self.todo_in.setFont(ui_font(12))
        self.todo_in.setPlaceholderText("Tulis tugas baru lalu tekan Enter")
        self.todo_in.setAccessibleName("Tugas baru")
        self.todo_in.returnPressed.connect(self._todo_add)
        row.addWidget(self.todo_in, 1)
        add = GlyphButton("plus", "Tambah tugas", 28)
        add.clicked.connect(self._todo_add)
        row.addWidget(add)
        v.addLayout(row)

        self.todo_list = QListWidget()
        self.todo_list.setFont(ui_font(12))
        self.todo_list.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.todo_list.itemChanged.connect(self._todo_changed)
        self.todo_list.installEventFilter(self)
        self.todo_list.setToolTip("Centang jika selesai. Klik dua kali untuk mengubah, Delete untuk menghapus.")
        v.addWidget(self.todo_list, 1)
        self.todo_empty = self._dim("Belum ada tugas. Tulis di atas lalu tekan Enter.", 12)
        self.todo_empty.setAlignment(Qt.AlignCenter)
        v.addWidget(self.todo_empty, 1)

        foot = QHBoxLayout()
        self.todo_count = self._dim(shrink=True)
        foot.addWidget(self.todo_count, 1)
        self.todo_clear = chip("Hapus yang selesai")
        self.todo_clear.clicked.connect(self._todo_clear_done)
        foot.addWidget(self.todo_clear)
        v.addLayout(foot)
        self._todo_render()
        return w

    def _page_shelf(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 4, 0)
        v.setSpacing(6)
        self.shelf_list = ShelfList()
        self.shelf_list.setFont(ui_font(12))
        self.shelf_list.itemDoubleClicked.connect(
            lambda it: QDesktopServices.openUrl(QUrl.fromLocalFile(it.data(Qt.UserRole))))
        self.shelf_list.itemSelectionChanged.connect(self._shelf_buttons)
        self.shelf_list.installEventFilter(self)
        self.shelf_list.setToolTip("Seret ke WhatsApp, email, atau folder. Klik dua kali untuk membuka.")
        v.addWidget(self.shelf_list, 1)
        self.shelf_empty = self._dim("Rak kosong. Seret file ke pulau untuk menitipkannya,\n"
                                     "lalu seret lagi ke WhatsApp, email, atau folder lain.", 12)
        self.shelf_empty.setAlignment(Qt.AlignCenter)
        v.addWidget(self.shelf_empty, 1)
        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.shelf_count = self._dim(shrink=True)
        foot.addWidget(self.shelf_count, 1)
        self.shelf_copy = chip("Salin", "Salin file terpilih, lalu tempel di Explorer atau aplikasi lain")
        self.shelf_open = chip("Folder", "Buka folder tempat file berada")
        self.shelf_remove = chip("Keluarkan", "Keluarkan dari rak (file aslinya tidak dihapus)")
        self.shelf_clear = chip("Kosongkan")
        self.shelf_copy.clicked.connect(self._shelf_copy)
        self.shelf_open.clicked.connect(self._shelf_open)
        self.shelf_remove.clicked.connect(self._shelf_remove)
        self.shelf_clear.clicked.connect(self._shelf_clear)
        for b in (self.shelf_copy, self.shelf_open, self.shelf_remove, self.shelf_clear):
            foot.addWidget(b)
        v.addLayout(foot)
        self.s["shelf"] = [f for f in self.s.get("shelf", []) if Path(f).exists()]
        self._shelf_render()
        return w

    def _page_clip(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 4, 0)
        v.setSpacing(6)
        self.clip_search = QLineEdit()
        self.clip_search.setFont(ui_font(12))
        self.clip_search.setPlaceholderText("Cari di riwayat salinan")
        self.clip_search.setAccessibleName("Cari riwayat salinan")
        self.clip_search.textChanged.connect(self._clip_render)
        v.addWidget(self.clip_search)
        self.clip_list = QListWidget()
        self.clip_list.setFont(ui_font(12))
        self.clip_list.itemClicked.connect(self._clip_copy)
        self.clip_list.itemActivated.connect(self._clip_copy)
        self.clip_list.installEventFilter(self)
        v.addWidget(self.clip_list, 1)
        self.clip_empty = self._dim("", 12)
        self.clip_empty.setAlignment(Qt.AlignCenter)
        v.addWidget(self.clip_empty, 1)
        foot = QHBoxLayout()
        self.clip_count = self._dim(shrink=True)
        foot.addWidget(self.clip_count, 1)
        clear = chip("Bersihkan")
        clear.clicked.connect(self._clip_clear)
        foot.addWidget(clear)
        v.addLayout(foot)
        self._clip_render()
        return w

    # ---- tab
    def current_tab(self):
        for key, w in self.pages.items():
            if self.stack.currentWidget() is w:
                return key
        return "focus"

    def show_tab(self, key):
        if key not in self.pages or (key == "focus" and not self.s.get("show_timer", True)):
            key = "music"
        self.stack.setCurrentWidget(self.pages[key])
        for k, b in self.tabs.items():
            b.setChecked(k == key)
        if self.s.get("tab") != key:
            self.s["tab"] = key
            self._save_soon()
        self._sync_music_timer()
        self._tick_music()
        self.update()

    def _first_focus(self):
        return {"focus": self.btn_play, "music": self.m_play, "todo": self.todo_in,
                "shelf": self.shelf_list, "clip": self.clip_search}[self.current_tab()]

    # ---- daftar tugas
    def _todo_render(self):
        lst = self.todo_list
        lst.blockSignals(True)
        lst.clear()
        for i, t in enumerate(self.s.get("todos", [])):
            it = QListWidgetItem(t["text"])
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
            it.setCheckState(Qt.Checked if t.get("done") else Qt.Unchecked)
            it.setData(Qt.UserRole, i)
            if t.get("done"):
                f = it.font()
                f.setStrikeOut(True)
                it.setFont(f)
                it.setForeground(DIM)
            lst.addItem(it)
        lst.blockSignals(False)
        todos = self.s.get("todos", [])
        has = bool(todos)
        lst.setVisible(has)
        self.todo_empty.setVisible(not has)
        done = sum(1 for t in todos if t.get("done"))
        today_done = sum(1 for t in todos if t.get("done_on") == today())
        if has:
            text = f"{done} dari {len(todos)} selesai"
            if today_done:
                text += f" · {today_done} hari ini"
        else:
            text = ""
        self.todo_count.setText(text)
        self.todo_clear.setEnabled(done > 0)

    def _todo_add(self):
        text = self.todo_in.text().strip()
        if not text:
            return
        self.s.setdefault("todos", []).append({"text": text, "done": False, "done_on": ""})
        self.todo_in.clear()
        save_settings(self.s)
        self._todo_render()
        self.todo_list.scrollToBottom()

    def _todo_changed(self, it):
        todos = self.s.get("todos", [])
        i = it.data(Qt.UserRole)
        if i is None or i >= len(todos):
            return
        text = it.text().strip()
        if not text:
            del todos[i]
        else:
            done = it.checkState() == Qt.Checked
            if done and not todos[i].get("done"):
                todos[i]["done_on"] = today()
            if not done:
                todos[i]["done_on"] = ""
            todos[i].update(text=text, done=done)
        save_settings(self.s)
        QTimer.singleShot(0, self._todo_render)

    def _todo_delete_current(self):
        it = self.todo_list.currentItem()
        if it is None:
            return
        del self.s["todos"][it.data(Qt.UserRole)]
        save_settings(self.s)
        self._todo_render()

    def _todo_clear_done(self):
        self.s["todos"] = [t for t in self.s.get("todos", []) if not t.get("done")]
        save_settings(self.s)
        self._todo_render()

    # ---- rak berkas
    def _shelf_render(self):
        lst = self.shelf_list
        lst.clear()
        for path in self.s.get("shelf", []):
            info = QFileInfo(path)
            it = QListWidgetItem(self.icons.icon(info), info.fileName() or path)
            it.setData(Qt.UserRole, path)
            it.setToolTip(path)
            lst.addItem(it)
        n = lst.count()
        lst.setVisible(n > 0)
        self.shelf_empty.setVisible(n == 0)
        self.shelf_count.setText(f"{n} file" if n else "")
        self._shelf_buttons()

    def _shelf_buttons(self):
        n = self.shelf_list.count()
        sel = len(self.shelf_list.selectedItems())
        self.shelf_copy.setEnabled(n > 0)
        self.shelf_open.setEnabled(n > 0)
        self.shelf_remove.setEnabled(sel > 0)
        self.shelf_clear.setEnabled(n > 0)
        self.shelf_copy.setText(f"Salin {sel}" if sel else "Salin")

    def _shelf_targets(self):
        items = self.shelf_list.selectedItems() or [self.shelf_list.item(i) for i in range(self.shelf_list.count())]
        return [i.data(Qt.UserRole) for i in items]

    def add_files(self, paths):
        shelf = self.s.setdefault("shelf", [])
        added = 0
        for path in paths:
            if path and Path(path).exists() and path not in shelf:
                shelf.append(path)
                added += 1
        save_settings(self.s)
        self._shelf_render()
        return added

    def _shelf_copy(self):
        m = QMimeData()
        m.setUrls([QUrl.fromLocalFile(f) for f in self._shelf_targets()])
        QApplication.clipboard().setMimeData(m)
        self.shelf_count.setText("Tersalin, tinggal tempel")

    def _shelf_open(self):
        targets = self._shelf_targets()
        if not targets:
            return
        if IS_WIN:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(targets[0])])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(targets[0]).parent)))

    def _shelf_remove(self):
        drop = {i.data(Qt.UserRole) for i in self.shelf_list.selectedItems()}
        self.s["shelf"] = [f for f in self.s.get("shelf", []) if f not in drop]
        save_settings(self.s)
        self._shelf_render()

    def _shelf_clear(self):
        self.s["shelf"] = []
        save_settings(self.s)
        self._shelf_render()

    # ---- riwayat salinan
    def _on_clipboard(self):
        if not self.s.get("clip_monitor", True):
            return
        md = QApplication.clipboard().mimeData()
        if md is None or not md.hasText() or md.hasUrls():
            return
        text = md.text()
        if not text.strip():
            return
        if text == self._own_clip:
            self._own_clip = None
            return
        self.clips = [c for c in self.clips if c["text"] != text]
        self.clips.insert(0, {"text": text[:20000], "t": time.time()})
        del self.clips[50:]
        self._clip_render()

    def _clip_render(self):
        q = self.clip_search.text().strip().lower()
        lst = self.clip_list
        lst.clear()
        shown = [c for c in self.clips if q in c["text"].lower()] if q else self.clips
        for c in shown:
            one = " ".join(c["text"].split())
            it = QListWidgetItem(one[:200])
            it.setData(Qt.UserRole, c["text"])
            it.setToolTip(f"{ago(c['t'])} · {len(c['text'])} karakter · klik untuk menyalin lagi")
            lst.addItem(it)
        has = bool(shown)
        lst.setVisible(has)
        self.clip_empty.setVisible(not has)
        if not self.s.get("clip_monitor", True):
            self.clip_empty.setText("Perekaman riwayat salinan sedang dimatikan di Pengaturan.")
        elif q and self.clips:
            self.clip_empty.setText("Tidak ada salinan yang cocok.")
        else:
            self.clip_empty.setText("Belum ada riwayat. Teks yang kamu salin akan muncul di sini.\n"
                                    "Riwayat hanya disimpan selama Pulo berjalan.")
        self.clip_count.setText(f"{len(self.clips)} salinan" if self.clips else "")

    def _clip_copy(self, it):
        text = it.data(Qt.UserRole)
        self._own_clip = text
        QApplication.clipboard().setText(text)
        self.clips = [c for c in self.clips if c["text"] != text]
        self.clips.insert(0, {"text": text, "t": time.time()})
        self._clip_render()
        self.clip_count.setText("Disalin ke clipboard")

    def _clip_delete_current(self):
        it = self.clip_list.currentItem()
        if it is None:
            return
        text = it.data(Qt.UserRole)
        self.clips = [c for c in self.clips if c["text"] != text]
        self._clip_render()

    def _clip_clear(self):
        self.clips = []
        self._clip_render()

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Delete:
            if obj is self.todo_list and self.todo_list.state() != QAbstractItemView.EditingState:
                self._todo_delete_current()
                return True
            if obj is self.shelf_list:
                self._shelf_remove()
                return True
            if obj is self.clip_list:
                self._clip_delete_current()
                return True
        return super().eventFilter(obj, ev)

    # ---- lirik
    def media_pos(self):
        base, stamp = self.mpos
        if self.now.get("playing"):
            base += time.monotonic() - stamp
        return base

    def _on_lyrics(self, key, lines, status):
        if key != self.song_key:
            return
        self.ly_lines = lines
        self.ly_state = status
        self._tick_music()

    def _nudge_lyrics(self, delta):
        self.s["lyric_offset"] = round(float(self.s.get("lyric_offset", 0.0)) + delta, 2)
        self._save_soon()
        self._tick_music()

    def _on_seek(self):
        if self.mend > 0:
            target = self.seek.value() / 1000 * self.mend
            self.mpos = (target, time.monotonic())
            self.media.command("seek", target)

    def current_lyric(self):
        lines = self.ly_lines
        if not lines:
            return -1
        pos = self.media_pos() + float(self.s.get("lyric_offset", 0.0))
        return bisect_right([t for t, _ in lines], pos) - 1

    def _sync_music_timer(self):
        on = bool(self.now.get("title")) and (
            (self.panel.isVisible() and self.current_tab() == "music")
            or (self.t < 0.5 and not self.s.get("show_timer", True) and bool(self.ly_lines)))
        if on and not self.music_timer.isActive():
            self.music_timer.start()
        elif not on and self.music_timer.isActive():
            self.music_timer.stop()

    def _tick_music(self):
        title = self.now.get("title")
        pos = self.media_pos()
        has_tl = self.mend > 0
        self.seek_row.setVisible(has_tl)
        if has_tl and not self.seek.isSliderDown():
            self.seek.blockSignals(True)
            self.seek.setValue(int(1000 * min(1.0, pos / self.mend)))
            self.seek.blockSignals(False)
            self.m_elapsed.setText(fmt_time(min(pos, self.mend)))
            self.m_total.setText(fmt_time(self.mend))
        lines = self.ly_lines
        i = self.current_lyric()
        if not title:
            prev, cur, nxt = "", "Belum ada lagu yang diputar", ""
            state = "Putar lagu di Spotify, YouTube, atau pemutar lain"
        elif lines:
            pick = lambda k: (lines[k][1] or "♪") if 0 <= k < len(lines) else ""
            prev, cur, nxt = pick(i - 1), (pick(i) if i >= 0 else "♪"), pick(i + 1)
            off = float(self.s.get("lyric_offset", 0.0))
            state = "Lirik dari lrclib.net"
            if off:
                state += f" · geser {off:+.1f} dtk"
            if not has_tl:
                state += " · waktu perkiraan"
        else:
            prev, nxt = "", ""
            cur = "♪" if self.ly_state == "Lagu instrumental" else ""
            state = self.ly_state if self.s.get("lyrics", True) else "Lirik dimatikan di Pengaturan"
        self.ly_prev.setFullText(prev)
        self.ly_cur.setText(cur)
        self.ly_next.setFullText(nxt)
        self.ly_status.setText(state)
        self.ly_early.setVisible(bool(lines))
        self.ly_late.setVisible(bool(lines))
        if self.t < 0.5:
            self.update()

    # ---- geometri & animasi
    def _body_size(self):
        w = COLLAPSED.width() + (self.exp.width() - COLLAPSED.width()) * self.t
        h = COLLAPSED.height() + (self.exp.height() - COLLAPSED.height()) * self.t
        return max(1, round(w)), max(1, round(h))

    def _apply_geometry(self):
        scr = QGuiApplication.primaryScreen().geometry()
        bw, bh = self._body_size()
        w = bw + 2 * EAR
        frac = self._drag_frac if self.dragging else float(self.s.get("pos_x", 0.5))
        cx = scr.x() + scr.width() * max(0.0, min(1.0, frac))
        x = int(max(scr.x(), min(scr.x() + scr.width() - w, cx - w / 2)))
        if self.is_bottom():
            area = QGuiApplication.primaryScreen().availableGeometry()
            y = area.y() + area.height() - bh
        else:
            y = scr.y()
        self.setGeometry(x, y, w, bh)

    def is_bottom(self):
        return self.s.get("position") == "bottom"

    def apply_feature(self):
        """Fitur fokus (timer) bisa ditampilkan atau disembunyikan."""
        on = bool(self.s.get("show_timer", True))
        if not on and self.timer.phase != "idle":
            self.timer.reset()
        self.tabs["focus"].setVisible(on)
        self.show_tab(self.s.get("tab", "focus"))
        self.exp = QSize(EXPANDED)
        self.panel.setGeometry(EAR, 0, self.exp.width(), self.exp.height())
        self._apply_geometry()
        self._refresh()

    def _on_anim(self, v):
        self.t = float(v)
        self._apply_geometry()
        show = self.t > 0.82
        self.panel.setVisible(show)
        self.fade.setOpacity(max(0.0, min(1.0, (self.t - 0.82) / 0.18)) if show else 0)
        self._sync_eq()
        self._sync_music_timer()
        self.update()

    def _animate(self, target):
        self.anim.stop()
        self.anim.setStartValue(self.t)
        self.anim.setEndValue(float(target))
        if target > self.t:
            curve = QEasingCurve(QEasingCurve.OutBack)
            curve.setOvershoot(0.9)
            self.anim.setEasingCurve(curve)
            self.anim.setDuration(300)
        else:
            self.anim.setEasingCurve(QEasingCurve.InOutCubic)
            self.anim.setDuration(200)
        self.anim.start()

    def expand(self):
        if self.t < 1 or self.anim.state() == QVariantAnimation.Running:
            self._animate(1)

    def collapse(self):
        self.pinned = False
        self._animate(0)

    def _maybe_collapse(self):
        typing = self.isActiveWindow() and isinstance(QApplication.focusWidget(), QLineEdit)
        busy = (self.pinned or self.flashing or self.drop_hover or typing
                or QApplication.activePopupWidget() is not None
                or QApplication.mouseButtons() != Qt.NoButton
                or self.frameGeometry().contains(QCursor.pos()))
        if busy:
            if not self.pinned:
                self.collapse_timer.start()
            return
        self.collapse()

    def toggle_pinned(self):
        if self.pinned and self.t > 0.5:
            self.collapse()
        else:
            self.pinned = True
            self.show_island()
            self.expand()
            self.activateWindow()
            self._first_focus().setFocus()

    def flash(self, text):
        self.flash_text = text
        if not self.flashing:
            self._tab_before_flash = self.current_tab()
        self.flashing = True
        if self.s.get("show_timer", True):
            self.show_tab("focus")
        self.show_island(force=True)
        self.expand()
        self._refresh()
        self.flash_timer.start()

    def _end_flash(self):
        self.flashing = False
        self.flash_text = ""
        self.show_tab(getattr(self, "_tab_before_flash", self.current_tab()))
        self._refresh()
        if not self.user_visible:
            self.reset_collapsed()
            self.hide()
            return
        self.collapse_timer.start()

    def reset_collapsed(self):
        self.anim.stop()
        self.pinned = False
        self.t = 0.0
        self.panel.hide()
        self._apply_geometry()

    def show_island(self, force=False):
        if force or self.user_visible:
            self.show()

    # ---- event
    def enterEvent(self, e):
        self.collapse_timer.stop()
        if self.s.get("open_on") == "hover" and self.t < 1:
            self.hover_timer.start()

    def leaveEvent(self, e):
        self.hover_timer.stop()
        if not self.pinned:
            self.collapse_timer.start()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self.dragging = False

    def mouseMoveEvent(self, e):
        if self._press is None or not (e.buttons() & Qt.LeftButton):
            return
        dx = e.globalPosition().toPoint().x() - self._press.x()
        if not self.dragging and abs(dx) < 5:
            return
        if not self.dragging:
            self.dragging = True
            self.hover_timer.stop()
            self.setCursor(Qt.SizeHorCursor)
            self._drag_start = float(self.s.get("pos_x", 0.5))
        scr = QGuiApplication.primaryScreen().geometry()
        self._drag_frac = max(0.0, min(1.0, self._drag_start + dx / max(1, scr.width())))
        self._apply_geometry()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.dragging:
            scr = QGuiApplication.primaryScreen().geometry()
            centre = self.geometry().center().x() - scr.x()
            self.s["pos_x"] = round(centre / max(1, scr.width()), 4)
            self.dragging = False
            self.unsetCursor()
            save_settings(self.s)
            self._apply_geometry()
        elif self.t < 0.5:
            self.expand()
        self._press = None

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton and self.t > 0.5:
            self.center()

    def center(self):
        self.s["pos_x"] = 0.5
        save_settings(self.s)
        self._apply_geometry()

    def _local_files(self, md):
        if md is None or not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, e):
        if e.source() is None and self._local_files(e.mimeData()):
            e.acceptProposedAction()
            self.drop_hover = True
            self.collapse_timer.stop()
            self.hover_timer.stop()
            self.show_tab("shelf")
            self.expand()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self.drop_hover:
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.drop_hover = False
        self.collapse_timer.start()

    def dropEvent(self, e):
        files = self._local_files(e.mimeData())
        self.drop_hover = False
        if files:
            e.acceptProposedAction()
            n = self.add_files(files)
            self.shelf_count.setText(f"{n} file dititipkan" if n else "Sudah ada di rak")
        self.collapse_timer.start()

    def changeEvent(self, e):
        if e.type() == QEvent.ActivationChange and not self.isActiveWindow() and not self.pinned:
            self.collapse_timer.start()
        super().changeEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.collapse()
        else:
            super().keyPressEvent(e)

    def nativeEvent(self, event_type, message):
        if IS_WIN and bytes(event_type) == b"windows_generic_MSG":
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312:  # WM_HOTKEY
                self.hotkey.emit(int(msg.wParam))
                return True, 0
        return super().nativeEvent(event_type, message)

    # ---- data
    def _on_media(self, info):
        old = self.now
        self.now = info
        key = (info.get("title", ""), info.get("artist", "")) if info.get("title") else None
        changed = key != self.song_key
        if info.get("pos") is not None:
            self.mpos = (info["pos"], time.monotonic())
            self.mend = float(info.get("end") or 0)
        else:
            self.mend = 0.0
            if changed:
                self.mpos = (0.0, time.monotonic())
            elif old.get("playing") != info.get("playing"):
                base, stamp = self.mpos
                if old.get("playing"):
                    base += time.monotonic() - stamp
                self.mpos = (base, time.monotonic())
        if changed:
            self.song_key = key
            self.ly_lines = None
            if not key:
                self.ly_state = "Putar lagu untuk melihat lirik"
            elif self.s.get("lyrics", True):
                self.ly_state = "Mencari lirik..."
                self.lyrics.fetch(key, info.get("artist", ""), info.get("title", ""), self.mend)
            else:
                self.ly_state = "Lirik dimatikan di Pengaturan"
        self._refresh_media()
        self._sync_music_timer()
        self._tick_music()
        self._sync_eq()
        self.update()

    def _refresh_media(self):
        title = self.now.get("title")
        if title:
            self.song_label.setFullText(title)
            meta = " · ".join(x for x in (self.now.get("artist"), app_label(self.now.get("app"))) if x)
            self.artist_label.setFullText(meta)
        elif self.media.available:
            self.song_label.setFullText("Belum ada musik yang diputar")
            self.artist_label.setFullText("Putar lagu di Spotify, YouTube, atau pemutar lain")
        else:
            self.song_label.setFullText("Kontrol musik")
            self.artist_label.setFullText("Judul lagu muncul setelah paket winrt terpasang")
        self.btn_media.glyph = "pause" if self.now.get("playing") else "play"
        self.btn_media.update()
        self.m_title.setFullText(self.song_label._full)
        self.m_artist.setFullText(self.artist_label._full)
        self.m_play.glyph = self.btn_media.glyph
        self.m_play.update()

    def _on_music_choice(self, _):
        self.s["focus_music"] = self.combo.currentData()
        save_settings(self.s)
        if not self.music.ok:
            return
        self.music.reload()
        self.music_note.hide()
        self.volume.setVisible(self.s["focus_music"] != "off")
        self.sync_music()

    def _on_volume(self, v):
        self.s["volume"] = v
        self.music.set_volume(v)
        self._save_soon()

    def _save_soon(self):
        if not hasattr(self, "_save_timer"):
            self._save_timer = QTimer(self, singleShot=True, interval=600,
                                      timeout=lambda: save_settings(self.s))
        self._save_timer.start()

    def _on_music_status(self, text):
        self.music_note.setText(text)
        self.music_note.setVisible(bool(text))
        self.volume.setVisible(not text and self.s.get("focus_music") != "off")

    def sync_music(self):
        src = self.s.get("focus_music", "off")
        timer_on = self.s.get("show_timer", True)
        want = src != "off" and (not self.s.get("music_follows", True) or not timer_on
                                 or (self.timer.phase == "focus" and self.timer.running))
        if want:
            self.music.play()
        else:
            self.music.pause()

    def _refresh(self):
        tm = self.timer
        name = PHASE_NAME[tm.phase]
        if tm.phase == "focus":
            n = max(1, int(self.s.get("cycles", 4)))
            name += f" · sesi {tm.done_in_cycle % n + 1} dari {n}"
        if tm.phase != "idle" and not tm.running:
            name += " · dijeda"
        if not self.s.get("show_timer", True):
            name = "Musik"
        self.phase_label.setFullText(self.flash_text or name)
        self.time_label.setText(tm.display())
        self.btn_play.glyph = "pause" if tm.running else "play"
        self.btn_play.setToolTip(("Jeda" if tm.running else "Mulai") + "  (Ctrl+Alt+Spasi)")
        self.btn_play.update()
        self.btn_reset.setEnabled(tm.phase != "idle")
        self.btn_skip.setEnabled(tm.phase != "idle")
        st = self.s["stats"]
        if st.get("date") == today() and st.get("sessions"):
            self.stats_label.setText(f"Hari ini {st['sessions']} sesi fokus, {st['minutes']} menit")
        else:
            self.stats_label.setText("Belum ada sesi fokus hari ini")
        self.dots.updateGeometry()
        self.dots.update()
        self.volume.setVisible(self.music.ok and self.s.get("focus_music") != "off"
                               and not self.music_note.isVisible())
        self.sync_music()
        self.update()

    def _sync_eq(self):
        on = self.now.get("playing") and self.t < 0.5 and self.isVisible()
        if on and not self.eq_timer.isActive():
            self.eq_timer.start()
        elif not on and self.eq_timer.isActive():
            self.eq_timer.stop()

    def _eq_step(self):
        k = time.monotonic() * 6
        self.bars = [0.35 + 0.65 * abs(math.sin(k + i * 1.7)) for i in range(3)]
        self.update()

    # ---- gambar
    def _pill_path(self):
        w, h = self.width(), self.height()
        r = 13 + 11 * self.t
        r = min(r, h / 2, (w - 2 * EAR) / 2)
        p = QPainterPath()
        p.moveTo(0, 0)
        p.lineTo(w, 0)
        p.quadTo(w - EAR, 0, w - EAR, EAR)
        p.lineTo(w - EAR, h - r)
        p.quadTo(w - EAR, h, w - EAR - r, h)
        p.lineTo(EAR + r, h)
        p.quadTo(EAR, h, EAR, h - r)
        p.lineTo(EAR, EAR)
        p.quadTo(EAR, 0, 0, 0)
        p.closeSubpath()
        return p, r

    def paintEvent(self, e):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        path, r = self._pill_path()
        w, h = self.width(), self.height()
        qp.save()
        if self.is_bottom():
            qp.translate(0, h)
            qp.scale(1, -1)
        qp.fillPath(path, INK)

        tm = self.timer
        color = ACCENT if tm.phase in ("focus", "idle") else TEXT

        # Garis progres tipis di tepi bawah pil, ciri khas Pulo.
        timer_on = self.s.get("show_timer", True)
        prog = tm.progress() if timer_on else 0
        if prog > 0:
            x0, x1 = EAR + r * 0.75, w - EAR - r * 0.75
            y = h - 2.2
            qp.setPen(QPen(QColor(255, 255, 255, 30), 2.2, Qt.SolidLine, Qt.RoundCap))
            qp.drawLine(QPointF(x0, y), QPointF(x1, y))
            qp.setPen(QPen(color, 2.2, Qt.SolidLine, Qt.RoundCap))
            qp.drawLine(QPointF(x0, y), QPointF(x0 + (x1 - x0) * prog, y))
        qp.restore()

        if timer_on and self.panel.isVisible() and self.t > 0.9 and self.current_tab() == "focus":
            ly = self.song_label.mapTo(self, QPoint(0, 0)).y() - 8
            qp.setPen(QPen(LINE, 1))
            qp.drawLine(QPointF(EAR + 20, ly), QPointF(w - EAR - 18, ly))

        a = max(0.0, 1 - self.t * 2.2)
        if a <= 0:
            return
        qp.setOpacity(a)
        cy = h / 2 + (1 if self.is_bottom() else -1)
        left = EAR + 14
        right = w - EAR - 16
        if not timer_on:
            title = self.now.get("title")
            i = self.current_lyric() if self.now.get("playing") else -1
            if i >= 0 and self.ly_lines[i][1]:
                title = self.ly_lines[i][1]
            if title:
                qp.setFont(ui_font(12, QFont.DemiBold))
                qp.setPen(TEXT if self.now.get("playing") else DIM)
                room = right - 26 - left
                text = QFontMetrics(qp.font()).elidedText(title, Qt.ElideRight, int(room))
                qp.drawText(QRectF(left, cy - 10, room, 20), Qt.AlignVCenter | Qt.AlignLeft, text)
            self._paint_eq(qp, right, cy)
            return
        if tm.phase in ("short", "long", "coffee"):
            GlyphButton._draw(None, qp, "cup", QRectF(left - 1, cy - 7, 14, 14), TEXT)
        else:
            qp.setPen(Qt.NoPen)
            qp.setBrush(ACCENT if tm.running else QColor(255, 255, 255, 90))
            qp.drawEllipse(QPointF(left + 5, cy), 4.2, 4.2)
        qp.setFont(ui_font(13, QFont.DemiBold))
        qp.setPen(TEXT if tm.running else DIM)
        qp.drawText(QRectF(left + 18, cy - 10, 90, 20), Qt.AlignVCenter | Qt.AlignLeft, tm.display())

        if self.now.get("playing"):
            self._paint_eq(qp, right, cy)
        elif tm.phase != "idle" and not tm.running:
            qp.setPen(Qt.NoPen)
            qp.setBrush(DIM)
            qp.drawPath(glyph_path("pause", QRectF(right - 13, cy - 7, 14, 14)))


    def _paint_eq(self, qp, right, cy):
        if not self.now.get("playing"):
            return
        qp.setPen(Qt.NoPen)
        qp.setBrush(TEXT)
        for i, b in enumerate(self.bars):
            bh = 12 * b
            qp.drawRoundedRect(QRectF(right - 16 + i * 6, cy - bh / 2, 3, bh), 1.5, 1.5)


# ---------------------------------------------------------------- pengaturan

class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.setWindowTitle("Pengaturan Pulo")
        self.setWindowFlag(Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(380)
        form = QFormLayout()
        form.setVerticalSpacing(8)

        def spin(key, lo, hi):
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setValue(int(settings.get(key, DEFAULTS[key])))
            sp.setSuffix(" menit" if key != "cycles" else " sesi")
            return sp

        self.focus = spin("focus", 1, 180)
        self.short = spin("short", 1, 60)
        self.long = spin("long", 1, 90)
        self.coffee = spin("coffee", 1, 90)
        self.cycles = spin("cycles", 1, 12)
        form.addRow("Fokus", self.focus)
        form.addRow("Rehat sebentar", self.short)
        form.addRow("Rehat panjang", self.long)
        form.addRow("Rehat ngopi", self.coffee)
        form.addRow("Rehat panjang setelah", self.cycles)

        self.open_on = QComboBox()
        self.open_on.addItem("Kursor mendekat", "hover")
        self.open_on.addItem("Diklik", "click")
        self.open_on.setCurrentIndex(max(0, self.open_on.findData(settings.get("open_on"))))
        form.addRow("Buka pulau saat", self.open_on)

        self.position = QComboBox()
        self.position.addItem("Atas layar", "top")
        self.position.addItem("Bawah layar (di atas taskbar)", "bottom")
        self.position.setCurrentIndex(max(0, self.position.findData(settings.get("position", "top"))))
        form.addRow("Posisi pulau", self.position)

        row = QHBoxLayout()
        self.folder = QLineEdit(settings.get("music_folder", ""))
        self.folder.setPlaceholderText("Belum dipilih")
        self.folder.setReadOnly(True)
        pick = QPushButton("Pilih folder")
        pick.clicked.connect(self._pick)
        row.addWidget(self.folder, 1)
        row.addWidget(pick)
        form.addRow("Folder musik fokus", row)

        self.show_timer = QCheckBox("Tampilkan fitur fokus (timer Pomodoro)")
        self.show_timer.setChecked(bool(settings.get("show_timer", True)))
        self.auto_next = QCheckBox("Lanjut otomatis ke fase berikutnya")
        self.auto_next.setChecked(bool(settings.get("auto_next")))
        self.follows = QCheckBox("Musik fokus hanya bunyi saat fokus berjalan")
        self.follows.setChecked(bool(settings.get("music_follows", True)))
        self.chime = QCheckBox("Bunyi penanda saat fase selesai")
        self.chime.setChecked(bool(settings.get("chime", True)))
        self.lyrics = QCheckBox("Cari lirik lagu secara online (lrclib.net)")
        self.lyrics.setChecked(bool(settings.get("lyrics", True)))
        self.clip_monitor = QCheckBox("Rekam riwayat salinan (hanya selama Pulo berjalan, tidak disimpan)")
        self.clip_monitor.setChecked(bool(settings.get("clip_monitor", True)))
        self.autostart = QCheckBox("Jalankan saat Windows menyala")
        self.autostart.setChecked(bool(settings.get("autostart")))
        self.autostart.setEnabled(IS_WIN)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addSpacing(6)
        for c in (self.show_timer, self.auto_next, self.follows, self.chime,
                  self.lyrics, self.clip_monitor, self.autostart):
            lay.addWidget(c)
        hint = QLabel("Seret pil ke kiri/kanan untuk menggeser, klik dua kali panel untuk kembali ke tengah. "
                      "Pintasan: Ctrl+Alt+Spasi mulai/jeda, Ctrl+Alt+P buka pulau, Esc tutup.")
        hint.setWordWrap(True)
        lay.addSpacing(6)
        lay.addWidget(hint)
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Save).setText("Simpan")
        btns.button(QDialogButtonBox.Cancel).setText("Batal")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, "Pilih folder musik fokus", self.folder.text())
        if d:
            self.folder.setText(d)

    def apply(self):
        s = self.s
        s.update(focus=self.focus.value(), short=self.short.value(), long=self.long.value(),
                 coffee=self.coffee.value(), cycles=self.cycles.value(),
                 open_on=self.open_on.currentData(), music_folder=self.folder.text(),
                 auto_next=self.auto_next.isChecked(), music_follows=self.follows.isChecked(),
                 chime=self.chime.isChecked(), show_timer=self.show_timer.isChecked(),
                 position=self.position.currentData(), lyrics=self.lyrics.isChecked(),
                 clip_monitor=self.clip_monitor.isChecked())
        auto_ok = True
        if IS_WIN and self.autostart.isChecked() != bool(s.get("autostart")):
            auto_ok = set_autostart(self.autostart.isChecked())
            if auto_ok:
                s["autostart"] = self.autostart.isChecked()
        return save_settings(s) and auto_ok


# ---------------------------------------------------------------- aplikasi

def tray_icon():
    """Ikon sementara: siluet pil Pulo dengan garis progres."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    qp = QPainter(pm)
    qp.setRenderHint(QPainter.Antialiasing)
    qp.setPen(QPen(QColor(255, 255, 255, 200), 3))
    qp.setBrush(INK)
    qp.drawRoundedRect(QRectF(4, 18, 56, 28), 14, 14)
    qp.setPen(QPen(ACCENT, 4, Qt.SolidLine, Qt.RoundCap))
    qp.drawLine(QPointF(16, 38), QPointF(36, 38))
    qp.end()
    return QIcon(pm)


class PuloApp(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.s = load_settings()
        if self.s["stats"].get("date") != today():
            self.s["stats"] = {"date": today(), "sessions": 0, "minutes": 0}
        self.timer = FocusTimer(self.s)
        self.media = MediaBridge()
        self.music = FocusMusic(self.s)
        self.island = Island(self.s, self.timer, self.media, self.music)
        self.island.open_settings.connect(self.show_settings)
        self.island.close_requested.connect(self.close_island)
        self.island.hotkey.connect(self._on_hotkey)
        self.timer.finished.connect(self._on_finished)

        self.chime = None
        if HAS_AUDIO:
            try:
                self.chime = QSoundEffect(self)
                self.chime.setSource(QUrl.fromLocalFile(str(ensure_chime())))
                self.chime.setVolume(0.6)
            except Exception:
                self.chime = None

        self._build_tray()
        self.island.show()
        self.island._refresh_media()
        self._register_hotkeys()

        self.guard = QTimer(self, interval=1500, timeout=self._guard)
        self.guard.start()

    def _build_tray(self):
        self.tray = QSystemTrayIcon(tray_icon(), self)
        self.tray.setToolTip(APP)
        m = QMenu()
        self.act_toggle = QAction("Mulai fokus", m, triggered=self.timer.toggle)
        self.act_coffee = QAction("Rehat ngopi", m, triggered=self.timer.coffee)
        self.act_show = QAction("Tampilkan pulau", m, checkable=True, checked=True,
                                triggered=self._set_visible)
        m.addAction(self.act_toggle)
        m.addAction(self.act_coffee)
        m.addSeparator()
        m.addAction(self.act_show)
        self.act_timer = QAction("Fitur fokus (timer)", m, checkable=True,
                                 checked=bool(self.s.get("show_timer", True)),
                                 triggered=self.set_timer_feature)
        m.addAction(self.act_timer)
        m.addAction(QAction("Kembalikan ke tengah", m, triggered=self.island.center))
        m.addAction(QAction("Pengaturan", m, triggered=self.show_settings))
        m.addSeparator()
        m.addAction(QAction("Keluar", m, triggered=self.quit))
        self.menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self._on_tray)
        self.timer.changed.connect(self._sync_tray)
        self.tray.show()
        self._sync_tray()

    def _sync_tray(self):
        tm = self.timer
        on = bool(self.s.get("show_timer", True))
        self.act_toggle.setVisible(on)
        self.act_coffee.setVisible(on)
        self.act_timer.setChecked(on)
        if not on:
            self.tray.setToolTip(APP)
            return
        self.act_toggle.setText("Jeda" if tm.running else ("Mulai fokus" if tm.phase == "idle" else "Lanjutkan"))
        self.act_coffee.setText(f"Rehat ngopi {self.s.get('coffee', 15)} menit")
        self.tray.setToolTip(f"{APP}: {PHASE_NAME[tm.phase]} {tm.display()}")

    def _on_tray(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self._set_visible(True)
            self.island.toggle_pinned()

    def _set_visible(self, on):
        self.island.user_visible = bool(on)
        self.act_show.setChecked(bool(on))
        if on:
            self.island.show()
        else:
            self.island.hide()

    def set_timer_feature(self, on):
        self.s["show_timer"] = bool(on)
        save_settings(self.s)
        self.island.apply_feature()
        self._sync_tray()

    def close_island(self):
        self.island.reset_collapsed()
        self._set_visible(False)
        if not self.s.get("hint_close"):
            self.tray.showMessage(APP, "Pulo disembunyikan. Klik ikon Pulo di tray untuk menampilkannya lagi.")
            self.s["hint_close"] = True
            save_settings(self.s)

    def _register_hotkeys(self):
        if not IS_WIN:
            return
        hwnd = int(self.island.winId())
        user32 = ctypes.windll.user32
        mods = 0x0001 | 0x0002 | 0x4000  # Alt + Ctrl, tanpa ulang
        failed = []
        if not user32.RegisterHotKey(hwnd, 1, mods, 0x20):
            failed.append("Ctrl+Alt+Spasi")
        if not user32.RegisterHotKey(hwnd, 2, mods, 0x50):
            failed.append("Ctrl+Alt+P")
        if failed:
            self.tray.showMessage(APP, "Pintasan sudah dipakai aplikasi lain: " + ", ".join(failed))

    def _on_hotkey(self, hid):
        if hid == 1 and not self.s.get("show_timer", True):
            self.media.command("toggle")
        elif hid == 1:
            self.timer.toggle()
            self.island.flash("Mulai" if self.timer.running else "Dijeda")
        elif hid == 2:
            self._set_visible(True)
            self.island.toggle_pinned()

    def _on_finished(self, ended, nxt):
        if self.s.get("chime", True) and self.chime:
            self.chime.play()
        mins = self.timer.minutes(nxt)
        if ended == "focus":
            text = f"Sesi fokus selesai. {PHASE_NAME[nxt]} {mins} menit."
        elif nxt == "focus":
            text = f"Rehat selesai. Siap fokus {mins} menit."
        else:
            text = f"{PHASE_NAME[nxt]} {mins} menit."
        if not self.timer.running:
            text += " Tekan mulai."
        self.island.flash(text)

    def _guard(self):
        isl = self.island
        if not isl.user_visible:
            return
        hwnd = int(isl.winId())
        full = foreground_is_fullscreen(hwnd)
        if full and not isl.flashing and not isl.pinned:
            if isl.isVisible():
                isl.hide()
        elif not isl.isVisible():
            isl.show()
        if isl.isVisible():
            keep_on_top(hwnd)

    def show_settings(self):
        self.island.pinned = True
        dlg = SettingsDialog(self.s)
        ok = dlg.exec() == QDialog.Accepted
        if ok:
            if not dlg.apply():
                self.tray.showMessage(APP, "Pengaturan gagal disimpan ke " + str(SETTINGS_FILE))
            self.music.reload()
            self.timer.refresh_durations()
            self.island.apply_feature()
            self.island._clip_render()
            self.island._tick_music()
            self._sync_tray()
        self.island.pinned = False
        self.island.collapse_timer.start()
        return ok

    def quit(self):
        save_settings(self.s)
        if IS_WIN:
            hwnd = int(self.island.winId())
            ctypes.windll.user32.UnregisterHotKey(hwnd, 1)
            ctypes.windll.user32.UnregisterHotKey(hwnd, 2)
        self.tray.hide()
        self.app.quit()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setQuitOnLastWindowClosed(False)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(DATA_DIR / "pulo.lock"))
    if not lock.tryLock(100):
        return 0
    pulo = PuloApp(app)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
