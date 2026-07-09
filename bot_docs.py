# -*- coding: utf-8 -*-
"""Волна D — документы: 167 /ticket (заявка по шаблону с симптомами из
диагностики), 168 «📎 В тикет» (история диагностики по IP из лога → .txt),
169 /act (акт о неисправности, автонумерация в _acts.json, plain-текст + .txt),
170 /weekly (недельный отчёт: доступность, инциденты, MTTR),
171 /passport (паспорт камеры + QR). Пароли камер НЕ меняются."""
import time
import datetime

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_issues as iss
import bot_store as store
from bot_tg import send, send_chunks, send_document, send_photo, chat_action, answer_cq
from bot_util import log, log_exc, esc, LOG_PATH

_ACT_DEF = {"seq": 0, "acts": []}


def _resolve(arg):
    a = (arg or "").strip()
    if net.valid_ip(a):
        return a
    return inv.resolve_ip(a)


def _symptoms(ip: str) -> str:
    """Симптомы из health-состояния и журнала для тикета/акта."""
    import bot_health as bh
    e = bh.snapshot()["ips"].get(ip)
    bits = []
    if e is None:
        bits.append("health-check камеру ещё не проверял")
    elif e.get("ok") is False:
        bits.append("офлайн с "
                    + time.strftime("%d.%m %H:%M", time.localtime(e.get("since", 0)))
                    + f" ({bh._fmt_dur(time.time() - e.get('since', time.time()))})")
    else:
        bits.append("сейчас отвечает (TCP), проблема плавающая")
    downs7 = sum(1 for ev in bh.history_events(7)
                 if ev.get("ip") == ip and ev.get("ev") == "down")
    if downs7:
        bits.append(f"падений за 7 дней: {downs7}")
    if iss.is_chronic(ip):
        bits.append("хроническая: "
                    f"{iss.repairs_count(ip)} ремонтов за 90 дней, кандидат на замену")
    return "; ".join(bits)


_TICKET_TPL = (
    "ЗАЯВКА НА РЕМОНТ КАМЕРЫ ВИДЕОНАБЛЮДЕНИЯ\n"
    "Дата: {date}\n"
    "Камера: {name} (инв. №{n}, модель {model})\n"
    "IP: {ip} · MAC: {mac}\n"
    "Расположение: {location}\n"
    "Подключение: {switch} ({sw_ip}), порт {port}, VLAN {vlan}\n"
    "Симптомы: {symptoms}\n"
    "Гарантия: {warranty}\n"
    "Заявку подал: оператор видеонаблюдения МФК «Зарядье»")


def _fill_ticket(ip: str) -> str:
    rec = inv.get(ip) or {}
    try:
        from bot_lifecycle import warranty_line
        w = warranty_line(ip) or "данных нет"
    except Exception:
        w = "данных нет"
    tpl = _TICKET_TPL
    path = st.cget("ticket_template")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                tpl = f.read()
        except OSError:
            log(f"ticket: шаблон {path} не читается — беру встроенный")
    vals = {"date": datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
            "ip": ip, "symptoms": _symptoms(ip) or "—", "warranty": w}
    for k in ("name", "n", "model", "mac", "location", "switch", "sw_ip",
              "port", "vlan", "obj"):
        vals[k] = str(rec.get(k) or "—")
    try:
        return tpl.format(**vals)
    except (KeyError, IndexError) as e:
        log(f"ticket: кривой плейсхолдер в шаблоне ({e}) — встроенный")
        return _TICKET_TPL.format(**vals)


# ---------- 167: /ticket ----------
def cmd_ticket(chat, arg="", reply_to=None):
    ip = _resolve(arg)
    if not ip:
        send(chat, "Заявка на ремонт: <code>/ticket 10.20.50.51</code> или "
                   "по имени", reply_to=reply_to)
        return
    txt = _fill_ticket(ip)
    send(chat, f"🎫 <b>Заявка</b> (копируй в сервисную службу):\n"
               f"<pre>{esc(txt)}</pre>",
         markup={"inline_keyboard": [[
             {"text": "📎 История диагностики (.txt)", "callback_data": f"tlog:{ip}"},
             {"text": "📄 Акт", "callback_data": f"actq:{ip}"}]]},
         reply_to=reply_to)


def cb_tck(chat, cq, ip):
    answer_cq(cq.get("id"), "🎫 Формирую заявку…")
    if net.valid_ip(ip):
        cmd_ticket(chat, ip)


# ---------- 168: экспорт сессии диагностики ----------
def session_log(ip: str, hours: float = 1.0) -> list:
    """Строки camera_bot.log за последние hours, где упомянут ip."""
    cut = datetime.datetime.now() - datetime.timedelta(hours=hours)
    out = []
    try:
        with open(LOG_PATH, encoding="utf-8-sig", errors="replace") as f:
            for ln in f.readlines()[-8000:]:
                if ip not in ln:
                    continue
                try:
                    ts = datetime.datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
                    if ts < cut:
                        continue
                except ValueError:
                    pass
                out.append(ln.rstrip())
    except OSError:
        log_exc("session_log")
    return out


def cb_tlog(chat, cq, ip):
    if not net.valid_ip(ip):
        answer_cq(cq.get("id"))
        return
    answer_cq(cq.get("id"), "📎 Собираю историю…")
    lines = session_log(ip)
    if not lines:
        send(chat, f"За последний час в логе по <code>{ip}</code> пусто.")
        return
    head = (f"История диагностики {ip} ({inv.label(ip) or 'нет в инвентаре'})\n"
            f"Сформировано: {datetime.datetime.now():%d.%m.%Y %H:%M}\n"
            + "=" * 60 + "\n")
    data = (head + "\n".join(lines)).encode("utf-8-sig")
    send_document(chat, data,
                  f"diag_{ip.replace('.', '-')}_{datetime.datetime.now():%H%M}.txt",
                  caption=f"📎 {len(lines)} записей за час — приложи к заявке")


# ---------- 169: /act ----------
def cmd_act(chat, arg="", reply_to=None):
    ip = _resolve(arg)
    if not ip:
        send(chat, "Акт о неисправности: <code>/act 10.20.50.51</code>",
             reply_to=reply_to)
        return
    chat_action(chat)
    alive = net.ping(ip) is not None
    ports = net.open_ports(ip, ports=(80, 554, 8080))
    onvif_s = "не проверялся"
    if ports:
        try:
            from onvif_snap import device_info
            info = device_info(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
            onvif_s = (f"отвечает ({info.get('manufacturer')} {info.get('model')})"
                       if info.get("model") else f"не отвечает ({info.get('error')})")
        except Exception as e:
            onvif_s = f"ошибка опроса: {e}"
    verdict = ("неисправность НЕ подтверждена: камера отвечает"
               if alive and ports else
               "неисправность подтверждена: камера недоступна по сети")
    num = [0]
    def _fn(d):
        d["seq"] = int(d.get("seq") or 0) + 1
        num[0] = d["seq"]
        d["acts"] = (d.get("acts") or [])[-500:] + [{
            "id": d["seq"], "ip": ip, "ts": int(time.time()), "verdict": verdict}]
        return d
    store.jupdate(st.cget("acts_path"), _ACT_DEF, _fn)
    rec = inv.get(ip) or {}
    txt = (f"АКТ №{num[0]} о проверке технического состояния камеры\n"
           f"Дата: {datetime.datetime.now():%d.%m.%Y %H:%M}\n"
           f"Камера: {rec.get('name') or '—'} (инв. №{rec.get('n') or '—'}), "
           f"IP {ip}\n"
           f"Расположение: {rec.get('location') or '—'}\n"
           f"Результаты проверки:\n"
           f"  - ping: {'отвечает' if alive else 'НЕ отвечает'}\n"
           f"  - TCP-порты: {', '.join(map(str, ports)) or 'закрыты'}\n"
           f"  - ONVIF: {onvif_s}\n"
           f"  - симптомы: {_symptoms(ip) or '—'}\n"
           f"Вывод: {verdict}.\n"
           f"Проверку выполнил: оператор видеонаблюдения (бот-диагностика)")
    send(chat, f"📄 <b>Акт №{num[0]}</b>\n<pre>{esc(txt)}</pre>", reply_to=reply_to)
    send_document(chat, txt.encode("utf-8-sig"),
                  f"act_{num[0]}_{ip.replace('.', '-')}.txt",
                  caption=f"📄 Акт №{num[0]} · журнал актов в _acts.json")


def cb_actq(chat, cq, ip):
    answer_cq(cq.get("id"), "📄 Составляю акт…")
    if net.valid_ip(ip):
        cmd_act(chat, ip)


# ---------- 170: /weekly ----------
def cmd_weekly(chat, arg="", reply_to=None):
    import bot_health as bh
    days = float(st.cget("weekly_days"))
    ev = bh.history_events(days)
    snap = bh.snapshot()["ips"]
    n_cams = len(snap) or 1
    total_down = sum(int(e.get("dur") or 0) for e in ev if e.get("ev") == "up")
    now = time.time()
    for ip, e in snap.items():  # текущие лежащие тоже в даунтайм
        if e.get("ok") is False:
            total_down += int(min(now - e.get("since", now), days * 86400))
    avail = max(0.0, 100.0 * (1 - total_down / (n_cams * days * 86400)))
    downs = [e for e in ev if e.get("ev") == "down"]
    by_ip = {}
    for e in ev:
        if e.get("ev") == "up" and e.get("dur"):
            by_ip[e["ip"]] = by_ip.get(e["ip"], 0) + int(e["dur"])
    top = sorted(by_ip.items(), key=lambda kv: -kv[1])[:7]
    avg, n_fixed = iss.mttr(days)
    open_n = len(iss.open_issues())
    lines = [f"📅 <b>Отчёт за {int(days)} дней</b> "
             f"({datetime.date.today():%d.%m.%Y})",
             f"Доступность парка: <b>{avail:.2f}%</b> ({n_cams} камер)",
             f"Инцидентов (падений): <b>{len(downs)}</b>",
             f"Починено: <b>{n_fixed}</b>"
             + (f" · MTTR {avg / 3600:.1f} ч" if avg else ""),
             f"Открытых проблем: <b>{open_n}</b> (/issues)"]
    if top:
        lines.append("\nСамый большой даунтайм:")
        for ip, dsec in top:
            lines.append(f"• {esc(inv.label(ip) or ip)} — {bh._fmt_dur(dsec)}")
    chronics = [ip for ip in by_ip if iss.is_chronic(ip)]
    if chronics:
        lines.append("\n🔁 Кандидаты на замену (3+ ремонта за квартал):")
        lines += [f"• {esc(inv.label(ip) or ip)}" for ip in chronics[:10]]
    send_chunks(chat, lines)
    plain = "\n".join(ln.replace("<b>", "").replace("</b>", "")
                      .replace("•", "-") for ln in lines)
    send_document(chat, plain.encode("utf-8-sig"),
                  f"weekly_{datetime.date.today():%Y%m%d}.txt",
                  caption="📅 Отчёт файлом — можно переслать начальству")


# ---------- 171: /passport ----------
def cmd_passport(chat, arg="", reply_to=None):
    ip = _resolve(arg)
    if not ip:
        send(chat, "Паспорт камеры: <code>/passport 10.20.50.51</code>",
             reply_to=reply_to)
        return
    rec = inv.get(ip)
    if not rec:
        send(chat, f"<code>{ip}</code> нет в инвентаре — паспорт не собрать.",
             reply_to=reply_to)
        return
    lines = ["🪪 <b>ПАСПОРТ КАМЕРЫ</b>", inv.card_text(rec)]
    try:
        from bot_lifecycle import status_of, warranty_line
        from bot_lifecycle import _ST_RU as stru
        lines.append(f"♻️ Статус: {stru.get(status_of(ip), 'в строю')}")
        w = warranty_line(ip)
        if w:
            lines.append(f"🛡 {esc(w)}")
    except Exception:
        pass
    n_rep = iss.repairs_count(ip, 365)
    if n_rep:
        lines.append(f"🔧 Ремонтов за год: {n_rep}"
                     + (" · 🔁 хроническая" if iss.is_chronic(ip) else ""))
    import bot_health as bh
    pct, downs, down_s = bh.uptime(ip, 30)
    lines.append(f"📈 Доступность 30 дн: {pct:.2f}% · падений {downs}")
    send(chat, "\n".join(lines), reply_to=reply_to)
    try:
        import bot_qr
        png = bot_qr.qr_png(bot_qr.deep_link(rec))
        send_photo(chat, png, caption=f"🔳 QR для шкафа: {rec.get('name') or ip}")
    except Exception:
        log_exc("passport: qr")


HANDLERS = {
    "/ticket": cmd_ticket, "/act": cmd_act, "/weekly": cmd_weekly,
    "/passport": cmd_passport,
}
ALIASES = {
    "/тикет": "/ticket", "/заявка": "/ticket", "/акт": "/act",
    "/неделя": "/weekly", "/паспорт": "/passport",
}
CALLBACKS = {"tlog": cb_tlog, "tck": cb_tck, "actq": cb_actq}
