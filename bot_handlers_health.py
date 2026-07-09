# -*- coding: utf-8 -*-
"""Команды здоровья парка (Волна C): /report /offline /uptime /top_flaky
/health /watch + /reboot_soft (ONVIF SystemReboot, двухшаговое подтверждение)
+ /unknown (лист «Неизвестные устройства», read-only с пагинацией).
I13, I19, I20, I22, I23, I35, U30, U31. Пароли камер НЕ меняются."""
import time
import threading
from typing import Optional

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_health as bh
from bot_tg import send, send_chunks, edit_message, answer_cq, chat_action
from bot_util import log, log_exc, esc, human_err
from onvif_snap import system_reboot

_RBT = {}  # ip -> ts выдачи подтверждения (двухшаговость + TTL)
_RBT_TTL = 120
_rbt_lock = threading.Lock()


def _resolve(chat, arg: str, usage: str, reply_to=None) -> Optional[str]:
    a = (arg or "").strip()
    if not a:
        send(chat, usage, reply_to=reply_to)
        return None
    if net.valid_ip(a):
        return a
    ip = inv.resolve_ip(a)
    if not ip:
        send(chat, f"«{esc(a)}» не найдено в инвентаре (или неоднозначно) — "
                   f"уточни через /cam.", reply_to=reply_to)
    return ip


# ---------- I20: /report ----------
def cmd_report(chat, arg="", reply_to=None):
    send(chat, bh.report_text(), reply_to=reply_to)


# ---------- I19: /offline ----------
def cmd_offline(chat, arg="", reply_to=None):
    ips = bh.offline_ips()
    if not ips:
        snap = bh.snapshot()
        if not snap["ips"]:
            send(chat, bh.report_text(), reply_to=reply_to)
        else:
            send(chat, "🟢 Все камеры инвентаря отвечают (по последнему прогону).",
                 reply_to=reply_to)
        return
    now = time.time()
    snap = bh.snapshot()["ips"]
    by_sub = {}
    for ip in ips:
        by_sub.setdefault(ip.rsplit(".", 1)[0], []).append(ip)
    lines = [f"🔴 <b>Офлайн сейчас: {len(ips)}</b>"]
    for sub, sub_ips in sorted(by_sub.items()):
        lines.append(f"\n<code>{sub}.x</code> — {len(sub_ips)}:")
        for ip in sub_ips[:30]:
            e = snap.get(ip) or {}
            dur = f" · лежит {bh._fmt_dur(now - e['since'])}" if e.get("since") else ""
            lbl = inv.label(ip)
            lines.append(f"• <code>{ip}</code>{(' ' + esc(lbl)) if lbl else ''}{dur}")
        if len(sub_ips) > 30:
            lines.append(f"… и ещё {len(sub_ips) - 30}")
    send_chunks(chat, lines)
    rows = [[{"text": f"🩺 {ip}", "callback_data": f"diag:{ip}"}] for ip in ips[:6]]
    if rows:
        send(chat, "Диагностика первых офлайн:", silent=True,
             markup={"inline_keyboard": rows})


# ---------- I22: /uptime ----------
def cmd_uptime(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    ip = _resolve(chat, parts[0] if parts else "",
                  "Доступность: <code>/uptime 10.20.50.51</code> или по имени",
                  reply_to)
    if not ip:
        return
    lines = [f"📈 <b>Доступность {esc(bh._label(ip))}</b>"]
    for days, name in ((1, "24 часа"), (7, "7 дней"), (30, "30 дней")):
        pct, downs, down_s = bh.uptime(ip, days)
        lines.append(f"• {name}: <b>{pct:.2f}%</b> · падений {downs}"
                     + (f" · даунтайм {bh._fmt_dur(down_s)}" if down_s else ""))
    cur = bh.snapshot()["ips"].get(ip)
    if cur is None:
        lines.append("ℹ️ health-check её ещё не проверял.")
    else:
        state = "🟢 онлайн" if cur.get("ok") else "🔴 офлайн"
        lines.append(f"Сейчас: {state} · проверена "
                     + time.strftime("%d.%m %H:%M", time.localtime(cur.get("checked", 0))))
    last = [e for e in bh.history_events(30) if e.get("ip") == ip][-5:]
    if last:
        lines.append("Последние переходы:")
        for e in last:
            t = time.strftime("%d.%m %H:%M", time.localtime(e["ts"]))
            lines.append(f"  {t} {'🔴 упала' if e['ev'] == 'down' else '🟢 ожила'}"
                         + (f" (лежала {bh._fmt_dur(e['dur'])})" if e.get("dur") else ""))
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- I23: /top_flaky ----------
def cmd_top_flaky(chat, arg="", reply_to=None):
    try:
        days = max(1, min(int(arg), 30))
    except (TypeError, ValueError):
        days = 7
    top = bh.top_flaky(days=days)
    if not top:
        send(chat, f"📉 За {days} дн. падений не зафиксировано (или история пуста).",
             reply_to=reply_to)
        return
    lines = [f"📉 <b>Топ нестабильных за {days} дн.</b> (по числу падений):"]
    for i, (ip, n) in enumerate(top, 1):
        lbl = inv.label(ip)
        lines.append(f"{i}. <code>{ip}</code> — <b>{n}</b>"
                     + (f" · {esc(lbl)}" if lbl else ""))
    send_chunks(chat, lines)


# ---------- U31: /health — прогон по избранным с прогрессом ----------
def cmd_health(chat, arg="", reply_to=None):
    ips = net.find_ips(arg) or list(dict.fromkeys(
        st.get_ips("fav_ips") + st.get_ips("watch_ips")))
    if not ips:
        send(chat, "Нет избранных/наблюдаемых. Добавь: /fav <code>&lt;ip&gt;</code>, "
                   "/watch <code>&lt;ip&gt;</code> — или дай IP аргументом.",
             reply_to=reply_to)
        return
    r = send(chat, f"💓 Проверяю {len(ips)} камер… 0/{len(ips)}",
             reply_to=reply_to, silent=True)
    mid = ((r or {}).get("result") or {}).get("message_id")
    lines, ok_n = [], 0
    for i, ip in enumerate(ips, 1):
        ok = bh.probe(ip)
        ok_n += 1 if ok else 0
        lbl = inv.label(ip)
        lines.append(f"{'🟢' if ok else '🔴'} <code>{ip}</code>"
                     + (f" {esc(lbl)}" if lbl else ""))
        if mid and (i % 3 == 0 or i == len(ips)):
            edit_message(chat, mid, f"💓 Проверяю {len(ips)} камер… {i}/{len(ips)}")
    txt = (f"💓 <b>Проверка по запросу</b>: 🟢 {ok_n}/{len(ips)}\n"
           + "\n".join(lines))
    if mid:
        edit_message(chat, mid, txt)
    else:
        send_chunks(chat, txt.split("\n"))


# ---------- U30: /watch ----------
def cmd_watch(chat, arg="", reply_to=None):
    a = (arg or "").strip()
    if not a:
        ips = st.get_ips("watch_ips")
        if not ips:
            send(chat, "👁 Надзор пуст. <code>/watch 10.20.50.51</code> — добавить "
                       "(алерт с ПЕРВОГО провала пробы, без дебаунса).",
                 reply_to=reply_to)
            return
        lines = ["👁 <b>Под надзором</b> (алерт с первого провала):"]
        snap = bh.snapshot()["ips"]
        for ip in ips:
            e = snap.get(ip)
            state = ("🟢" if e.get("ok") else "🔴") if e else "❔"
            lbl = inv.label(ip)
            lines.append(f"{state} <code>{ip}</code>"
                         + (f" {esc(lbl)}" if lbl else ""))
        lines.append("Убрать: /watch <code>&lt;ip&gt;</code> (повторно).")
        send(chat, "\n".join(lines), reply_to=reply_to)
        return
    ip = _resolve(chat, a, "Надзор: <code>/watch 10.20.50.51</code>", reply_to)
    if not ip:
        return
    added = st.toggle_ip("watch_ips", ip)
    send(chat, (f"👁 Добавил <code>{ip}</code> под надзор — алерт с первого провала."
                if added else f"👁 Убрал <code>{ip}</code> из надзора."),
         reply_to=reply_to)


# ---------- I13: /reboot_soft — двухшаговое подтверждение ----------
def cmd_reboot_soft(chat, arg="", reply_to=None):
    ip = _resolve(chat, arg, "Мягкий ребут: <code>/reboot_soft 10.20.50.51</code> "
                             "или по имени (ONVIF SystemReboot).", reply_to)
    if not ip:
        return
    send(chat, f"⚠️ <b>Мягкая перезагрузка</b> {esc(bh._label(ip))}\n"
               f"Камера уйдёт из эфира на ~1-2 мин. Точно?",
         reply_to=reply_to,
         markup={"inline_keyboard": [[
             {"text": "♻️ Перезагрузить…", "callback_data": f"rbt:{ip}"},
             {"text": "✖️ Отмена", "callback_data": "cancel"}]]})


def cb_rbt(chat, cq, ip):
    """Шаг 2: превращаем кнопку в финальное подтверждение (TTL 120с)."""
    if not net.valid_ip(ip):
        answer_cq(cq.get("id"))
        return
    with _rbt_lock:
        _RBT[ip] = time.time()
    answer_cq(cq.get("id"), "Нужно финальное подтверждение")
    mid = (cq.get("message") or {}).get("message_id")
    txt = (f"⚠️ <b>Финальное подтверждение</b>\n"
           f"ONVIF SystemReboot для {esc(bh._label(ip))} — жми ✅ в течение 2 мин.")
    kb = {"inline_keyboard": [[
        {"text": "✅ Да, перезагрузить", "callback_data": f"rbty:{ip}"},
        {"text": "✖️ Отмена", "callback_data": "cancel"}]]}
    if mid:
        edit_message(chat, mid, txt, markup=kb)
    else:
        send(chat, txt, markup=kb)


def cb_rbty(chat, cq, ip):
    """Шаг 3: выполняем SystemReboot (только после rbt и в пределах TTL)."""
    with _rbt_lock:
        ts = _RBT.pop(ip, None)
    if not net.valid_ip(ip) or ts is None or time.time() - ts > _RBT_TTL:
        answer_cq(cq.get("id"), "⌛ Подтверждение устарело — повтори /reboot_soft")
        return
    answer_cq(cq.get("id"), "♻️ Отправляю SystemReboot…")
    chat_action(chat)
    log(f"REBOOT_SOFT {ip} подтверждён владельцем")
    ok, msg = system_reboot(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
    if ok:
        send(chat, f"♻️ <code>{ip}</code>: камера приняла SystemReboot "
                   f"(«{esc(msg)}»). Вернётся через ~1-2 мин — проверь /diag.",
             markup={"inline_keyboard": [[
                 {"text": "🩺 Диаг", "callback_data": f"diag:{ip}"},
                 {"text": "📸 Снимок", "callback_data": f"shot:{ip}"}]]})
    else:
        send(chat, human_err(f"SystemReboot для <code>{ip}</code> не прошёл", msg))


# ---------- I35: /unknown — read-only пагинация ----------
_UNK_PAGE = 8


def _unk_page_text(page: int):
    hdr, rows = inv.unknown_devices()
    if not rows:
        return "Лист «Неизвестные устройства» пуст или отсутствует.", None, 0
    pages = max(1, (len(rows) + _UNK_PAGE - 1) // _UNK_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * _UNK_PAGE:(page + 1) * _UNK_PAGE]
    lines = [f"❔ <b>Неизвестные устройства</b> — {len(rows)} строк · "
             f"стр. {page + 1}/{pages}"]
    for r in chunk:
        d = {h: r[i] if i < len(r) else None for i, h in enumerate(hdr)}
        mac = d.get("MAC-адрес") or "?"
        lines.append(f"\n<code>{esc(mac)}</code> · {esc(d.get('Вендор (OUI)') or '?')}")
        lines.append(f"  {esc(d.get('Категория') or '—')}")
        sw = " · ".join(str(d[k]) for k in ("Коммутатор", "IP коммутатора", "Порт")
                        if d.get(k))
        if sw:
            lines.append(f"  🔌 {esc(sw)} · VLAN {esc(d.get('VLAN') or '?')}")
        if d.get("Примечание"):
            lines.append(f"  📝 {esc(d['Примечание'])}")
    kb = None
    if pages > 1:
        kb = {"inline_keyboard": [[
            {"text": "◀️", "callback_data": f"unk:{page - 1}"},
            {"text": f"{page + 1}/{pages}", "callback_data": f"unk:{page}"},
            {"text": "▶️", "callback_data": f"unk:{page + 1}"}]]}
    return "\n".join(lines), kb, pages


def cmd_unknown(chat, arg="", reply_to=None):
    try:
        page = max(1, int(arg)) - 1
    except (TypeError, ValueError):
        page = 0
    txt, kb, _p = _unk_page_text(page)
    send(chat, txt, markup=kb, reply_to=reply_to)


def cb_unk(chat, cq, payload):
    try:
        page = max(0, int(payload))
    except ValueError:
        page = 0
    answer_cq(cq.get("id"))
    txt, kb, _p = _unk_page_text(page)
    mid = (cq.get("message") or {}).get("message_id")
    if mid:
        edit_message(chat, mid, txt, markup=kb)


HANDLERS = {
    "/report": cmd_report, "/offline": cmd_offline, "/uptime": cmd_uptime,
    "/top_flaky": cmd_top_flaky, "/health": cmd_health, "/watch": cmd_watch,
    "/reboot_soft": cmd_reboot_soft, "/unknown": cmd_unknown,
}
ALIASES = {
    "/отчет": "/report", "/отчёт": "/report", "/офлайн": "/offline",
    "/аптайм": "/uptime", "/здоровье": "/health", "/надзор": "/watch",
    "/ребут": "/reboot_soft", "/неизвестные": "/unknown",
}
CALLBACKS = {"rbt": cb_rbt, "rbty": cb_rbty, "unk": cb_unk}
