# -*- coding: utf-8 -*-
"""Команды бота по инвентарю и Google (Волна B): /cam /where /port /mac /rtsp
/note /verify /audit /fw /export /free_ip /diff /sync /sheet + /info из кэша.
Подключается в bot_handlers.dispatch через HANDLERS/ALIASES этого модуля.
Пароли камер бот НИКОГДА не меняет. Запись в xlsx — только /note (с бэкапом)."""
import os
import time
import threading
import collections

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_sheets as sh
from bot_tg import send, send_chunks, send_document, chat_action
from bot_util import log, log_exc, esc, human_err
from onvif_snap import device_info, rtsp_uri

DIFF_LOCK = threading.Lock()


def _kb(ip, refresh=False):
    """Кнопки действий по камере (лениво из bot_handlers, чтобы без циклов)."""
    import bot_handlers as h
    kb = h.actions_kb(ip)
    if refresh:
        kb["inline_keyboard"].append(
            [{"text": "🔄 Обновить (живой ONVIF)", "callback_data": f"info:{ip}"}])
    return kb


def _resolve(chat, arg, usage, reply_to=None):
    """IP как есть; имя/MAC/серийник -> IP через инвентарь (I8/U11).
    При неоднозначности/провале сам отвечает в чат и возвращает None."""
    a = (arg or "").strip()
    if not a:
        send(chat, usage, reply_to=reply_to)
        return None
    if net.valid_ip(a):
        return a
    res = [c for c in inv.search(a) if c.get("ip")]
    if len(res) == 1:
        return res[0]["ip"]
    if res:
        lines = [f"Нашлось {len(res)} камер по «{esc(a)}» — уточни:"]
        lines += [f"• <code>{c['ip']}</code> {esc(c.get('name') or '')} "
                  f"{esc(c.get('location') or '')}" for c in res[:12]]
        if len(res) > 12:
            lines.append(f"… и ещё {len(res) - 12}")
        send_chunks(chat, lines)
        return None
    sug = inv.suggest(a)
    hint = (" Похожие: " + ", ".join(f"<code>{esc(s)}</code>" for s in sug)) if sug else ""
    send(chat, f"«{esc(a)}» в инвентаре не найдено.{hint}", reply_to=reply_to)
    return None


# ---------- I2 + I3: /cam ----------
def cmd_cam(chat, arg="", reply_to=None):
    a = (arg or "").strip()
    if not a:
        send(chat, "Поиск камеры: <code>/cam AS-7C.01</code>, "
                   "<code>/cam 10.20.50.51</code>, по MAC или серийнику.",
             reply_to=reply_to)
        return
    res = inv.search(a)
    if not res:
        sug = inv.suggest(a)
        hint = ("\nПохожие: " + ", ".join(f"<code>{esc(s)}</code>" for s in sug)) if sug else ""
        send(chat, f"По «{esc(a)}» ничего не нашёл в инвентаре.{hint}", reply_to=reply_to)
        return
    if len(res) == 1:
        rec = res[0]
        send(chat, inv.card_text(rec),
             markup=_kb(rec["ip"]) if rec.get("ip") else None, reply_to=reply_to)
        return
    lines = [f"🔎 Нашлось <b>{len(res)}</b> по «{esc(a)}»:"]
    lines += [f"• <code>{c.get('ip') or '—'}</code> <b>{esc(c.get('name') or '?')}</b> "
              f"· {esc(c.get('location') or '')}" for c in res[:20]]
    if len(res) > 20:
        lines.append(f"… и ещё {len(res) - 20}. Уточни запрос.")
    send_chunks(chat, lines)


# ---------- I7: /where ----------
def cmd_where(chat, arg="", reply_to=None):
    ip = _resolve(chat, arg, "Где стоит камера: <code>/where 10.20.50.51</code> "
                             "или <code>/where AS-7C.01</code>", reply_to)
    if not ip:
        return
    rec = inv.get(ip)
    if not rec:
        send(chat, f"<code>{ip}</code> — 🆕 в инвентаре нет, где стоит — неизвестно.",
             reply_to=reply_to)
        return
    parts = [f"📍 <b>{esc(rec.get('name') or ip)}</b>"]
    if rec.get("location"):
        parts.append(f"Расположение: {esc(rec['location'])}")
    if rec.get("obj"):
        parts.append(f"Объект: {esc(rec['obj'])}")
    sw = " · ".join(str(rec[k]) for k in ("switch", "sw_ip", "port") if rec.get(k))
    if sw:
        parts.append(f"🔌 {esc(sw)}")
    if rec.get("cable"):
        parts.append(f"📏 кабель ~{esc(rec['cable'])} м")
    send(chat, "\n".join(parts), markup=_kb(ip), reply_to=reply_to)


# ---------- I9: /port ----------
def cmd_port(chat, arg="", reply_to=None):
    a = (arg or "").strip()
    if not a:
        send(chat, "Порт свитча: <code>/port 10.20.50.51</code> или "
                   "<code>/port E0:7F:88:06:43:51</code>", reply_to=reply_to)
        return
    rec = inv.get(a) if net.valid_ip(a) else None
    mac = ((rec or {}).get("mac") or net.arp_table().get(a)) if net.valid_ip(a) else a
    if not mac:
        send(chat, f"MAC для <code>{a}</code> неизвестен (нет в инвентаре и ARP).",
             reply_to=reply_to)
        return
    hits = inv.switch_ports(mac)
    lines = [f"🔌 MAC <code>{esc(mac)}</code>:"]
    if rec and rec.get("switch"):
        lines.append(f"📒 по инвентарю: {esc(rec['switch'])} "
                     f"(<code>{esc(rec.get('sw_ip') or '?')}</code>) "
                     f"порт {esc(rec.get('port') or '?')}")
    if hits:
        lines.append("По фактам коммутаторов (_facts_switches.json):")
        for h in hits[:6]:
            k = "🎯" if h["density"] <= 2 else "↔️"
            lines.append(f"{k} {esc(h['host'])} (<code>{h['sw_ip']}</code>) "
                         f"порт <b>{esc(h['port'])}</b> · VLAN {esc(h['vlan'])} "
                         f"· MAC-ов на порту: {h['density']}")
        if hits[0]["density"] > 2:
            lines.append("↔️ = порт-uplink (много MAC), 🎯 = похоже на access-порт.")
    else:
        lines.append("В фактах коммутаторов этот MAC не найден "
                     "(факты собирались разово — могли устареть).")
    send_chunks(chat, lines)


# ---------- U45: /mac ----------
def cmd_mac(chat, arg="", reply_to=None):
    a = (arg or "").strip()
    nm = inv.norm_mac(a)
    if len(nm) < 4:
        send(chat, "Поиск IP по MAC: <code>/mac E0:7F:88:06:43:51</code> "
                   "(можно хвост от 4 hex-знаков)", reply_to=reply_to)
        return
    found = {}  # ip -> источники
    for ip2, m in net.arp_table().items():
        if inv.norm_mac(m).endswith(nm):
            found.setdefault(ip2, []).append("ARP")
    for c in inv.cams():
        if c.get("ip") and c["nmac"].endswith(nm):
            found.setdefault(c["ip"], []).append("инвентарь")
    lines = [f"🔎 MAC <code>{esc(a)}</code>:"]
    if not found:
        send(chat, lines[0] + "\nНе найден ни в ARP, ни в инвентаре.", reply_to=reply_to)
        return
    for ip2, srcs in sorted(found.items()):
        lbl = inv.label(ip2)
        lines.append(f"• <code>{ip2}</code> [{', '.join(srcs)}]"
                     + (f" — {esc(lbl)}" if lbl else " — 🆕 нет в инвентаре"))
    send_chunks(chat, lines)
    if len(found) == 1:
        ip2 = next(iter(found))
        send(chat, inv.ip_card_text(ip2), markup=_kb(ip2), silent=True)


# ---------- I50/U15: /rtsp ----------
def cmd_rtsp(chat, arg="", reply_to=None):
    ip = _resolve(chat, arg, "RTSP-поток: <code>/rtsp 10.20.50.51</code> "
                             "или <code>/rtsp AS-7C.01</code>", reply_to)
    if not ip:
        return
    chat_action(chat)
    uri, err = rtsp_uri(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
    lbl = inv.label(ip)
    head = f"🎬 <b>{esc(lbl) if lbl else ip}</b> (<code>{ip}</code>)"
    kb = {"inline_keyboard": [[{"text": "🌐 Веб-интерфейс", "url": f"http://{ip}/"}]]}
    if uri:
        cred = uri.replace("rtsp://", f"rtsp://{st.CAM_USER}:{st.CAM_PASS}@", 1)
        send(chat, f"{head}\nRTSP: <code>{esc(uri)}</code>\n"
                   f"С кредами (для VLC):\n<code>{esc(cred)}</code>",
             markup=kb, reply_to=reply_to)
    else:
        send(chat, f"{head}\nONVIF не отдал RTSP-URL ({esc(err)}).\n"
                   f"Типовой для Apix: <code>rtsp://{st.CAM_USER}:****@{ip}:554/</code>",
             markup=kb, reply_to=reply_to)


# ---------- I43 (+I42): /note ----------
def cmd_note(chat, arg="", reply_to=None):
    parts = (arg or "").split(maxsplit=1)
    if len(parts) < 2:
        send(chat, "Примечание в инвентарь: <code>/note 10.20.50.51 текст</code>\n"
                   "(в колонку «Примечание» листа «Все камеры», с автобэкапом)",
             reply_to=reply_to)
        return
    ip = _resolve(chat, parts[0], "Укажи IP или имя камеры.", reply_to)
    if not ip:
        return
    text = parts[1].strip()
    try:
        bak, row = inv.write_note(ip, text)
    except Exception as e:
        log_exc(f"/note {ip}")
        send(chat, human_err(f"Не смог записать примечание для <code>{ip}</code>", e),
             reply_to=reply_to)
        return
    send(chat, f"📝 Записал примечание для <code>{ip}</code> (строка {row}):\n"
               f"<i>{esc(text)}</i>\n💾 Бэкап: <code>{esc(os.path.basename(bak))}</code>",
         reply_to=reply_to)


# ---------- I45: /verify ----------
def cmd_verify(chat, arg="", reply_to=None):
    ip = _resolve(chat, arg, "Сверка с инвентарём: <code>/verify 10.20.50.51</code>",
                  reply_to)
    if not ip:
        return
    chat_action(chat)
    rec = inv.get(ip)
    info = device_info(ip, user=st.CAM_USER, pwd=st.CAM_PASS)
    inv.note_onvif(ip, info)
    live_mac = net.arp_table().get(ip)
    lines = [f"🧾 <b>Сверка <code>{ip}</code></b>"]
    if not rec:
        lines.append("📒 в инвентаре: ❌ НЕТ (🆕 новая?)")
    else:
        lines.append(f"📒 в инвентаре: ✅ {esc(rec.get('name') or 'без имени')} · "
                     f"{esc(rec.get('location') or '?')}")
    if info.get("model"):
        lines.append(f"📡 ONVIF: ✅ {esc(info.get('manufacturer'))} {esc(info['model'])} "
                     f"· fw {esc(info.get('firmware'))} · sn {esc(info.get('serial'))}")
    else:
        lines.append(f"📡 ONVIF: ❌ не ответил ({esc(info.get('error'))})")
    if rec:
        if live_mac and rec.get("mac"):
            if inv.norm_mac(live_mac) == rec["nmac"]:
                lines.append(f"🔗 MAC: ✅ совпадает (<code>{esc(live_mac)}</code>)")
            else:
                lines.append(f"🔗 MAC: ⚠️ СМЕНИЛСЯ! сеть <code>{esc(live_mac)}</code> "
                             f"≠ инвентарь <code>{esc(rec['mac'])}</code>")
        elif rec.get("mac"):
            lines.append(f"🔗 MAC: сеть не дала ARP; в инвентаре <code>{esc(rec['mac'])}</code>")
        if info.get("model") and rec.get("model"):
            ok = inv.norm_name(info["model"]) in inv.norm_name(rec["model"])
            lines.append("🎥 Модель: " + ("✅ совпадает"
                         if ok else f"⚠️ инвентарь «{esc(rec['model'])}»"))
        if info.get("serial") and rec.get("mac"):
            ok = rec["nmac"].endswith(inv.norm_mac(info["serial"])[-6:] or "  ")
            lines.append("#️⃣ Серийник~MAC: " + ("✅" if ok else "⚠️ не бьётся"))
    send(chat, "\n".join(lines), markup=_kb(ip), reply_to=reply_to)


# ---------- I46: /audit ----------
def cmd_audit(chat, arg="", reply_to=None):
    cams = [c for c in inv.cams()]
    ips = collections.Counter(c["ip"] for c in cams if c.get("ip"))
    macs = collections.Counter(c["nmac"] for c in cams if c.get("nmac"))
    dup_ip = {k: v for k, v in ips.items() if v > 1}
    dup_mac = {k: v for k, v in macs.items() if v > 1}
    empty = {f: sum(1 for c in cams if not c.get(k))
             for k, f in (("name", "имя"), ("mac", "MAC"), ("location", "расположение"),
                          ("switch", "коммутатор"), ("port", "порт"), ("model", "модель"))}
    lines = [f"🧮 <b>Аудит инвентаря</b> — {len(cams)} строк",
             f"IP-дублей: <b>{len(dup_ip)}</b> · MAC-дублей: <b>{len(dup_mac)}</b>"]
    for k, v in list(dup_ip.items())[:8]:
        names = [esc(c.get("name") or f"№{c.get('n')}") for c in cams if c.get("ip") == k]
        lines.append(f"• IP <code>{k}</code> ×{v}: {', '.join(names[:4])}")
    for k, v in list(dup_mac.items())[:8]:
        rows = [str(c.get("n") or c["row"]) for c in cams if c.get("nmac") == k]
        lines.append(f"• MAC <code>{k}</code> ×{v} (№ {', '.join(rows[:6])})")
    lines.append("Пустые поля: " + " · ".join(f"{f}: {n}" for f, n in empty.items()))
    st_cnt = collections.Counter(str(c.get("status") or "—") for c in cams)
    lines.append("Статусы: " + " · ".join(f"{esc(k)}: {v}"
                 for k, v in st_cnt.most_common(5)))
    send_chunks(chat, lines)


# ---------- I48: /fw ----------
def cmd_fw(chat, arg="", reply_to=None):
    fwc = inv.fw_cache()
    models = collections.Counter(str(c.get("model") or "—") for c in inv.cams()
                                 if c.get("ip"))
    lines = ["🧩 <b>Сводка по моделям (инвентарь)</b>"]
    lines += [f"• {esc(m)}: <b>{n}</b>" for m, n in models.most_common(12)]
    if fwc:
        combo = collections.Counter((e.get("model") or "?", e.get("fw") or "?")
                                    for e in fwc.values())
        lines.append(f"\n🔬 <b>Прошивки по ONVIF-кэшу</b> ({len(fwc)} камер опрошено):")
        lines += [f"• {esc(m)} · fw <code>{esc(f)}</code>: <b>{n}</b>"
                  for (m, f), n in combo.most_common(15)]
    else:
        lines.append("\n🔬 ONVIF-кэш прошивок пуст — он копится из /info, /verify "
                     "и /find автоматически.")
    send_chunks(chat, lines)


# ---------- I49: /export ----------
def cmd_export(chat, arg="", reply_to=None):
    mode = (arg or "").strip().lower()
    chat_action(chat, "upload_document")
    if mode in ("офлайн", "offline"):
        import openpyxl
        import tempfile
        cams = [c for c in inv.cams()
                if "офлайн" in str(c.get("status") or "").lower()
                or "offline" in str(c.get("status") or "").lower()]
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Офлайн"
        ws.append(["№", "Название", "IP", "MAC", "Расположение", "Объект",
                   "Коммутатор", "IP коммутатора", "Порт", "Модель", inv.status_header()])
        for c in cams:
            ws.append([c.get(k) for k in ("n", "name", "ip", "mac", "location", "obj",
                                          "switch", "sw_ip", "port", "model")]
                      + [c.get("status")])
        fd, tmp = tempfile.mkstemp(suffix=".xlsx", prefix="offline_")
        os.close(fd)
        wb.save(tmp)
        with open(tmp, "rb") as f:
            data = f.read()
        os.unlink(tmp)
        fn = f"Офлайн_камеры_{time.strftime('%Y%m%d_%H%M')}.xlsx"
        send_document(chat, data, fn, caption=f"📦 Срез офлайн: {len(cams)} камер")
        return
    try:
        with open(inv.inv_path(), "rb") as f:
            data = f.read()
    except Exception as e:
        send(chat, human_err("Не смог прочитать Все_камеры.xlsx", e), reply_to=reply_to)
        return
    send_document(chat, data, "Все_камеры.xlsx",
                  caption=f"📦 Инвентарь целиком · {len(data) // 1024} КБ · "
                          f"{len(inv.cams())} строк. Срез: /export офлайн")


# ---------- I39: /free_ip ----------
def cmd_free_ip(chat, arg="", reply_to=None):
    prefix = (arg or "").strip().rstrip(".")
    if not net.valid_prefix(prefix):
        send(chat, "Свободные адреса: <code>/free_ip 10.20.50</code>", reply_to=reply_to)
        return
    if not net.prefix_allowed(prefix):
        send(chat, f"⛔ Подсеть <code>{esc(prefix)}.0/24</code> не в разрешённых "
                   f"(ключ find_allow).", reply_to=reply_to)
        return
    chat_action(chat)
    send(chat, f"🧮 Считаю свободные в <b>{esc(prefix)}.0/24</b>: "
               f"инвентарь + живой TCP-скан …", silent=True, reply_to=reply_to)
    used_inv = {c["ip"] for c in inv.cams()
                if c.get("ip") and c["ip"].startswith(prefix + ".")}
    live = set(net.scan_subnet(prefix))
    free = [i for i in range(1, 255)
            if f"{prefix}.{i}" not in used_inv and f"{prefix}.{i}" not in live]
    # сжатие в диапазоны
    rng, out = [], []
    for i in free:
        if rng and i == rng[-1] + 1:
            rng.append(i)
        else:
            if rng:
                out.append(rng)
            rng = [i]
    if rng:
        out.append(rng)
    s = ", ".join(f".{r[0]}" if len(r) == 1 else f".{r[0]}–.{r[-1]}" for r in out)
    send(chat, f"🟢 <b>{esc(prefix)}.0/24</b>: свободно <b>{len(free)}</b> адресов\n"
               f"(в инвентаре {len(used_inv)}, живых в сети {len(live)})\n"
               f"<code>{esc(s)}</code>\n"
               f"⚠️ занятость по инвентарю+скану сейчас; выключенное устройство "
               f"могло не ответить.")


# ---------- I33 + I34: /diff ----------
def cmd_diff(chat, arg="", reply_to=None):
    if not DIFF_LOCK.acquire(blocking=False):
        send(chat, "⏳ /diff уже выполняется — подожди.", reply_to=reply_to)
        return
    try:
        prefixes = [p for p in ((arg or "").split() or st.cget("diff_subnets"))
                    if net.valid_prefix(p) and net.prefix_allowed(p)]
        if not prefixes:
            send(chat, "Сверка сети с инвентарём: <code>/diff</code> (камерные подсети) "
                       "или <code>/diff 10.20.50 10.20.51</code>", reply_to=reply_to)
            return
        chat_action(chat)
        send(chat, f"🔀 Сверяю сеть и инвентарь: {esc(' '.join(prefixes))} "
                   f"(TCP-скан, ~5с на подсеть) …", silent=True, reply_to=reply_to)
        t0 = time.time()
        live = {}
        for p in prefixes:
            live.update(net.scan_subnet(p))
        inv_cams = [c for c in inv.cams() if c.get("ip")
                    and any(c["ip"].startswith(p + ".") for p in prefixes)]
        by_ip = {c["ip"]: c for c in inv_cams}
        new = sorted((ip for ip in live if ip not in by_ip),
                     key=lambda x: tuple(map(int, x.split("."))))
        down = [c for c in inv_cams if c["ip"] not in live]
        macdiff = []
        for c in inv_cams:  # I34: пометка «MAC сменился»
            m = live.get(c["ip"])
            if m and m != "—" and c.get("nmac") and inv.norm_mac(m) != c["nmac"]:
                macdiff.append((c, m))
        lines = [f"🔀 <b>Сеть vs инвентарь</b> ({esc(' '.join(prefixes))}) · "
                 f"{time.time() - t0:.0f}s",
                 f"живых {len(live)} · в инвентаре {len(inv_cams)} · "
                 f"🆕 {len(new)} · ❌ {len(down)} · ⚠️MAC {len(macdiff)}"]
        if new:
            lines.append(f"\n🆕 <b>В сети, но НЕ в инвентаре</b> ({len(new)}):")
            lines += [f"• <code>{ip}</code> {esc(live[ip])}" for ip in new[:25]]
            if len(new) > 25:
                lines.append(f"… и ещё {len(new) - 25}")
        if down:
            lines.append(f"\n❌ <b>В инвентаре, но НЕ отвечают</b> ({len(down)}):")
            lines += [f"• <code>{c['ip']}</code> {esc(c.get('name') or '')} "
                      f"{esc(c.get('location') or '')}" for c in down[:25]]
            if len(down) > 25:
                lines.append(f"… и ещё {len(down) - 25}")
        if macdiff:
            lines.append(f"\n⚠️ <b>MAC сменился</b> ({len(macdiff)}):")
            lines += [f"• <code>{c['ip']}</code> {esc(c.get('name') or '')}: "
                      f"сеть <code>{esc(m)}</code> ≠ инв. <code>{esc(c.get('mac'))}</code>"
                      for c, m in macdiff[:15]]
        if not (new or down or macdiff):
            lines.append("✅ Расхождений нет.")
        send_chunks(chat, lines)
    finally:
        DIFF_LOCK.release()


# ---------- I25 + I29: /sync ----------
def cmd_sync(chat, arg="", reply_to=None):
    chat_action(chat)
    send(chat, "☁️ Пушу Все_камеры.xlsx в Google-таблицу (полная перезапись "
               "значений, ~20-40с) …", silent=True, reply_to=reply_to)
    try:
        send(chat, sh.sync())
    except Exception as e:
        log_exc("/sync")
        send(chat, human_err("Синк в Google Sheets не удался", e), reply_to=reply_to)


# ---------- I26: /sheet ----------
def cmd_sheet(chat, arg="", reply_to=None):
    s = sh.last_sync()
    if s:
        det = (f"Последний синк: <b>{esc(s.get('ts'))}</b> · "
               f"{s.get('cells')} ячеек · 🔴 офлайн {s.get('offline')} · "
               f"{s.get('sec')}s")
    else:
        det = "Синка из бота ещё не было — запусти /sync."
    send(chat, f"📗 <b>Google-таблица инвентаря</b>\n"
               f'<a href="{sh.sheet_url()}">Все камеры МФК Зарядье</a>\n{det}',
         markup={"inline_keyboard": [[{"text": "📗 Открыть таблицу",
                                       "url": sh.sheet_url()}]]},
         reply_to=reply_to)


# ---------- I47: /info из кэшей + кнопка «Обновить» ----------
def cmd_info_cached(chat, arg="", reply_to=None):
    ip = _resolve(chat, arg, "Инфо: <code>/info 10.20.51.50</code> или "
                             "<code>/info AS-7C.01</code>", reply_to)
    if not ip:
        return
    rec = inv.get(ip)
    fc = inv.facts_cam(ip)
    lines = []
    if rec:
        lines.append(inv.card_text(rec))
    else:
        lines.append(f"<code>{ip}</code> — 🆕 в инвентаре не найдена.")
    if fc:
        lines.append(f"📚 Факты (последний свип): "
                     f"{'✅ отвечала' if fc.get('alive') else '❌ молчала'}"
                     + (f" · ARP <code>{esc(fc.get('arp_mac'))}</code>"
                        if fc.get("arp_mac") else ""))
    lines.append("ℹ️ Это данные из кэшей — живой опрос по кнопке ниже.")
    send(chat, "\n".join(lines), markup=_kb(ip, refresh=True), reply_to=reply_to)


HANDLERS = {
    "/cam": cmd_cam, "/where": cmd_where, "/port": cmd_port, "/mac": cmd_mac,
    "/rtsp": cmd_rtsp, "/note": cmd_note, "/verify": cmd_verify,
    "/audit": cmd_audit, "/fw": cmd_fw, "/export": cmd_export,
    "/free_ip": cmd_free_ip, "/diff": cmd_diff, "/sync": cmd_sync,
    "/sheet": cmd_sheet, "/info": cmd_info_cached,
}
ALIASES = {
    "/кам": "/cam", "/камера": "/cam", "/где": "/where", "/порт": "/port",
    "/мак": "/mac", "/ртсп": "/rtsp", "/заметка": "/note", "/сверка": "/verify",
    "/аудит": "/audit", "/прошивки": "/fw", "/экспорт": "/export",
    "/свободные": "/free_ip", "/дифф": "/diff", "/синк": "/sync", "/таблица": "/sheet",
}
