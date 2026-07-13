import os
# -*- coding: utf-8 -*-
"""Общие утилиты бота: логирование с ротацией, экранирование HTML,
человеческие тексты ошибок, память процесса.
Волна A: R1-R3 (даты/уровни/ротация), R50 (юникод в консоли),
R35/U19 (esc), U20/U48 (шаблон ошибок), часть R39 (mem_mb).
Волна E: LOG_HOOKS (201/207 — NDJSON и ring-buffer в bot_obs),
TRACE_FN (202 — trace-id апдейта в каждой строке лога).
"""
import sys
import html
import logging
import logging.handlers

BOT_VERSION = "2.9-J (2026-07-09)"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_bot.log")

_logger = None

# Волна E: хуки логирования. LOG_HOOKS — список fn(level:int, msg:str),
# вызываются на каждую запись (bot_obs пишет NDJSON и ring-buffer);
# TRACE_FN[0] — callable -> короткий trace-id текущего апдейта или None.
LOG_HOOKS: list = []
TRACE_FN: list = [None]


def setup_logging():
    """R1: дата %Y-%m-%d %H:%M:%S; R2: модуль logging с уровнями;
    R3: ротация 5МБ x 5; R50: консоль не падает на юникоде (errors=replace)."""
    global _logger
    if _logger is not None:
        return _logger
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    lg = logging.getLogger("camera_bot")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    try:
        # utf-8-sig: BOM в начале файла, чтобы PowerShell (Get-Content без
        # -Encoding) читал кириллицу лога без кракозябр
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8-sig")
        fh.setFormatter(fmt)
        lg.addHandler(fh)
    except Exception:
        pass  # нет прав на лог — работаем хотя бы в консоль
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    lg.addHandler(ch)
    _logger = lg
    return lg


def _traced(msg):
    """202: подмешивает [trace-id] в начало строки, если апдейт его установил."""
    fn = TRACE_FN[0]
    if fn:
        try:
            tid = fn()
            if tid:
                return f"[{tid}] {msg}"
        except Exception:
            pass
    return msg


def _run_hooks(level, msg):
    for h in list(LOG_HOOKS):
        try:
            h(level, msg)
        except Exception:
            pass


def log(msg, level=logging.INFO):
    msg = _traced(msg)
    setup_logging().log(level, msg)
    _run_hooks(level, msg)


def log_exc(msg):
    """R2: лог ошибки с трейсбеком (exc_info)."""
    import traceback
    msg = _traced(msg)
    setup_logging().error(msg, exc_info=True)
    _run_hooks(logging.ERROR, msg + "\n" + traceback.format_exc())


def esc(s):
    """R35/U19: экранирование ЛЮБЫХ подстановок в HTML-сообщения."""
    return html.escape(str(s), quote=False)


# ---------- U20/U48: человеческие тексты ошибок ----------
# (подстрока в сырой ошибке, причина, что делать)
_HINTS = (
    ("timed out",        "камера не отвечает по сети (таймаут)",
     "проверь PoE/линк на порту свитча и пинг до камеры"),
    ("timeout",          "камера не отвечает по сети (таймаут)",
     "проверь PoE/линк на порту свитча и пинг до камеры"),
    ("refused",          "порт закрыт — хост жив, но сервис не слушает",
     "проверь, что это камера; попробуй порт 8080 кнопкой ниже"),
    ("connectionerror",  "хост недоступен (нет маршрута или сброс соединения)",
     "проверь IP, подсеть и линк; возможно камера обесточена"),
    ("connection",       "проблема соединения с хостом",
     "проверь IP и доступность подсети с этой машины"),
    ("401",              "не подошли креды ONVIF",
     "проверь пользователя/пароль (env CAM_USER/CAM_PASS); пароли бот НЕ меняет"),
    ("unauthorized",     "не подошли креды ONVIF",
     "проверь пользователя/пароль (env CAM_USER/CAM_PASS); пароли бот НЕ меняет"),
    ("no profile token", "ONVIF отвечает, но медиа-профилей нет",
     "попробуй порт 8080 или зайди в веб-интерфейс камеры"),
    ("no model",         "ONVIF-ответ без данных устройства",
     "возможно это не камера или нестандартная прошивка"),
    ("порты onvif закрыты", "порты 80/8080 на хосте закрыты",
     "камера с нестандартным портом — или это вовсе не камера"),
    ("getsnapshoturi",   "камера не отдала URL снимка",
     "попробуй порт 8080 или проверь камеру в веб-интерфейсе"),
)


def human_err(what, raw):
    """U48: единый шаблон ошибки «что -> причина -> что делать».
    what — уже готовый HTML (не экранируем), raw — сырая ошибка (экранируем)."""
    raw_s = str(raw)
    low = raw_s.lower()
    cause, hint = "неизвестная ошибка", "посмотри /log и повтори попытку"
    for sub, c, h in _HINTS:
        if sub in low:
            cause, hint = c, h
            break
    return (f"❌ {what}\n"
            f"<b>Причина:</b> {esc(cause)}\n"
            f"💡 {esc(hint)}\n"
            f"<code>{esc(raw_s)[:200]}</code>")


def mem_mb():
    """Рабочий набор процесса в МБ (для /status, R39). Windows, через psapi."""
    try:
        import ctypes
        import ctypes.wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return None
