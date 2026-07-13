# -*- coding: utf-8 -*-
"""Центр управления фоновыми алертами: какие слать владельцу, какие нет.

Каждый фоновый алерт помечен стабильным id (aid). Отправка идёт через
mx.owner_alert(text, aid=...) / bot_health._alert(aid=...) и т.п.; если aid в
конфиге alerts_off — сообщение не уходит. Список и переключатели — команда
/alerts (кнопки), состояние хранится в tg_bot_config.json (alerts_off)."""
import bot_state as st
from bot_tg import send, edit_message, answer_cq
from bot_util import log, esc

# (aid, человекочитаемая подпись). Порядок = вид в /alerts. Группы — пустой aid.
REGISTRY = [
    ("_g", "💓 Health-надзор парка"),
    ("cam_down", "🔴 Камера упала"),
    ("cam_mass", "🔴🔴 Массовое падение"),
    ("cam_up", "🟢 Камера ожила"),
    ("factory_appeared", "🏭 Появилась заводская камера"),
    ("daily_report", "🗓 Ежедневный отчёт"),
    ("_g", "🆕 Watchdog зависших камер"),
    ("newcam_waking", "⏳ Новая камера поднимается"),
    ("newcam_conflict", "🔀 Конфликт IP (неск. MAC)"),
    ("newcam_hung", "🧊 Камера зависла"),
    ("newcam_autoreset", "🔌 Авто-ребут зависшей"),
    ("newcam_dead", "🚨 Камера не оживает (брак)"),
    ("_g", "🔀 Мониторинг свитчей"),
    ("sw_reboot", "♻️ Свитч перезагрузился"),
    ("sw_cpu_high", "🌡 CPU свитча высокий"),
    ("sw_temp_high", "🌡 Свитч греется"),
    ("poe_budget", "⚡ PoE-бюджет >85%"),
    ("port_flap", "📉 Порт флапает"),
    ("poe_zero", "⚡ Линк up, PoE 0 Вт"),
    ("speed_low", "🐌 Линк на 10 Мбит/с"),
    ("multi_mac", "🔀 >1 MAC на порту"),
    ("cam_online", "📷 Камера появилась на порту"),
    ("mac_new", "🛡 Новый/чужой MAC на порту"),
    ("mac_move", "🚚 Камера переехала на порт"),
    ("gw_down", "🚨 Шлюз недоступен"),
    ("gw_mac_change", "🛡 MAC шлюза сменился"),
    ("_g", "⚙️ Прочий мониторинг"),
    ("sw_cfg_change", "📝 Конфиг свитча изменился"),
    ("ttl_change", "🛣 Сменился TTL до камеры"),
    ("rtt_spike", "🐢 RTT ×3 к базовой линии"),
    ("risk_digest", "⚠️ Риск-дайджест"),
    ("img_black", "🖤 Чёрный/белый кадр"),
    ("img_frozen", "🧊 Кадр залип"),
    ("rtsp_zombie", "🧟 RTSP-зомби"),
    ("rtsp_drift", "🎛 Дрейф RTSP-потока"),
    ("_g", "🛡 Безопасность"),
    ("cred_fail", "🚨 Не подошли креды Admin/1234"),
    ("ip_dup", "⚡ Дубль IP (ARP-флап)"),
    ("users_change", "👤 Изменились юзеры камеры"),
    ("risky_ports", "🔓 Открылись рискованные порты"),
    ("enc_drift", "🎛 Дрейф энкодера"),
    ("factory_reset", "🏭 Сброс камеры к заводским"),
    ("_g", "📡 Служебные"),
    ("camtime_cred_fail", "🚨 Креды при синхро времени"),
    ("camtime_summary", "⏰ Дрейф часов камер"),
    ("chan_state", "📶 Смена состояния канала TG"),
    ("gapi_broken", "☁️ Google-доступы сломаны"),
    ("autosync", "🤖 Автосинк xlsx→Sheets"),
    ("nightly_report", "🌙 Ночной отчёт"),
    ("reminder", "⏰ Напоминания ППР/задач"),
]

# «шумные» — кандидаты на выключение одной кнопкой
NOISY = {"daily_report", "newcam_waking", "sw_cpu_high", "poe_zero", "speed_low",
         "cam_online", "ttl_change", "rtt_spike", "risk_digest", "img_frozen",
         "rtsp_drift", "enc_drift", "camtime_summary", "chan_state", "autosync",
         "nightly_report"}

_LABELS = {a: l for a, l in REGISTRY if a != "_g"}


def muted(aid: str) -> bool:
    """True = алерт выключен (не слать)."""
    if not aid:
        return False
    try:
        return aid in (st.cget("alerts_off") or [])
    except Exception:
        return False


def _set_off(off_list) -> None:
    with st._cfg_lock:
        st.CFG["alerts_off"] = sorted(set(off_list))
        st.save_cfg()
    log(f"alerts: alerts_off = {st.CFG['alerts_off']}")


def toggle(aid: str) -> bool:
    """Переключает и возвращает новое состояние muted (True=выключен)."""
    off = set(st.cget("alerts_off") or [])
    now_off = aid not in off
    off.discard(aid) if not now_off else off.add(aid)
    _set_off(off)
    return now_off


# ---------- команда /alerts ----------
def _kb():
    off = set(st.cget("alerts_off") or [])
    rows = []
    for aid, label in REGISTRY:
        if aid == "_g":
            continue
        mark = "🔕" if aid in off else "✅"
        rows.append([{"text": f"{mark} {label}", "callback_data": f"altg:{aid}"}])
    rows.append([
        {"text": "🔕 Выключить все шумные", "callback_data": "altbulk:noisy"},
        {"text": "🔔 Включить все", "callback_data": "altbulk:allon"}])
    return {"inline_keyboard": rows}


def _text():
    off = set(st.cget("alerts_off") or [])
    lines = ["🔔 <b>Управление алертами</b> — жми, чтобы вкл/выкл. "
             "✅ = шлётся, 🔕 = выключен."]
    for aid, label in REGISTRY:
        if aid == "_g":
            lines.append(f"\n<b>{esc(label)}</b>")
        else:
            lines.append(f"{'🔕' if aid in off else '✅'} {esc(label)}")
    n = len(off)
    lines.append(f"\nВыключено: <b>{n}</b>. Меняется на лету, перезапуск не нужен.")
    return "\n".join(lines)


def cmd_alerts(chat, arg="", reply_to=None):
    send(chat, _text(), markup=_kb(), reply_to=reply_to)


def cb_toggle(chat, cq, aid):
    if aid not in _LABELS:
        answer_cq(cq.get("id"), "неизвестный алерт")
        return
    now_off = toggle(aid)
    answer_cq(cq.get("id"),
              f"{'🔕 выключен' if now_off else '✅ включён'}: {_LABELS[aid]}")
    mid = (cq.get("message") or {}).get("message_id")
    if mid:
        edit_message(chat, mid, _text(), markup=_kb())


def cb_bulk(chat, cq, what):
    off = set(st.cget("alerts_off") or [])
    if what == "noisy":
        off |= NOISY
        msg = f"🔕 Выключил все шумные ({len(NOISY)})"
    else:  # allon
        off = set()
        msg = "🔔 Включил все алерты"
    _set_off(off)
    answer_cq(cq.get("id"), msg)
    mid = (cq.get("message") or {}).get("message_id")
    if mid:
        edit_message(chat, mid, _text(), markup=_kb())


HANDLERS = {"/alerts": cmd_alerts}
ALIASES = {"/алерты": "/alerts", "/уведомления": "/alerts"}
CALLBACKS = {"altg": cb_toggle, "altbulk": cb_bulk}
