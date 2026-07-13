# -*- coding: utf-8 -*-
"""Волна G — аудит безопасности парка (330-340), всё read-only:
330 дубли IP по ARP-флаппингу (один IP отвечает разными MAC);
331 камеры на DHCP (GetNetworkInterfaces); 332 дефолтный hostname;
333 контроль gateway (ловушка SetNetworkDefaultGateway=500 из ПНР);
334 аудит пользователей GetUsers (диффы — алерт), пароли НЕ трогаем;
335 канарейка кредов: 401 на Admin/1234 -> критический алерт (bot_camtime
    и этот модуль); 336 порт-скан против baseline per камера (telnet/ftp
    открылись -> алерт); 337 прошивки против эталона (models.json волны F);
    338 детект сброса на заводские по совокупности (hostname+DHCP+часы+юзеры);
339 инвентаризация энкодеров в _facts_encoders.json; 340 дрейф-детект
    конфигурации энкодера. Фон — ротацией secaudit_batch раз в
    secaudit_period_min; 322-329 (PoE/порты свитчей) -> волна H."""
import time
import collections
from concurrent.futures import ThreadPoolExecutor

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_store as store
import bot_metrics as mx
import bot_onvifq as q
from bot_tg import send, send_chunks, chat_action
from bot_util import log, log_exc, esc

_arp_prev: dict = {}                       # ip -> mac (последний виденный)
_arp_hist: dict = {}                       # ip -> [(ts, mac_old, mac_new)]


def _spath():
    return st.cget("secaudit_state_path")


def _state() -> dict:
    return store.jload(_spath(), {})


def is_default_host(name: str, patterns=None) -> bool:
    """332: заводской hostname (чистая функция)."""
    low = (name or "").strip().lower()
    if not low:
        return False
    return any(p in low for p in (patterns or st.cget("sec_default_hosts")))


def gw_expected(ip: str) -> str:
    """333: шлюз камеры должен быть .254 своей /24 (политика сети)."""
    return ip.rsplit(".", 1)[0] + ".254"


def enc_drift(ref: list, cur: list) -> list:
    """340: [(поток, поле, было, стало)] по name-совпадению конфигураций."""
    out = []
    by_name = {e.get("name"): e for e in ref or []}
    for e in cur or []:
        r = by_name.get(e.get("name"))
        if not r:
            continue
        for k in ("codec", "res", "fps", "kbps"):
            if r.get(k) is not None and e.get(k) is not None \
                    and r[k] != e[k]:
                out.append((e.get("name") or "?", k, r[k], e[k]))
    return out


def _canary(ip: str) -> None:
    """335: 401 на Admin/1234 — критический алерт (не «офлайн»!)."""
    if mx.event_add(ip, "auth401", cooldown_h=24):
        mx.owner_alert(f"🚨 <b>КРИТИЧНО: не подошли креды Admin/1234</b> на "
                       f"{esc(inv.label(ip) or ip)} — кто-то сменил пароль "
                       f"камеры! (335)", aid="cred_fail")


# ---------- 330: ARP-флаппинг ----------
def _cam_ip(ip: str) -> bool:
    return any(ip.startswith(s + ".") for s in st.cget("cam_subnets") or [])


def _tick_arp() -> None:
    if not mx.due("arp_flap", 2, first_delay_s=150):
        return
    win = float(st.cget("arp_flap_window_min")) * 60
    now = time.time()
    for ip, mac in net.arp_table().items():
        if not _cam_ip(ip) or not mac or mac.startswith("ff:"):
            continue
        prev = _arp_prev.get(ip)
        if prev and prev != mac:
            hist = _arp_hist.setdefault(ip, [])
            hist.append((now, prev, mac))
            _arp_hist[ip] = [h for h in hist if now - h[0] <= win]
            if len(_arp_hist[ip]) >= 2:
                macs = sorted({m for _t, a, b in _arp_hist[ip]
                               for m in (a, b)})
                if mx.event_add(ip, "arp_flap", " / ".join(macs)):
                    mx.owner_alert(
                        f"⚡ <b>Дубль IP (ARP-флаппинг)</b>: <code>{ip}</code> "
                        f"отвечает попеременно MAC:\n"
                        + "\n".join(f"• <code>{esc(m)}</code> "
                                    f"({esc(net.vendor(m))})" for m in macs)
                        + "\nКлассика после ПНР заводских камер (330).",
                        aid="ip_dup")
        _arp_prev[ip] = mac


# ---------- фоновый свип (331-334, 336, 338-340) ----------
def _sweep_one(ip: str) -> None:
    rec = {"checked": int(time.time())}
    n = q.get_net(ip)
    if n.get("auth"):
        _canary(ip)
        return
    if "error" not in n:
        rec["dhcp"] = bool(n.get("dhcp"))                       # 331
        rec["gw"] = n.get("gateway") or ""
        rec["gw_bad"] = bool(rec["gw"]) and rec["gw"] != gw_expected(ip)  # 333
    h = q.get_hostname(ip)
    if h.get("auth"):
        _canary(ip)
        return
    if "error" not in h:
        rec["host"] = h.get("name") or ""
        rec["host_def"] = is_default_host(rec["host"])          # 332
    u = q.get_users(ip)
    if "users" in u:
        old = (_state().get(ip) or {}).get("users")
        rec["users"] = u["users"]
        if old is not None and old != u["users"]:               # 334
            if mx.event_add(ip, "users_change",
                            f"{','.join(old)} -> {','.join(u['users'])}"):
                mx.owner_alert(f"👤 <b>Изменились пользователи камеры</b> "
                               f"{esc(inv.label(ip) or ip)}:\nбыло: "
                               f"<code>{esc(', '.join(old) or '—')}</code>\n"
                               f"стало: <code>{esc(', '.join(u['users']))}"
                               f"</code>\nЧьё-то вмешательство? (334)",
                               aid="users_change")
    ports = net.open_ports(ip, ports=tuple(st.cget("sec_ports")))
    old_ports = (_state().get(ip) or {}).get("ports")
    rec["ports"] = ports
    if old_ports is not None:                                   # 336
        new = sorted(set(ports) - set(old_ports))
        if new:
            risky = [p for p in new if p in (st.cget("sec_risky_ports") or [])]
            if mx.event_add(ip, "port_new", ",".join(map(str, new))):
                if risky:
                    mx.owner_alert(f"🔓 <b>Открылись рискованные порты</b> "
                                   f"{esc(', '.join(map(str, risky)))} на "
                                   f"{esc(inv.label(ip) or ip)} — сброс в "
                                   f"дефолт или зловред? (336)", aid="risky_ports")
    e = q.get_encoders(ip)
    if "encoders" in e and e["encoders"]:                       # 339/340
        def _fn(d):
            ent = d.get(ip) or {}
            if not ent.get("ref"):
                ent["ref"] = e["encoders"]
            else:
                for name, k, was, now_ in enc_drift(ent["ref"], e["encoders"]):
                    if mx.event_add(ip, "enc_drift",
                                    f"{name}.{k}: {was} -> {now_}"):
                        mx.owner_alert(
                            f"🎛 <b>Дрейф энкодера</b> "
                            f"{esc(inv.label(ip) or ip)} [{esc(name)}]: "
                            f"{esc(k)} {esc(was)} → {esc(now_)} (340)",
                            aid="enc_drift")
            ent["cur"] = e["encoders"]
            ent["ts"] = int(time.time())
            d[ip] = ent
            return d
        store.jupdate(st.cget("encoders_facts_path"), {}, _fn)
    # 338: сброс на заводские по совокупности признаков
    try:
        year = (store.jload(st.cget("camtime_state_path"), {})
                .get(ip) or {}).get("year")
    except Exception:
        year = None
    if rec.get("host_def") and rec.get("dhcp") \
            and len(rec.get("users") or ["x"]) <= 1 \
            and (year or 9999) < 2020:
        if mx.event_add(ip, "factory_reset", rec.get("host") or ""):
            mx.owner_alert(f"🏭 <b>Похоже на сброс к заводским</b>: "
                           f"{esc(inv.label(ip) or ip)} — дефолтный hostname "
                           f"+ DHCP + часы {year} + один пользователь (338)",
                           aid="factory_reset")
    def _save(d):
        cur = d.get(ip) or {}
        cur.update(rec)
        d[ip] = cur
        return d
    store.jupdate(_spath(), {}, _save)


def _tick_sweep() -> None:
    if not st.cget("secaudit_enabled"):
        return
    if not mx.due("secaudit", float(st.cget("secaudit_period_min")),
                  first_delay_s=540):
        return
    batch = mx.rotation_batch("secaudit", int(st.cget("secaudit_batch")))
    if not batch:
        return
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="sec") as ex:
        list(ex.map(_sweep_one, batch))
    log(f"secaudit: свип {len(batch)} камер за {time.time() - t0:.1f}s")


def _tick() -> None:
    _tick_arp()
    _tick_sweep()


# ---------- /secaudit ----------
def _report_summary() -> list:
    d = _state()
    dhcp = [ip for ip, r in d.items() if r.get("dhcp")]
    hdef = [ip for ip, r in d.items() if r.get("host_def")]
    gwb = [ip for ip, r in d.items() if r.get("gw_bad")]
    cnt = mx.event_counts(7)
    lines = [f"🛡 <b>Аудит безопасности парка</b> — обследовано "
             f"{len(d)} камер (фоновая ротация):",
             f"• на DHCP (331): <b>{len(dhcp)}</b> · дефолтный hostname "
             f"(332): <b>{len(hdef)}</b> · кривой gateway (333): "
             f"<b>{len(gwb)}</b>",
             f"• события за 7 дн.: канарейка кредов (335): "
             f"<b>{cnt.get('auth401', 0)}</b> · ARP-флаппинг (330): "
             f"{cnt.get('arp_flap', 0)} · новые порты (336): "
             f"{cnt.get('port_new', 0)} · смена юзеров (334): "
             f"{cnt.get('users_change', 0)}",
             f"• дрейф энкодеров (340): {cnt.get('enc_drift', 0)} · "
             f"сбросы к заводским (338): {cnt.get('factory_reset', 0)}"]
    try:
        import bot_reports
        lag = bot_reports.fw_lag()
        lines.append(f"• не на эталонной прошивке (337): <b>{len(lag)}</b>")
    except Exception:
        pass
    lines.append("Разделы: /secaudit net · users · ports · enc · fw · events\n"
                 "322-329 (PoE/порты свитчей) → волна H.")
    return lines


def _fmt_ips(ips, n=15):
    s = ", ".join(f"<code>{i}</code>" for i in sorted(ips)[:n])
    return s + (f" … и ещё {len(ips) - n}" if len(ips) > n else "")


def cmd_secaudit(chat, arg="", reply_to=None):
    chat_action(chat)
    a = (arg or "").strip().lower()
    d = _state()
    if a in ("", "sum", "сводка"):
        send_chunks(chat, _report_summary())
        return
    if a == "net":
        lines = ["🛡 <b>Сеть камер</b> (331-333):"]
        dhcp = [ip for ip, r in d.items() if r.get("dhcp")]
        gwb = [(ip, r.get("gw")) for ip, r in d.items() if r.get("gw_bad")]
        hdef = [(ip, r.get("host")) for ip, r in d.items() if r.get("host_def")]
        lines.append(f"На DHCP ({len(dhcp)}) — уплывут при сбое DHCP:")
        if dhcp:
            lines.append(_fmt_ips(dhcp))
        lines.append(f"\nGateway ≠ .254 ({len(gwb)}):")
        lines += [f"• <code>{ip}</code>: gw {esc(g or '—')} "
                  f"(ожидался {gw_expected(ip)})" for ip, g in gwb[:15]]
        lines.append(f"\nДефолтный hostname ({len(hdef)}):")
        lines += [f"• <code>{ip}</code>: {esc(h or '?')}"
                  for ip, h in hdef[:15]]
        send_chunks(chat, lines)
        return
    if a == "users":
        cnt = collections.Counter(tuple(r.get("users") or [])
                                  for r in d.values() if r.get("users"))
        lines = ["👤 <b>Пользователи камер</b> (334, read-only):"]
        for users, n in cnt.most_common(10):
            lines.append(f"• <code>{esc(', '.join(users))}</code>: {n} камер")
        ev = mx.events(kind="users_change", days=30)
        if ev:
            lines.append("\nИзменения за 30 дн.:")
            lines += [f"• {time.strftime('%d.%m', time.localtime(e['ts']))} "
                      f"<code>{e['ip']}</code>: {esc(e['info'])}"
                      for e in ev[-10:]]
        send_chunks(chat, lines)
        return
    if a == "ports":
        base = {80, 554, 8080}
        odd = [(ip, r.get("ports")) for ip, r in d.items()
               if set(r.get("ports") or []) - base]
        lines = [f"🔓 <b>Порты сверх типовых 80/554/8080</b> "
                 f"({len(odd)} камер, 336):"]
        lines += [f"• <code>{ip}</code>: "
                  f"{esc(', '.join(str(p) for p in sorted(set(p_) - base)))}"
                  for ip, p_ in odd[:20]]
        ev = mx.events(kind="port_new", days=30)
        if ev:
            lines.append("\nОткрывались за 30 дн.:")
            lines += [f"• <code>{e['ip']}</code>: {esc(e['info'])}"
                      for e in ev[-10:]]
        send_chunks(chat, lines)
        return
    if a == "enc":
        f = store.jload(st.cget("encoders_facts_path"), {})
        lines = [f"🎛 <b>Энкодеры</b> (339): снято с {len(f)} камер"]
        cnt = collections.Counter()
        for ent in f.values():
            for e in ent.get("cur") or []:
                cnt[f"{e.get('codec')} {e.get('res')}"] += 1
        for k, n in cnt.most_common(10):
            lines.append(f"• {esc(k)}: {n} потоков")
        ev = mx.events(kind="enc_drift", days=30)
        if ev:
            lines.append("\nДрейфы за 30 дн. (340):")
            lines += [f"• <code>{e['ip']}</code>: {esc(e['info'])}"
                      for e in ev[-10:]]
        send_chunks(chat, lines)
        return
    if a == "fw":
        try:
            import bot_reports
            lag = bot_reports.fw_lag()
        except Exception:
            lag = []
        lines = [f"🧩 <b>Прошивки против эталона</b> (337): отстают "
                 f"{len(lag)} (эталон — models.json, волна F)"]
        lines += [f"• <code>{ip}</code> {esc(m)}: {esc(fw)} ≠ {esc(rfw)}"
                  for ip, m, fw, rfw in lag[:20]]
        send_chunks(chat, lines)
        return
    if a == "events":
        ev = mx.events(days=7)
        sec = [e for e in ev if e["kind"] in
               ("auth401", "arp_flap", "port_new", "users_change",
                "factory_reset", "enc_drift")]
        lines = [f"🗂 <b>События безопасности за 7 дн.</b>: {len(sec)}"]
        lines += [f"• {time.strftime('%d.%m %H:%M', time.localtime(e['ts']))} "
                  f"<code>{e['ip']}</code> {e['kind']}: {esc(e['info'] or '')}"
                  for e in sec[-25:]]
        send_chunks(chat, lines)
        return
    send(chat, "Разделы: /secaudit [net|users|ports|enc|fw|events]",
         reply_to=reply_to)


try:
    import bot_health as _bh
    _bh.MINUTE_TICKS.append(_tick)
except Exception:
    pass

HANDLERS = {"/secaudit": cmd_secaudit}
ALIASES = {"/аудитсек": "/secaudit"}
CALLBACKS: dict = {}
