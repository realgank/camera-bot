# -*- coding: utf-8 -*-
"""Волна J — I37+I38+I41: /provision — мастер ПНР заводской камеры Apix
(с завода все на 192.168.0.250 / Admin / 1234). Шаги: обнаружить → показать
модель/серийник/MAC (ONVIF) → /provision <целевой IP> (валидация I38: свободен
в инвентаре И не отвечает в сети) → двухшаговое подтверждение → смена IP
raw-SOAP SetNetworkInterfaces (камера уходит в ребут и поднимается на новом
IP) → шлюз ОТДЕЛЬНЫМ шагом на НОВОМ IP с ретраями (ловушка: HTTP 500 в окне
ребута — из памяти apix-factory-commissioning) → проверка доступности и
hwaddr → автозапись строки в xlsx с бэкапом (I41) + dirty для /sync.
Каждый шаг — в аудит. Пароли камер НИКОГДА не меняются."""
import re
import time
import datetime

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_onvifq as oq
import bot_sw_api as sw
from onvif_snap import DEV_NS, device_info, _grab
from bot_tg import send, edit_message, answer_cq, chat_action
from bot_util import log, log_exc, esc, human_err

SCH = "http://www.onvif.org/ver10/schema"
_confirm = sw.Confirm()


def _audit(step, detail):
    log(f"provision: {step}: {detail}")
    try:
        import bot_obs
        bot_obs.audit(f"provision:{step}", detail, "")
    except Exception:
        pass


def _fip():
    return st.cget("health_factory_ip")


# ---------- парсер GetNetworkInterfaces (тестируется) ----------
def parse_ifaces(xml):
    """XML GetNetworkInterfaces -> {'token','hwaddr','addr','prefix','dhcp'}."""
    t = str(xml or "")
    m = re.search(r'<(?:\w+:)?NetworkInterfaces[^>]*\btoken="([^"]+)"', t)
    out = {"token": m.group(1) if m else None,
           "hwaddr": (_grab(t, "HwAddress") or "").upper(),
           "dhcp": (_grab(t, "DHCP") or "").strip().lower() == "true"}
    mm = re.search(r"<(?:\w+:)?Manual>(.*?)</(?:\w+:)?Manual>", t, re.S)
    src = mm.group(1) if mm else t
    out["addr"] = (_grab(src, "Address") or "").strip()
    try:
        out["prefix"] = int(_grab(src, "PrefixLength"))
    except (TypeError, ValueError):
        out["prefix"] = None
    return out


def get_ifaces(ip, timeout=8):
    """Живой GetNetworkInterfaces -> parse_ifaces (или {'error': …})."""
    r = oq.call(ip, "GetNetworkInterfaces",
                f'<GetNetworkInterfaces xmlns="{DEV_NS}"/>', timeout=timeout)
    if "text" not in r:
        return {"error": r.get("error") or "?"}
    return parse_ifaces(r["text"])


# ---------- I38: валидация целевого IP (чистая, тестируется) ----------
def target_check(ip_s, taken_ips, alive):
    """(ok, причина). taken_ips — IP из инвентаря; alive — отвечает ли адрес
    в сети (TCP/ping). Свободен = нет в инвентаре И молчит в сети."""
    if not net.valid_ip(ip_s):
        return False, "это не IPv4-адрес"
    last = int(ip_s.split(".")[-1])
    if last in (0, 255):
        return False, "сетевой/широковещательный адрес"
    if last == 254:
        return False, ".254 — шлюз подсети, занято по плану"
    if ip_s == _fip():
        return False, "это заводской адрес, целевым быть не может"
    if ip_s in taken_ips:
        return False, "занят в инвентаре"
    if alive:
        return False, "отвечает в сети (кем-то занят)"
    return True, ""


def _target_alive(ip):
    """I38: точно ли адрес молчит — TCP-проба + ping."""
    return net.tcp_alive(ip, ports=(80, 554, 8080), t=1.0) \
        or net.ping(ip) is not None


# ---------- raw-SOAP запись (только в рамках /provision) ----------
def _set_ip_body(token, new_ip, prefix):
    return (f'<SetNetworkInterfaces xmlns="{DEV_NS}">'
            f'<InterfaceToken>{token}</InterfaceToken>'
            f'<NetworkInterface>'
            f'<Enabled xmlns="{SCH}">true</Enabled>'
            f'<IPv4 xmlns="{SCH}"><Enabled>true</Enabled>'
            f'<Manual><Address>{new_ip}</Address>'
            f'<PrefixLength>{prefix}</PrefixLength></Manual>'
            f'<DHCP>false</DHCP></IPv4>'
            f'</NetworkInterface></SetNetworkInterfaces>')


def set_ip(old_ip, token, new_ip, prefix):
    """SetNetworkInterfaces: HTTP 200 = камера приняла и уходит в ребут."""
    r = oq.call(old_ip, "SetNetworkInterfaces",
                _set_ip_body(token, new_ip, prefix), timeout=10)
    if "text" in r and "fault" not in (r["text"] or "").lower():
        return True, ""
    return False, r.get("error") or _grab(r.get("text") or "", "Text") \
        or "SOAP Fault"


def set_gateway(ip, gw, retries=None, delay=None, sleep=time.sleep):
    """Шлюз ОТДЕЛЬНО, на новом IP, с ретраями: в окне ребута камера отдаёт
    HTTP 500 / рвёт соединение (память apix-factory-commissioning).
    Успех = верифицирован через GetNetworkDefaultGateway."""
    retries = retries or int(st.cget("provision_gw_retries"))
    delay = float(delay if delay is not None
                  else st.cget("provision_gw_delay_s"))
    body = (f'<SetNetworkDefaultGateway xmlns="{DEV_NS}">'
            f'<IPv4Address>{gw}</IPv4Address></SetNetworkDefaultGateway>')
    last = "?"
    for i in range(1, retries + 1):
        r = oq.call(ip, "SetNetworkDefaultGateway", body, timeout=8)
        last = r.get("error") or ""
        try:
            gnow = oq.get_net(ip)
            if (gnow.get("gateway") or "").strip() == gw:
                return True, i
            last = last or f"шлюз сейчас «{gnow.get('gateway')}»"
        except Exception as e:
            last = last or type(e).__name__
        if i < retries:
            sleep(delay)
    return False, f"{retries} попыток ({last})"


# ---------- I41: новая строка инвентаря с бэкапом ----------
def append_inventory_row(ip, mac, model, serial, note):
    """Добавляет строку в «Все камеры» ПОСЛЕ автобэкапа. -> (бэкап, строка)."""
    import openpyxl
    import bot_dq
    bak = bot_dq.backup_xlsx("provision")
    path = inv.inv_path()
    n_max = 0
    for c in inv.cams():
        try:
            n_max = max(n_max, int(c.get("n") or 0))
        except (TypeError, ValueError):
            pass
    wb = openpyxl.load_workbook(path)
    try:
        ws = wb[inv.SHEET_MAIN]
        headers = [c.value for c in ws[1]]
        hi = {str(h): i + 1 for i, h in enumerate(headers) if h}
        rn = ws.max_row + 1
        vals = {"№": n_max + 1, "IP-адрес": ip, "MAC-адрес": mac,
                "Модель камеры": model, "Примечание": note}
        if serial:
            for h in hi:
                if "серийн" in h.lower():
                    vals[h] = serial
                    break
        for col, v in vals.items():
            if col in hi and v not in (None, ""):
                ws.cell(row=rn, column=hi[col], value=v)
        wb.save(path)
    finally:
        wb.close()
    with inv._lock:
        inv._inv["mtime"] = None
    try:
        import bot_reconcile
        bot_reconcile.record_change("provision", inv.SHEET_MAIN, mac or ip,
                                    "IP-адрес", "", ip)
        bot_reconcile.after_xlsx_write(f"/provision {ip}")
    except Exception:
        log_exc("provision: журнал изменений не записался")
    return bak, rn


# ---------- команда ----------
def _discover(chat, reply_to=None):
    fip = _fip()
    chat_action(chat)
    if not net.tcp_alive(fip, ports=(80,), t=1.5):
        send(chat, f"🏭 Заводская камера <code>{fip}</code> сейчас НЕ отвечает."
                   f"\nПодключи камеру (health-цикл сам заметит появление) и "
                   f"повтори /provision.", reply_to=reply_to)
        return
    info = device_info(fip, user=st.CAM_USER, pwd=st.CAM_PASS)
    ifc = get_ifaces(fip)
    if info.get("error") or ifc.get("error"):
        send(chat, human_err(f"<code>{fip}</code> отвечает по TCP, но ONVIF "
                             f"не дал данных",
                             info.get("error") or ifc.get("error")),
             reply_to=reply_to)
        return
    subs = st.cget("cam_subnets") or []
    send(chat,
         f"🏭 <b>Заводская камера найдена</b> <code>{fip}</code>\n"
         f"🎥 {esc(info.get('manufacturer'))} {esc(info.get('model'))} · "
         f"fw {esc(info.get('firmware'))}\n"
         f"#️⃣ sn {esc(info.get('serial'))} · MAC <code>{esc(ifc['hwaddr'])}</code>\n"
         f"⚠️ Если ARP и ONVIF-MAC различаются — на .250 сидит НЕСКОЛЬКО "
         f"камер (IP-конфликт): сначала разведи их по PoE.\n\n"
         f"Шаг 2 — целевой адрес: <code>/provision 10.20.50.123</code>\n"
         f"Свободные: " + " · ".join(f"/free_ip {s}" for s in subs[:4]),
         reply_to=reply_to)


def cmd_provision(chat, arg="", reply_to=None):
    a = (arg or "").strip()
    if not a:
        _discover(chat, reply_to)
        return
    target = a.split()[0]
    fip = _fip()
    chat_action(chat)
    taken = {c["ip"] for c in inv.cams() if c.get("ip")}
    ok, why = target_check(target, taken, net.valid_ip(target)
                           and _target_alive(target))
    if not ok:
        pfx = target.rsplit(".", 1)[0] if net.valid_ip(target) else \
            (st.cget("cam_subnets") or ["10.20.50"])[0]
        send(chat, f"⛔ Целевой <code>{esc(target)}</code> не подходит: {why} "
                   f"(I38).\nСвободные адреса: /free_ip {pfx}",
             reply_to=reply_to)
        return
    if not net.tcp_alive(fip, ports=(80,), t=1.5):
        send(chat, f"🏭 Заводская <code>{fip}</code> не отвечает — нечего "
                   f"провижинить.", reply_to=reply_to)
        return
    info = device_info(fip, user=st.CAM_USER, pwd=st.CAM_PASS)
    ifc = get_ifaces(fip)
    if ifc.get("error") or not ifc.get("token"):
        send(chat, human_err("ONVIF заводской камеры не дал InterfaceToken",
                             ifc.get("error")), reply_to=reply_to)
        return
    gw = target.rsplit(".", 1)[0] + ".254"
    prefix = int(st.cget("provision_prefix_len"))
    key = f"{target}|{ifc['hwaddr']}"
    _confirm.put(key, {"target": target, "gw": gw, "prefix": prefix,
                       "token": ifc["token"], "hwaddr": ifc["hwaddr"],
                       "model": info.get("model") or "?",
                       "serial": info.get("serial") or ""})
    _audit("plan", f"{fip} -> {target}/{prefix} gw {gw} "
                   f"mac {ifc['hwaddr']} sn {info.get('serial')}")
    send(chat,
         f"🛠 <b>ПНР: смена IP заводской камеры</b>\n"
         f"🎥 {esc(info.get('model'))} · sn {esc(info.get('serial'))} · "
         f"MAC <code>{esc(ifc['hwaddr'])}</code>\n"
         f"<code>{fip}</code> → <code>{target}</code>/{prefix}, "
         f"шлюз <code>{gw}</code>, DHCP off\n"
         f"После смены: проверка на новом IP, шлюз с ретраями (ловушка "
         f"HTTP 500), строка в xlsx с бэкапом (I41).\n"
         f"Подтверди в течение {st.cget('sw_confirm_ttl_s')}с:",
         markup={"inline_keyboard": [[
             {"text": "🛠 Сменить IP…", "callback_data": f"prov:{key}"},
             {"text": "✖️ Отмена", "callback_data": "cancel"}]]},
         reply_to=reply_to)


def cb_prov(chat, cq, key):
    """Шаг 2: финальное подтверждение."""
    p = _confirm.take(key)
    if not p:
        answer_cq(cq.get("id"), "⌛ Подтверждение устарело — повтори /provision")
        return
    _confirm.put(key, p)
    answer_cq(cq.get("id"), "Нужно финальное подтверждение")
    mid = (cq.get("message") or {}).get("message_id")
    txt = (f"⚠️ <b>Финальное подтверждение ПНР</b>\n"
           f"<code>{_fip()}</code> → <code>{p['target']}</code> "
           f"(MAC {esc(p['hwaddr'])}) — камера перезагрузится. Жми ✅ в "
           f"течение {st.cget('sw_confirm_ttl_s')}с.")
    kb = {"inline_keyboard": [[
        {"text": "✅ Да, сменить IP", "callback_data": f"provy:{key}"},
        {"text": "✖️ Отмена", "callback_data": "cancel"}]]}
    if mid:
        edit_message(chat, mid, txt, markup=kb)
    else:
        send(chat, txt, markup=kb)


def cb_provy(chat, cq, key):
    p = _confirm.take(key)
    if not p:
        answer_cq(cq.get("id"), "⌛ Подтверждение устарело — повтори /provision")
        return
    answer_cq(cq.get("id"), "🛠 Меняю IP…")
    chat_action(chat)
    do_provision(chat, p)


def do_provision(chat, p):
    """Исполнение ПНР по подтверждённому плану p (шаги — в аудит)."""
    fip, target, gw = _fip(), p["target"], p["gw"]
    steps = []
    _audit("go", f"{fip} -> {target} подтверждён владельцем")
    ok, err = set_ip(fip, p["token"], target, p["prefix"])
    if not ok:
        _audit("set_ip_fail", f"{target}: {err}")
        send(chat, human_err(f"SetNetworkInterfaces на <code>{fip}</code> "
                             f"не прошёл", err))
        return
    steps.append("✅ SetNetworkInterfaces принят — камера ушла в ребут")
    _audit("set_ip", f"{fip} -> {target} принят, жду на новом IP")
    # ждём камеру на новом IP
    wait_s = float(st.cget("provision_wait_s"))
    t0 = time.time()
    up = None
    while time.time() - t0 < wait_s:
        if net.tcp_alive(target, ports=(80,), t=1.0):
            up = time.time() - t0
            break
        time.sleep(3)
    if up is None:
        _audit("no_up", f"{target} не поднялась за {wait_s:.0f}s")
        send(chat, "\n".join(steps) + f"\n❌ Камера не поднялась на "
             f"<code>{target}</code> за {wait_s:.0f}с. Проверь /diag {target} "
             f"и /diag {fip}; строка в xlsx НЕ записана.")
        return
    steps.append(f"✅ Поднялась на <code>{target}</code> за {up:.0f}с")
    _audit("up", f"{target} за {up:.0f}s")
    # шлюз отдельным шагом с ретраями (ловушка HTTP 500 в окне ребута)
    gok, tries = set_gateway(target, gw)
    if gok:
        steps.append(f"✅ Шлюз <code>{gw}</code> установлен "
                     f"(с попытки {tries})")
        _audit("gateway", f"{target} gw {gw} с попытки {tries}")
    else:
        steps.append(f"⚠️ Шлюз НЕ подтвердился: {esc(str(tries))} — поставь "
                     f"вручную (камера при этом работает)")
        _audit("gateway_fail", f"{target}: {tries}")
    # верификация hwaddr — та ли камера поднялась
    ifc = get_ifaces(target)
    hw_ok = (ifc.get("hwaddr") or "").upper() == (p["hwaddr"] or "").upper()
    if not hw_ok:
        _audit("hwaddr_mismatch",
               f"{target}: ожидал {p['hwaddr']}, вижу {ifc.get('hwaddr')}")
        steps.append(f"❌ MAC на новом IP <code>{esc(ifc.get('hwaddr') or '?')}"
                     f"</code> ≠ ожидаемому <code>{esc(p['hwaddr'])}</code> — "
                     f"на .250 было НЕСКОЛЬКО камер?! Строку в xlsx не пишу.")
        send(chat, "🛠 <b>ПНР: итог</b>\n" + "\n".join(steps))
        return
    steps.append(f"✅ MAC подтверждён: <code>{esc(p['hwaddr'])}</code>")
    # I41: строка в инвентарь (бэкап внутри) + dirty для /sync
    try:
        note = (f"ПНР ботом {datetime.datetime.now():%d.%m.%Y %H:%M}"
                + ("" if gok else " (шлюз НЕ установлен!)"))
        bak, rn = append_inventory_row(target, p["hwaddr"], p["model"],
                                       p["serial"], note)
        steps.append(f"✅ Строка {rn} в xlsx (бэкап "
                     f"{esc(bak.rsplit(chr(92), 1)[-1])})")
        _audit("xlsx", f"{target} строка {rn}, бэкап {bak}")
        try:
            import bot_autosync
            bot_autosync.mark_dirty(f"/provision {target}")
        except Exception:
            pass
    except Exception as e:
        log_exc("provision: запись в xlsx")
        steps.append(f"❌ Строка в xlsx не записалась: {esc(type(e).__name__)}"
                     f" — добавь вручную")
    send(chat, "🛠 <b>ПНР завершён</b>\n" + "\n".join(steps)
         + f"\nДальше: /accept {target} (приёмка), /sync (таблица).",
         markup={"inline_keyboard": [[
             {"text": "📸 Снимок", "callback_data": f"shot:{target}"},
             {"text": "🩺 Диаг", "callback_data": f"diag:{target}"}]]})


HANDLERS = {"/provision": cmd_provision}
ALIASES = {"/пнр": "/provision"}
CALLBACKS = {"prov": cb_prov, "provy": cb_provy}

try:  # /help: таб «⚙️ Операции» (Волна J)
    import bot_handlers_ux as _ux
    _ux.HELP_TABS["ops"] = ("⚙️ Операции", (
        "<b>Операции с парком</b> (Волна J, всё с двухшаговым "
        "подтверждением):\n"
        "/reboot <code>ip|имя</code> — PoE-ребут порта Cross-24 (отказ, если "
        "на порту >1 MAC)\n"
        "/reboot_soft — мягкий ONVIF-ребут\n"
        "/provision — ПНР заводской камеры 192.168.0.250: обнаружить,\n"
        "/provision <code>ip</code> — сменить IP + шлюз + строка в xlsx\n"
        "/macfill — заполнить пустые MAC в инвентаре (партиями по 10)\n"
        "/unknown_queue — очередь новых хостов → лист «Неизвестные»\n"
        "/autosync <code>on|off|N</code> — автосинк xlsx→Sheets раз в N часов\n"
        "/snapall <code>подсеть|зона</code> — снимки пачкой → Drive (дедуп)\n"
        "/clip <code>ip [сек]</code> — видеоклип с RTSP (нужен ffmpeg)\n"
        "Inline: <code>@бот запрос</code> в любом чате — карточки камер "
        "(включи у BotFather: /setinline; только для владельца)."))
except Exception:
    pass
