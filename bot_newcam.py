# -*- coding: utf-8 -*-
"""Watchdog новых/заводских камер, зависших в полу-боот состоянии.

Кейс (реальный): только что воткнутая камера — заводской IP 192.168.0.250
(health_factory_ip) или из newcam_watch_ips — пингуется по ICMP, но ONVIF/HTTP
(порт 80) молчит: «зависла»/грузится, либо конфликт IP (несколько MAC на одном
адресе). Тик из health MINUTE_TICKS раз в newcam_period_min. Автомат по IP:

  нет ICMP                         -> absent   (сброс трекинга, тихо)
  MAC сменился с прошлого тика      -> conflict (алерт; НЕ ребутим — человек
                                       решает, какую камеру гасить)
  TCP/80 открыт                    -> up       (сброс hung; «онлайн» шлёт I40)
  пинг да / TCP нет, < boot_grace  -> booting  (норм. загрузка, тихо)
  boot_grace..hung_after           -> waking   (1 info «поднимается»)
  >= hung_after                    -> HUNG -> обход:
      порт по MAC (bot_poe.find_port + port_guard); если Cross-24, 1 MAC на
      порту, медный GE1-24 и newcam_auto_poe — PoE off/on (bot_poe.poe_cycle),
      ждём возврат, отчёт. Лимит 1 ребут / newcam_reset_cd_h. Не ожила после
      ребута -> эскалация «брак/замена». Порт не найден / >1 MAC / авто off ->
      алерт с кнопкой ручного /reboot.

Гарды: только Cross-24, только 1 MAC на порту (иначе можно погасить аплинк),
save на свитче не вызывается, всё под cooldown. Huawei не трогаем (bot_poe
сам откажет). Blast-radius ограничен заводским/провижн-IP."""
import time
import json

import bot_state as st
import bot_net as net
import bot_inventory as inv
import bot_metrics as mx
from bot_tg import send, answer_cq
from bot_util import log, log_exc, esc, human_err


# ---------- состояние per-IP (kv в metrics-БД, переживает рестарт) ----------
def _load(ip: str) -> dict:
    try:
        return json.loads(mx.kv_get(f"newcam:{ip}") or "{}")
    except Exception:
        return {}


def _save(ip: str, d: dict) -> None:
    try:
        mx.kv_set(f"newcam:{ip}", json.dumps(d, ensure_ascii=False)[:2000])
    except Exception:
        log_exc(f"newcam: save {ip}")


def _mac_h(nmac: str) -> str:
    if not nmac or len(nmac) < 12:
        return nmac or ""
    return ":".join(nmac[i:i + 2] for i in range(0, 12, 2))


def _alert(text: str, markup=None, aid: str = None) -> None:
    """Алерт владельцу (с кнопкой при необходимости)."""
    if aid:
        try:
            import bot_alerts
            if bot_alerts.muted(aid):
                return
        except Exception:
            pass
    owner = st.cget("owner_chat_id")
    if owner:
        try:
            send(owner, text, markup=markup)
        except Exception:
            log_exc("newcam: alert")


def _targets() -> list:
    ips = []
    if st.cget("newcam_watch_factory"):
        fip = str(st.cget("health_factory_ip") or "").strip()
        if fip:
            ips.append(fip)
    for x in (st.cget("newcam_watch_ips") or []):
        x = str(x).strip()
        if x and x not in ips:
            ips.append(x)
    return ips


# ---------- конечный автомат ----------
def _check(ip: str) -> None:
    now = int(time.time())
    d = _load(ip)

    # ping освежает ARP-запись перед чтением MAC
    if net.ping(ip) is None:
        if d.get("state") not in (None, "absent"):
            log(f"newcam: {ip} пропал из сети")
        _save(ip, {"state": "absent"})
        return

    nmac = inv.norm_mac(net.arp_table().get(ip) or "")
    prev = d.get("mac") or ""
    seen = set(d.get("macs") or [])
    if nmac:
        seen.add(nmac)

    # конфликт IP: MAC сменился между тиками -> ≥2 устройства на одном адресе
    if nmac and prev and nmac != prev:
        seen.add(prev)
        if mx.event_add(ip, "newcam_conflict", ",".join(sorted(seen))[:200],
                        cooldown_h=float(st.cget("newcam_conflict_cd_h"))):
            _alert_conflict(ip, sorted(seen))

    if net.tcp_alive(ip, ports=(80, 554), t=1.5):
        if d.get("state") == "hung":
            log(f"newcam: {ip} ожил (TCP up)")
        _save(ip, {"state": "up", "mac": nmac, "macs": sorted(seen)})
        return

    # пинг есть, ONVIF/TCP нет -> booting / waking / hung
    hs = d.get("hung_since")
    new_episode = (not hs) or (nmac and nmac != prev) \
        or d.get("state") in (None, "up", "absent")
    if new_episode:
        hs = now
    elapsed = now - int(hs)
    grace = float(st.cget("newcam_boot_grace_s"))
    hung_after = float(st.cget("newcam_hung_after_s"))

    if elapsed >= hung_after:
        state = "hung"
    elif elapsed >= grace:
        state = "waking"
        if mx.event_add(ip, "newcam_waking", "", cooldown_h=1):
            _alert(f"⏳ <b>Новая камера поднимается</b> <code>{ip}</code> — "
                   f"пингуется, но ONVIF/порт 80 молчит {elapsed}с. Жду…",
                   aid="newcam_waking")
    else:
        state = "booting"

    _save(ip, {"state": state, "mac": nmac, "macs": sorted(seen),
               "hung_since": int(hs)})

    if state == "hung":
        _handle_hung(ip, nmac, elapsed)


def _alert_conflict(ip: str, macs: list) -> None:
    lines = [f"🔀 <b>Конфликт IP</b> <code>{ip}</code>: адрес отвечает с "
             f"{len(macs)} разных MAC — несколько заводских камер на одном "
             f"адресе. Разведи по одной (обесточь/выдерни лишние, провижень "
             f"по очереди):"]
    for m in macs[:6]:
        hh = _mac_h(m)
        lines.append(f"• <code>{esc(hh)}</code> — {esc(net.vendor(hh))}")
    _alert("\n".join(lines), aid="newcam_conflict")


def _handle_hung(ip: str, nmac: str, elapsed: int) -> None:
    """Обход зависшей камеры: локация порта -> авто-PoE или кнопка вручную."""
    import bot_poe as poe

    if not nmac:
        if mx.event_add(ip, "newcam_hung_nomac", "", cooldown_h=6):
            _alert(f"🧊 <b>Камера зависла</b> <code>{ip}</code>: пинг есть, "
                   f"ONVIF молчит {elapsed}с, но MAC не виден в ARP — порт не "
                   f"найти. Нужен ручной осмотр / power-cycle.", aid="newcam_hung")
        return

    try:
        info, err = poe.find_port(ip, nmac)
    except Exception:
        log_exc(f"newcam: find_port {ip}")
        info, err = None, "ошибка поиска порта"
    if err or not info:
        if mx.event_add(ip, "newcam_hung_noport", str(err)[:80], cooldown_h=6):
            _alert(f"🧊 <b>Камера зависла</b> <code>{ip}</code> ({elapsed}с): "
                   f"порт на свитче не найден — {esc(str(err))}. Нужен ручной "
                   f"power-cycle.", aid="newcam_hung")
        return

    guard = poe.port_guard(info, nmac)
    auto = bool(st.cget("newcam_auto_poe"))
    host = info.get("host") or info["sw_ip"]

    # авто нельзя (гард/выключено) -> предложить ручной ребут кнопкой
    if guard or not auto:
        if mx.event_add(ip, "newcam_hung_manual", "", cooldown_h=6):
            reason = (f"⛔ авто-PoE нельзя: {guard}" if guard
                      else "авто-PoE выключен (newcam_auto_poe=false)")
            _alert(
                f"🧊 <b>Камера зависла</b> <code>{ip}</code>: пинг есть, ONVIF "
                f"молчит {elapsed}с.\n🔌 порт <b>{esc(info['port'])}</b> @ "
                f"{esc(host)} <code>{info['sw_ip']}</code>\n{reason}",
                markup={"inline_keyboard": [[
                    {"text": "⚡ Ребут по PoE…", "callback_data": f"ncReb:{ip}"},
                    {"text": "🩺 Диаг", "callback_data": f"diag:{ip}"}]]},
                aid="newcam_hung")
        return

    # авто-PoE: лимит 1 ребут / newcam_reset_cd_h (event_add=False -> недавно был)
    if not mx.event_add(ip, "newcam_autoreset", f"{info['sw_ip']}:{info['port']}",
                        cooldown_h=float(st.cget("newcam_reset_cd_h"))):
        if mx.event_add(ip, "newcam_dead", "", cooldown_h=24):
            _alert(f"🚨 <b>Камера не оживает</b> <code>{ip}</code> — после авто-"
                   f"PoE-ребута всё ещё пинг-есть/ONVIF-нет ({elapsed}с). "
                   f"Вероятно брак / битая прошивка. Порт {esc(info['port'])} @ "
                   f"<code>{info['sw_ip']}</code> — нужна замена / ручной осмотр.",
                   aid="newcam_dead")
        return

    _alert(f"🔌 <b>Авто-ребут зависшей камеры</b> <code>{ip}</code>: пинг есть, "
           f"ONVIF молчит {elapsed}с. Дёргаю PoE порт <b>{esc(info['port'])}</b> "
           f"@ {esc(host)} <code>{info['sw_ip']}</code>…", aid="newcam_autoreset")
    try:
        poe_ok, back = poe.poe_cycle(info["sw_ip"], info["port"], ip)
    except Exception as e:
        log_exc(f"newcam: poe_cycle {ip}")
        _alert(f"❌ Авто-ребут <code>{ip}</code>: ошибка PoE — "
               f"{esc(human_err(e))}. Ручной /reboot {ip}.", aid="newcam_autoreset")
        return

    if not poe_ok:
        _alert(f"❌ Авто-ребут <code>{ip}</code>: PoE порта {esc(info['port'])} @ "
               f"<code>{info['sw_ip']}</code> не вернулся — проверь "
               f"/poe {info['sw_ip']}", aid="newcam_autoreset")
        return
    if back is not None:
        _alert(f"✅ <b>Камера ожила после авто-ребута</b> <code>{ip}</code> — "
               f"вернулась за {back:.0f}с (порт {esc(info['port'])} @ "
               f"<code>{info['sw_ip']}</code>). Провижень: /provision {ip}",
               aid="newcam_autoreset")
        _save(ip, {"state": "up", "mac": nmac})
    else:
        _alert(f"⚠️ Авто-ребут <code>{ip}</code>: PoE вернулся, но камера пока "
               f"не ответила — ещё грузится или брак. Проверю на след. тике.",
               aid="newcam_autoreset")


# ---------- команда /newcam: ручной статус ----------
def cmd_newcam(chat, arg="", reply_to=None):
    a = (arg or "").strip()
    ips = _targets() if not a else [a if net.valid_ip(a)
                                    else (inv.resolve_ip(a) or a)]
    if not ips:
        send(chat, "Наблюдение пусто. Заводской IP — в <code>health_factory_ip</code>"
                   ", доп. — <code>newcam_watch_ips</code>. Пример: "
                   "<code>/newcam 192.168.0.250</code>", reply_to=reply_to)
        return
    lines = ["🩺 <b>Watchdog новых/заводских камер</b>:"]
    for ip in ips:
        if net.ping(ip) is None:
            lines.append(f"⚫ <code>{ip}</code> — не в сети")
            continue
        up = net.tcp_alive(ip, ports=(80, 554), t=1.5)
        nmac = inv.norm_mac(net.arp_table().get(ip) or "")
        ven = net.vendor(_mac_h(nmac)) if nmac else "—"
        d = _load(ip)
        if up:
            lines.append(f"🟢 <code>{ip}</code> — ONLINE (ONVIF/80 ок) · {esc(ven)}")
        else:
            hs = d.get("hung_since")
            el = int(time.time()) - int(hs) if hs else 0
            lines.append(f"🟠 <code>{ip}</code> — пинг есть, ONVIF молчит {el}с "
                         f"[{esc(str(d.get('state') or '?'))}] · {esc(ven)}")
    kb = None
    if len(ips) == 1 and net.ping(ips[0]) is not None \
            and not net.tcp_alive(ips[0], ports=(80,), t=1.0):
        kb = {"inline_keyboard": [[
            {"text": "⚡ Ребут по PoE…", "callback_data": f"ncReb:{ips[0]}"},
            {"text": "🩺 Диаг", "callback_data": f"diag:{ips[0]}"}]]}
    send(chat, "\n".join(lines), markup=kb, reply_to=reply_to)


def cb_manual_reboot(chat, cq, ip):
    """Кнопка «Ребут по PoE» -> штатный двухшаговый /reboot из bot_poe."""
    answer_cq(cq.get("id"), "Готовлю PoE-ребут…")
    try:
        import bot_poe as poe
        poe.cmd_reboot(chat, ip)
    except Exception:
        log_exc(f"newcam: manual reboot {ip}")


# ---------- тик ----------
def _tick():
    if not st.cget("newcam_enabled"):
        return
    if not mx.due("newcam", float(st.cget("newcam_period_min")),
                  first_delay_s=300):
        return
    for ip in _targets():
        try:
            _check(ip)
        except Exception:
            log_exc(f"newcam: тик {ip}")


try:
    import bot_health as _bh
    _bh.MINUTE_TICKS.append(_tick)
except Exception:
    pass

HANDLERS = {"/newcam": cmd_newcam}
ALIASES = {"/новаякамера": "/newcam", "/зависшие": "/newcam"}
CALLBACKS = {"ncReb": cb_manual_reboot}
