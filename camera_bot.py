# -*- coding: utf-8 -*-
"""
Telegram-бот для диагностики и поиска IP-камер парка МФК «Зарядье».
Точка входа: main-цикл getUpdates + маршрутизация сообщений.
Модули: bot_tg (API/ретраи), bot_net (сеть), bot_handlers (команды/колбэки),
bot_state (конфиг/состояние), bot_util (лог/утилиты).

Команды: /find /shot /diag /info /status /ping /log /restart /help
Волна B: /cam /where /port /mac /rtsp /note /verify /audit /fw /export
/free_ip /diff /sync /sheet (bot_inventory, bot_sheets, bot_handlers_inv).
Волна C: фоновый health-check с алертами (bot_health), /report /offline
/uptime /top_flaky /health /watch /reboot_soft /unknown (bot_handlers_health),
/fav /last /map /lapse /compare + новый /find (bot_handlers_ux).
Волна D: зоны/этажи/обходы (bot_zones), журнал проблем и snooze (bot_issues),
окна работ/тихие часы/эскалация (bot_ops), тикеты/акты/отчёты (bot_docs),
QR/наклейки (bot_qr), полевой режим (bot_field), чек-листы/patrol (bot_check),
смены/ППР (bot_shift), досье/эталоны (bot_dossier), lifecycle/напоминания/
справочники (bot_lifecycle); фото/голос — в досье, свободный текст — hook.
Волна E: наблюдаемость (bot_obs: NDJSON/trace/ring/метрики канала/metrics.csv),
качество канала и сеть машины (bot_chan), форензика (bot_debug: excepthook'и,
faulthandler, /debug /mem /trace /env /crashes), релизы (bot_release:
/version /changelog /upgrade /slo /profile, маркер выхода, flapping).
Бот НИКОГДА не меняет пароли камер.
Канал Telegram в РФ-сети дёрганый (DPI) — все запросы с ретраями.
TOFU: первый написавший становится владельцем (owner_chat_id); при заданном
bind_secret привязка только по /start <секрет>.

Волна A: R7-R9, R12-R18, R22-R25, R32, R37.
"""
import os
import sys
import json
import time
import signal
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot_util import setup_logging, log, log_exc, BOT_VERSION
import bot_state as st
import bot_net as net
import bot_obs as obs
import bot_tg as tgm
import bot_handlers as h
import bot_debug
import bot_release

STOP = threading.Event()
LAST_ALIVE = [time.time()]   # для watchdog (R18)
_LOCK_FH = [None]            # держим лок-файл открытым весь срок жизни


def acquire_instance_lock():
    """R37: вторая копия бота не стартует (msvcrt-лок на camera_bot.lock)."""
    import msvcrt
    fh = open(st.LOCK_PATH, "a+")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        log("Вторая копия бота уже работает (camera_bot.lock занят) — выходим",
            logging.CRITICAL)
        sys.exit(1)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _LOCK_FH[0] = fh


def self_test():
    """R15: self-test при старте + fail-fast (run_bot.cmd перезапустит)."""
    log("self-test: запись в лог OK")
    try:
        import onvif_snap  # noqa: F401
    except Exception:
        log_exc("self-test: import onvif_snap FAIL")
        sys.exit(1)
    r = tgm.tg("getMe", {}, retries=3, timeout=(5, 15))
    if not r or not r.get("ok"):
        log("self-test: getMe не ответил (токен/сеть?) — выходим", logging.CRITICAL)
        try:  # Волна E (230)
            bot_release.mark_exit("selftest_fail", 1)
        except Exception:
            pass
        sys.exit(1)
    log(f"self-test OK: бот @{(r['result'] or {}).get('username')}")


def watchdog():
    """R17: heartbeat-файл раз в 60с; R18: цикл молчит N минут → os._exit(1)."""
    hb_int = st.cget("heartbeat_s")
    wd_min = st.cget("watchdog_min")
    last_hb = 0.0
    while not STOP.is_set():
        now = time.time()
        if now - last_hb >= hb_int:
            try:
                with open(st.HEARTBEAT_PATH, "w") as f:
                    f.write(str(int(now)))
            except Exception:
                pass
            last_hb = now
        if now - LAST_ALIVE[0] > wd_min * 60:
            log(f"WATCHDOG: главный цикл молчит > {wd_min} мин — аварийный выход",
                logging.CRITICAL)
            try:  # Волна E: 230 маркер выхода + 207 «чёрный ящик»
                bot_release.mark_exit("watchdog", 1)
                obs.ring_dump(reason="watchdog")
            except Exception:
                pass
            os._exit(1)
        STOP.wait(5)


def _sig_handler(signum, frame):
    """R13: graceful shutdown по SIGTERM/SIGBREAK/Ctrl+C."""
    log(f"Сигнал {signum} — останавливаюсь", logging.WARNING)
    try:  # Волна E (230): причина выхода
        bot_release.mark_exit(f"signal_{signum}", 0)
    except Exception:
        pass
    STOP.set()


def _who(msg):
    u = msg.get("from", {})
    name = (u.get("first_name", "") + " " + u.get("last_name", "")).strip()
    uname = ("@" + u["username"]) if u.get("username") else ""
    return f"{u.get('id', '?')} {uname} ({name})".strip()


def handle_message(upd):
    """Маршрутизация входящего сообщения: TOFU, кнопки, команды, голый IP."""
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    if "text" not in msg:
        if msg.get("photo") or msg.get("voice"):  # Волна D (193/194): досье
            try:
                h.on_media(msg)
            except Exception:
                log_exc("on_media")
        return
    chat = msg["chat"]["id"]
    text = msg["text"].strip()
    mid = msg.get("message_id")
    uid = (msg.get("from") or {}).get("id")
    who = _who(msg)
    log(f"REQ chat={chat} from={who} text={text!r}")

    owner = st.cget("owner_chat_id")
    if owner is None:
        # R22: если задан bind_secret — привязка только по /start <секрет>
        secret = st.cget("bind_secret")
        if secret:
            parts = text.split(maxsplit=1)
            ok = (parts and parts[0].split("@")[0].lower() == "/start"
                  and len(parts) > 1 and parts[1].strip() == str(secret))
            if not ok:
                log(f"DENY bind chat={chat} from={who} — нужен /start <секрет>",
                    logging.WARNING)
                tgm.send(chat, "Бот не привязан. Отправь "
                               "<code>/start &lt;секрет&gt;</code>.")
                return
        st.bind_owner(chat, uid)  # TOFU (+R23)
        tgm.send(chat, "✅ Бот привязан к этому чату. Пользуйся кнопками ниже "
                       "или /help.", markup=h.MAIN_KB)
        h.cmd_start(chat, "")
        return
    if chat != owner:
        log(f"DENY chat={chat} from={who} (не владелец)", logging.WARNING)
        tgm.send(chat, "Доступ только у владельца бота.")
        return
    # R23: сверяем и from.id, не только chat_id
    ouid = st.cget("owner_user_id")
    if ouid is None and uid is not None:
        st.set_owner_user(uid)  # дозаполняем для старых конфигов
    elif ouid is not None and uid is not None and uid != ouid:
        log(f"DENY from.id={uid} != owner_user_id={ouid} chat={chat}", logging.WARNING)
        return
    # R24: rate-limit владельца
    limited, warn = st.rate_limited()
    if limited:
        if warn:
            tgm.send(chat, "⏳ Слишком много команд подряд — притормози на пару секунд.")
        log(f"RATE-LIMIT chat={chat} text={text!r}", logging.WARNING)
        return

    # 1) кнопки постоянной клавиатуры
    if text in h.BTN_ASK:
        h.ask_ip(chat, h.BTN_ASK[text])
        return
    if text in h.BTN_CMD:
        text = h.BTN_CMD[text]  # кнопка → команда

    # 2) команды (/...)
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        h.dispatch(chat, cmd, arg, reply_to=mid)
        return

    # 3) обычный текст — ожидался IP?
    pend = st.pop_pending(chat)
    ips = net.find_ips(text)
    if len(ips) >= 2 and not pend:  # U24: мульти-IP -> «снимки/диаг всех»
        h.multi_ip_offer(chat, ips, reply_to=mid)
        return
    if net.valid_ip(text):
        if pend:
            h.run_action(chat, pend, text)
        else:
            # I3: голый IP -> карточка из инвентаря (имя, локация, свитч/порт)
            tgm.send(chat, h.ip_card_text(text),
                     markup=h.actions_kb(text), reply_to=mid)
        return
    if pend:
        st.set_pending(chat, pend)  # вернём ожидание — пусть пришлёт корректный IP
        tgm.send(chat, "Это не похоже на IP. Пришли адрес вида "
                       "<code>10.20.52.100</code>.", markup=h.CANCEL_KB)
        return
    # Волна D (176-178, 182): кнопки полевого режима, номера в зоне,
    # замечания к шагу обхода
    try:
        if h.free_text(chat, text, reply_to=mid):
            return
    except Exception:
        log_exc("free_text")
    log(f"UNK текст {text!r} chat={chat}")
    tgm.send(chat, "Не понял. Нажми кнопку ниже или /help",
             markup=h.MAIN_KB, reply_to=mid)


def process_update(upd):
    obs.set_trace(upd.get("update_id"))  # Волна E (202): trace-id всей цепочки
    try:
        if "callback_query" in upd:
            h.on_callback(upd["callback_query"])
        elif "inline_query" in upd:  # Волна J (U44), только владелец
            h.on_inline(upd["inline_query"])
        else:
            handle_message(upd)
    except Exception as e:
        st.note_error()
        log_exc(f"update error (id={upd.get('update_id')})")
        try:  # Волна E (245): крэш-репорт с исходным апдейтом
            bot_debug.crash_report({"where": "process_update", "update": upd}, e)
        except Exception:
            pass


_HUNG = []                    # R12+: брошенные по таймауту потоки (живые daemon)
_HUNG_LOCK = threading.Lock()


def hung_count():
    """Число ещё живых «брошенных» потоков; умершие вычищаются из списка."""
    with _HUNG_LOCK:
        _HUNG[:] = [t for t in _HUNG if t.is_alive()]
        return len(_HUNG)


def _upd_chat(upd):
    """chat_id из апдейта — чтобы ответить отправителю при перегрузке."""
    m = upd.get("message") or upd.get("edited_message") or {}
    cbm = (upd.get("callback_query") or {}).get("message") or {}
    return (m.get("chat") or cbm.get("chat") or {}).get("id")


def run_update(upd):
    """R12: таймаут на обработку — зависшая команда не копится молча.
    Убить поток нельзя (daemon живёт и держит сокеты) — поэтому лимит:
    при избытке живых брошенных потоков новые команды отклоняем."""
    limit = int(st.cget("hung_threads_max") or 20)
    if hung_count() >= limit:
        log(f"HUNG-LIMIT: живых брошенных потоков >= {limit} — апдейт "
            f"{upd.get('update_id')} отклонён", logging.WARNING)
        chat = _upd_chat(upd)
        if chat:
            tgm.send(chat, "⏳ Слишком много зависших операций, подожди.")
        return
    t = threading.Thread(target=process_update, args=(upd,), daemon=True)
    t.start()
    t.join(st.cget("cmd_timeout_s"))
    if t.is_alive():
        with _HUNG_LOCK:
            _HUNG.append(t)
        st.note_error()
        log(f"TIMEOUT: апдейт {upd.get('update_id')} висит "
            f"> {st.cget('cmd_timeout_s')}s — бросаю "
            f"(зависших: {hung_count()}/{limit})", logging.ERROR)
        owner = st.cget("owner_chat_id")
        if owner:
            tgm.send(owner, "⏱ Команда выполняется слишком долго и была брошена "
                            "(подробности в /log).")


def main():
    setup_logging()
    obs.register()                     # Волна E: 201/202/207 + минутный тик
    bot_debug.install_hooks()          # Волна E: 205/206
    bot_release.install_exit_marker()  # Волна E: 230
    acquire_instance_lock()  # R37
    for sname in ("SIGINT", "SIGBREAK", "SIGTERM"):  # R13
        if hasattr(signal, sname):
            try:
                signal.signal(getattr(signal, sname), _sig_handler)
            except Exception:
                pass
    bot_release.startup_diagnostics()  # Волна E: 225/242/248
    self_test()  # R15
    tgm.set_my_commands()  # U1
    threading.Thread(target=watchdog, daemon=True).start()  # R17 + R18
    if st.cget("health_enabled"):  # Волна C (I14): первый прогон через ~2 мин
        import bot_health
        threading.Thread(target=bot_health.run_loop, args=(STOP,),
                         daemon=True, name="health").start()
    try:  # Волна I (442): SA видит таблицу и папку снимков (фоном, алерт)
        import bot_gdrive2
        bot_gdrive2.startup_check()
    except Exception:
        log_exc("startup_check (Google) не запустился")

    owner = st.cget("owner_chat_id")
    if owner:  # R16/U34 + Волна E (229/234): почему был рестарт, flapping
        reason = ""
        try:
            reason = bot_release.restart_report()
        except Exception:
            log_exc("restart_report")
        tgm.send(owner, f"♻️ Бот перезапущен · v{BOT_VERSION} · PID {os.getpid()}"
                        + (f"\n{reason}" if reason else ""),
                 silent=True)

    offset = st.load_offset()  # R14: не терять апдейты даунтайма
    if offset is None:
        # первый запуск без состояния — сбрасываем накопленный бэклог, как раньше
        r = tgm.tg("getUpdates", {"timeout": 0}, retries=2)
        if r and r.get("ok") and r["result"]:
            offset = r["result"][-1]["update_id"] + 1

    pool = ThreadPoolExecutor(max_workers=st.cget("workers"),
                              thread_name_prefix="cmd")  # R9/U49
    # R32 + Волна J (U44): inline_query приходят только если включён /setinline
    allowed = json.dumps(["message", "callback_query", "inline_query"])
    fails, delay, loop_errs = 0, 2, 0
    log(f"bot start v{BOT_VERSION} pid={os.getpid()} offset={offset}")

    while not STOP.is_set():
        try:  # исключение итерации (obs.* и пр.) не должно убить цикл молча
            LAST_ALIVE[0] = time.time()
            pt = obs.poll_timeout()  # Волна E (215): короче в DEGRADED/BAD
            t0p = time.time()
            r = tgm.tg("getUpdates",
                       {"timeout": pt, "offset": offset, "allowed_updates": allowed},
                       retries=1, timeout=(10, pt + 30))
            ok_poll = bool(r and r.get("ok"))
            obs.note_poll(time.time() - t0p, ok_poll,   # Волна E (213/246)
                          empty=not (ok_poll and r["result"]), timeout_s=pt)
            LAST_ALIVE[0] = time.time()
            if STOP.is_set():
                break
            if not r or not r.get("ok"):
                fails += 1
                if fails >= st.cget("max_poll_fails"):  # R8
                    log(f"getUpdates: {fails} неудач подряд — выходим, "
                        f"run_bot.cmd перезапустит", logging.CRITICAL)
                    try:  # Волна E (230)
                        bot_release.mark_exit("poll_fails", 1)
                    except Exception:
                        pass
                    sys.exit(1)
                log(f"getUpdates неудача #{fails}, пауза {delay}s", logging.WARNING)
                STOP.wait(delay)
                delay = min(delay * 2, 60)  # R7: бэкофф 2→60с
                continue
            fails, delay, loop_errs = 0, 2, 0  # R7: сброс после успеха
            for upd in r["result"]:
                offset = upd["update_id"] + 1
                if st.seen_update(upd["update_id"]):  # R25
                    continue
                pool.submit(run_update, upd)
            if r["result"]:
                st.save_offset(offset)
        except Exception:  # транзиентная ошибка — пауза и дальше; серия — выход
            st.note_error()
            loop_errs += 1
            log_exc(f"main loop: исключение итерации #{loop_errs}")
            if loop_errs >= st.cget("max_poll_fails"):
                log(f"main loop: {loop_errs} исключений подряд — выходим, "
                    f"run_bot.cmd перезапустит", logging.CRITICAL)
                try:  # Волна E (230): маркер как в остальных ветках выхода
                    bot_release.mark_exit("mainloop_error", 1)
                except Exception:
                    pass
                sys.exit(1)
            STOP.wait(3)  # R14

    # R13: graceful — подтверждаем offset, чтобы апдейты не пришли повторно
    log("Останавливаюсь: ack offset + сохранение состояния", logging.WARNING)
    try:
        if offset:
            tgm.tg("getUpdates", {"timeout": 0, "offset": offset, "limit": 1},
                   retries=1, timeout=(5, 10))
            st.save_offset(offset)
    except Exception:
        pass
    pool.shutdown(wait=False)
    log("bot stop")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("KeyboardInterrupt — выходим", logging.WARNING)
