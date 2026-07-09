# -*- coding: utf-8 -*-
"""Волна J — U44: inline-режим (@бот <запрос> в любом чате -> до 10 карточек
камер из инвентаря). СТРОГО только для владельца: проверяется from.id самого
inline_query (не chat!), чужим — пустая выдача + лог DENY.
Требование Telegram: inline надо ВКЛЮЧИТЬ у BotFather (/setinline) — иначе
inline_query просто не приходят (см. /help, таб «⚙️ Операции»).
camera_bot добавляет "inline_query" в allowed_updates и зовёт on_inline()."""
import json
import logging

import bot_state as st
import bot_inventory as inv
from bot_tg import tg
from bot_util import log, log_exc, esc


def _card(rec):
    """Компактная HTML-карточка для inline-результата (самодостаточная)."""
    name = rec.get("name") or "(без имени)"
    lines = [f"📷 <b>{esc(name)}</b> — <code>{esc(rec.get('ip') or '?')}</code>"]
    if rec.get("model"):
        lines.append(f"🎥 {esc(rec['model'])}")
    if rec.get("mac"):
        lines.append(f"MAC: <code>{esc(rec['mac'])}</code>")
    loc = " · ".join(str(rec[k]) for k in ("location", "obj") if rec.get(k))
    if loc:
        lines.append(f"📍 {esc(loc)}")
    swp = " · ".join(s for s in (
        str(rec.get("switch") or "") or None,
        str(rec.get("sw_ip") or "") or None,
        f"порт {rec.get('port')}" if rec.get("port") else None) if s)
    if swp:
        lines.append(f"🔌 {esc(swp)}")
    return "\n".join(lines)


def build_inline_results(q, recs):
    """Чистый билдер: записи инвентаря -> список InlineQueryResultArticle
    (максимум 10, уникальные id)."""
    out = []
    for i, rec in enumerate(recs):
        if len(out) >= 10:
            break
        if not isinstance(rec, dict):
            continue
        title = str(rec.get("name") or rec.get("ip") or "?")[:60]
        desc = " · ".join(str(rec[k]) for k in ("ip", "location", "model")
                          if rec.get(k))[:100]
        out.append({"type": "article",
                    "id": f"cam{i}_{rec.get('row', i)}",
                    "title": title, "description": desc,
                    "input_message_content": {
                        "message_text": _card(rec), "parse_mode": "HTML",
                        "disable_web_page_preview": True}})
    return out


def _default_recs():
    """Пустой запрос: последние + избранные, иначе первые камеры."""
    ips = list(dict.fromkeys(list(st.RECENT) + st.get_ips("fav_ips")))
    recs = [inv.get(ip) for ip in ips]
    recs = [r for r in recs if r]
    return recs or inv.cams()[:10]


def on_inline(iq):
    """Обработчик inline_query из main-цикла (camera_bot)."""
    if not st.cget("inline_enabled"):
        return
    iq_id = iq.get("id")
    uid = (iq.get("from") or {}).get("id")
    owner_uid = st.cget("owner_user_id") or st.cget("owner_chat_id")
    q = (iq.get("query") or "").strip()
    if not iq_id:
        return
    if owner_uid is None or uid != owner_uid:
        log(f"DENY inline from={uid} q={q!r}", logging.WARNING)
        tg("answerInlineQuery", {"inline_query_id": iq_id,
                                 "results": "[]", "cache_time": 300,
                                 "is_personal": True},
           retries=1, timeout=(5, 10))
        return
    try:
        recs = inv.search(q) if q else _default_recs()
    except Exception:
        log_exc("inline: поиск")
        recs = []
    results = build_inline_results(q, recs)
    log(f"INLINE q={q!r} -> {len(results)} результатов")
    tg("answerInlineQuery", {"inline_query_id": iq_id,
                             "results": json.dumps(results, ensure_ascii=False),
                             "cache_time": 5, "is_personal": True},
       retries=2, timeout=(5, 10))


HANDLERS = {}
ALIASES = {}
CALLBACKS = {}
