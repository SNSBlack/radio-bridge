"""
Universal Radio Bridge v3.1
============================
Виртуальная аудиоколонка FH6 Radio для Forza Horizon 6.
Аддон к spotify-radio v1.1.3-preview3.

Что делает:
  - Виртуальный динамик через VB-Cable (приложения переключаются на него вручную)
  - Переключение приложений по кнопке в веб-интерфейсе
  - Now Playing через Windows Media Session API
  - Синхронизация опций с оригинальным модом (эквалайзер, нормализация)
  - Автовыключение через 30с после закрытия игры
  - Единственный экземпляр (убивает предыдущий)
  - Лог с ротацией 1МБ + дедупликация
"""

import sys, os, time, json, math, threading, socket, struct
import logging, logging.handlers, subprocess, ctypes, uuid, re, atexit
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ctypes.wintypes is a submodule and is NOT auto-loaded by `import ctypes`
# in Python 3.13. Import it explicitly so ctypes.wintypes.HANDLE/DWORD work.
if sys.platform == "win32":
    import ctypes.wintypes  # noqa: F401

# winreg только на Windows
try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

# ---------------------------------------------------------------------------
# Silent subprocess helpers — без этого каждый tasklist/wmic/ipconfig вспышет
# чёрное окно cmd. Передаём CREATE_NO_WINDOW + STARTF_USESHOWWINDOW=SW_HIDE
# во все вызовы.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000
    _SW_HIDE          = 0
    _STARTF_USESHOWWINDOW = 1

    def _silent_startupinfo():
        si = subprocess.STARTUPINFO()
        si.dwFlags    |= _STARTF_USESHOWWINDOW
        si.wShowWindow = _SW_HIDE
        return si
else:
    _CREATE_NO_WINDOW = 0
    def _silent_startupinfo(): return None

def _silent_kwargs():
    return {"creationflags": _CREATE_NO_WINDOW,
            "startupinfo":   _silent_startupinfo()}

def run_silent(fn, *args, **kw):
    """Wrap subprocess.* to suppress the console flash on Windows."""
    kw.setdefault("creationflags", _CREATE_NO_WINDOW)
    kw.setdefault("startupinfo",   _silent_startupinfo())
    return fn(*args, **kw)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE      = Path(__file__).parent
LOG_PATH  = BASE / "bridge.log"
CFG_PATH  = BASE / "config.json"
PID_PATH  = BASE / "bridge.pid"
OPTS_PATH = BASE.parent / "spotify-radio" / "options.json"

# ---------------------------------------------------------------------------
# Logging: ротация 1МБ + дедупликация
# ---------------------------------------------------------------------------
class DedupHandler(logging.handlers.RotatingFileHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._last = None; self._last_lvl = logging.INFO
        self._n = 0; self._t = 0.0

    def emit(self, record):
        msg = record.getMessage(); now = time.time()
        if msg == self._last:
            self._n += 1
            if self._n % 50 == 0 or now - self._t > 30:
                r = logging.makeLogRecord({'name': record.name,
                    'levelno': record.levelno, 'levelname': record.levelname,
                    'msg': "[x%d] %s" % (self._n, msg), 'args': ()})
                self._t = now; super().emit(r)
            return
        if self._n > 1:
            super().emit(logging.makeLogRecord({'name': 'URB',
                'levelno': self._last_lvl,
                'levelname': logging.getLevelName(self._last_lvl),
                'msg': "[repeated %d times]" % self._n, 'args': ()}))
        self._last = msg; self._last_lvl = record.levelno
        self._n = 1; self._t = now; super().emit(record)

root = logging.getLogger()
root.setLevel(logging.INFO)
root.handlers.clear()
_fh = DedupHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
root.addHandler(_fh)
_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.WARNING)
root.addHandler(_ch)
log = logging.getLogger("URB")

# ---------------------------------------------------------------------------
# Config (v1.1.3 compatible options)
# ---------------------------------------------------------------------------
DEFAULT_CFG = {
    "device_name":       "FH6 Radio",
    "http_port":         8104,
    "orig_port":         8103,
    "sample_rate":       44100,
    "channels":          2,
    "buf_ms":            80,
    # Mirrored from original mod options.json (v1.1.3)
    "menuPlayback":          "pause",
    "raceStartPlayback":     "next",
    "volumeNormalization":   "on",
    "equalizerEnabled":      False,
    "equalizerBands":        [0, 0, 0, 0, 0],
    # Our own
    "lan_ip":            "",
    "shutdown_delay":    30,
}

def load_cfg():
    c = dict(DEFAULT_CFG)
    if CFG_PATH.exists():
        try: c.update(json.loads(CFG_PATH.read_text("utf-8")))
        except Exception as e: log.warning("Config: %s", e)
    else:
        CFG_PATH.write_text(json.dumps(DEFAULT_CFG, indent=2), "utf-8")
    return c

def save_cfg(c):
    CFG_PATH.write_text(json.dumps(c, indent=2, ensure_ascii=False), "utf-8")

def sync_orig_options(cfg):
    """Записать options.json для оригинального мода."""
    opts = {
        "menuPlayback":        cfg.get("menuPlayback",        "pause"),
        "raceStartPlayback":   cfg.get("raceStartPlayback",   "next"),
        "volumeNormalization": cfg.get("volumeNormalization", "on"),
        "equalizerEnabled":    cfg.get("equalizerEnabled",    False),
        "equalizerBands":      cfg.get("equalizerBands",      [0,0,0,0,0]),
    }
    try:
        OPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        OPTS_PATH.write_text(json.dumps(opts, ensure_ascii=False), "utf-8")
    except Exception as e:
        log.debug("sync_orig_options: %s", e)
    # POST к оригинальному моду если он запущен
    try:
        import urllib.request
        body = json.dumps(opts).encode()
        req = urllib.request.Request(
            "http://localhost:%d/api/options" % DEFAULT_CFG["orig_port"],
            data=body, method="POST",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=0.5)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
_shutdown = threading.Event()

# ---------------------------------------------------------------------------
# Single instance — убиваем предыдущий процесс
# ---------------------------------------------------------------------------
def kill_old_instance():
    if not PID_PATH.exists(): return
    try:
        old = int(PID_PATH.read_text().strip())
        if old == os.getpid(): return
        if sys.platform == "win32":
            h = ctypes.windll.kernel32.OpenProcess(0x0001, False, old)
            if h:
                ctypes.windll.kernel32.TerminateProcess(h, 0)
                ctypes.windll.kernel32.CloseHandle(h)
                log.info("Killed old PID %d", old)
                time.sleep(1.5)  # дать время освободить порт
    except Exception: pass
    PID_PATH.unlink(missing_ok=True)

def write_pid(): PID_PATH.write_text(str(os.getpid()))

# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------
class Ring:
    def __init__(self, sec, rate, ch):
        cap = int(sec * rate * ch * 2 * 8)
        self._b = bytearray(cap); self._cap = cap
        self._w = 0; self._lk = threading.Lock()

    def push(self, data):
        n = len(data)
        if n >= self._cap: data = data[-self._cap:]; n = self._cap
        with self._lk:
            end = self._w + n
            if end <= self._cap: self._b[self._w:end] = data
            else:
                s = self._cap - self._w
                self._b[self._w:] = data[:s]; self._b[:n-s] = data[s:]
            self._w = end % self._cap

    def latest(self, n):
        with self._lk:
            s = (self._w - n) % self._cap
            if s < 0: s += self._cap
            if s + n <= self._cap: return bytes(self._b[s:s+n])
            return bytes(self._b[s:]) + bytes(self._b[:n-(self._cap-s)])

RING = None

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self._lk = threading.Lock()
        self.source = "idle"; self.peak_db = -60.0
        self.bytes_out = 0; self.pipe_clients = 0
        self.errors = []; self.orig_state = {}
        self.game_running = False
        self.vbcable_ok = False; self.vbcable_name = ""
        self.routed_pids = {}  # pid -> exe_name
        self.now_playing = {"title": "", "artist": "", "app": ""}

    def snap(self):
        with self._lk:
            return dict(source=self.source, peak_db=round(self.peak_db,1),
                bytes_out=self.bytes_out, pipe_clients=self.pipe_clients,
                errors=list(self.errors[-5:]), orig=dict(self.orig_state),
                game_running=self.game_running,
                vbcable_ok=self.vbcable_ok, vbcable_name=self.vbcable_name,
                routed_pids=dict(self.routed_pids),
                now_playing=dict(self.now_playing))

G = State()

# ---------------------------------------------------------------------------
# LAN IP
# ---------------------------------------------------------------------------
_forced_ip = ""
DEVICE_UUID = str(uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname() + "-urb3"))

def get_local_ip():
    if _forced_ip: return _forced_ip
    ips = []
    try:
        out = run_silent(subprocess.check_output,
            ["ipconfig"], encoding="cp866", errors="replace", timeout=3)
        for m in re.finditer(r"IPv4[^:]*:\s*([\d.]+)", out):
            ip = m.group(1).strip()
            if not ip.startswith("127."): ips.append(ip)
    except Exception: pass
    for prefix in ("192.168.", "10."):
        found = [ip for ip in ips if ip.startswith(prefix)]
        if found: return found[0]
    return ips[0] if ips else "127.0.0.1"

# ---------------------------------------------------------------------------
# Auto-install helper
# ---------------------------------------------------------------------------
def ensure_pkg(pkg, import_as=None):
    name = import_as or pkg
    try: __import__(name); return True
    except ImportError: pass
    log.info("pip install %s...", pkg)
    try:
        run_silent(subprocess.check_call,
                   [sys.executable, "-m", "pip", "install", pkg, "--quiet"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log.error("pip install %s failed: %s", pkg, e)
        with G._lk: G.errors.append("pip install %s failed" % pkg)
        return False

# ---------------------------------------------------------------------------
# VB-Cable capture
# ---------------------------------------------------------------------------
def vbcable_capture_thread(cfg):
    if not ensure_pkg("pyaudiowpatch"): return
    import pyaudiowpatch as pa_mod  # type: ignore
    pa = pa_mod.PyAudio()

    while not _shutdown.is_set():
        try:
            dev = None
            cable_names = ["cable output", "vb-audio", "fh6 radio",
                           "virtual cable", "cable input"]
            # Приоритет: входное устройство VB-Cable Output (что мы читаем)
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                nl = info.get("name", "").lower()
                if info.get("maxInputChannels", 0) > 0 and any(
                        n in nl for n in cable_names):
                    dev = info; break
            # Fallback: loopback
            if dev is None:
                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    nl = info.get("name", "").lower()
                    if (info.get("isLoopbackDevice") and
                            any(n in nl for n in cable_names)):
                        dev = info; break

            if dev is None:
                with G._lk:
                    G.vbcable_ok = False; G.vbcable_name = "Not found"
                    if "VB-Cable not found" not in G.errors:
                        G.errors.append("VB-Cable not found — install from vb-audio.com/Cable/")
                _shutdown.wait(5); continue

            with G._lk:
                G.vbcable_ok = True; G.vbcable_name = dev["name"]
                G.source = "vbcable"
                G.errors = [e for e in G.errors if "VB-Cable" not in e]

            rate  = int(dev["defaultSampleRate"])
            ch    = min(int(dev.get("maxInputChannels", 2)), 2)
            chunk = max(64, int(rate * cfg["buf_ms"] / 1000))
            log.info("[vbcable] '%s' %dHz %dch", dev["name"], rate, ch)

            stream = pa.open(format=pa_mod.paInt16, channels=ch, rate=rate,
                             input=True, input_device_index=int(dev["index"]),
                             frames_per_buffer=chunk)
            try:
                while not _shutdown.is_set():
                    raw = stream.read(chunk, exception_on_overflow=False)
                    RING.push(raw)
                    samples = struct.unpack("%dh" % (len(raw)//2), raw)
                    if samples:
                        pk = max(abs(x) for x in samples) / 32768.0
                        with G._lk:
                            G.peak_db = 20*math.log10(pk) if pk > 1e-9 else -60.0
            finally:
                stream.stop_stream(); stream.close()
        except Exception as e:
            log.error("[vbcable] %s", e)
            _shutdown.wait(3)

# ---------------------------------------------------------------------------
# Named pipe -> FMOD
# ---------------------------------------------------------------------------
PIPE_NAME = r"\\.\pipe\urb-pcm"

def pipe_thread(cfg):
    if sys.platform != "win32": return
    k32   = ctypes.windll.kernel32
    OB    = 0x00000002
    INV   = ctypes.wintypes.HANDLE(-1).value
    chunk = cfg["sample_rate"] * cfg["channels"] * 2 * 2
    fast  = 0

    while not _shutdown.is_set():
        pipe = k32.CreateNamedPipeW(PIPE_NAME, OB, 0, 1, chunk*4, 0, 0, None)
        if pipe == INV: _shutdown.wait(2); continue
        k32.ConnectNamedPipe(pipe, None)
        err = ctypes.get_last_error()
        if err not in (0, 535): k32.CloseHandle(pipe); _shutdown.wait(0.5); continue
        t0 = time.time()
        with G._lk: G.pipe_clients += 1
        try:
            iv = cfg["buf_ms"] / 1000.0 * 0.5
            while not _shutdown.is_set():
                pcm = RING.latest(chunk)
                wr  = ctypes.wintypes.DWORD(0)
                ok  = k32.WriteFile(pipe, pcm, len(pcm), ctypes.byref(wr), None)
                if not ok: break
                with G._lk: G.bytes_out += wr.value
                _shutdown.wait(iv)
        except Exception: pass
        finally:
            k32.CloseHandle(pipe)
            with G._lk: G.pipe_clients = max(0, G.pipe_clients - 1)
        dt = time.time() - t0
        if dt < 0.1:
            fast += 1
            if fast > 5: _shutdown.wait(min(10, 0.5 * fast))
        else:
            fast = 0

# ---------------------------------------------------------------------------
# Original mod state poller
# ---------------------------------------------------------------------------
def orig_poller_thread(cfg):
    import urllib.request
    url = "http://localhost:%d/api/state" % cfg["orig_port"]
    fails = 0
    while not _shutdown.is_set():
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                with G._lk: G.orig_state = json.loads(r.read())
                fails = 0
        except Exception:
            fails += 1
            if fails == 1: log.debug("[poller] port %d not responding", cfg["orig_port"])
        _shutdown.wait(1)

# ---------------------------------------------------------------------------
# Windows Media Session (SMTC) — Now Playing
# ---------------------------------------------------------------------------
def smtc_thread():
    # Try PyWinRT (winrt-*) first — works on Python 3.13.
    # Fall back to legacy `winsdk` for older Python.
    Manager = None
    backend = None
    try:
        from winrt.windows.media.control import (  # type: ignore
            GlobalSystemMediaTransportControlsSessionManager as Manager)
        backend = "winrt"
    except ImportError:
        pass
    if Manager is None:
        if ensure_pkg("winrt-Windows.Media.Control",
                      "winrt.windows.media.control"):
            try:
                from winrt.windows.media.control import (  # type: ignore
                    GlobalSystemMediaTransportControlsSessionManager as Manager)
                backend = "winrt"
            except ImportError:
                pass
    if Manager is None:
        try:
            from winsdk.windows.media.control import (  # type: ignore
                GlobalSystemMediaTransportControlsSessionManager as Manager)
            backend = "winsdk"
        except ImportError:
            if ensure_pkg("winsdk"):
                try:
                    from winsdk.windows.media.control import (  # type: ignore
                        GlobalSystemMediaTransportControlsSessionManager as Manager)
                    backend = "winsdk"
                except ImportError:
                    pass
    if Manager is None:
        log.warning("[smtc] Neither PyWinRT (winrt-*) nor winsdk available — "
                    "Now Playing disabled")
        return

    try:
        import asyncio

        async def _get():
            mgr = await Manager.request_async()
            cur = mgr.get_current_session()
            if not cur: return None
            props = await cur.try_get_media_properties_async()
            if not props: return None
            return {"title": props.title or "",
                    "artist": props.artist or "",
                    "app": cur.source_app_user_model_id or ""}

        log.info("[smtc] Windows media session active (%s)", backend)
        while not _shutdown.is_set():
            try:
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(_get())
                loop.close()
                if result:
                    with G._lk: G.now_playing = result
            except Exception as e:
                log.debug("[smtc] %s", e)
            _shutdown.wait(2)
    except Exception as e:
        log.warning("[smtc] unavailable: %s", e)

# ---------------------------------------------------------------------------
# App routing via Windows Audio Session API (pycaw)
# ---------------------------------------------------------------------------
_routed_originals = {}  # pid -> original device id
_route_lock = threading.Lock()

def get_audio_sessions():
    if not ensure_pkg("pycaw") or not ensure_pkg("comtypes"): return []
    try:
        from pycaw.pycaw import AudioUtilities  # type: ignore
        sessions = []
        for s in AudioUtilities.GetAllSessions():
            try:
                if s.Process is None: continue
                sessions.append({
                    "pid":  s.Process.pid,
                    "name": s.Process.name(),
                    "exe":  s.Process.exe() if hasattr(s.Process, "exe") else "",
                })
            except Exception: pass
        return sessions
    except Exception as e:
        log.debug("get_audio_sessions: %s", e)
        return []

def _find_cable_device_id(device_name: str):
    """Найти GUID устройства VB-Cable в реестре."""
    if not HAS_WINREG: return None
    try:
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root_key:
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(root_key, i)
                    prop_path = base + "\\" + guid + "\\Properties"
                    try:
                        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, prop_path) as pk:
                            try:
                                val, _ = winreg.QueryValueEx(
                                    pk, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")
                                nl = val.lower()
                                if ("cable" in nl or "fh6 radio" in nl
                                        or device_name.lower() in nl):
                                    return "{" + guid + "}"
                            except Exception: pass
                    except Exception: pass
                    i += 1
                except OSError: break
    except Exception as e:
        log.debug("_find_cable_device_id: %s", e)
    return None

def route_app_to_fh6(pid: int, device_name: str) -> bool:
    """
    Переключить приложение на VB-Cable через реестр PerProcess.
    Работает на Windows 10 v1803+.
    """
    if not HAS_WINREG: return False
    sessions = get_audio_sessions()
    exe_name = None
    for s in sessions:
        if s["pid"] == pid:
            exe_name = os.path.basename(s["exe"]) if s["exe"] else s["name"]
            break
    if not exe_name: return False

    dev_id = _find_cable_device_id(device_name)
    if not dev_id:
        log.warning("[route] VB-Cable device not found in registry")
        return False

    key_path = (r"Software\Microsoft\Windows\CurrentVersion"
                r"\Audio\PerProcess\%s" % exe_name)
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            # Сохранить текущее устройство для последующего восстановления
            try:
                old, _ = winreg.QueryValueEx(k, "DefaultRenderDeviceId")
            except Exception:
                old = ""
            winreg.SetValueEx(k, "DefaultRenderDeviceId",
                              0, winreg.REG_SZ, dev_id)
            with _route_lock:
                _routed_originals[pid] = (exe_name, old)
            log.info("[route] %s (PID %d) -> FH6 Radio (%s)", exe_name, pid, dev_id[:20])
            return True
    except PermissionError:
        log.warning("[route] Permission denied for %s - try running as Admin", exe_name)
        return False
    except Exception as e:
        log.error("[route] %s", e)
        return False

def unroute_app(pid: int) -> bool:
    with _route_lock:
        info = _routed_originals.pop(pid, None)
    if not info: return False
    exe_name, old_id = info
    key_path = (r"Software\Microsoft\Windows\CurrentVersion"
                r"\Audio\PerProcess\%s" % exe_name)
    try:
        if old_id:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path,
                                access=winreg.KEY_ALL_ACCESS) as k:
                winreg.SetValueEx(k, "DefaultRenderDeviceId",
                                  0, winreg.REG_SZ, old_id)
        else:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        log.info("[route] %s restored", exe_name)
        return True
    except Exception as e:
        log.debug("[route] unroute %s: %s", exe_name, e)
        return False

def unroute_all():
    with _route_lock:
        pids = list(_routed_originals.keys())
    for pid in pids:
        unroute_app(pid)
    log.info("[route] All apps restored")

# ---------------------------------------------------------------------------
# Game watchdog
# ---------------------------------------------------------------------------
def _is_game_running():
    """Detect forzahorizon6.exe using multiple methods."""
    # Method 1: tasklist (works for most installs)
    try:
        out = run_silent(subprocess.check_output,
            ["tasklist", "/fi", "imagename eq forzahorizon6.exe",
             "/fo", "csv", "/nh"],
            encoding="cp866", errors="replace", timeout=3)
        if "forzahorizon6" in out.lower():
            return True
    except Exception:
        pass
    # Method 2: wmic (more reliable for MS Store apps)
    try:
        out = run_silent(subprocess.check_output,
            ["wmic", "process", "where",
             "name='forzahorizon6.exe'", "get", "name"],
            encoding="cp866", errors="replace", timeout=3)
        if "forzahorizon6" in out.lower():
            return True
    except Exception:
        pass
    # Method 3: check if port 8103 is listening (original mod running = game running)
    try:
        import socket as _s
        sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        sock.settimeout(0.3)
        result = sock.connect_ex(("127.0.0.1", 8103))
        sock.close()
        if result == 0:
            return True
    except Exception:
        pass
    return False


def game_watchdog_thread(cfg):
    delay = cfg.get("shutdown_delay", 30)
    # Start as "seen=True" if game is already running when bridge starts
    # This ensures shutdown works even when bridge started manually
    seen = _is_game_running()
    gone = None
    if seen:
        log.info("[watchdog] Game already running at startup")
    while not _shutdown.is_set():
        running = _is_game_running()
        with G._lk: G.game_running = running
        if running:
            seen = True; gone = None
        elif seen:
            if gone is None:
                gone = time.time()
                log.info("[watchdog] Game exited, shutdown in %ds", delay)
            elif time.time() - gone >= delay:
                log.info("[watchdog] Shutdown now")
                unroute_all()
                _shutdown.set()
                PID_PATH.unlink(missing_ok=True)
                os._exit(0)
        _shutdown.wait(5)

# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------
def build_dashboard(cfg):
    s     = G.snap()
    orig  = s.get("orig", {})
    audio = orig.get("audio", {})
    track = orig.get("track", {})
    game  = orig.get("game", {})
    opts_orig = orig.get("options", {})
    np    = s.get("now_playing", {})
    ip    = get_local_ip()
    pct   = max(0.0, min(100.0, (s["peak_db"] + 60.0) / 60.0 * 100.0))

    # Game state — proc detection beats orig-mod polling, so if the game
    # process is alive we say so even when port 8103 hasn't been polled yet.
    if not s.get("game_running"):
        game_state = "&#x23F9; Game not running"
    elif audio.get("r10_active"):
        game_state = "&#x2705; Radio active"
    elif orig:
        game_state = "&#x1F3AE; Game running &mdash; switch to Spotify Radio station"
    else:
        game_state = "&#x1F3AE; Game running &mdash; orig mod loading&hellip;"

    # Audio path — what is actually carrying sound to FMOD right now.
    # The internal urb-pcm named pipe is reserved for a future custom
    # FMOD hook; until that ships, the orig mod's Spotify Connect path
    # is what drives in-game audio.  Showing "Waiting for game" here was
    # misleading because the pipe will simply never connect on its own.
    if s["pipe_clients"] > 0:
        audio_path = "&#x1F50C; FMOD pipe (%d)" % s["pipe_clients"]
    elif audio.get("r10_active"):
        audio_path = "&#x1F4FB; Spotify Connect (orig mod)"
    elif s["peak_db"] > -55:
        audio_path = "&#x1F39A; Capturing %.1f dB" % s["peak_db"]
    elif s["vbcable_ok"]:
        audio_path = "&#x1F507; Idle (no audio on VB-Cable)"
    else:
        audio_path = "&#x274C; VB-Cable not installed"

    # Options from orig mod or our config
    menu_pb = opts_orig.get("menuPlayback",        cfg.get("menuPlayback",        "pause"))
    race_st = opts_orig.get("raceStartPlayback",    cfg.get("raceStartPlayback",   "next"))
    vol_norm = opts_orig.get("volumeNormalization", cfg.get("volumeNormalization", "on"))
    eq_en   = opts_orig.get("equalizerEnabled",     cfg.get("equalizerEnabled",    False))
    eq_bands = opts_orig.get("equalizerBands",      cfg.get("equalizerBands",      [0,0,0,0,0]))

    t_title  = np.get("title")  or track.get("title",  "") or "&mdash;"
    t_artist = np.get("artist") or track.get("artist", "")
    t_app    = np.get("app", "")

    def ab(c): return " active" if c else ""

    vbc = ("&#x2705; " + s["vbcable_name"]) if s["vbcable_ok"] else "&#x274C; Not installed"

    # Audio sessions
    sessions = get_audio_sessions()
    routed   = s.get("routed_pids", {})
    sess_rows = ""
    for ss in sessions:
        pid  = ss["pid"]
        name = ss["name"]
        is_r = pid in routed
        if is_r:
            btn = ('<a href="/route?pid=%d&action=off" class="rbtn off">'
                   '&#x1F3A7;&rarr;&#x1F50A; Headphones</a>' % pid)
        else:
            btn = ('<a href="/route?pid=%d&action=on" class="rbtn on">'
                   '&#x1F50A;&rarr;&#x1F3AE; FH6</a>' % pid)
        sess_rows += ('<div class="srow"><span class="sname">%s</span>'
                      '<span class="spid">%d</span>%s</div>'
                      % (name, pid, btn))
    if not sess_rows:
        sess_rows = '<div class="no-sess">No audio apps found</div>'

    vbc_warn = ""
    if not s["vbcable_ok"]:
        vbc_warn = ('<div class="card warn">'
                    '<div class="lbl">&#x26A0; VB-Cable required</div>'
                    '<p style="margin-top:8px;font-size:.84rem;color:#ccc">'
                    'Download free from '
                    '<a href="https://vb-audio.com/Cable/" target="_blank" '
                    'style="color:#1db954">vb-audio.com/Cable/</a><br>'
                    'After install: in Spotify/Yandex/browser change output device '
                    'to <b>CABLE Input</b></p></div>')

    errs = ('<div class="card err">%s</div>' % "<br>".join(s["errors"])
            if s["errors"] else "")

    # Equalizer sliders
    band_labels = ["60Hz", "250Hz", "1kHz", "4kHz", "16kHz"]
    eq_sliders = ""
    for i, (label, val) in enumerate(zip(band_labels, eq_bands)):
        eq_sliders += (
            '<div class="eq-band">'
            '<input type="range" min="-12" max="12" step="1" '
            'value="%d" name="eq_%d" '
            'oninput="this.nextElementSibling.textContent=this.value+\'dB\'">'
            '<span>%ddB</span>'
            '<div class="eq-label">%s</div>'
            '</div>' % (val, i, val, label))

    return ("""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>URB v3.1 - Universal Radio Bridge</title>
<meta http-equiv="refresh" content="3">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0d;color:#e0e0e0;font-family:'Segoe UI',sans-serif;
     padding:24px;max-width:720px;margin:auto}
h1{color:#1db954;font-size:1.4rem;margin-bottom:2px}
.sub{color:#555;font-size:.78rem;margin-bottom:18px}
.sub a{color:#1db954;text-decoration:none}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.card{background:#181818;border-radius:9px;padding:14px 16px;margin-bottom:10px}
.lbl{font-size:.66rem;text-transform:uppercase;letter-spacing:.09em;
     color:#555;margin-bottom:5px}
.val{font-size:1rem;font-weight:600}
.bar{height:5px;background:#222;border-radius:3px;overflow:hidden;margin-top:8px}
.fill{height:100%%;background:linear-gradient(90deg,#1db954,#7c3aed);border-radius:3px}
.opts{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.opt-btn{background:#222;border:1px solid #333;color:#888;
         padding:3px 10px;border-radius:6px;font-size:.74rem;text-decoration:none}
.opt-btn.active{background:#0a2e1a;border-color:#1db954;color:#1db954}
select,input[type=text]{background:#222;border:1px solid #333;color:#e0e0e0;
       padding:4px 8px;border-radius:6px;font-size:.78rem;margin-top:6px}
button,.sbtn{background:#1db954;border:none;color:#000;
        padding:4px 11px;border-radius:6px;font-size:.78rem;
        cursor:pointer;margin-left:6px;margin-top:6px;text-decoration:none;
        display:inline-block}
.srow{display:flex;align-items:center;gap:10px;padding:6px 0;
      border-bottom:1px solid #1e1e1e}
.srow:last-child{border-bottom:none}
.sname{flex:1;font-size:.85rem}
.spid{font-size:.7rem;color:#555;min-width:55px}
.rbtn{padding:3px 10px;border-radius:6px;font-size:.76rem;text-decoration:none;
      border:1px solid #333;background:#222;color:#888;white-space:nowrap}
.rbtn.on{border-color:#1db954;color:#1db954}
.rbtn.off{border-color:#e74c3c;color:#e74c3c}
.no-sess{color:#555;font-size:.8rem;padding:8px 0}
.warn{border:1px solid #f39c12;background:#1a1500}
.err{color:#e74c3c;font-size:.78rem}
.eq-band{display:flex;flex-direction:column;align-items:center;gap:4px;flex:1}
.eq-band input[type=range]{writing-mode:vertical-lr;direction:rtl;
    height:80px;width:24px;background:transparent;cursor:pointer}
.eq-band span{font-size:.7rem;color:#888;min-width:36px;text-align:center}
.eq-label{font-size:.65rem;color:#555}
.eq-row{display:flex;gap:8px;align-items:flex-end;padding:10px 0 4px}
.toggle{position:relative;display:inline-block;width:36px;height:20px;margin-left:8px;vertical-align:middle}
.toggle input{opacity:0;width:0;height:0}
.slider-t{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;
          background:#333;border-radius:20px;transition:.2s}
.slider-t:before{position:absolute;content:"";height:14px;width:14px;left:3px;bottom:3px;
                 background:#888;border-radius:50%%;transition:.2s}
input:checked + .slider-t{background:#1db954}
input:checked + .slider-t:before{transform:translateX(16px);background:#000}
.footer{font-size:.65rem;color:#2a2a2a;margin-top:14px}
</style></head><body>
<h1>Universal Radio Bridge</h1>
<div class="sub">v3.1 &nbsp;&middot;&nbsp;
<a href="/api/status">JSON</a> &nbsp;&middot;&nbsp;
<a href="http://localhost:%(orig_port)d" target="_blank">Spotify Radio UI &#x2192;</a>
</div>
""" + vbc_warn + """
<div class="g2">
<div class="card"><div class="lbl">Virtual Speaker (VB-Cable)</div>
<div class="val">%(vbc)s</div></div>
<div class="card"><div class="lbl">Game State</div>
<div class="val" style="font-size:.88rem">%(game_state)s</div></div>
</div>

<div class="card">
<div class="lbl">Now Playing <span style="color:#444;font-weight:400;margin-left:6px">via Windows Media Session</span></div>
<div style="font-size:.95rem;color:#ddd;margin-top:4px">%(t_title)s</div>
<div style="font-size:.82rem;color:#777;margin-top:2px">%(t_artist)s</div>
<div style="font-size:.7rem;color:#444;margin-top:2px">%(t_app)s</div>
</div>

<div class="g2">
<div class="card"><div class="lbl">Signal %(db).1f dB</div>
<div class="bar"><div class="fill" style="width:%(pct).1f%%"></div></div></div>
<div class="card"><div class="lbl">Audio Path</div>
<div class="val" style="font-size:.88rem">%(audio_path)s</div></div>
</div>

<div class="card">
<div class="lbl">&#x1F4F1; App Routing — switch apps between FH6 Radio and headphones</div>
<div style="margin-top:6px">%(sess_rows)s</div>
<div style="margin-top:8px">
<a href="/route?action=restore_all" class="sbtn" style="background:#c0392b">
&#x21A9; Restore all to headphones</a>
</div>
</div>

<div class="card">
<div class="lbl">Playback Settings</div>
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:6px">
<div>
<div style="font-size:.7rem;color:#777;margin-bottom:3px">Menu open:</div>
<div class="opts">
<a class="opt-btn%(pause_a)s" href="/set_option?menuPlayback=pause">Pause</a>
<a class="opt-btn%(cont_a)s"  href="/set_option?menuPlayback=continue">Continue</a>
</div></div>
<div>
<div style="font-size:.7rem;color:#777;margin-bottom:3px">Race start:</div>
<div class="opts">
<a class="opt-btn%(rs_a)s"  href="/set_option?raceStartPlayback=restart">Restart</a>
<a class="opt-btn%(rn_a)s"  href="/set_option?raceStartPlayback=next">Next</a>
<a class="opt-btn%(rk_a)s"  href="/set_option?raceStartPlayback=keep">Keep</a>
</div></div>
<div>
<div style="font-size:.7rem;color:#777;margin-bottom:3px">Volume normalize:</div>
<div class="opts">
<a class="opt-btn%(vn_on_a)s"  href="/set_option?volumeNormalization=on">On</a>
<a class="opt-btn%(vn_off_a)s" href="/set_option?volumeNormalization=off">Off</a>
</div></div>
</div>
</div>

<div class="card">
<div class="lbl">5-Band Equalizer
<label class="toggle"><input type="checkbox" id="eq_toggle" %(eq_checked)s
  onchange="fetch('/set_option?equalizerEnabled='+this.checked)">
<span class="slider-t"></span></label>
<span style="color:#555;font-size:.7rem;margin-left:6px">%(eq_state)s</span>
</div>
<form action="/set_eq" method="get">
<div class="eq-row">%(eq_sliders)s</div>
<button type="submit">Apply EQ</button>
</form>
</div>

<div class="card">
<div class="lbl">LAN IP <span style="color:#444;font-weight:400;margin-left:6px">(fix if VPN shows wrong)</span></div>
<form action="/set_option" method="get" style="display:flex;align-items:center;flex-wrap:wrap">
<input type="text" name="lan_ip" value="%(lan_ip)s" style="width:150px" placeholder="192.168.1.80">
<button type="submit">Set</button>
<span style="font-size:.7rem;color:#555;margin-left:10px;margin-top:6px">Current: <b>%(ip)s</b></span>
</form>
</div>
""" + errs + """
<div class="footer">
Pipe clients: %(cli)d &nbsp;&middot;&nbsp;
Transferred: %(mb).1f MB &nbsp;&middot;&nbsp;
Shutdown %(shutdown)ds after game exit &nbsp;&middot;&nbsp;
Auto-refresh 3s
</div>
</body></html>""") % {
        "orig_port": cfg.get("orig_port", 8103),
        "vbc": vbc, "game_state": game_state,
        "t_title": t_title, "t_artist": t_artist, "t_app": t_app,
        "db": s["peak_db"], "pct": pct,
        "audio_path": audio_path,
        "sess_rows": sess_rows,
        "pause_a": ab(menu_pb=="pause"),    "cont_a": ab(menu_pb=="continue"),
        "rs_a": ab(race_st=="restart"),     "rn_a": ab(race_st=="next"),
        "rk_a": ab(race_st=="keep"),
        "vn_on_a": ab(vol_norm=="on"),      "vn_off_a": ab(vol_norm=="off"),
        "eq_checked": "checked" if eq_en else "",
        "eq_state": "Enabled" if eq_en else "Disabled",
        "eq_sliders": eq_sliders,
        "lan_ip": cfg.get("lan_ip",""), "ip": ip,
        "cli": s["pipe_clients"], "mb": s["bytes_out"]/1e6,
        "shutdown": cfg.get("shutdown_delay", 30),
    }

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    cfg = {}
    def log_message(self, *a): pass

    def do_GET(self):
        p  = urlparse(self.path)
        pt = p.path
        qs = parse_qs(p.query)
        if pt in ("/", "/dashboard"):
            self._send(build_dashboard(load_cfg()).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif pt == "/api/status":
            data = G.snap()
            self._send(json.dumps(data, ensure_ascii=False).encode(),
                       "application/json")
        elif pt == "/route":
            self._handle_route(qs)
        elif pt == "/set_option":
            self._set_option(qs)
        elif pt == "/set_eq":
            self._set_eq(qs)
        else:
            self.send_error(404)

    def do_POST(self):
        pt = urlparse(self.path).path
        n  = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode(errors="replace") if n else ""
        if pt == "/api/options":
            try:
                opts = json.loads(body)
                c = load_cfg()
                for k in ("menuPlayback","raceStartPlayback",
                          "volumeNormalization","equalizerEnabled","equalizerBands"):
                    if k in opts: c[k] = opts[k]
                save_cfg(c)
                sync_orig_options(c)
                self._send(json.dumps(opts).encode(), "application/json")
            except Exception as e:
                self._send(json.dumps({"error":str(e)}).encode(), "application/json")
        else:
            self.send_error(404)

    def _handle_route(self, qs):
        action = qs.get("action",[""])[0]
        cfg    = load_cfg()
        dev    = cfg.get("device_name", "FH6 Radio")
        if action == "restore_all":
            unroute_all()
            with G._lk: G.routed_pids.clear()
        elif action == "on":
            try:
                pid = int(qs.get("pid",[0])[0])
                if route_app_to_fh6(pid, dev):
                    sessions = get_audio_sessions()
                    name = next((s["name"] for s in sessions if s["pid"]==pid), str(pid))
                    with G._lk: G.routed_pids[pid] = name
            except Exception as e: log.error("[route] on: %s", e)
        elif action == "off":
            try:
                pid = int(qs.get("pid",[0])[0])
                unroute_app(pid)
                with G._lk: G.routed_pids.pop(pid, None)
            except Exception as e: log.error("[route] off: %s", e)
        self.send_response(302)
        self.send_header("Location", "/")
        self.end_headers()

    def _set_option(self, qs):
        global _forced_ip
        c = load_cfg()
        changed = False
        for key, allowed in [
            ("menuPlayback",        ["pause","continue"]),
            ("raceStartPlayback",   ["restart","next","keep"]),
            ("volumeNormalization", ["on","off"]),
        ]:
            if key in qs:
                v = qs[key][0]
                if v in allowed: c[key] = v; changed = True
        if "equalizerEnabled" in qs:
            v = qs["equalizerEnabled"][0].lower()
            c["equalizerEnabled"] = v in ("true","1","on"); changed = True
        if "lan_ip" in qs:
            _forced_ip = qs["lan_ip"][0].strip()
            c["lan_ip"] = _forced_ip; changed = True
        if changed:
            save_cfg(c); sync_orig_options(c)
        self.send_response(302)
        self.send_header("Location", "/")
        self.end_headers()

    def _set_eq(self, qs):
        c = load_cfg()
        bands = list(c.get("equalizerBands", [0,0,0,0,0]))
        for i in range(5):
            key = "eq_%d" % i
            if key in qs:
                try: bands[i] = max(-12, min(12, int(qs[key][0])))
                except ValueError: pass
        c["equalizerBands"] = bands
        save_cfg(c); sync_orig_options(c)
        self.send_response(302)
        self.send_header("Location", "/")
        self.end_headers()

    def _send(self, data, ct):
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

def http_server_thread(cfg):
    Handler.cfg = cfg
    port = cfg["http_port"]
    # Попробовать освободить порт если занят
    try:
        import socket as _s
        test = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        test.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        test.bind(("0.0.0.0", port))
        test.close()
    except OSError:
        log.warning("[http] Port %d busy, waiting 3s...", port)
        time.sleep(3)
    try:
        srv = HTTPServer(("0.0.0.0", port), Handler)
        log.info("[http] http://localhost:%d", port)
        srv.serve_forever()
    except Exception as e:
        log.error("[http] FAILED: %s", e)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    global RING, _forced_ip

    kill_old_instance()
    write_pid()
    atexit.register(PID_PATH.unlink, missing_ok=True)
    atexit.register(unroute_all)

    log.info("=== Universal Radio Bridge v3.1 (PID %d) ===", os.getpid())
    cfg = load_cfg()

    if cfg.get("lan_ip"):
        _forced_ip = cfg["lan_ip"].strip()
    log.info("LAN IP: %s", get_local_ip())

    RING = Ring(sec=12, rate=cfg["sample_rate"], ch=cfg["channels"])

    tasks = [
        threading.Thread(target=vbcable_capture_thread, args=(cfg,),
                         daemon=True, name="vbcable"),
        threading.Thread(target=pipe_thread, args=(cfg,),
                         daemon=True, name="pipe"),
        threading.Thread(target=orig_poller_thread, args=(cfg,),
                         daemon=True, name="poller"),
        threading.Thread(target=game_watchdog_thread, args=(cfg,),
                         daemon=True, name="watchdog"),
        threading.Thread(target=smtc_thread,
                         daemon=True, name="smtc"),
        threading.Thread(target=http_server_thread, args=(cfg,),
                         daemon=True, name="http"),
    ]
    for t in tasks: t.start()

    log.info("Dashboard: http://localhost:%d", cfg["http_port"])
    _shutdown.wait()

if __name__ == "__main__":
    main()
