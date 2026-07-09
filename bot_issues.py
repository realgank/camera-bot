# -*- coding: utf-8 -*-
"""Волна D — журнал проблем (_issues.json):
161 snooze «в ремонте до…» (кнопки + напоминание по истечении),
163 эскалация висящих (метка escalated), 164 статусы open → in_progress →
fixed кнопками под алертом, 165 MTTR, 166 счётчик ремонтов «под замену».
Команда /issues — открытые проблемы. Пароли камер НЕ меняются."""
import time
import datetime

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_store as store
from bot_tg import send, send_chunks, answer_cq, edit_message
from bot_util import log, esc

_DEF = {"seq": 0, "issues": [], "quiet": []}


def _path():
    return st.cget("issues_path")


def data() -> dict:
    return store.jload(_path(), _DEF)


def get_open(ip: str):
    """Последняя незакрытая проблема по IP или None."""
    for it in reversed(data()["issues"]):
        if it["ip"] == ip and it["status"] in ("open", "in_progress"):
            return it
    return None


def open_issue(ip: str, now: float = None) -> dict:
    """164: завести проблему (если открытой нет). Возвращает запись."""
    now = now or time.time()
    res = {}
    def _fn(d):
        for it in reversed(d["issues"]):
            if it["ip"] == ip and it["status"] in ("open", "in_progress"):
                res.update(it)
                return None
        d["seq"] = int(d.get("seq") or 0) + 1
        it = {"id": d["seq"], "ip": ip, "opened": int(now), "status": "open",
              "taken": None, "fixed": None, "note": "",
              "escalated": None, "snooze_until": None, "snooze_ping": False}
        d["issues"].append(it)
        d["issues"] = d["issues"][-2000:]
        res.update(it)
        return d
    store.jupdate(_path(), _DEF, _fn)
    return res


def set_status(ip: str, status: str, note: str = "") -> bool:
    """open→in_progress («взял») → fixed («починил»)."""
    ok = [False]
    now = int(time.time())
    def _fn(d):
        for it in reversed(d["issues"]):
            if it["ip"] == ip and it["status"] in ("open", "in_progress"):
                it["status"] = status
                if status == "in_progress":
                    it["taken"] = it.get("taken") or now
                if status == "fixed":
                    it["fixed"] = now
                    it["snooze_until"] = None
                if note:
                    it["note"] = (it.get("note") or "") + note
                ok[0] = True
                return d
        return None
    store.jupdate(_path(), _DEF, _fn)
    return ok[0]


def snooze(ip: str, until_ts: int) -> None:
    """161: «в ремонте до …» — алерты подавлены до срока."""
    open_issue(ip)
    def _fn(d):
        for it in reversed(d["issues"]):
            if it["ip"] == ip and it["status"] in ("open", "in_progress"):
                it["snooze_until"] = int(until_ts)
                it["snooze_ping"] = False
                if it["status"] == "open":
                    it["status"] = "in_progress"
                    it["taken"] = it.get("taken") or int(time.time())
                return d
        return None
    store.jupdate(_path(), _DEF, _fn)


def snoozed(ip: str) -> bool:
    it = get_open(ip)
    return bool(it and it.get("snooze_until")
                and it["snooze_until"] > time.time())


def mark_escalated(ip: str) -> None:
    def _fn(d):
        for it in reversed(d["issues"]):
            if it["ip"] == ip and it["status"] in ("open", "in_progress"):
                it["escalated"] = int(time.time())
                return d
        return None
    store.jupdate(_path(), _DEF, _fn)


def open_issues() -> list:
    return [it for it in data()["issues"]
            if it["status"] in ("open", "in_progress")]


def mttr(days: float = 30, ips: set = None):
    """165: (среднее время устранения в сек, число закрытых) за окно."""
    cut = time.time() - days * 86400
    ds = [it["fixed"] - it["opened"] for it in data()["issues"]
          if it["status"] == "fixed" and (it.get("fixed") or 0) >= cut
          and it.get("opened") and (ips is None or it["ip"] in ips)]
    return (sum(ds) / len(ds), len(ds)) if ds else (None, 0)


def repairs_count(ip: str, days: float = 90) -> int:
    """166: сколько раз чинили за период."""
    cut = time.time() - days * 86400
    return sum(1 for it in data()["issues"]
               if it["ip"] == ip and it["status"] == "fixed"
               and (it.get("fixed") or 0) >= cut)


def is_chronic(ip: str) -> bool:
    """166: N+ ремонтов за квартал → «хроническая, кандидат на замену»."""
    return repairs_count(ip, 90) >= int(st.cget("chronic_repairs"))


def chronic_mark(ip: str) -> str:
    return "🔁 хроническая, кандидат на замену" if is_chronic(ip) else ""


def issue_kb(ip: str) -> dict:
    """Кнопки статусов под алертом (164) + snooze (161) + диагностика."""
    return {"inline_keyboard": [
        [{"text": "🩺 Диаг", "callback_data": f"diag:{ip}"},
         {"text": "📸 Снимок", "callback_data": f"shot:{ip}"}],
        [{"text": "🔧 Взял в работу", "callback_data": f"iw:{ip}"},
         {"text": "✅ Починил", "callback_data": f"ifx:{ip}"}],
        [{"text": "😴 В ремонте до…", "callback_data": f"isnz:{ip}"}]]}


def _fmt_issue(it, now=None) -> str:
    now = now or time.time()
    age = int(now - it["opened"])
    age_s = f"{age // 3600}ч {(age % 3600) // 60}м" if age >= 3600 else f"{age // 60}м"
    stat = {"open": "🔴 открыта", "in_progress": "🔧 в работе",
            "fixed": "✅ починена"}.get(it["status"], it["status"])
    lbl = inv.label(it["ip"])
    s = (f"#{it['id']} <code>{it['ip']}</code>"
         + (f" {esc(lbl)}" if lbl else "") + f" — {stat}, висит {age_s}")
    if it.get("snooze_until") and it["snooze_until"] > now:
        s += ("\n  😴 в ремонте до "
              + time.strftime("%d.%m %H:%M", time.localtime(it["snooze_until"])))
    if it.get("note"):
        s += f"\n  📝 {esc(it['note'][:120])}"
    if is_chronic(it["ip"]):
        s += "\n  🔁 хроническая (кандидат на замену)"
    return s


def cmd_issues(chat, arg="", reply_to=None):
    its = open_issues()
    if not its:
        avg, n = mttr(30)
        s = "✅ Открытых проблем нет."
        if n:
            s += f"\n⏱ MTTR за 30 дн: {avg / 3600:.1f} ч (закрыто {n})."
        send(chat, s, reply_to=reply_to)
        return
    lines = [f"🗂 <b>Открытые проблемы: {len(its)}</b>"]
    lines += [_fmt_issue(it) for it in its[:25]]
    avg, n = mttr(30)
    if n:
        lines.append(f"\n⏱ MTTR за 30 дн: {avg / 3600:.1f} ч (закрыто {n})")
    send_chunks(chat, lines)
    rows = [[{"text": f"🔧 Взял {it['ip']}", "callback_data": f"iw:{it['ip']}"},
             {"text": "✅ Починил", "callback_data": f"ifx:{it['ip']}"}]
            for it in its[:6]]
    if rows:
        send(chat, "Быстрые статусы:", silent=True,
             markup={"inline_keyboard": rows})


# ---------- колбэки ----------
def cb_iw(chat, cq, ip):
    if not net.valid_ip(ip):
        answer_cq(cq.get("id"))
        return
    open_issue(ip)
    set_status(ip, "in_progress")
    log(f"issue: {ip} -> in_progress")
    answer_cq(cq.get("id"), "🔧 Взято в работу")
    send(chat, f"🔧 <code>{ip}</code> — взято в работу. Закрыть: кнопкой "
               f"«✅ Починил» или /issues.", silent=True)


def cb_ifx(chat, cq, ip):
    if not net.valid_ip(ip):
        answer_cq(cq.get("id"))
        return
    it = get_open(ip)
    if not it:
        answer_cq(cq.get("id"), "Открытой проблемы нет")
        return
    set_status(ip, "fixed")
    dur = int(time.time() - it["opened"])
    log(f"issue: {ip} -> fixed за {dur}s")
    answer_cq(cq.get("id"), "✅ Починено")
    extra = f"\n{chronic_mark(ip)}" if is_chronic(ip) else ""
    send(chat, f"✅ <code>{ip}</code> — починена за "
               f"{dur // 3600}ч {(dur % 3600) // 60}м (в MTTR).{extra}",
         markup={"inline_keyboard": [[
             {"text": "📸 Проверить снимком", "callback_data": f"shot:{ip}"}]]})


def cb_isnz(chat, cq, ip):
    """Меню срока snooze (161)."""
    if not net.valid_ip(ip):
        answer_cq(cq.get("id"))
        return
    answer_cq(cq.get("id"))
    send(chat, f"😴 <code>{ip}</code> — в ремонте до:",
         markup={"inline_keyboard": [[
             {"text": "конца дня", "callback_data": f"isnd:{ip}:0"},
             {"text": "завтра", "callback_data": f"isnd:{ip}:1"},
             {"text": "недели", "callback_data": f"isnd:{ip}:7"}]]})


def cb_isnd(chat, cq, payload):
    ip, _, days = payload.partition(":")
    if not net.valid_ip(ip):
        answer_cq(cq.get("id"))
        return
    try:
        d = int(days)
    except ValueError:
        d = 1
    base = datetime.datetime.now().replace(hour=23, minute=59, second=0,
                                           microsecond=0)
    until = base + datetime.timedelta(days=d)
    snooze(ip, int(until.timestamp()))
    answer_cq(cq.get("id"), "😴 Принято")
    send(chat, f"😴 <code>{ip}</code> — в ремонте до "
               f"{until:%d.%m %H:%M}: алерты подавлены, по истечении срока "
               f"напомню, если всё ещё офлайн.", silent=True)


HANDLERS = {"/issues": cmd_issues}
ALIASES = {"/проблемы": "/issues"}
CALLBACKS = {"iw": cb_iw, "ifx": cb_ifx, "isnz": cb_isnz, "isnd": cb_isnd}
