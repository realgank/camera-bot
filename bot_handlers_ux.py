# -*- coding: utf-8 -*-
"""UX Волны C: /find с живым прогрессом (U5), пагинацией (U9), кнопками
подсетей (U14), «Опросить ещё» (U27), подтверждением чужой подсети (U38),
кнопкой Стоп (U39) и ETA (U40); /fav (U6) + снимки избранных (U50);
/last (U8); /map (U29); /help с табами (U46). Тихие промежуточные — U32.
Альбомы и мульти-IP (U10/U17/U18/U24) — bot_handlers_media;
U28/U42/U43 — в bot_handlers. Пароли камер НЕ меняются."""
import time
import threading
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor

import bot_state as st
import bot_net as net
import bot_inventory as inv
from bot_handlers_media import _send_album
from bot_tg import send, edit_message, chat_action, answer_cq
from bot_util import log, esc
from onvif_snap import device_info

FIND_LOCK = threading.Lock()          # R10: single-flight
_stop = threading.Event()             # U39
_fs = {"pages": [], "page": 0, "chat": None, "mid": None,
       "rest": [], "ports": {}, "prefix": ""}   # состояние последнего /find
_fs_lock = threading.Lock()


# ---------- U14/U38: /find — вход ----------
def cmd_find(chat, arg="", reply_to=None):
    prefix = (arg or "").strip().rstrip(".")
    known = list(st.cget("diff_subnets")) + [st.cget("scan_subnet")]
    if not prefix:  # U14: кнопки подсетей
        rows, row = [], []
        for p in dict.fromkeys(known):
            row.append({"text": f"🔍 {p}.x", "callback_data": f"fsub:{p}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        send(chat, "Какую подсеть сканировать? Или задай явно: "
                   "<code>/find 10.20.50</code>",
             markup={"inline_keyboard": rows}, reply_to=reply_to)
        return
    if not net.valid_prefix(prefix):
        send(chat, "Подсеть в формате <code>192.168.0</code>", reply_to=reply_to)
        return
    if not net.prefix_allowed(prefix):  # R28
        send(chat, f"⛔ Подсеть <code>{esc(prefix)}.0/24</code> не входит в "
                   f"разрешённые (ключ <code>find_allow</code>).", reply_to=reply_to)
        return
    if prefix not in known:  # U38: подтверждение чужой подсети
        send(chat, f"⚠️ <code>{esc(prefix)}.0/24</code> — не камерная подсеть "
                   f"из типовых. Точно сканировать?",
             markup={"inline_keyboard": [[
                 {"text": "✅ Да, сканировать", "callback_data": f"fgo:{prefix}"},
                 {"text": "✖️ Отмена", "callback_data": "cancel"}]]},
             reply_to=reply_to)
        return
    start_find(chat, prefix, reply_to)


def start_find(chat, prefix: str, reply_to=None):
    if not FIND_LOCK.acquire(blocking=False):  # R10
        send(chat, "⏳ Уже сканирую — дождись завершения предыдущего /find.",
             reply_to=reply_to)
        return
    try:
        st.note_last("find", prefix)  # U8
        _stop.clear()
        _do_find(chat, prefix, reply_to)
    finally:
        FIND_LOCK.release()


def _stop_kb() -> dict:
    return {"inline_keyboard": [[{"text": "⏹ Стоп", "callback_data": "fstop"}]]}


def _scan_live(chat, prefix: str, mid, eta_s: str) -> List[str]:
    """TCP-скан /24 чанками с прогрессом (U5) и остановкой (U39)."""
    ips = [f"{prefix}.{i}" for i in range(1, 255)]
    live: List[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=st.cget("scan_workers")) as ex:
        for c0 in range(0, len(ips), 64):
            if _stop.is_set():
                break
            chunk = ips[c0:c0 + 64]
            for ip, ok in zip(chunk, ex.map(net.tcp_alive, chunk)):
                if ok:
                    live.append(ip)
            done = min(c0 + 64, len(ips))
            if mid and done < len(ips):
                edit_message(chat, mid,
                             f"🔍 Сканирую <b>{esc(prefix)}.0/24</b> … "
                             f"{done}/254 · найдено {len(live)}{eta_s}",
                             markup=_stop_kb())
    return live


def _do_find(chat, prefix: str, reply_to=None):
    chat_action(chat)
    eta = st.get_eta(prefix)  # U40
    eta_s = f" · ≈{eta:.0f}s по прошлому разу" if eta else ""
    r = send(chat, f"🔍 Сканирую <b>{esc(prefix)}.0/24</b> … 0/254{eta_s}",
             markup=_stop_kb(), reply_to=reply_to, silent=True)  # U32
    mid = ((r or {}).get("result") or {}).get("message_id")
    t0 = time.time()
    live = _scan_live(chat, prefix, mid, eta_s)
    stopped = _stop.is_set()
    arp = net.arp_table()
    hosts = {ip: arp.get(ip, "—") for ip in live}
    __import__("bot_unknownq").note_hosts(hosts, source="find")  # I36, safe
    ips = sorted(hosts, key=lambda x: int(x.split(".")[-1]))
    ports_map = net.probe_many(ips)  # при остановке — по частичному списку
    cams = [ip for ip in ips if (554 in ports_map[ip] or 80 in ports_map[ip])]
    rtsp = [ip for ip in ips if 554 in ports_map[ip]]
    dur = time.time() - t0
    if not stopped:
        st.set_eta(prefix, dur)  # U40
    log(f"find {prefix}: hosts={len(ips)} cams={len(cams)} rtsp={len(rtsp)} "
        f"{'STOPPED ' if stopped else ''}({dur:.1f}s)")
    head = (f"{'⏹ Скан ОСТАНОВЛЕН' if stopped else '📊'} "
            f"<b>{esc(prefix)}.0/24</b>: хостов <b>{len(ips)}</b> · "
            f"камер/веб <b>{len(cams)}</b> · RTSP(554) <b>{len(rtsp)}</b> · "
            f"{dur:.1f}s")
    lines = []
    if not ips:
        lines.append("Ничего не найдено в этой подсети.")
    if ips and not cams:
        lines.append("Камер (порт 80/554) не найдено — только прочие хосты.")
    for ip in cams:
        p = ports_map[ip]
        mark = "📷" if 554 in p else "🌐"
        lbl = inv.label(ip)  # I6
        tag = f" · {esc(lbl)}" if lbl else " · 🆕 <b>НЕТ в инвентаре</b>"
        lines.append(f"{mark} <code>{ip}</code> {esc(hosts[ip])} · "
                     f"п:{','.join(map(str, p))}{tag}")
    # U9: страницы
    psz = int(st.cget("find_page_size"))
    pages = [head + "\n" + "\n".join(lines[i:i + psz])
             for i in range(0, max(len(lines), 1), psz)] or [head]
    with _fs_lock:
        _fs.update({"pages": pages, "page": 0, "chat": chat, "mid": mid,
                    "rest": list(cams), "ports": ports_map, "prefix": prefix})
    if mid:
        edit_message(chat, mid, pages[0], markup=_find_kb(0))
    else:
        send(chat, pages[0], markup=_find_kb(0))
    if cams:  # кнопки действий по первым камерам (как раньше)
        limit = st.cget("onvif_limit")
        rows = [[{"text": f"📸 {ip}", "callback_data": f"shot:{ip}"},
                 {"text": "🩺", "callback_data": f"diag:{ip}"},
                 {"text": "ℹ️", "callback_data": f"info:{ip}"}] for ip in cams[:limit]]
        send(chat, "Действия по найденным камерам:", silent=True,
             markup={"inline_keyboard": rows})
        _onvif_more(chat)  # первые N ONVIF сразу (U27 — остальные по кнопке)


def _find_kb(page: int) -> Optional[dict]:
    rows = []
    n = len(_fs["pages"])
    if n > 1:
        rows.append([{"text": "◀️", "callback_data": f"fpg:{page - 1}"},
                     {"text": f"{page + 1}/{n}", "callback_data": f"fpg:{page}"},
                     {"text": "▶️", "callback_data": f"fpg:{page + 1}"}])
    if _fs["rest"]:
        rows.append([{"text": f"🔬 Опросить ONVIF ещё {min(len(_fs['rest']), st.cget('onvif_limit'))}",
                      "callback_data": "fmore"}])
    return {"inline_keyboard": rows} if rows else None


def _onvif_more(chat):
    """U27: опрос ONVIF очередной пачки найденных камер."""
    limit = st.cget("onvif_limit")
    with _fs_lock:
        batch = _fs["rest"][:limit]
        _fs["rest"] = _fs["rest"][limit:]
        ports_map = dict(_fs["ports"])
        left = len(_fs["rest"])
    if not batch:
        return
    chat_action(chat)
    lines = ["— ONVIF —"]
    for ip in batch:
        if _stop.is_set():
            lines.append("⏹ остановлено")
            break
        st.remember_ip(ip)
        p = ports_map.get(ip) or []
        oport = 80 if 80 in p else (8080 if 8080 in p else 80)
        info = device_info(ip, port=oport, user=st.CAM_USER, pwd=st.CAM_PASS)
        inv.note_onvif(ip, info)  # I48
        if info.get("model"):
            lines.append(f"📷 <code>{ip}</code>: {esc(info['manufacturer'])} "
                         f"{esc(info['model'])} · fw {esc(info['firmware'])} · "
                         f"sn {esc(info['serial'])}")
        else:
            lines.append(f"📷 <code>{ip}</code>: ONVIF не ответил "
                         f"({esc(info.get('error'))})")
    kb = None
    if left:
        kb = {"inline_keyboard": [[
            {"text": f"🔬 Опросить ещё {min(left, limit)} (осталось {left})",
             "callback_data": "fmore"}]]}
    send(chat, "\n".join(lines), markup=kb, silent=True)


def cb_fpg(chat, cq, payload):
    answer_cq(cq.get("id"))
    with _fs_lock:
        n = len(_fs["pages"])
        if not n:
            return
        try:
            page = max(0, min(int(payload), n - 1))
        except ValueError:
            return
        _fs["page"] = page
        txt, mid = _fs["pages"][page], _fs["mid"]
    if mid:
        edit_message(chat, mid, txt, markup=_find_kb(page))


def cb_fmore(chat, cq, payload):
    answer_cq(cq.get("id"), "🔬 Опрашиваю…")
    _stop.clear()
    _onvif_more(chat)


def cb_fstop(chat, cq, payload):
    _stop.set()
    answer_cq(cq.get("id"), "⏹ Останавливаю…")


def cb_fsub(chat, cq, payload):
    answer_cq(cq.get("id"), f"🔍 {payload}.0/24")
    start_find(chat, payload)


def cb_fgo(chat, cq, payload):
    if not net.valid_prefix(payload) or not net.prefix_allowed(payload):
        answer_cq(cq.get("id"), "⛔ Подсеть не разрешена")
        return
    answer_cq(cq.get("id"), "🔍 Сканирую…")
    start_find(chat, payload)


# ---------- U6 + U50: /fav ----------
def cmd_fav(chat, arg="", reply_to=None):
    a = (arg or "").strip()
    if a:
        ip = a if net.valid_ip(a) else inv.resolve_ip(a)
        if not ip:
            send(chat, f"«{esc(a)}» — не IP и не имя из инвентаря.", reply_to=reply_to)
            return
        added = st.toggle_ip("fav_ips", ip)
        send(chat, (f"⭐ Добавил <code>{ip}</code> в избранное."
                    if added else f"☆ Убрал <code>{ip}</code> из избранного."),
             reply_to=reply_to)
        return
    favs = st.get_ips("fav_ips")
    if not favs:
        send(chat, "⭐ Избранного нет. <code>/fav 10.20.50.51</code> или "
                   "<code>/fav AS-7C.01</code> — добавить/убрать.", reply_to=reply_to)
        return
    lines = ["⭐ <b>Избранные камеры</b>:"]
    rows = []
    for ip in favs:
        lbl = inv.label(ip)
        lines.append(f"• <code>{ip}</code>" + (f" {esc(lbl)}" if lbl else ""))
        rows.append([{"text": f"📸 {ip}", "callback_data": f"shot:{ip}"},
                     {"text": "🩺", "callback_data": f"diag:{ip}"},
                     {"text": "ℹ️", "callback_data": f"info:{ip}"}])
    rows.append([{"text": "🌅 Снимки всех избранных", "callback_data": "favshots"}])
    lines.append("Убрать: /fav <code>&lt;ip&gt;</code> повторно · проверить: /health")
    send(chat, "\n".join(lines), markup={"inline_keyboard": rows}, reply_to=reply_to)


def cb_favshots(chat, cq, payload):
    """U50: альбом снимков всех избранных."""
    favs = st.get_ips("fav_ips")
    if not favs:
        answer_cq(cq.get("id"), "Избранного нет")
        return
    answer_cq(cq.get("id"), f"🌅 Снимаю {len(favs)} камер…")
    _send_album(chat, favs)


# ---------- U8: /last ----------
def cmd_last(chat, arg="", reply_to=None):
    acts = st.last_actions()
    if not acts:
        send(chat, "История пуста — она копится с запуска бота.", reply_to=reply_to)
        return
    icons = {"shot": "📸", "diag": "🩺", "info": "ℹ️", "find": "🔍"}
    lines = ["🕘 <b>Последние запросы</b> (кнопка — повтор):"]
    rows = []
    for action, a in acts:
        lines.append(f"• {icons.get(action, '▫️')} {esc(action)} {esc(a)}")
        if action == "find" and net.valid_prefix(a):
            rows.append([{"text": f"🔍 {a}.x", "callback_data": f"fsub:{a}"}])
        elif action in ("shot", "diag", "info") and net.valid_ip(a):
            rows.append([{"text": f"{icons[action]} {action} {a}",
                          "callback_data": f"{action}:{a}"}])
    send(chat, "\n".join(lines),
         markup={"inline_keyboard": rows[:10]} if rows else None, reply_to=reply_to)


# ---------- U29: /map ----------
def cmd_map(chat, arg="", reply_to=None):
    prefix = (arg or "").strip().rstrip(".")
    if not net.valid_prefix(prefix):
        send(chat, "Карта подсети: <code>/map 10.20.50</code>\n"
                   "🟩 отвечает · 🟥 в инвентаре, но молчит · ⬛ пусто",
             reply_to=reply_to)
        return
    if not net.prefix_allowed(prefix):
        send(chat, f"⛔ Подсеть <code>{esc(prefix)}.0/24</code> не в разрешённых.",
             reply_to=reply_to)
        return
    chat_action(chat)
    send(chat, f"🗺 Сканирую <b>{esc(prefix)}.0/24</b> для карты (~5с)…",
         silent=True, reply_to=reply_to)
    live = set(net.scan_subnet(prefix))
    inv_ips = {c["ip"] for c in inv.cams()
               if c.get("ip") and c["ip"].startswith(prefix + ".")}
    cells = []
    for i in range(256):
        ip = f"{prefix}.{i}"
        cells.append("🟩" if ip in live else ("🟥" if ip in inv_ips else "⬛"))
    grid = "\n".join("".join(cells[r * 16:(r + 1) * 16]) for r in range(16))
    dead = sorted(inv_ips - live, key=lambda x: int(x.split(".")[-1]))
    txt = (f"🗺 <b>{esc(prefix)}.0/24</b> (строки по 16: .0-.15, .16-.31 …)\n"
           f"{grid}\n"
           f"🟩 живых {len(live)} · 🟥 инвентарь молчит {len(dead)} · "
           f"в инвентаре {len(inv_ips)}")
    if dead:
        txt += "\n🟥: " + ", ".join(f"<code>{i}</code>" for i in dead[:15])
        if len(dead) > 15:
            txt += f" … и ещё {len(dead) - 15}"
    send(chat, txt)


# ---------- U46: /help с табами ----------
HELP_TABS = {
    "cam": ("📷 Камера",
            "<b>Камера</b> (везде можно IP <i>или имя</i> вида AS-7C.01):\n"
            "/cam — поиск: имя/IP/MAC/серийник, карточка\n"
            "/shot — снимок (несколько IP → альбом) · /lapse — серия кадров\n"
            "/compare <code>ip1 ip2</code> — два снимка рядом\n"
            "/diag — ping+MAC+порты+ONVIF · /info — карточка из кэшей\n"
            "/where — где стоит · /rtsp — RTSP-URL · /verify — сверка\n"
            "/port — порт свитча · /mac — IP по MAC\n"
            "/note <code>ip текст</code> — примечание в xlsx (с бэкапом)\n"
            "/reboot_soft — мягкий ребут ONVIF (с подтверждением)\n"
            "\nМожно просто прислать IP (или несколько) — предложу действия."),
    "park": ("🌐 Парк",
             "<b>Парк</b>:\n"
             "/find [подсеть] — скан: прогресс, страницы, кнопка Стоп\n"
             "/map <code>подсеть</code> — эмодзи-карта 🟩🟥⬛\n"
             "/diff — сеть vs инвентарь · /audit — дубли и пустые поля\n"
             "/fw — модели и прошивки · /free_ip — свободные IP\n"
             "/export [офлайн] — xlsx в чат · /unknown — неизвестные устройства\n"
             "<b>Google</b>: /sync — пуш в таблицу (+колонки online) · /sheet — ссылка"),
    "hp": ("💓 Здоровье",
           "<b>Здоровье парка</b> (фоновая TCP-проба раз в "
           "N мин, алерты владельцу):\n"
           "/report — сводка онлайн/офлайн по подсетям\n"
           "/offline — кто лежит сейчас · /uptime — доступность камеры\n"
           "/top_flaky — топ нестабильных · /health — прогон по избранным\n"
           "/watch <code>ip</code> — надзор (алерт с первого провала)\n"
           "/fav <code>ip|имя</code> — избранные · /last — история запросов"),
    "zn": ("🧭 Зоны",
           "<b>Зоны и обход</b> (Волна D):\n"
           "/zone — зоны: create/add/del/show · /floor <code>7C [эт]</code>\n"
           "/zoneshot /zonediag /zonestat — снимки/сводка/рейтинг зоны\n"
           "/route — порядок обхода · /patrol — обход с отметками и актом\n"
           "/switchcams <code>ip</code> — что висит на свитче · /plan — план этажа\n"
           "/mode field — режим монтажника · /at — «я у камеры»/зона\n"
           "/lost /accept /checklist — мастера проверок (checklists.json)"),
    "doc": ("🗂 Служба",
            "<b>Операционка и документы</b> (Волна D):\n"
            "/issues — проблемы (кнопки «Взял/Починил/Ремонт до…»)\n"
            "/maint <code>зона часы</code> — окно работ + автопротокол\n"
            "/ticket /act /weekly /passport — заявка, акт, отчёт, паспорт\n"
            "/qr /qrsheet /label — QR-наклейки и текст для термопринтера\n"
            "/shift start|end — смена · /away /today — дайджест и план дня\n"
            "/ppr — календарь ППР · /baseline — эталон обзора\n"
            "/timeline /dossier — биография и фото-досье (шли фото с именем)\n"
            "/lifecycle /remind /kit /contact /warranty — статус, напоминания,\n"
            "сборы, контакты, гарантия"),
    "dq": ("🧹 Данные",
           "<b>Качество данных и отчётность</b> (Волна F):\n"
           "/dq — сводный DQ-отчёт (score, тренд, xlsx) · /lint — валидатор\n"
           "/macfix — нормализация MAC (dry-run → кнопка, с бэкапом)\n"
           "/migrate_plan — план миграции колонок (только по кнопке)\n"
           "/names — пустые «Название (по ТЗ)» + заготовки (без записи)\n"
           "/models /floors /coverage — модели · теплокарта · покрытие\n"
           "/cablelog /report_xlsx /smis /pnr — журналы и отчёты файлами\n"
           "/history <code>ip</code> — история камеры · /digest — правки за неделю\n"
           "/backups — ротация бэкапов · /diffxlsx — дифф двух бэкапов\n"
           "/diff3 /sheetdrift — xlsx↔Sheets↔сеть · /nosnap — без снимка\n"
           "/reconcile /orphans /cablecheck — сверки · /enrich — дозаполнение"),
    "an": ("📈 Аналитика",
           "<b>Глубокий мониторинг и аналитика</b> (Волна G):\n"
           "/trend <code>ip</code> — спарклайны ▁▂▃▅▇: аптайм/RTT/кадр/часы\n"
           "/risk — прогноз отказов, топ-10 (+еженедельно сам)\n"
           "/mtbf [ip] — наработка на отказ и MTTR за 30 дн.\n"
           "/sla [ГГГГ-ММ] — месячный SLA xlsx (минус окна /maint)\n"
           "/heat [подсеть] — теплокарта аптайма 🟩🟨🟥 за неделю\n"
           "/season — падения по часам/дням недели\n"
           "/matrix <code>ip</code> — слои: ping/80/554/ONVIF/снимок/RTSP\n"
           "/imgqa [ip] — залипший/чёрный кадр, тренд размера, vs эталон\n"
           "/clock_report /clock <code>ip</code> — дрейф часов, NTP, «прыжки»\n"
           "/rtsp_check <code>ip</code> — DESCRIBE+SDP vs эталон · "
           "/bitrate — замер потока\n"
           "/secaudit — DHCP/hostname/gateway/юзеры/порты/энкодеры/прошивки\n"
           "/nvr /rec — NVR (нужен nvr_list в конфиге)\n"
           "Фон: ротации малыми порциями, метрики в _metrics.db (90 дн.).\n"
           "PoE/порты свитчей → таб «🔌 Сеть»."),
    "sw": ("🔌 Сеть/свитчи",
           "<b>Сеть, коммутаторы, топология</b> (Волна H):\n"
           "/topo [ip|имя] — дерево свитчей / цепочка камеры до ядра\n"
           "/topo_map — SVG-схема · /topo unknown — неизвестные LLDP-соседи\n"
           "/sw <code>ip</code> — карточка свитча · /sw_list — реестр\n"
           "/poe — PoE по портам и бюджет · /sw_env — CPU/темп./память\n"
           "/flap — ошибки/флапы портов · /port_history — история порта\n"
           "/swtraffic — загрузка портов и аплинков · /gw — шлюзы\n"
           "/sw_audit [ports|sec|vlan|trunk|fw|time|svc|contact] — сверки\n"
           "/stp — STP/шторм · /duplex — 10М/half · /ipplan /dupip — IP-план\n"
           "/vlan_report — VLAN-сводка · /patchpanel — кроссировка xlsx\n"
           "/swbackup [diff] — бэкапы конфигов и дрейф\n"
           "/facts_refresh — пересборка фактов · /facts_diff — что изменилось\n"
           "/netcheck — сеть этого ПК (DAD/маршруты) + кнопка «Починить»\n"
           "/trmatrix — трассировка до подсетей\n"
           "⚠️ Запись (/cable /sw_ntp /sw_contact /portdesc) — только через\n"
           "двухшаговое подтверждение; save на свитчах не выполняется.\n"
           "Фон: ротация 2 свитчей/10 мин, суточный бэкап конфигов."),
    "svc": ("🛠 Сервис",
            "<b>Сервис</b>:\n"
            "/status — состояние бота · /ping — задержка Telegram\n"
            "/log [N] — хвост лога · /restart — перезапуск бота\n"
            "\n🔒 Пароли камер бот никогда не меняет."),
    "dbg": ("🔬 Диагностика бота",
            "<b>Диагностика бота</b> (Волна E):\n"
            "/debug — zip-архив: логи, ring-buffer, конфиг, сеть, крэши\n"
            "/slo [часов] — отчёт по командам: count, p50/p95, ошибки\n"
            "/env — сеть машины: адаптеры, маршруты, фронты Telegram, DNS\n"
            "/trace <code>ip</code> — tracert до камеры (где рвётся маршрут)\n"
            "/mem on|off — tracemalloc, /mem — топ роста аллокаций\n"
            "/crashes — крэш-репорты · файл <code>dump_now.flag</code> — "
            "дамп стеков без RDP\n"
            "/version — версия кода (git) · /changelog — что меняли\n"
            "/upgrade — git pull + py_compile + перезапуск (грязный репо = отказ)\n"
            "/profile [имя] — профиль таймаутов канала (normal/harsh/fast)\n"
            "/metrics_push — metrics.csv в Google-таблицу\n"
            "NDJSON-лог: <code>camera_bot.jsonl</code> · метрики: "
            "<code>metrics.csv</code> · аудит: <code>audit.log</code>"),
}


def _help_kb(active: str) -> dict:
    btns = [{"text": ("• " if k == active else "") + name,
             "callback_data": f"help:{k}"} for k, (name, _t) in HELP_TABS.items()]
    return {"inline_keyboard": [btns[i:i + 3] for i in range(0, len(btns), 3)]}


def cmd_help(chat, arg="", reply_to=None):
    """U46: /help с inline-табами."""
    name, txt = HELP_TABS["cam"]
    send(chat, f"<b>Бот камер МФК «Зарядье»</b> · {name}\n\n{txt}",
         markup=_help_kb("cam"), reply_to=reply_to)


def cb_help(chat, cq, tab):
    answer_cq(cq.get("id"))
    if tab not in HELP_TABS:
        tab = "cam"
    name, txt = HELP_TABS[tab]
    mid = (cq.get("message") or {}).get("message_id")
    if mid:
        edit_message(chat, mid,
                     f"<b>Бот камер МФК «Зарядье»</b> · {name}\n\n{txt}",
                     markup=_help_kb(tab))


HANDLERS = {
    "/find": cmd_find, "/help": cmd_help, "/fav": cmd_fav, "/last": cmd_last,
    "/map": cmd_map,
}
ALIASES = {
    "/поиск": "/find", "/помощь": "/help", "/избранное": "/fav",
    "/история": "/last", "/карта": "/map",
}
CALLBACKS = {
    "fpg": cb_fpg, "fmore": cb_fmore, "fstop": cb_fstop, "fsub": cb_fsub,
    "fgo": cb_fgo, "favshots": cb_favshots, "help": cb_help,
}
