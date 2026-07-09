# CHANGELOG — camera_bot (@your_bot)

Файл читается командой /changelog (последние 5 записей). Формат записи:
`## vX.Y-Z — дата — заголовок`, ниже — краткая суть. Новые записи — СВЕРХУ.

## v2.9-J — 2026-07-09 — Волна J: отложенные операции (DEFER, финальная)

Все 8 пунктов раздела DEFER по явному разрешению владельца; каждая запись
в инфраструктуру — превью → ДВУХШАГОВОЕ inline-подтверждение с TTL → аудит.
`bot_poe.py` — I11+I12 /reboot: PoE-цикл порта Cross-24 (порт по MAC из
живого mac_dynamic + фактов + инвентаря, превью всех MAC на порту с
вендорами, ОТКАЗ при >1 MAC/чужом MAC/аплинке, PoE on в finally с
верификацией, ожидание камеры до 90с; save не вызывается).
`bot_provision.py` — I37+I38+I41 /provision: мастер ПНР заводской
192.168.0.250 (модель/серийник/MAC по ONVIF, валидация целевого IP —
свободен в инвентаре И молчит в сети, смена IP raw-SOAP, шлюз отдельным
шагом с ретраями на HTTP 500 в окне ребута, сверка HwAddress, строка в
xlsx с автобэкапом + dirty для /sync). `bot_macfill.py` — I44 /macfill:
пустые MAC партиями по 10 (ONVIF приоритетнее ARP), превью → запись с
бэкапом. `bot_unknownq.py` — I36: новые хосты из /find и ночной сверки →
`_unknown_queue.json`, суточная сводка с кнопкой «Записать N в
"Неизвестные устройства"» (только по кнопке, с бэкапом). `bot_autosync.py`
— I27 /autosync on|off|N (0=выкл дефолт): dirty-xlsx + N часов → бережный
дифф-синк волны I. `bot_media_ops.py` — I32 /snapall <подсеть|зона>
(порции по 5 → Drive, md5-дедуп, итог X/Y) и U16 /clip <ip> [сек]
(ffmpeg -version → честный отказ, пока ffmpeg не установлен; RTSP → mp4 →
sendVideo). `bot_inline.py` — U44 inline-режим: @бот <запрос> → до 10
карточек камер, строго from.id владельца (нужен /setinline у BotFather).
/help — таб «⚙️ Операции». Тесты: 177 юнит (157 старых + 20 новых),
смоук 39/39 на моках (Telegram/сеть/xlsx-прод не тронуты).

## v2.8-I — 2026-07-09 — Волна I: Google-экосистема и автоматизация

Идеи 401–450. `google_api.py`: 431 ретраи с Retry-After, 437 fields-маски,
438 файловый кэш токена `_gtoken_cache.json`, 445 resumable upload с
докачкой. Новые модули: `bot_gsheets2.py` — ДИФФ-СИНК вместо clear+rewrite
(429/430: batchGet → сравнение → один values.batchUpdate; ручные заметки и
форматирование не затираются, повторный синк = 0 записей), 432 лок
developerMetadata, 433 /sync dry с кнопкой, 434 детект чужих правок по
revisions.list, 435 обратный синк «Комментария» (код+тесты, живьём выкл),
436 контроль лимита ячеек, 413 автодифф в лист «Изменённые (бот)»,
405 версия синка на «Дашборде» + developerMetadata, 402 лист «Журнал
событий» (хук owner_alert); `bot_gfmt.py` — 401 диаграммы addChart,
403 protected ranges, 404 именованные диапазоны, 406 COUNTIF-сводка,
407 свежесть проверок цветом, 408 dropdown-валидация, 409 filter views,
410 автоширина+banding, 411 =HYPERLINK на снимки, 412 =IMAGE (флаг),
414 /accept_sheet — лист приёмки; фикс: формулы «Дашборда» теперь с
разделителем по локали документа (en_US — раньше не парсились);
`bot_gdrive2.py` — 420 папки снимков по датам, 423 md5-дедуп,
424 appProperties, 425 /snaps, 426/427 снапшот-копии таблицы + /gsnap,
421 /snaprotate (удаления только с подтверждением, в корзину),
428 keepRevisionForever, 447 ночной бэкап xlsx, 416 /report_pdf,
440 /ga_health, 442 проверка доступов при старте; `bot_gcal.py` —
417/418 /gcal: календарь ППР и напоминания (нужен Calendar API);
`bot_nightly.py` — 446 ночная сверка сеть-vs-инвентарь 03:00 + утренняя
сводка, 450 git-автокоммит инвентаря, 448 еженедельная чистка,
415 SLA-таблица по месяцам (/sla_gs), 419 лист «Ночные задачи», /nightly.
443/444: scripts/push_sheets.py и upload_drive.py — конфиг вместо
хардкода, find-or-create папки. 449: задача camera_bot_probe
зарегистрирована (10 мин, текущий пользователь) и прогнана — Result 0;
исправлен external_probe.ps1 (UTF-8 BOM — раньше не парсился PS 5.1).
Живьём: оформление таблицы + один дифф-синк со снапшотом (1279 ячеек,
дальше 0 — идемпотентно). /help — таб «☁️ Google». Тесты 157 зелёные.

## v2.7-H — 2026-07-09 — Волна H: сеть, коммутаторы, топология

Идеи 351–400 + отложенные 322–329. Новые модули: `bot_sw_api.py`
(транспорт Cross-24 web-API: RSA-логин PKCS#1 на чистом stdlib, кэш сессий
с релогином; 399 нормализация фактов, реестр свитчей из фактов+«Лист1»+
конфига, Confirm с TTL), `bot_sw_hw.py` (Huawei VRP через plink:
интерактивный shell по stdin — exec-канал VRP не работает, host-key TOFU
в `_sw_hostkeys.json`, перебор паролей #/@; парсеры display-выводов на
живых образцах S5736), `bot_topo.py` (351 /topo цепочка камеры до ядра,
352 дерево LLDP-связей, 353 /topo_map SVG документом, 354 неизвестные
соседи, 374 /sw, 375 /sw_list, 395 /patchpanel xlsx), `bot_sw_mon.py`
(фоновая ротация 2 свитчей/10 мин: 376 сброс аптайма, 322/357/358
PoE-бюджет >85%, 323 тренд PoE per порт, 359 PoE-аномалии, 324 порты на
грани, 327 линк 10М, 400 история up/down, 356 флаппинг, 328 переезды камер,
329/365 >1 MAC, 362 новые MAC, 380 CPU/темп.; 389 /gw + фон; 388
root-cause в массовых алертах bot_health), `bot_sw_cmds.py` (/poe /flap
/swtraffic /sw_env /port_history; 385 /cable TDR с PoE-off строго через
двухшаговое подтверждение, PoE возвращается в finally), `bot_sw_audit.py`
(/sw_audit: 360/361 порты↔инвентарь, 362-365 безопасность, 368-370 VLAN
и транки, 377/378 прошивки/часы, 381-383 сервисы/sysContact; /stp /duplex;
379 /sw_ntp fix и 383 /sw_contact fix — dry-run → подтверждение → set.cgi
БЕЗ save), `bot_sw_cfg.py` (371 суточный бэкап конфигов в config_backups\
— Cross-24 JSON-снапшотом, Huawei сырым cfg; 372 дрейф-дифф с алертом;
397 /facts_refresh с prev-срезом; 398 /facts_diff; 396 /portdesc с
подтверждением), `bot_netcheck.py` (390 /netcheck — DAD/Duplicate/метрика/
маршруты по рецепту из памяти, 391 «Починить» netsh по подтверждению,
392 /trmatrix, 393 /ipplan, 394 /dupip). save/reboot свитчей не вызываются
нигде. /help — таб «🔌 Сеть/свитчи» (меню 100/100 не трогалось).

## v2.6-G — 2026-07-08 — Волна G: глубокий мониторинг и аналитика парка

Идеи 301–350 (322–329 PoE/порты свитчей → волна H). Новые модули:
`bot_metrics.py` (347: SQLite `_metrics.db` WAL — transitions/runs/subnets/
metrics/events/kv, ретенция 90 дн., разовый импорт JSON-истории, курсоры
ротаций, общий due/rotation_batch/owner_alert), `bot_onvifq.py` (read-only
raw-SOAP: часы/hostname/NTP/сеть/юзеры/энкодеры + детект 401-канарейки),
`bot_imgqa.py` (301 MD5 залипшего кадра с контрольным снимком, 302 чёрный/
белый по размеру, 305 vs эталон волны D по байтам, 306 профили день/ночь,
308 тренд размера; 303/304/307 частично — без Pillow), `bot_camtime.py`
(309-312 дрейф/NTP/«часы прыгнули», /clock_report /clock, 321 латентность
ONVIF, 335 канарейка кредов), `bot_rtsp.py` (313 DESCRIBE raw-сокетом с
digest → «зомби»-детект, 314 /bitrate SETUP/PLAY interleaved, 315 SDP vs
эталон `_facts_sdp.json`, 316 ротация; 349-350 /nvr /rec — честные заглушки
+ TCP-проба nvr_list), `bot_predict.py` (317-319 RTT/джиттер/TTL сериями
пингов, 320 /matrix, 341 микро-ребуты, 342 /risk + еженедельный топ-10,
343 /mtbf, 344 /sla xlsx с учётом /maint, 345 /season, 346 /heat,
348 /trend спарклайны ▁▂▃▅▇), `bot_secaudit.py` (330 ARP-флаппинг дублей IP,
331-334 DHCP/hostname/gateway/юзеры, 336 порт-скан против baseline,
337-338 прошивки/сброс к заводским, 339-340 инвентаризация и дрейф
энкодеров, /secaudit). Все фоновые опросы — ротацией малых порций через
MINUTE_TICKS. /help — таб «📈 Аналитика», меню команд 100/100.

## v2.5-F — 2026-07-08 — Волна F: качество данных инвентаря и отчётность

Идеи 251–300. Новые модули: `bot_dq.py` (схема inventory_schema.json, lint,
канон MAC/моделей, IP/VLAN/даты/OUI-валидации, DQ-score 0–100, полнота с
трендом, apply_writes — запись в xlsx только с автобэкапом), `bot_dq_cmds.py`
(/dq /lint /macfix /migrate_plan /names — все записи только по кнопкам),
`bot_reports.py` (/models /floors /coverage /cablelog /report_xlsx с
диаграммами, /smis CSV+JSON, /pnr, возраст по серийникам, models.json),
`bot_reconcile.py` (_changelog.jsonl, /history, CSV-срезы в exports\ +
git-коммиты «inventory:», еженедельный дайджест, история статусов,
свежесть фактов), `bot_backups.py` (/backups — ротация с подтверждением,
/diffxlsx — дифф бэкапов по UID), `bot_reconcile_net.py` (/diff3 /sheetdrift
/reconcile /orphans /cablecheck), `bot_enrich.py` (/nosnap, /enrich —
дообогащение по одной с подтверждением). /sync дополнен KPI-листом
«Дашборд». /help — таб «🧹 Данные». Миграция прод-xlsx НЕ выполнялась.

## v2.4-E — 2026-07-08 — Волна E: наблюдаемость и устойчивость процесса

Идеи 201–250. Новые модули: `bot_obs.py` (NDJSON-лог camera_bot.jsonl,
trace-id, канонический лог команд, ring-buffer 500, аудит с hash-chain,
перцентили getUpdates, машина состояний канала GOOD/DEGRADED/BAD,
автоадаптация таймаутов, RSS/потоки/handles, metrics.csv, slow.log,
контроль диска, детект сна, дрейф времени), `bot_chan.py` (канарейка getMe,
детект залипшего DNS + DNS-кэш, проба фронтов Telegram, детект смены
сети/исходящего IP → reset Session, sentinel тихого long-poll),
`bot_debug.py` (/debug zip-архив, /mem tracemalloc, /trace, /env, /crashes,
дамп по dump_now.flag, faulthandler + excepthook'и, крэш-репорты,
снапшот окружения, sha256 кода), `bot_release.py` (/version, /changelog,
/upgrade, /slo, /profile, /metrics_push, маркер выхода, отчёт «почему был
рестарт», детект flapping). run_bot.cmd: эскалация паузы 3/30/300с +
restarts.csv. scripts/: external_probe.ps1 (независимый контролёр),
chaos_bot.py и drill.ps1 (учения — только ручной запуск).

## v2.3-D — 2026-07-08 — Волна D: сценарии оператора и полевой UX

Идеи 151–200. Зоны/этажи/обходы (bot_zones), журнал проблем и snooze
(bot_issues), окна работ/тихие часы/эскалация (bot_ops), тикеты/акты/отчёты
(bot_docs), QR/наклейки (bot_qr), полевой режим (bot_field), чек-листы
lost/accept/patrol (bot_check), смены и ППР (bot_shift), досье/эталоны
(bot_dossier), lifecycle/напоминания/справочники (bot_lifecycle), общий
JSON-стор (bot_store), реестр минутных тиков MINUTE_TICKS в bot_health.

## v2.2-C — 2026-07-08 — Волна C: health-check, алерты, UX

Фоновый health-check парка (bot_health): TCP-пробы, дебаунс, алерты
падение/восстановление, группировка массовых, ежедневный отчёт.
/report /offline /uptime /top_flaky /health /watch /reboot_soft /unknown.
Новый /find с прогрессом/страницами/Стоп/ETA, /fav /last /map, /help
с табами, альбомы снимков (/lapse /compare, мульти-IP).

## v2.1-B — 2026-07-08 — Волна B: инвентарь и Google Sheets

Кэш Все_камеры.xlsx (bot_inventory): поиск по имени/IP/MAC/серийнику,
карточки, fuzzy-имена. /cam /where /port /mac /rtsp /note /verify /audit
/fw /export /free_ip /diff /sync /sheet. Google: пуш в таблицу с заливкой
офлайн, снимки /shot в Drive с индексом (google_api, bot_sheets).

## v2.0-A — 2026-07-08 — Волна A: фундамент и надёжность

Монолит разнесён на модули (camera_bot/bot_tg/bot_net/bot_handlers/
bot_state/bot_util). Ретраи tg() с бэкоффом и классами ошибок, воркер-пул,
watchdog + heartbeat, instance-lock, graceful shutdown, персист offset,
rate-limit, TOFU-привязка владельца, ротация логов, /status /log /restart,
меню команд, человеческие тексты ошибок.
