# -*- coding: utf-8 -*-
"""Волна H — интерактивные команды портов свитчей (вынос из bot_sw_mon):
357/324 /poe — таблица PoE и бюджет; 355 /flap — ошибки/флапы; 386/387
/swtraffic — загрузка портов и аплинков; 380 /sw_env — сенсоры;
400 /port_history; 385 /cable — TDR с PoE-off СТРОГО через двухшаговое
подтверждение с TTL (PoE возвращается в finally, save не вызывается)."""
import time

import bot_state as st
import bot_metrics as mx
import bot_sw_api as sw
from bot_tg import send, send_chunks, chat_action, answer_cq
from bot_util import log, log_exc, esc

_confirm = sw.Confirm()


def _fmt_w(mw) -> str:
    return f"{(mw or 0) / 1000:.1f}"


# ---------- /poe (357, 324) ----------
def cmd_poe(chat, arg="", reply_to=None):
    r = sw.find_switch(arg)
    if not r:
        send(chat, "PoE по портам: <code>/poe 10.10.60.52</code>", reply_to=reply_to)
        return
    chat_action(chat)
    ip = r["ip"]
    if r.get("kind") == "huawei":
        out = sw.huawei_cli(ip, ["display poe power", "display poe information"])
        ports = sw.hw_parse_poe_ports(out)
        info = sw.hw_parse_poe_info(out)
        act = {p: v for p, v in ports.items() if v["cur_mw"]}
        lines = [f"⚡ <b>PoE {esc(r.get('host') or ip)}</b> (Huawei):",
                 f"потребление {_fmt_w(info.get('consume_mw'))} Вт из "
                 f"{_fmt_w(info.get('supply_mw'))} Вт · пик "
                 f"{_fmt_w(info.get('peak_mw'))} Вт · активных портов {len(act)}"]
        rows = [f"{p:<10} класс {v['cls']:<2} {v['cur_mw'] / 1000:>5.1f} Вт "
                f"(пик {v['peak_mw'] / 1000:.1f})" for p, v in sorted(act.items())]
        if rows:
            lines.append(f"<pre>{esc(chr(10).join(rows))}</pre>")
        send(chat, "\n".join(lines), reply_to=reply_to)
        return
    poe = sw.cross24_get(ip, "poe_poe")
    panel = sw.cross24_get(ip, "panel_info").get("ports") or []
    budget = float(st.cget("sw_poe_budget_w"))
    total = (poe.get("devPower") or 0) / 1000
    rows, edge = [], []
    for i, pp in enumerate(poe.get("ports") or []):
        pname = f"GE{i + 1}"
        li = panel[i] if i < len(panel) else {}
        stat = ("⚡" if pp.get("portStatus") else
                ("·" if pp.get("portEnable") else "✖"))
        w = (pp.get("portPower") or 0) / 1000
        if w >= 12.0:  # 324: AF-порт на грани 15.4 Вт
            edge.append(pname)
        rows.append(f"{pname:<5}{stat} {w:>5.1f}Вт {pp.get('portType') or '':<6}"
                    f"{'up' if li.get('linkup') else 'down':<5}"
                    f"{str(li.get('speed') or ''):>4}")
    lines = [f"⚡ <b>PoE {esc(r.get('host') or ip)}</b> <code>{ip}</code>: "
             f"<b>{total:.1f} Вт</b> из {budget:.0f} "
             f"({100 * total / budget:.0f}% бюджета) · темп. {poe.get('devTemp')}°C",
             f"<pre>{esc(chr(10).join(rows))}</pre>"]
    if edge:
        lines.append(f"⚠️ Порты на грани AF-лимита (324): {esc(', '.join(edge))}")
    lines.append("Кабельная диагностика: /cable <свитч> <порт> (камера мигнёт)")
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- /flap (355) ----------
def cmd_flap(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    r = sw.find_switch(parts[0]) if parts else None
    if not r:
        send(chat, "Ошибки/флапы: <code>/flap 10.20.5.91 [порт]</code> — два "
                   "замера с паузой 5с. История линка Cross-24: /port_history",
             reply_to=reply_to)
        return
    chat_action(chat)
    ip, port_f = r["ip"], (parts[1] if len(parts) > 1 else "")
    if r.get("kind") == "huawei":
        o1 = sw.hw_parse_int_brief(sw.huawei_cli(ip, ["display interface brief"]))
        time.sleep(5)
        o2 = sw.hw_parse_int_brief(sw.huawei_cli(ip, ["display interface brief"]))
        rows = []
        for name, v2 in sorted(o2.items()):
            if port_f and port_f.lower() not in name.lower():
                continue
            v1 = o1.get(name) or {}
            d_in = v2["in_err"] - (v1.get("in_err") or 0)
            d_out = v2["out_err"] - (v1.get("out_err") or 0)
            if v2["in_err"] or v2["out_err"] or d_in or d_out:
                rows.append(f"{name:<12} {v2['phy']:<5} inErr {v2['in_err']}"
                            f"(+{d_in}/5с) outErr {v2['out_err']}(+{d_out})")
        send(chat, f"📉 <b>Ошибки портов {esc(r.get('host') or ip)}</b> (355):\n"
                   + (f"<pre>{esc(chr(10).join(rows))}</pre>" if rows
                      else "ошибок на портах нет ✅"), reply_to=reply_to)
        return
    # Cross-24: web-API отдаёт счётчики только одного порта — используем
    # историю линка из фоновой ротации (ограничение прошивки, 355 частично)
    ev = mx.events(days=7)
    ev = [e for e in ev if e["ip"].startswith(ip + ":")
          and e["kind"] in ("port_down", "port_up", "port_flap")]
    if port_f:
        ev = [e for e in ev if e["ip"].endswith(":" + port_f)]
    lines = [f"📉 <b>Флапы портов {esc(r.get('host') or ip)}</b> за 7 дн. "
             f"(из фоновой ротации; per-port CRC у Cross-24 web-API нет):"]
    cnt: dict = {}
    for e in ev:
        if e["kind"] == "port_down":
            cnt[e["ip"].split(":")[1]] = cnt.get(e["ip"].split(":")[1], 0) + 1
    for p, n in sorted(cnt.items(), key=lambda kv: -kv[1])[:15]:
        lines.append(f"• {esc(p)}: {n} падений — /port_history {ip} {p}")
    if not cnt:
        lines.append("падений линка не зафиксировано ✅")
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- /swtraffic (386/387) ----------
def cmd_swtraffic(chat, arg="", reply_to=None):
    r = sw.find_switch(arg)
    if not r or r.get("kind") != "cross24":
        send(chat, "Загрузка портов: <code>/swtraffic 10.10.60.52</code> "
                   "(Cross-24)", reply_to=reply_to)
        return
    chat_action(chat)
    ip = r["ip"]
    bw = sw.cross24_get(ip, "port_bwutilz")
    names = {i: n for i, n in bw.get("ticks") or []}
    import bot_topo
    ups = set(bot_topo.uplink_ports(ip))
    rows = []
    for dr, title in ((bw.get("plotDataRx") or [], "RX"),
                      (bw.get("plotDataTx") or [], "TX")):
        for series in dr:
            for util, idx in series.get("data") or []:
                pname = str(names.get(idx, idx)).strip()
                if util and pname:
                    rows.append((pname, title, util, series.get("label")))
    lines = [f"📶 <b>Трафик портов {esc(r.get('host') or ip)}</b> (386):"]
    if rows:
        rows.sort(key=lambda x: -x[2])
        for pname, d, util, spd in rows[:20]:
            mark = " ⬆️аплинк" if pname in ups else ""
            lines.append(f"• {esc(pname)} {d}: {util}% ({esc(spd)}){mark}")
    else:
        lines.append("нагрузки нет (все порты ~0%)")
    warn = [x for x in rows if x[0] in ups
            and x[2] >= float(st.cget("uplink_util_warn_pct") or 80)]
    if warn:
        lines.append("⚠️ Аплинк близок к полке (387)!")
    send(chat, "\n".join(lines), reply_to=reply_to)


# ---------- /sw_env (380) ----------
def cmd_sw_env(chat, arg="", reply_to=None):
    r = sw.find_switch(arg)
    if not r:
        send(chat, "Сенсоры свитча: <code>/sw_env 10.10.60.52</code>",
             reply_to=reply_to)
        return
    chat_action(chat)
    ip = r["ip"]
    if r.get("kind") == "huawei":
        out = sw.huawei_cli(ip, ["display temperature all", "display version"])
        t = sw.hw_parse_temperature(out)
        v = sw.hw_parse_version(out)
        up = v.get("uptime_s")
        send(chat, f"🌡 <b>{esc(r.get('host') or ip)}</b> (Huawei {esc(v.get('model'))}):\n"
                   f"температура {t.get('current_c')}°C "
                   f"(порог {t.get('upper_c')}°C, {esc(t.get('status'))})\n"
                   f"аптайм {up // 86400 if up else '?'} дн · VRP {esc(v.get('version'))}",
             reply_to=reply_to)
        return
    cm = sw.cross24_get(ip, "sys_cpumem")
    poe = sw.cross24_get(ip, "poe_poe")
    send(chat, f"🌡 <b>{esc(r.get('host') or ip)}</b> <code>{ip}</code> (380):\n"
               f"CPU {cm.get('cpu')}% · память {cm.get('mem')}% · "
               f"темп. PoE-контроллера {poe.get('devTemp')}°C\n"
               f"PoE сейчас: {_fmt_w(poe.get('devPower'))} Вт", reply_to=reply_to)


# ---------- /port_history (400) ----------
def cmd_port_history(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    if len(parts) < 2:
        send(chat, "История порта: <code>/port_history 10.10.60.52 GE7</code>",
             reply_to=reply_to)
        return
    key = f"{parts[0]}:{parts[1]}"
    ev = mx.events(ip=key, days=30)
    lines = [f"🗂 <b>События порта {esc(parts[1])} @ {esc(parts[0])}</b> за 30 дн.: "
             f"{len(ev)}"]
    for e in ev[-25:]:
        lines.append(f"• {time.strftime('%d.%m %H:%M', time.localtime(e['ts']))} "
                     f"{esc(e['kind'])} {esc(e['info'] or '')}")
    if not ev:
        lines.append("событий нет (порт наблюдается фоновой ротацией)")
    send_chunks(chat, lines)


# ---------- 385: /cable — TDR с PoE-off (двухшаговое подтверждение) ----------
def cmd_cable(chat, arg="", reply_to=None):
    parts = (arg or "").split()
    r = sw.find_switch(parts[0]) if parts else None
    if not r or len(parts) < 2 or r.get("kind") != "cross24":
        send(chat, "TDR-диагностика кабеля: <code>/cable 10.10.60.52 GE7</code>\n"
                   "⚠️ на время теста PoE порта выключается (~30-60с простоя "
                   "камеры) — потребуется подтверждение.", reply_to=reply_to)
        return
    port = parts[1].upper()
    idx = sw.port_index(port)
    if idx is None or idx >= 24:
        send(chat, f"«{esc(parts[1])}» — не медный порт (GE1-GE24).",
             reply_to=reply_to)
        return
    key = f"{r['ip']}|{port}"
    _confirm.put(key, {"ip": r["ip"], "port": port, "idx": idx})
    send(chat, f"⚠️ <b>TDR {esc(port)} @ {esc(r.get('host') or r['ip'])}</b>: "
               f"PoE порта будет выключен на ~3с, камера мигнёт (простой "
               f"~30-60с с её загрузкой).\nПодтверди в течение "
               f"{st.cget('sw_confirm_ttl_s')}с:",
         markup={"inline_keyboard": [[
             {"text": "✅ Да, выполнить TDR", "callback_data": f"cable:{key}"},
             {"text": "✖️ Отмена", "callback_data": "cancel"}]]},
         reply_to=reply_to)


def cb_cable(chat, cq, payload):
    p = _confirm.take(payload)
    if not p:
        answer_cq(cq.get("id"), "⌛ Подтверждение устарело — повтори /cable")
        return
    answer_cq(cq.get("id"), "🔬 Выполняю TDR…")
    ip, port, idx = p["ip"], p["port"], p["idx"]
    log(f"sw_mon: TDR {ip} {port} (PoE off/on) по подтверждению владельца")
    try:
        # PoE off -> TDR -> PoE on (portEnable — RadioGroup 1/0!)
        sw.cross24_set(ip, "poe_poeEdit",
                       {"portList": port, "portEnable": 0, "portWatchDog": 0})
        time.sleep(3)
        res = sw.cross24_set(ip, "diag_copper", {"port": idx})
    finally:
        try:
            sw.cross24_set(ip, "poe_poeEdit",
                           {"portList": port, "portEnable": 1, "portWatchDog": 0})
            state = sw.cross24_get(ip, "poe_poe")["ports"][idx]
            poe_ok = bool(state.get("portEnable"))
        except Exception:
            poe_ok = False
            log_exc(f"sw_mon: не смог вернуть PoE {ip} {port}")
    verdict = sw.lang_label(res.get("copperResult") or "")
    length = res.get("copperLength") or "?"
    txt = {"CopperShort": "короткое замыкание/обесточенное устройство",
           "CopperOpen": "обрыв или свободный конец",
           "CopperNormal": "норма (длина при Normal не измеряется)"} \
        .get(verdict, verdict)
    send(chat, f"🔬 <b>TDR {esc(port)} @ <code>{ip}</code></b> (385):\n"
               f"результат: {esc(txt)}\nдлина: <b>{esc(length)} м</b>\n"
               f"PoE возвращён: {'✅' if poe_ok else '❌ ПРОВЕРЬ ВРУЧНУЮ /poe ' + ip}\n"
               f"⚠️ конфиг свитча не сохранялся (save в этой волне запрещён)")
    mx.event_add(f"{ip}:{port}", "tdr", f"{verdict} {length}m", cooldown_h=0)


HANDLERS = {
    "/poe": cmd_poe, "/flap": cmd_flap, "/swtraffic": cmd_swtraffic,
    "/sw_env": cmd_sw_env, "/port_history": cmd_port_history,
    "/cable": cmd_cable,
}
ALIASES = {"/пое": "/poe", "/кабель": "/cable"}
CALLBACKS = {"cable": cb_cable}
