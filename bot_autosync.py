# -*- coding: utf-8 -*-
"""Волна J — I27: автосинк xlsx → Google Sheets по таймеру.
Выключен по умолчанию (autosync_hours = 0). Логика: любая запись бота в xlsx
помечает dirty (mark_dirty — зовётся из bot_reconcile.after_xlsx_write, т.е.
из /note, apply_writes, /provision, /macfill, «Неизвестных»); минутный тик —
если dirty и с прошлого автосинка прошло N часов — БЕРЕЖНЫЙ дифф-синк волны I
(bot_gsheets2.diff_sync: снапшот-копия перед записью, ручные колонки не
трогаются, идемпотентен). /autosync on|off|N — управление."""
import time
import threading

import bot_state as st
import bot_store as store
from bot_util import log, log_exc, esc

_run_lock = threading.Lock()


def _spath():
    return st.cget("autosync_state_path")


def mark_dirty(reason=""):
    """Пометить xlsx изменённым (для тика автосинка)."""
    try:
        store.jupdate(_spath(), {}, lambda d: {
            **d, "dirty": True, "dirty_ts": time.time(),
            "reason": str(reason)[:120]})
    except Exception:
        log_exc("autosync: mark_dirty")


def clear_dirty():
    store.jupdate(_spath(), {}, lambda d: {**d, "dirty": False,
                                           "last_run": time.time()})


def state():
    return store.jload(_spath(), {})


def should_sync(dirty, hours, last_run, now):
    """Чистая: пора ли синкать. hours<=0 = выключено; не чаще раза в hours."""
    try:
        hours = float(hours or 0)
    except (TypeError, ValueError):
        return False
    if hours <= 0 or not dirty:
        return False
    return (now - float(last_run or 0)) >= hours * 3600


def _tick():
    s = state()
    if not should_sync(s.get("dirty"), st.cget("autosync_hours"),
                       s.get("last_run"), time.time()):
        return
    if not _run_lock.acquire(blocking=False):
        return
    try:
        log(f"autosync: запускаю дифф-синк (dirty: {s.get('reason')})")
        import bot_gsheets2
        txt, _kb = bot_gsheets2.diff_sync(dry=False)
        clear_dirty()
        try:
            import bot_metrics as mx
            mx.owner_alert("🤖 <b>Автосинк</b> (I27, /autosync):\n" + txt,
                           silent=True)
        except Exception:
            pass
    except Exception:
        log_exc("autosync: дифф-синк упал (dirty остаётся, повтор через N ч)")
        store.jupdate(_spath(), {}, lambda d: {**d, "last_run": time.time()})
    finally:
        _run_lock.release()


def cmd_autosync(chat, arg="", reply_to=None):
    from bot_tg import send
    a = (arg or "").strip().lower()
    if a:
        if a in ("off", "0", "выкл"):
            hours = 0
        elif a in ("on", "вкл"):
            hours = 6
        else:
            try:
                hours = max(0, min(int(a), 168))
            except ValueError:
                send(chat, "Автосинк: <code>/autosync on|off|N</code> "
                           "(N — часов между синками, 0 = выкл).",
                     reply_to=reply_to)
                return
        with st._cfg_lock:
            st.CFG["autosync_hours"] = hours
            st.save_cfg()
        log(f"autosync: hours -> {hours}")
    hours = st.cget("autosync_hours")
    s = state()
    lines = [f"🔁 <b>Автосинк xlsx → Sheets</b> (I27): "
             + (f"<b>вкл</b>, раз в {hours} ч" if hours else "<b>выкл</b>")]
    lines.append("xlsx: " + ("🟡 есть несинкованные правки "
                             f"({esc(s.get('reason') or '?')})"
                             if s.get("dirty") else "🟢 чистый"))
    if s.get("last_run"):
        lines.append("последний автосинк: "
                     + time.strftime("%d.%m %H:%M",
                                     time.localtime(s["last_run"])))
    lines.append("Синк бережный (дифф волны I): снапшот-копия перед записью, "
                 "ручные колонки таблицы не трогаются.\n"
                 "Вручную: /sync · /sync dry")
    send(chat, "\n".join(lines), reply_to=reply_to)


HANDLERS = {"/autosync": cmd_autosync}
ALIASES = {"/автосинк": "/autosync"}
CALLBACKS = {}

try:
    import bot_health as _bh
    _bh.MINUTE_TICKS.append(_tick)
except Exception:
    log_exc("autosync: тик не зарегистрировался")
