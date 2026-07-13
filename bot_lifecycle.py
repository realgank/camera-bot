# -*- coding: utf-8 -*-
"""Волна D — жизненный цикл и справочники: 196 /lifecycle (active / in_repair /
dismantled / planned в _lifecycle.json; демонтированные вне health/обходов),
197 /remind (напоминания к камере, минутный тик), 198 /kit (что взять с собой),
199 /contact (ответственные по зонам), 200 /warranty (гарантийный учёт,
warranty.json — в xlsx колонок гарантии нет). Пароли камер НЕ меняются."""
import re
import time
import datetime

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_store as store
from bot_tg import send, send_chunks, answer_cq
from bot_util import log, esc

_LC_DEF = {"cams": {}, "log": []}
_RM_DEF = {"seq": 0, "items": []}
STATUSES = ("active", "in_repair", "dismantled", "planned")
_ST_RU = {"active": "🟢 в строю", "in_repair": "🔧 в ремонте",
          "dismantled": "📦 демонтирована", "planned": "📐 планируется"}


def _resolve(arg):
    a = (arg or "").strip()
    if net.valid_ip(a):
        return a
    return inv.resolve_ip(a)


# ---------- 196: lifecycle ----------
def status_of(ip: str) -> str:
    e = store.jload(st.cget("lifecycle_path"), _LC_DEF)["cams"].get(ip)
    return (e or {}).get("status") or "active"


def is_monitored(ip: str, name=None) -> bool:
    """Для health-check и обходов: демонтированные/плановые исключаем."""
    return status_of(ip) not in ("dismantled", "planned")


def set_status(ip: str, status: str) -> None:
    now = int(time.time())
    def _fn(d):
        d["cams"][ip] = {"status": status, "ts": now}
        d["log"] = (d.get("log") or [])[-500:] + [
            {"ip": ip, "status": status, "ts": now}]
        return d
    store.jupdate(st.cget("lifecycle_path"), _LC_DEF, _fn)
    log(f"lifecycle: {ip} -> {status}")


def lc_events(ip: str) -> list:
    return [e for e in store.jload(st.cget("lifecycle_path"), _LC_DEF)["log"]
            if e.get("ip") == ip]


def cmd_lifecycle(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    if not parts:
        d = store.jload(st.cget("lifecycle_path"), _LC_DEF)["cams"]
        odd = {ip: e for ip, e in d.items() if e.get("status") != "active"}
        if not odd:
            send(chat, "♻️ Все камеры в статусе «в строю».\n"
                       "<code>/lifecycle 10.20.50.51</code> — смена статуса "
                       "(active / in_repair / dismantled / planned).",
                 reply_to=reply_to)
            return
        lines = ["♻️ <b>Камеры вне строя</b>:"]
        for ip, e in sorted(odd.items()):
            lines.append(f"• <code>{ip}</code> {esc(inv.label(ip) or '')} — "
                         f"{_ST_RU.get(e['status'], e['status'])} с "
                         + time.strftime("%d.%m", time.localtime(e.get("ts", 0))))
        send_chunks(chat, lines)
        return
    ip = _resolve(parts[0])
    if not ip:
        send(chat, f"«{esc(parts[0])}» не найдено в инвентаре.", reply_to=reply_to)
        return
    if len(parts) > 1 and parts[1].lower() in STATUSES:
        set_status(ip, parts[1].lower())
        send(chat, f"♻️ <code>{ip}</code> → {_ST_RU[parts[1].lower()]}."
                   + ("\nИсключена из health-check и обходов."
                      if parts[1].lower() in ("dismantled", "planned") else ""),
             reply_to=reply_to)
        return
    cur = status_of(ip)
    rows = [[{"text": ("• " if s == cur else "") + _ST_RU[s],
              "callback_data": f"lcs:{ip}:{s}"}] for s in STATUSES]
    send(chat, f"♻️ {esc(inv.label(ip) or ip)} — сейчас {_ST_RU[cur]}.\n"
               f"Новый статус:", markup={"inline_keyboard": rows},
         reply_to=reply_to)


def cb_lcs(chat, cq, payload):
    ip, _, status = payload.partition(":")
    if not net.valid_ip(ip) or status not in STATUSES:
        answer_cq(cq.get("id"))
        return
    set_status(ip, status)
    answer_cq(cq.get("id"), "♻️ Статус изменён")
    send(chat, f"♻️ <code>{ip}</code> → {_ST_RU[status]}.", silent=True)


# ---------- 197: /remind ----------
_T_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_D_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.?$")
_REL_RE = re.compile(r"^(\d+)\s*(м|мин|ч|час|часа|часов|д|дн|дня|дней)$")


def parse_when(tokens: list, now: float = None):
    """(timestamp, съедено токенов) или (None, 0). Понимает: «завтра [10:00]»,
    «сегодня 18:00», «через 2ч», «15.07 [10:00]», «10:00»."""
    now_dt = datetime.datetime.fromtimestamp(now or time.time())
    if not tokens:
        return None, 0
    t0 = tokens[0].lower()

    def with_time(base, idx):
        if len(tokens) > idx:
            m = _T_RE.match(tokens[idx])
            if m:
                return (base.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                     second=0, microsecond=0), idx + 1)
        return base.replace(hour=10, minute=0, second=0, microsecond=0), idx

    if t0 in ("завтра", "tomorrow"):
        dt, used = with_time(now_dt + datetime.timedelta(days=1), 1)
        return dt.timestamp(), used
    if t0 in ("сегодня", "today"):
        dt, used = with_time(now_dt, 1)
        return dt.timestamp(), used
    if t0 in ("через", "in") and len(tokens) > 1:
        cand, used = tokens[1].lower(), 2
        m = _REL_RE.match(cand)
        if not m and len(tokens) > 2:  # «через 2 ч» (число и единица раздельно)
            m = _REL_RE.match(cand + tokens[2].lower())
            used = 3
        if m:
            n, unit = int(m.group(1)), m.group(2)[0]
            mult = {"м": 60, "ч": 3600, "д": 86400}[unit]
            return now_dt.timestamp() + n * mult, used
        return None, 0
    m = _D_RE.match(t0)
    if m:
        try:
            base = now_dt.replace(month=int(m.group(2)), day=int(m.group(1)))
        except ValueError:
            return None, 0
        if base < now_dt:
            base = base.replace(year=base.year + 1)
        dt, used = with_time(base, 1)
        return dt.timestamp(), used
    m = _T_RE.match(t0)
    if m:
        dt = now_dt.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                            second=0, microsecond=0)
        if dt <= now_dt:
            dt += datetime.timedelta(days=1)
        return dt.timestamp(), 1
    return None, 0


def reminders(include_done=False) -> list:
    items = store.jload(st.cget("reminders_path"), _RM_DEF)["items"]
    return items if include_done else [i for i in items if not i.get("done")]


def cmd_remind(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    if not parts:
        items = reminders()
        if not items:
            send(chat, "⏰ Напоминаний нет.\n<code>/remind AS-7C.01 завтра 10:00 "
                       "проверить после замены PoE</code>\n"
                       "Когда: завтра [чч:мм] · сегодня чч:мм · через 2ч · "
                       "15.07 [чч:мм] · чч:мм", reply_to=reply_to)
            return
        lines = ["⏰ <b>Напоминания</b>:"]
        for it in sorted(items, key=lambda x: x["ts"]):
            lines.append(f"#{it['id']} "
                         + time.strftime("%d.%m %H:%M", time.localtime(it["ts"]))
                         + f" — <code>{it['ip']}</code> {esc(it['text'])}")
        lines.append("Удалить: /remind del <номер>")
        send_chunks(chat, lines)
        return
    if parts[0].lower() in ("del", "удалить") and len(parts) > 1:
        try:
            rid = int(parts[1].lstrip("#"))
        except ValueError:
            send(chat, "Номер напоминания: /remind del 3", reply_to=reply_to)
            return
        def _fn(d):
            d["items"] = [i for i in d["items"] if i["id"] != rid]
            return d
        store.jupdate(st.cget("reminders_path"), _RM_DEF, _fn)
        send(chat, f"🗑 Напоминание #{rid} удалено.", reply_to=reply_to)
        return
    ip = _resolve(parts[0])
    if not ip:
        send(chat, f"«{esc(parts[0])}» не найдено в инвентаре.", reply_to=reply_to)
        return
    ts, used = parse_when(parts[1:])
    if not ts:
        send(chat, "Не понял срок. Примеры: завтра 10:00 · через 2ч · "
                   "15.07 14:30 · 18:00", reply_to=reply_to)
        return
    text = " ".join(parts[1 + used:]) or "проверить камеру"
    it = {}
    def _fn(d):
        d["seq"] = int(d.get("seq") or 0) + 1
        it.update({"id": d["seq"], "ip": ip, "ts": int(ts), "text": text,
                   "done": False})
        d["items"] = [i for i in d["items"] if not i.get("done")][-200:] + [it]
        return d
    store.jupdate(st.cget("reminders_path"), _RM_DEF, _fn)
    send(chat, f"⏰ Напоминание #{it['id']}: "
               + time.strftime("%d.%m %H:%M", time.localtime(ts))
               + f" — {esc(inv.label(ip) or ip)}: {esc(text)}", reply_to=reply_to)


def _tick_reminders():
    now = time.time()
    due = [i for i in reminders() if i["ts"] <= now]
    if not due:
        return
    def _fn(d):
        for i in d["items"]:
            if i["ts"] <= now:
                i["done"] = True
        return d
    store.jupdate(st.cget("reminders_path"), _RM_DEF, _fn)
    owner = st.cget("owner_chat_id")
    if not owner:
        return
    try:
        import bot_alerts
        if bot_alerts.muted("reminder"):
            return
    except Exception:
        pass
    for it in due:
        rec = inv.get(it["ip"])
        card = inv.card_text(rec) if rec else f"<code>{it['ip']}</code>"
        send(owner, f"⏰ <b>Напоминание #{it['id']}</b>: {esc(it['text'])}\n\n"
                    + card,
             markup={"inline_keyboard": [[
                 {"text": "🩺 Проверить сейчас", "callback_data": f"diag:{it['ip']}"},
                 {"text": "📸 Снимок", "callback_data": f"shot:{it['ip']}"}]]})


# ---------- 198: /kit ----------
_KIT_BUILTIN = {
    "нет линка": ["патч-корды 1м/5м", "коннекторы RJ-45 + кримпер",
                  "кабельный тестер", "стяжки", "изолента"],
    "нет poe": ["PoE-инжектор 802.3at", "мультиметр", "запасной БП",
                "патч-корд", "тестер PoE"],
    "нет изображения": ["ноутбук с ONVIF-клиентом", "патч-корд для прямого "
                        "подключения", "салфетки для объектива"],
    "замена камеры": ["камера из ЗИП (Apix)", "кронштейн + метизы",
                      "шуруповёрт", "коннектор RJ-45", "наклейка с именем",
                      "стремянка"],
    "грязный обзор": ["салфетки/спрей для оптики", "стремянка", "перчатки"],
}


def cmd_kit(chat, arg="", reply_to=None):
    kits = dict(_KIT_BUILTIN)
    kits.update(store.jload(st.cget("kit_path"), {}))
    a = (arg or "").strip().lower()
    if not a:
        send(chat, "🎒 <b>Что взять с собой</b> — типы выездов:\n"
                   + "\n".join(f"• <code>/kit {esc(k)}</code>" for k in kits)
                   + "\nСвои наборы — в kit.json.", reply_to=reply_to)
        return
    key = next((k for k in kits if a in k.lower()), None)
    if not key:
        send(chat, f"Набора «{esc(a)}» нет. Список: /kit", reply_to=reply_to)
        return
    send(chat, f"🎒 <b>{esc(key)}</b> — взять с собой:\n"
               + "\n".join(f"☐ {esc(x)}" for x in kits[key]), reply_to=reply_to)


# ---------- 199: /contact ----------
def cmd_contact(chat, arg="", reply_to=None):
    cts = store.jload(st.cget("contacts_path"), {})
    if not cts:
        send(chat, "📇 Контакты не заполнены — создай contacts.json вида\n"
                   "<code>{\"7C\": {\"эксплуатация\": \"Иванов +7…\"}}</code>",
             reply_to=reply_to)
        return
    a = (arg or "").strip().lower()
    if not a:
        send(chat, "📇 Зоны ответственности: "
                   + ", ".join(f"<code>{esc(k)}</code>" for k in sorted(cts))
                   + "\n<code>/contact 7C</code> — кого дёргать.",
             reply_to=reply_to)
        return
    key = next((k for k in cts if a == k.lower() or a in k.lower()), None)
    if not key:
        send(chat, f"По «{esc(a)}» контактов нет. Список: /contact",
             reply_to=reply_to)
        return
    v = cts[key]
    lines = [f"📇 <b>{esc(key)}</b>:"]
    if isinstance(v, dict):
        lines += [f"• {esc(role)}: {esc(who)}" for role, who in v.items()]
    else:
        lines.append(esc(v))
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- 200: /warranty ----------
def warranty_of(ip: str):
    """(дата окончания date, на_гарантии bool, подрядчик) или None."""
    d = store.jload(st.cget("warranty_path"), {})
    cams = d.get("cams") or {}
    rec = inv.get(ip) or {}
    keys = [ip, str(rec.get("name") or "")]
    e = next((cams[k] for k in keys if k and k in cams), None)
    if not e:
        return None
    try:
        mounted = datetime.date.fromisoformat(str(e.get("mounted")))
    except (TypeError, ValueError):
        return None
    months = int(e.get("months") or d.get("default_months") or 36)
    until = mounted + datetime.timedelta(days=int(months * 30.44))
    return until, until >= datetime.date.today(), e.get("vendor") or ""


def warranty_line(ip: str) -> str:
    w = warranty_of(ip)
    if not w:
        return ""
    until, on, vendor = w
    if on:
        return (f"✅ на гарантии до {until:%m.%Y}"
                + (f" → ремонт за счёт {vendor}" if vendor else
                   " → ремонт за счёт подрядчика"))
    return f"❌ гарантия вышла ({until:%m.%Y})"


def cmd_warranty(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    if parts and parts[0].lower() == "set" and len(parts) >= 3:
        name, mounted = parts[1], parts[2]
        try:
            datetime.date.fromisoformat(mounted)
        except ValueError:
            send(chat, "Дата монтажа в формате ГГГГ-ММ-ДД: "
                       "<code>/warranty set AS-7C.01 2024-03-01 [36]</code>",
                 reply_to=reply_to)
            return
        months = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
        def _fn(d):
            e = {"mounted": mounted}
            if months:
                e["months"] = months
            d.setdefault("cams", {})[name] = e
            return d
        store.jupdate(st.cget("warranty_path"), {}, _fn)
        send(chat, f"✅ Гарантия для «{esc(name)}» записана.", reply_to=reply_to)
        return
    if parts:
        ip = _resolve(parts[0])
        if not ip:
            send(chat, f"«{esc(parts[0])}» не найдено.", reply_to=reply_to)
            return
        line = warranty_line(ip)
        send(chat, f"🛡 {esc(inv.label(ip) or ip)}\n"
                   + (line or "Данных о гарантии нет — "
                      "<code>/warranty set &lt;имя&gt; ГГГГ-ММ-ДД [мес]</code>"),
             reply_to=reply_to)
        return
    d = store.jload(st.cget("warranty_path"), {})
    cams = d.get("cams") or {}
    if not cams:
        send(chat, "🛡 Гарантийных записей нет.\n"
                   "<code>/warranty set AS-7C.01 2024-03-01 36</code> — добавить\n"
                   "<code>/warranty AS-7C.01</code> — проверить камеру",
             reply_to=reply_to)
        return
    today = datetime.date.today()
    soon = today + datetime.timedelta(days=int(st.cget("warranty_soon_days")))
    lines = [f"🛡 <b>Гарантия</b> (истекает в ближайшие "
             f"{st.cget('warranty_soon_days')} дн.):"]
    n = 0
    for name in sorted(cams):
        ip = _resolve(name) or name
        w = warranty_of(ip) if net.valid_ip(ip) else None
        if not w:
            continue
        until, on, _v = w
        if on and until <= soon:
            n += 1
            lines.append(f"⚠️ {esc(name)} — до {until:%d.%m.%Y}")
    if not n:
        lines.append("Истекающих нет.")
    lines.append(f"Всего записей: {len(cams)}")
    send_chunks(chat, lines)


try:  # 197: минутный тик напоминаний
    import bot_health as _bh
    if _tick_reminders not in _bh.MINUTE_TICKS:
        _bh.MINUTE_TICKS.append(_tick_reminders)
except Exception:
    log("bot_lifecycle: тик напоминаний не зарегистрирован")


HANDLERS = {
    "/lifecycle": cmd_lifecycle, "/remind": cmd_remind, "/kit": cmd_kit,
    "/contact": cmd_contact, "/warranty": cmd_warranty,
}
ALIASES = {
    "/жизнь": "/lifecycle", "/напомни": "/remind", "/набор": "/kit",
    "/контакт": "/contact", "/гарантия": "/warranty",
}
CALLBACKS = {"lcs": cb_lcs}
