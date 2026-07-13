# -*- coding: utf-8 -*-
"""Состояние бота: конфиг (атомарная запись, валидация, дефолты для новых ключей),
STATS-счётчики, PENDING с TTL, RECENT с персистом, offset getUpdates,
rate-limit владельца, дедупликация update_id.
Волна A: R14, R19-R21, R23-R25, R38, R42, R43, R48.
Отсутствие новых ключей в живом tg_bot_config.json НЕ ломает старт — на всё дефолты.
Токен из файла никогда не изменяется, только читается (env TG_BOT_TOKEN приоритетнее, R20).
"""
import os
import json
import re
import time
import threading
from collections import deque

from bot_util import log, log_exc

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "tg_bot_config.json")
STATE_PATH = os.path.join(BASE, "tg_bot_state.json")       # offset и пр. рантайм (R14)
LOCK_PATH = os.path.join(BASE, "camera_bot.lock")          # instance-lock (R37)
HEARTBEAT_PATH = os.path.join(BASE, "camera_bot.heartbeat")  # heartbeat (R17)

# ---------- R48: тюнинг-константы с дефолтами ----------
DEFAULTS = {
    "scan_subnet": "192.168.0",
    "cam_user": "Admin",
    "cam_pass": "1234",
    "owner_chat_id": None,
    "owner_user_id": None,          # R23
    "bind_secret": None,            # R22: если задан — привязка только по /start <секрет>
    "recent_ips": [],               # R43
    "presets": ["192.168.0.250", "10.20.52.100"],  # новая камера / тестовая
    "find_allow": ["192.168.0.0/24", "10.20.0.0/16", "10.10.0.0/16"],  # R28
    "tg_retries": 6,
    "tg_connect_timeout_s": 10,
    "tg_read_timeout_s": 45,
    "send_min_interval": 0.7,
    "workers": 4,                   # R9: пул обработчиков команд
    "cmd_timeout_s": 300,           # R12
    "onvif_limit": 6,               # сколько камер опрашивать ONVIF за /find
    "scan_workers": 80,
    "probe_ports": [80, 554, 8080],
    "diag_ports": [80, 81, 443, 554, 8000, 8080, 8899, 37777],
    "ping_timeout_ms": 400,
    "subproc_timeout_s": 8,         # R29
    "rate_limit_n": 15,             # R24: команд за окно
    "rate_limit_window_s": 10,
    "pending_ttl_s": 600,           # R42
    "watchdog_min": 15,             # R18
    "heartbeat_s": 60,              # R17
    "max_poll_fails": 60,           # R8
    "log_tail_lines": 30,           # R40
    # ---------- Волна B: инвентарь и Google ----------
    "inventory_xlsx": os.path.join(BASE, r"Все_камеры.xlsx"),          # I1
    "facts_switches": os.path.join(BASE, r"_facts_switches.json"),     # I9
    "facts_cameras": os.path.join(BASE, r"_facts_cameras.json"),       # I47
    "snap_index_path": os.path.join(BASE, r"_snap_drive_index.json"),  # I31
    "sheet_id": "",      # I25
    "sa_path": os.path.join(BASE, "service-account.json"),
    "drive_folder_id": "",          # I30
    "drive_shot_upload": True,      # I30: копия каждого /shot в Drive
    "diff_subnets": ["10.20.50", "10.20.51", "10.20.52", "10.20.53"],  # I33
    # ---------- Волна C: health-check, алерты, UX ----------
    "health_enabled": True,         # I14: фоновый health-check парка
    "health_interval_min": 15,      # период прогонов
    "health_first_delay_s": 120,    # первый прогон через 2 мин (тихий рестарт)
    "health_ports": [80, 554],      # TCP-проба
    "health_workers": 24,           # умеренный пул — не душить сеть
    "health_tcp_timeout_s": 1.0,
    "health_fail_threshold": 1,     # I17: офлайн после N провальных прогонов
    "health_confirm_probes": 3,     # падение подтверждаем N пробами подряд
    "health_confirm_delay_s": 20,   # пауза между подтверждающими пробами
    "health_mass_threshold": 4,     # I18: столько падений в /24 = массовое
    "health_alerts_max": 8,         # одиночных алертов за прогон, не больше
    "health_daily_hour": 9,         # I24: час ежедневного отчёта (локальное время)
    "health_history_days": 30,      # I21: кольцо истории
    "health_factory_probe": True,   # I40: проба заводского IP
    "health_factory_ip": "192.168.0.250",
    "health_subnets": [],           # пусто = все IP инвентаря
    "health_state_path": os.path.join(BASE, r"_health_state.json"),
    "health_history_path": os.path.join(BASE, r"_health_history.json"),
    "watch_ips": [],                # U30: /watch — алерт с первого провала
    "fav_ips": [],                  # U6: избранные камеры
    "find_eta": {},                 # U40: прошлые длительности /find по подсетям
    "find_page_size": 22,           # U9: строк на страницу выдачи /find
    "lapse_max": 6,                 # U17: максимум кадров серии
    "lapse_delay_s": 2.0,
    "multi_ip_max": 8,              # U24/U10: максимум IP в мульти-действии
    # ---------- Волна D: зоны, операционка, документы, полевой режим ----------
    # 151: regex парсера имени камеры (группы sys/bld/num)
    "name_regex": r"^(?P<sys>[A-Za-zА-Яа-яЁё]+?)[oо]?\s*-?\s*"
                  r"(?P<bld>\d+\s?[A-Za-zА-ЯЁ])\s*[.\-\s]\s*(?P<num>\d+)",
    "zones_path": os.path.join(BASE, r"zones.json"),            # 153
    "checklists_path": os.path.join(BASE, r"checklists.json"),  # 181
    "ppr_path": os.path.join(BASE, r"ppr_schedule.json"),       # 189-190
    "kit_path": os.path.join(BASE, r"kit.json"),                # 198
    "contacts_path": os.path.join(BASE, r"contacts.json"),      # 199
    "warranty_path": os.path.join(BASE, r"warranty.json"),      # 200
    "issues_path": os.path.join(BASE, r"_issues.json"),         # 161/164-166
    "maint_path": os.path.join(BASE, r"_maint.json"),           # 159-160
    "reminders_path": os.path.join(BASE, r"_reminders.json"),   # 197
    "shift_path": os.path.join(BASE, r"_shift.json"),           # 185-186
    "lifecycle_path": os.path.join(BASE, r"_lifecycle.json"),   # 196
    "acts_path": os.path.join(BASE, r"_acts.json"),             # 169
    "cam_docs_dir": os.path.join(BASE, r"cam_docs"),            # 193-194
    "floor_plans": {},              # 157: "7C-1" -> путь к PNG плана этажа
    "bot_username": "your_bot",  # 172/175: deep-link для QR
    "quiet_enabled": True,          # 162: тихие часы
    "quiet_hours": [23, 8],         # с 23:00 до 08:00 — некритичное копим
    "escalate_hours": 24,           # 163: офлайн дольше -> эскалация
    "chronic_repairs": 3,           # 166: ремонтов за 90 дн -> «под замену»
    "warranty_soon_days": 60,       # 200: «гарантия истекает скоро»
    "field_ctx_ttl_min": 30,        # 177: TTL контекста «я у камеры»
    "zone_shot_max": 30,            # 154: максимум камер в /zoneshot
    "patrol_snap": True,            # 182: снимок каждой камеры при обходе
    "ppr_check_hour": 9,            # 189: час ежедневной проверки ППР/снузов
    "weekly_days": 7,               # 170: окно /weekly
    "ticket_template": "",          # 167: путь к файлу шаблона (пусто=встроенный)
    # ---------- Волна E: наблюдаемость и устойчивость процесса ----------
    "debug": False,                  # 233: мастер-флаг отладочных механик
    "debug_fault_pct": 0,            # 233: % инжектируемых сбоев tg() (только при debug)
    "obs_jsonl_path": os.path.join(BASE, r"camera_bot.jsonl"),   # 201
    "obs_jsonl_max_mb": 20,          # ротация NDJSON (одна .1-копия)
    "obs_ring_size": 500,            # 207: ring-buffer событий
    "audit_path": os.path.join(BASE, r"audit.log"),              # 212
    "slow_log_path": os.path.join(BASE, r"slow.log"),            # 240
    "slow_threshold_s": 15.0,        # 240: порог «медленной» операции
    "metrics_csv_path": os.path.join(BASE, r"metrics.csv"),      # 222
    "metrics_period_min": 5,         # период строки metrics.csv
    "metrics_rss_warn_mb": 300,      # 224: порог RSS для warning владельцу
    "metrics_threads_warn": 60,      # 211
    "metrics_handles_warn": 800,     # 210
    "disk_min_free_gb": 1.0,         # 241
    "chan_window": 200,              # 213: окно латентностей getUpdates
    "chan_over_degraded_s": 8.0,     # 214: p95 перебора сверх long-poll
    "chan_over_bad_s": 20.0,
    "chan_fail_degraded": 0.2,       # 214: доля неудачных poll'ов
    "chan_fail_bad": 0.5,
    "obs_adapt_timeouts": True,      # 215: автоадаптация таймаутов tg()
    "poll_timeout_s": 30,            # long-poll getUpdates (в DEGRADED укорачивается)
    "canary_min": 5,                 # 216: период канарейки getMe
    "sentinel_quiet_min": 60,        # 246: пустой getUpdates дольше -> reset Session
    "dns_check_min": 5,              # 218: период фоновой проверки DNS
    "dns_deadline_s": 5.0,           # 218: дедлайн getaddrinfo
    "dns_cache_path": os.path.join(BASE, r"_dns_cache.json"),    # 219
    "netwatch_enabled": True,        # 220/221: детект смены исходящего IP/адаптеров
    "exit_marker_path": os.path.join(BASE, r"exit_marker.json"),  # 230
    "restarts_csv": os.path.join(BASE, r"restarts.csv"),         # 236 (пишет run_bot.cmd)
    "flap_per_hour": 5,              # 234: рестартов за час = «цикл падений»
    "code_hash_path": os.path.join(BASE, r"_code_hash.json"),    # 248
    "crash_dir": os.path.join(BASE, r"crash_reports"),           # 245
    "crash_keep": 50,                # 245: сколько крэш-репортов хранить
    "tracemalloc_frames": 10,        # 209
    "active_profile": "normal",      # 228
    "timeout_profiles": {            # 228: /profile применяет набор в конфиг
        "normal": {"tg_retries": 6, "tg_connect_timeout_s": 10,
                   "tg_read_timeout_s": 45, "poll_timeout_s": 30},
        "harsh": {"tg_retries": 8, "tg_connect_timeout_s": 20,
                  "tg_read_timeout_s": 70, "poll_timeout_s": 15},
        "fast": {"tg_retries": 4, "tg_connect_timeout_s": 5,
                 "tg_read_timeout_s": 25, "poll_timeout_s": 30},
    },
    "metrics_sheet_name": "metrics_bot",  # 223: лист в Google-таблице
    "changelog_path": os.path.join(BASE, r"docs\CHANGELOG.md"),  # 226
    # ---------- Волна F: качество данных инвентаря и отчётность ----------
    "schema_path": os.path.join(BASE, r"inventory_schema.json"),     # 251
    "dq_history_path": os.path.join(BASE, r"_dq_history.json"),      # 287
    "models_path": os.path.join(BASE, r"models.json"),               # 261/262
    "oui_whitelist": ["E0:7F:88"],   # 258: EVIDENCE Network SIA
    "vlan_rules": {},                # 293: {"Апартаменты": [1], ...}
    "loc_min_count": 3,              # 255: значение «известно», если чаще N
    "cam_subnets": ["10.20.50", "10.20.51", "10.20.52", "10.20.53"],  # 256
    "sw_subnets": ["10.10.60", "10.10.61", "10.10.62",
                   "10.10.63", "10.10.64", "10.10.65"],
    "changelog_jsonl": os.path.join(BASE, r"_changelog.jsonl"),      # 265
    "provision_log_jsonl": os.path.join(BASE, r"_provision_log.jsonl"),  # ПНР-лог
    "exports_dir": os.path.join(BASE, r"exports"),                   # 267
    "exports_git": True,             # 267: git-коммит CSV-срезов (inventory:)
    "backup_keep_daily": 14,         # 269: дневные бэкапы, дней
    "backup_keep_weekly": 8,         # 269: недельные, недель
    "digest_weekday": 0,             # 268: 0=понедельник
    "digest_hour": 10,
    "status_history_path": os.path.join(BASE, r"_status_history.json"),  # 296
    "reconcile_state_path": os.path.join(BASE, r"_reconcile_state.json"),
    "facts_max_age_days": 14,        # 299: факты старше — предупреждение
    "smis_schema_version": "1.0",    # 286
    "enrich_batch": 10,              # 298: камер за партию /enrich
    "nosnap_batch": 10,              # 271: снимков за нажатие
    "kpi_sheet_enabled": True,       # 285: лист «Дашборд» при /sync
    "kpi_sheet_name": "Дашборд",
    # ---------- Волна G: глубокий мониторинг и аналитика парка ----------
    "metrics_db_path": os.path.join(BASE, r"_metrics.db"),  # 347: SQLite WAL
    "metrics_keep_days": 90,         # 347: ретенция рядов/событий
    "imgqa_enabled": True,           # 301-308: фоновая ротация снимков
    "imgqa_batch": 6,                # камер за цикл (не шторм)
    "imgqa_period_min": 10,
    "imgqa_black_ratio": 0.35,       # 302: кадр < 35% медианы = чёрный/белый
    "imgqa_verify_delay_s": 6,       # 301: пауза перед контрольным кадром
    "imgqa_state_path": os.path.join(BASE, r"_imgqa_state.json"),
    "imgqa_day_hours": [8, 20],      # 306: «день» с 8:00 до 20:00 локального
    "clock_enabled": True,           # 309-312: опрос часов ротацией
    "clock_batch": 15,
    "clock_period_min": 10,
    "clock_drift_warn_s": 30,        # 309: |смещение| больше -> в отчёт/алерт
    "clock_jump_s": 300,             # 312: скачок смещения = «часы прыгнули»
    "camtime_state_path": os.path.join(BASE, r"_camtime.json"),
    "rtsp_enabled": True,            # 313/316: DESCRIBE-ротация
    "rtsp_batch": 20,
    "rtsp_period_min": 15,
    "rtsp_timeout_s": 4.0,
    "rtsp_bitrate_s": 5,             # 314: секунд чтения interleaved-потока
    "sdp_facts_path": os.path.join(BASE, r"_facts_sdp.json"),  # 315: эталоны SDP
    "pingq_enabled": True,           # 317-319: серии пингов ротацией
    "pingq_batch": 15,
    "pingq_period_min": 10,
    "pingq_count": 4,                # пингов в серии (RTT/джиттер/TTL)
    "risk_weekday": 0,               # 342: еженедельный топ-10 риска (0=пн)
    "risk_hour": 10,
    "secaudit_enabled": True,        # 330-340: фоновый аудит ротацией
    "secaudit_batch": 8,
    "secaudit_period_min": 15,
    "sec_ports": [21, 23, 80, 554, 8000, 8080],  # 336: скан против baseline
    "sec_risky_ports": [21, 23],     # открылись -> алерт сразу
    "sec_default_hosts": ["ipc", "apix", "localhost", "ipcam", "nvt", "camera"],
    "secaudit_state_path": os.path.join(BASE, r"_secaudit.json"),
    "encoders_facts_path": os.path.join(BASE, r"_facts_encoders.json"),  # 339
    "arp_flap_window_min": 10,       # 330: окно детекта флаппинга ARP
    "nvr_list": [],                  # 349-350: [{"name","ip","port"}] (пусто)
    # ---------- Волна H: сеть, коммутаторы, топология ----------
    "sw_user": "admin",              # Cross-24 web-API
    "sw_pass": "admin",
    "sw_http_timeout_s": 6,
    "hw_user": "Administrator",      # Huawei VRP по SSH (plink)
    "hw_passwords": [],
    "hw_switches": [],               # IP Huawei-свитчей (10.20.5.x), пусто = нет
    "plink_timeout_s": 35,
    "sw_confirm_ttl_s": 120,         # TTL двухшаговых подтверждений записи
    "sw_state_path": os.path.join(BASE, r"_sw_state.json"),
    "sw_mon_enabled": True,          # 355-359/376/400: фоновая ротация свитчей
    "sw_mon_period_min": 10,         # период тика ротации
    "sw_mon_batch": 2,               # свитчей за тик (малые порции)
    "sw_poe_budget_w": 370.0,        # 358: бюджет PoE Cross-24, Вт
    "poe_budget_warn_pct": 85,       # 358: алерт при загрузке бюджета выше
    "poe_port_jump_w": 3.0,          # 359: скачок потребления порта, Вт
    "uplink_util_warn_pct": 80,      # 387: загрузка аплинка = предупреждение
    "gw_check_min": 10,              # 389: период проверки шлюзов
    "gw_list": [],                   # 389: пусто = .254 камерных /24 + 10.20.5.1
    "sw_backup_enabled": True,       # 371: плановый бэкап конфигов
    "sw_backup_hour": 3,             # час начала суточного бэкапа
    "sw_backup_batch": 4,            # свитчей за тик бэкапа
    "sw_backup_keep": 30,            # копий на свитч
    "config_backups_dir": os.path.join(BASE, r"config_backups"),
    "vlan_port_ref": {},             # 368: {"10.10.60.52": {"GE1": 1, ...}}
    "sw_ntp_server": "",             # 379: эталонный NTP (пусто = не настраивать)
    "facts_refresh_workers": 6,      # 397: потоков пересборки фактов
    "facts_prev_path": os.path.join(BASE, r"_facts_switches.prev.json"),  # 398
    # ---------- Волна I: Google-экосистема и автоматизация (401-450) ----------
    "sync_diff_enabled": True,       # 429: дифф-синк вместо clear+rewrite
    "sync_diff_sheet": "Изменённые (бот)",  # 413: автодифф синка (отдельный лист)
    "sync_lock_ttl_min": 10,         # 432: чужой лок старше — игнорируем
    "cells_limit_warn": 9000000,     # 436: предупреждение у лимита 10 млн
    "sheet_events_enabled": True,    # 402: лист «Журнал событий»
    "sheet_events_name": "Журнал событий",
    "sheet_events_max_rows": 20000,  # 436: журнал больше — совет архивировать
    "sheet_comment_col": "Комментарий",  # 435: обратный синк (живьём не гоняется)
    "sheet_image_thumbs": False,     # 412: колонка =IMAGE (тяжёлый лист, выкл)
    "sheet_fmt_enabled": True,       # 401/403-411: оформление после синка
    "drive_date_folders": True,      # 420: подпапки ГГГГ-ММ-ДД для снимков
    "drive_dedup_md5": True,         # 423: не заливать дубликаты
    "drive_snap_keep_days": 30,      # 421: ротация снимков (только с кнопкой)
    "drive_table_snapshots": 10,     # 427: снапшот-копий таблицы хранить
    "drive_backup_enabled": True,    # 447: ночной бэкап xlsx в Drive
    "drive_backup_hour": 4,
    "ga_state_path": os.path.join(BASE, r"_gapi_state.json"),
    "ga_check_on_start": True,       # 442: fail-fast проверка доступов при старте
    "sa_key_max_age_days": 180,      # 440/441: возраст ключа SA
    "gcal_enabled": False,           # 417: Calendar API может быть выключен
    "gcal_name": "ППР камеры",
    "gcal_share_email": "realgank@gmail.com",
    "nightly_recon_enabled": True,   # 446: ночная сверка сеть-vs-инвентарь
    "nightly_hour": 3,
    "nightly_sheet_name": "Ночные задачи",  # 419
    "cleanup_enabled": True,         # 448: еженедельная чистка
    "cleanup_weekday": 6,            # воскресенье
    "cleanup_snaps_days": 14,        # локальные JPEG старше — удалить
    "cleanup_backups_keep": 20,      # 448: бэкапов xlsx оставлять
    "cleanup_backups_auto": False,   # False = только отчёт, без удаления
    "git_autocommit_enabled": True,  # 450: ночной git-коммит инвентаря
    "sla_gsheet_enabled": True,      # 415: помесячная SLA-таблица
    "probe_task_name": "camera_bot_probe",  # 449
    # ---------- Волна J: отложенные операции (DEFER) ----------
    "poe_reboot_wait_s": 90,         # I11/I12: ждать возвращения камеры (TCP)
    "poe_reboot_off_s": 3,           # пауза PoE off -> on
    # ---------- watchdog зависших новых/заводских камер (bot_newcam) ----------
    "newcam_enabled": True,          # фон: следить за новыми/заводскими камерами
    "newcam_watch_factory": True,    # наблюдать health_factory_ip (192.168.0.250)
    "newcam_watch_ips": [],          # доп. IP под наблюдение (провижн/новые)
    "newcam_period_min": 2,          # период тика (ловим боот/зависание быстро)
    "newcam_boot_grace_s": 180,      # пинг-есть/ONVIF-нет короче — норм. боот, тихо
    "newcam_hung_after_s": 600,      # дольше — «зависла», включаем обход
    "newcam_auto_poe": True,         # авто-PoE-ребут зависшей (иначе кнопка вручную)
    "newcam_reset_cd_h": 6,          # не чаще 1 авто-ребута на IP за N часов
    "newcam_conflict_cd_h": 6,       # анти-спам алерта о конфликте IP
    "provision_prefix_len": 24,      # I37: маска нового IP
    "provision_gw_retries": 6,       # ловушка HTTP 500 в окне ребута камеры
    "provision_gw_delay_s": 6,
    "provision_wait_s": 90,          # ждать камеру на новом IP
    "macfill_batch": 10,             # I44: камер за партию /macfill
    "unknown_queue_path": os.path.join(BASE, r"_unknown_queue.json"),  # I36
    "unknown_daily_hour": 11,        # I36: час суточной сводки
    "unknown_keep_days": 30,         # I36: не видели дольше — из очереди
    "autosync_hours": 0,             # I27: 0 = автосинк выключен
    "autosync_state_path": os.path.join(BASE, r"_autosync.json"),
    "snapall_workers": 5,            # I32: параллельных снимков
    "snapall_max": 300,              # I32: потолок камер за один /snapall
    "clip_seconds": 8,               # U16: длительность клипа по умолчанию
    "clip_max_s": 15,                # U16: потолок длительности
    "inline_enabled": True,          # U44: inline-режим (нужен /setinline)
    "alerts_off": [],                # /alerts: id выключенных фоновых алертов
}

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")

_cfg_lock = threading.RLock()


def _atomic_write(path, obj):
    """R21: атомарная запись json (tmp + os.replace)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _validate(cfg):
    """R19: валидация конфига при старте — fail-fast с внятным текстом."""
    tok = os.environ.get("TG_BOT_TOKEN") or cfg.get("token") or ""
    if not TOKEN_RE.match(tok):
        raise SystemExit("Конфиг: token отсутствует или не похож на токен бота "
                         "(нужен вид 123456789:AA...); можно задать env TG_BOT_TOKEN")
    if cfg.get("owner_chat_id") is not None and not isinstance(cfg["owner_chat_id"], int):
        raise SystemExit("Конфиг: owner_chat_id должен быть числом или null")
    if not isinstance(cfg.get("scan_subnet", DEFAULTS["scan_subnet"]), str):
        raise SystemExit("Конфиг: scan_subnet должен быть строкой вида '192.168.0'")
    for key in ("tg_retries", "workers", "cmd_timeout_s", "scan_workers"):
        if key in cfg and not isinstance(cfg[key], int):
            raise SystemExit(f"Конфиг: {key} должен быть целым числом")
    return tok


def load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"Нет конфига {CFG_PATH}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"Конфиг повреждён (не JSON): {e}")


CFG = load_cfg()
TOKEN = _validate(CFG)  # R19 + R20 (env приоритетнее, но в файл не пишется)
CAM_USER = os.environ.get("CAM_USER", CFG.get("cam_user", DEFAULTS["cam_user"]))
CAM_PASS = os.environ.get("CAM_PASS", CFG.get("cam_pass", DEFAULTS["cam_pass"]))


def cget(key):
    """Значение из конфига с дефолтом (R48)."""
    v = CFG.get(key)
    return DEFAULTS.get(key) if v is None else v


def save_cfg():
    """R21: атомарно; токен остаётся тем, что был в файле."""
    with _cfg_lock:
        try:
            _atomic_write(CFG_PATH, CFG)
        except Exception:
            log_exc("save_cfg: не смог записать конфиг")


def bind_owner(chat_id, user_id=None):
    """TOFU-привязка владельца (+R23: запоминаем и user_id)."""
    with _cfg_lock:
        CFG["owner_chat_id"] = chat_id
        if user_id:
            CFG["owner_user_id"] = user_id
        save_cfg()
    log(f"owner bound -> chat={chat_id} user={user_id}")


def set_owner_user(user_id):
    """R23: дозаполняем owner_user_id для старых конфигов (где был только chat_id)."""
    with _cfg_lock:
        CFG["owner_user_id"] = user_id
        save_cfg()
    log(f"owner_user_id -> {user_id}")


# ---------- STATS (R38) ----------
STATS = {"requests": 0, "last_cmd": "—", "errors": 0, "retries": 0,
         "e429": 0, "started": time.time()}
_stats_lock = threading.Lock()
_err_times = deque()            # таймстемпы ошибок (для «ошибок за час»)
_durations = deque(maxlen=20)   # последние (cmd, сек)


def note_retry():
    with _stats_lock:
        STATS["retries"] += 1


def note_429():
    with _stats_lock:
        STATS["e429"] += 1


def note_error():
    with _stats_lock:
        STATS["errors"] += 1
        _err_times.append(time.time())


def note_cmd_start(cmd, arg=""):
    with _stats_lock:
        STATS["requests"] += 1
        STATS["last_cmd"] = cmd + (f" {arg}" if arg else "")


def note_cmd_done(cmd, dt):
    with _stats_lock:
        _durations.append((cmd, dt))


def stats_snapshot():
    now = time.time()
    with _stats_lock:
        while _err_times and now - _err_times[0] > 3600:
            _err_times.popleft()
        d = dict(STATS)
        d["errors_hour"] = len(_err_times)
        d["durations"] = list(_durations)
        return d


# ---------- PENDING с TTL (R42) ----------
_pending = {}          # chat_id -> (action, ts)
_pending_lock = threading.Lock()


def set_pending(chat, action):
    with _pending_lock:
        _pending[chat] = (action, time.time())


def pop_pending(chat):
    """None, если не было или протухло (TTL)."""
    with _pending_lock:
        v = _pending.pop(chat, None)
    if not v:
        return None
    action, ts = v
    if time.time() - ts > cget("pending_ttl_s"):
        log(f"PENDING протух (>{cget('pending_ttl_s')}s) chat={chat} action={action}")
        return None
    return action


def clear_pending(chat):
    with _pending_lock:
        _pending.pop(chat, None)


# ---------- RECENT с персистом (R43) ----------
RECENT = deque(maxlen=6)
for _ip in list(CFG.get("recent_ips") or [])[:6]:
    RECENT.append(_ip)


def remember_ip(ip):
    if ip in RECENT:
        RECENT.remove(ip)
    RECENT.appendleft(ip)
    with _cfg_lock:
        CFG["recent_ips"] = list(RECENT)
        save_cfg()


# ---------- offset getUpdates (R14) ----------
def load_offset():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f).get("offset")
    except Exception:
        return None


def save_offset(offset):
    try:
        _atomic_write(STATE_PATH, {"offset": offset, "saved_at": int(time.time())})
    except Exception:
        log_exc("save_offset: не смог записать состояние")


# ---------- rate-limit владельца (R24) ----------
_cmd_times = deque()
_rl_lock = threading.Lock()
_rl_warned = [0.0]


def rate_limited():
    """(превышен ли лимит, надо ли предупредить). Предупреждаем раз за окно."""
    now = time.time()
    win, n = cget("rate_limit_window_s"), cget("rate_limit_n")
    with _rl_lock:
        _cmd_times.append(now)
        while _cmd_times and now - _cmd_times[0] > win:
            _cmd_times.popleft()
        if len(_cmd_times) > n:
            warn = (now - _rl_warned[0]) > win
            if warn:
                _rl_warned[0] = now
            return True, warn
    return False, False


# ---------- дедупликация update_id (R25) ----------
_seen = deque()
_seen_set = set()
_seen_lock = threading.Lock()


def seen_update(update_id):
    """True, если такой update_id уже обрабатывали (последние 100)."""
    with _seen_lock:
        if update_id in _seen_set:
            return True
        _seen.append(update_id)
        _seen_set.add(update_id)
        while len(_seen) > 100:
            _seen_set.discard(_seen.popleft())
        return False


# ---------- Волна C: списки в конфиге (fav/watch), /last, ETA /find ----------
def get_ips(key: str) -> list:
    """Список IP из конфига (fav_ips / watch_ips)."""
    with _cfg_lock:
        return list(CFG.get(key) or DEFAULTS.get(key) or [])


def toggle_ip(key: str, ip: str) -> bool:
    """Добавить/убрать IP в списке конфига. True — добавлен, False — убран."""
    with _cfg_lock:
        cur = list(CFG.get(key) or [])
        if ip in cur:
            cur.remove(ip)
            added = False
        else:
            cur.append(ip)
            added = True
        CFG[key] = cur
        save_cfg()
    return added


_last = deque(maxlen=10)   # U8: (action, arg) последних запросов
_last_lock = threading.Lock()


def note_last(action: str, arg: str) -> None:
    """U8: копим историю действий для /last (в памяти процесса)."""
    if not arg:
        return
    with _last_lock:
        item = (action, str(arg))
        if item in _last:
            _last.remove(item)
        _last.appendleft(item)


def last_actions() -> list:
    with _last_lock:
        return list(_last)


def get_eta(prefix: str):
    """U40: длительность прошлого /find по подсети (сек) или None."""
    with _cfg_lock:
        return (CFG.get("find_eta") or {}).get(prefix)


def set_eta(prefix: str, sec: float) -> None:
    with _cfg_lock:
        eta = dict(CFG.get("find_eta") or {})
        eta[prefix] = round(sec, 1)
        CFG["find_eta"] = eta
        save_cfg()
