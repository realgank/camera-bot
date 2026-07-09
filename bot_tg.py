# -*- coding: utf-8 -*-
"""Telegram API: requests.Session, tg() с ретраями/экспоненциальным бэкоффом/
классами ошибок, троттлинг отправок под локом, send/send_photo/send_document/
send_chunks, sendChatAction, answerCallbackQuery, setMyCommands.
Канал в РФ дёрганый (DPI) — отсюда ретраи и keep-alive.
Волна A: R4-R6, R11, R31-R36, U1, U26 (тосты), U33 (reply), U41 (документ).
Волна E: 215 (адаптивные таймауты по состоянию канала), 233 (инжект ошибок
при debug=true), 239 (гистограмма ретраев), 250 (Date-заголовок -> дрейф),
reset_session() для 220/221/246.
"""
import os
import sys
import json
import time
import random
import logging
import threading

import requests

import bot_state as st
import bot_obs as obs
from bot_util import log

API = f"https://api.telegram.org/bot{st.TOKEN}"

SESSION = requests.Session()  # R31: keep-alive — меньше TLS-хендшейков под DPI


def reset_session():
    """Волна E (220/221/246): пересоздать Session — сбросить мёртвые keep-alive
    после смены сети (VPN/DHCP) или залипшего long-poll."""
    global SESSION
    old = SESSION
    SESSION = requests.Session()
    try:
        old.close()
    except Exception:
        pass
    log("tg: Session пересоздана (смена сети / залип канала)", logging.WARNING)


def _fault_inject():
    """233: при debug=true с вероятностью debug_fault_pct% симулируем сбой сети —
    тестирование ретрай-логики без реального шторма DPI."""
    if not st.cget("debug"):
        return False
    try:
        pct = float(st.cget("debug_fault_pct") or 0)
    except (TypeError, ValueError):
        return False
    return pct > 0 and random.random() * 100 < pct

_send_lock = threading.Lock()  # R36
_last_send = [0.0]

NO_RETRY_CODES = {400, 401, 403, 404}  # R33: ошибки запроса — ретраить бессмысленно


def _throttle():
    """Пауза между исходящими сообщениями, чтобы не ловить 429 (под локом, R36)."""
    interval = st.cget("send_min_interval")
    with _send_lock:
        dt = time.time() - _last_send[0]
        if dt < interval:
            time.sleep(interval - dt)
        _last_send[0] = time.time()


def _backoff(attempt):
    """R5: экспоненциальный бэкофф с джиттером, потолок 30с."""
    base = min(30.0, 1.5 * (2 ** attempt))
    time.sleep(base * (0.7 + 0.6 * random.random()))


def tg(method: str, params: dict = None, files: dict = None,
       retries: int = None, timeout: tuple = None):
    """Вызов Bot API. R33 — классы ошибок:
    сеть/5xx/не-JSON (заглушка DPI) — ретраим с бэкоффом; 400/403 — не ретраим;
    429 — ждём retry_after; 409 — CRITICAL и выход (вторая копия поллинга, R6)."""
    retries = retries or st.cget("tg_retries")
    if timeout is None:  # 215: адаптация дефолтных таймаутов под состояние канала
        f = obs.timeout_factor()
        timeout = (st.cget("tg_connect_timeout_s") * f,
                   st.cget("tg_read_timeout_s") * f)
    if method in ("sendMessage", "sendPhoto", "sendDocument",
                  "sendMediaGroup", "sendVideo"):
        _throttle()
    for i in range(retries):
        try:
            if _fault_inject():  # 233
                raise requests.ConnectionError("debug_fault_inject")
            r = SESSION.post(f"{API}/{method}", data=params, files=files, timeout=timeout)
        except requests.RequestException as e:
            st.note_retry()
            log(f"tg {method} попытка {i + 1}/{retries}: {type(e).__name__}",
                logging.WARNING)  # R4
            if i < retries - 1:
                _backoff(i)
            continue
        obs.note_server_date((getattr(r, "headers", None) or {}).get("Date"))  # 250
        try:
            j = r.json()
        except ValueError:
            st.note_retry()
            obs.note_nonjson()  # 214: доля заглушек DPI — сигнал качества канала
            log(f"tg {method} попытка {i + 1}/{retries}: не-JSON ответ "
                f"(заглушка DPI?), HTTP {r.status_code}", logging.WARNING)
            if i < retries - 1:
                _backoff(i)
            continue
        if j.get("ok"):
            obs.note_tg(i + 1, True)  # 239
            return j
        code = j.get("error_code") or r.status_code
        desc = j.get("description", "")
        if code == 409:  # R6: конфликт getUpdates — где-то живёт вторая копия
            log(f"tg {method} 409 Conflict — вторая копия бота?! {desc}", logging.CRITICAL)
            try:  # 230: маркер причины выхода
                import bot_release
                bot_release.mark_exit("conflict409", 1)
            except Exception:
                pass
            if threading.current_thread() is threading.main_thread():
                sys.exit(1)
            os._exit(1)
        if code == 429:
            ra = (j.get("parameters") or {}).get("retry_after", 2)
            st.note_429()
            log(f"tg {method} 429 → ждать {ra}s", logging.WARNING)
            time.sleep(ra + 0.3)
            with _send_lock:
                _last_send[0] = time.time()
            continue
        if code in NO_RETRY_CODES:  # R33: ошибка в самом запросе
            log(f"tg {method} {code} (не ретраим): {desc}", logging.WARNING)
            return j
        st.note_retry()
        log(f"tg {method} попытка {i + 1}/{retries}: {code} {desc}", logging.WARNING)
        if i < retries - 1:
            _backoff(i)
    st.note_error()
    obs.note_tg(retries, False)  # 239
    log(f"tg {method}: все {retries} попыток исчерпаны", logging.ERROR)
    return None


def send(chat: int, text: str, markup: dict = None,
         reply_to: int = None, silent: bool = False):
    """Отправка HTML-сообщения. R34: при «can't parse entities» повторяем
    без parse_mode. U33: reply_to привязывает ответ к сообщению-команде."""
    p = {"chat_id": chat, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if markup is not None:
        p["reply_markup"] = json.dumps(markup)
    if reply_to:
        p["reply_parameters"] = json.dumps(
            {"message_id": reply_to, "allow_sending_without_reply": True})
    if silent:
        p["disable_notification"] = True
    j = tg("sendMessage", p)
    if j and not j.get("ok") and "can't parse entities" in (j.get("description") or ""):
        log("send: HTML не распарсился — повтор без parse_mode", logging.WARNING)
        p.pop("parse_mode", None)
        j = tg("sendMessage", p)
    return j


def send_photo(chat, data, caption="", markup=None, reply_to=None):
    p = {"chat_id": chat, "caption": caption}
    if markup is not None:
        p["reply_markup"] = json.dumps(markup)
    if reply_to:
        p["reply_parameters"] = json.dumps(
            {"message_id": reply_to, "allow_sending_without_reply": True})
    return tg("sendPhoto", p, files={"photo": ("snap.jpg", data, "image/jpeg")})


_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
         "txt": "text/plain",
         "xlsx": "application/vnd.openxmlformats-officedocument"
                 ".spreadsheetml.sheet"}


def send_document(chat, data, filename, caption=""):
    """U41: снимок документом — без пересжатия Telegram (mime по расширению)."""
    mime = _MIME.get(filename.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    return tg("sendDocument", {"chat_id": chat, "caption": caption},
              files={"document": (filename, data, mime)})


def send_video(chat, data, filename, caption=""):
    """Волна J (U16): mp4-клип с камеры (RTSP через ffmpeg)."""
    return tg("sendVideo",
              {"chat_id": chat, "caption": caption, "supports_streaming": True},
              files={"video": (filename, data, "video/mp4")})


def get_file(file_id):
    """Волна D (193/194): скачать файл из Telegram (фото/голос в досье)."""
    r = tg("getFile", {"file_id": file_id}, retries=3, timeout=(10, 30))
    if not r or not r.get("ok"):
        return None
    fp = (r.get("result") or {}).get("file_path")
    if not fp:
        return None
    for _i in range(2):
        try:
            resp = SESSION.get(
                f"https://api.telegram.org/file/bot{st.TOKEN}/{fp}",
                timeout=(10, 60))
            if resp.status_code == 200:
                return resp.content
        except requests.RequestException as e:
            log(f"get_file: {type(e).__name__}", logging.WARNING)
    return None


def edit_message(chat, message_id, text, markup=None):
    """U5: живой прогресс — правим одно сообщение вместо залпа новых."""
    p = {"chat_id": chat, "message_id": message_id, "text": text,
         "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup is not None:
        p["reply_markup"] = json.dumps(markup)
    j = tg("editMessageText", p, retries=2, timeout=(5, 15))
    if j and not j.get("ok") and "can't parse entities" in (j.get("description") or ""):
        p.pop("parse_mode", None)
        j = tg("editMessageText", p, retries=1, timeout=(5, 15))
    return j


def send_media_group(chat, photos, reply_to=None):
    """U10: альбом снимков. photos — список (bytes, caption), максимум 10."""
    media, files = [], {}
    for i, (data, cap) in enumerate(photos[:10]):
        key = f"p{i}"
        m = {"type": "photo", "media": f"attach://{key}"}
        if cap:
            m["caption"] = cap[:1000]
        media.append(m)
        files[key] = (f"{key}.jpg", data, "image/jpeg")
    if not media:
        return None
    p = {"chat_id": chat, "media": json.dumps(media)}
    if reply_to:
        p["reply_parameters"] = json.dumps(
            {"message_id": reply_to, "allow_sending_without_reply": True})
    return tg("sendMediaGroup", p, files=files)


def send_chunks(chat: int, lines: list, limit: int = 3800):
    """Отправляет список строк, не превышая лимит Telegram (4096)."""
    buf = ""
    for ln in lines:
        if buf and len(buf) + len(ln) + 1 > limit:
            send(chat, buf)
            buf = ""
        buf += (("\n" + ln) if buf else ln)
    if buf:
        send(chat, buf)


def chat_action(chat, action="typing"):
    """R11/U4: индикатор «печатает…»/«отправляет фото…» при долгих операциях."""
    tg("sendChatAction", {"chat_id": chat, "action": action},
       retries=1, timeout=(5, 10))


def answer_cq(cq_id, text=None):
    """Ответ на callback; с text — тост в шторке (U26)."""
    p = {"callback_query_id": cq_id}
    if text:
        p["text"] = text
    tg("answerCallbackQuery", p, retries=2, timeout=(5, 10))


BOT_COMMANDS = [
    ("cam", "поиск камеры: имя/IP/MAC/серийник"),
    ("shot", "снимок с камеры (IP или имя)"),
    ("diag", "диагностика: ping+MAC+порты+ONVIF"),
    ("info", "карточка из инвентаря и кэшей"),
    ("where", "где физически стоит камера"),
    ("rtsp", "RTSP-URL потока + веб-интерфейс"),
    ("find", "поиск камер в подсети"),
    ("port", "порт свитча по MAC/IP"),
    ("mac", "обратный поиск IP по MAC"),
    ("note", "примечание в инвентарь (с бэкапом)"),
    ("verify", "сверка ONVIF-факта с инвентарём"),
    ("diff", "сеть vs инвентарь: новые/молчат/MAC"),
    ("audit", "дубли IP/MAC, пустые поля"),
    ("fw", "сводка по моделям и прошивкам"),
    ("free_ip", "свободные IP в подсети"),
    ("export", "инвентарь xlsx документом"),
    ("sync", "пуш xlsx в Google-таблицу"),
    ("sheet", "ссылка на Google-таблицу"),
    ("report", "сводка парка: онлайн по подсетям"),
    ("offline", "кто сейчас офлайн"),
    ("uptime", "доступность камеры, падения"),
    ("top_flaky", "топ-10 нестабильных камер"),
    ("health", "прогон по избранным сейчас"),
    ("watch", "надзор за камерой (алерты)"),
    ("fav", "избранные камеры"),
    ("last", "история запросов с повтором"),
    ("map", "эмодзи-карта подсети"),
    ("lapse", "серия снимков альбомом"),
    ("compare", "два снимка рядом"),
    ("unknown", "лист «Неизвестные устройства»"),
    ("reboot_soft", "мягкий ребут камеры (ONVIF)"),
    # Волна D
    ("zone", "именованные зоны камер"),
    ("floor", "камеры корпуса/этажа"),
    ("route", "порядок обхода зоны"),
    ("zoneshot", "снимки всей зоны альбомами"),
    ("zonediag", "сводная таблица по зоне"),
    ("zonestat", "рейтинг зон по падениям"),
    ("switchcams", "камеры на коммутаторе"),
    ("maint", "окно плановых работ"),
    ("issues", "открытые проблемы, статусы"),
    ("ticket", "заявка на ремонт по шаблону"),
    ("act", "акт о неисправности"),
    ("weekly", "недельный отчёт для начальства"),
    ("passport", "паспорт камеры + QR"),
    ("qr", "QR-код камеры (deep-link)"),
    ("label", "текст наклейки для принтера"),
    ("mode", "режим: field / normal"),
    ("at", "контекст: я у камеры / зона"),
    ("lost", "чек-лист «камера пропала»"),
    ("accept", "приёмка после монтажа"),
    ("patrol", "обход зоны с отметками"),
    ("shift", "журнал смены start|end"),
    ("away", "дайджест за N дней отпуска"),
    ("today", "план работ на день"),
    ("ppr", "календарь ППР"),
    ("baseline", "эталонный снимок обзора"),
    ("timeline", "лента событий камеры"),
    ("dossier", "фото-досье монтажа"),
    ("lifecycle", "статус: active/repair/demontage"),
    ("remind", "напоминание к камере"),
    ("kit", "что взять с собой на выезд"),
    ("contact", "ответственные по зонам"),
    ("warranty", "гарантийный учёт"),
    ("status", "состояние бота"),
    ("ping", "живость и задержка Telegram"),
    ("log", "хвост лога бота"),
    ("restart", "перезапустить бота"),
    # Волна E: диагностика бота
    ("debug", "zip-архив диагностики бота"),
    ("slo", "суточный отчёт по командам"),
    ("env", "сеть машины: адаптеры, маршруты"),
    ("trace", "tracert до камеры"),
    ("mem", "трассировка памяти tracemalloc"),
    ("crashes", "крэш-репорты бота"),
    ("version", "версия кода (git)"),
    ("changelog", "последние изменения бота"),
    ("upgrade", "git pull + проверка + рестарт"),
    ("profile", "профиль таймаутов канала"),
    ("metrics_push", "metrics.csv в Google-таблицу"),
    # Волна F: качество данных и отчётность
    ("dq", "сводный отчёт качества данных"),
    ("lint", "валидатор xlsx по схеме"),
    ("macfix", "нормализация MAC (dry-run)"),
    ("models", "срез по моделям и прошивкам"),
    ("floors", "теплокарта этажей"),
    ("history", "история камеры (журнал правок)"),
    ("backups", "бэкапы xlsx: ротация"),
    ("diffxlsx", "дифф двух бэкапов"),
    ("reconcile", "сверки инвентаря с фактами"),
    ("enrich", "очередь дообогащения"),
    ("smis", "экспорт CSV/JSON для СМИС"),
    ("cablelog", "кабельный журнал xlsx"),
    # Волна G: глубокий мониторинг и аналитика (301-350)
    ("trend", "спарклайны камеры: RTT/аптайм/кадр"),
    ("risk", "прогноз отказов: топ-10 риска"),
    ("sla", "месячный SLA-отчёт xlsx"),
    ("heat", "теплокарта стабильности недели"),
    ("clock_report", "часы камер: дрейф и NTP"),
    ("imgqa", "качество кадра: залипание/чернота"),
    ("rtsp_check", "RTSP DESCRIBE + SDP vs эталон"),
    ("secaudit", "аудит безопасности парка"),
    ("nvr", "мониторинг NVR (если настроен)"),
    ("help", "помощь"),
]


def set_my_commands():
    """U1: меню «/» с описаниями команд (безопасный одноразовый вызов при старте)."""
    cmds = [{"command": c, "description": d} for c, d in BOT_COMMANDS]
    r = tg("setMyCommands", {"commands": json.dumps(cmds)}, retries=2)
    log("setMyCommands: " + ("OK" if r and r.get("ok") else "не удалось (не критично)"))


def tg_ping():
    """Задержка до Telegram (мс) или None."""
    t0 = time.time()
    r = tg("getMe", {}, retries=1, timeout=(5, 12))
    if r and r.get("ok"):
        return (time.time() - t0) * 1000
    return None
