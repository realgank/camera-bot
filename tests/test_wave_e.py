# -*- coding: utf-8 -*-
"""Волна E: bot_obs (NDJSON, ring, перцентили, машина состояний канала,
аудит hash-chain, детект сна) и bot_release (парсер restarts.csv, flapping,
CHANGELOG, SLO-таблица). Всё в темп-файлах, сеть не трогаем."""
import os
import json
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, r"C:\Users\1\camera")

import bot_state as st  # noqa: E402
import bot_obs as obs  # noqa: E402
import bot_release as rel  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obs_test_")
        self._back = {}
        for k, v in {
            "obs_jsonl_path": os.path.join(self.tmp, "bot.jsonl"),
            "audit_path": os.path.join(self.tmp, "audit.log"),
            "slow_log_path": os.path.join(self.tmp, "slow.log"),
            "metrics_csv_path": os.path.join(self.tmp, "metrics.csv"),
            "slow_threshold_s": 15.0,
            "chan_over_degraded_s": 8.0,
            "chan_over_bad_s": 20.0,
            "chan_fail_degraded": 0.2,
            "chan_fail_bad": 0.5,
        }.items():
            self._back[k] = st.CFG.get(k)
            st.CFG[k] = v
        obs.RING.clear()
        obs._polls.clear()
        obs._chan["state"] = "GOOD"
        obs._audit_prev[0] = ""
        obs.set_trace(None)

    def tearDown(self):
        for k, v in self._back.items():
            if v is None:
                st.CFG.pop(k, None)
            else:
                st.CFG[k] = v


class TestNdjson(Base):
    def test_jlog_format(self):
        """201: валидный NDJSON с ts/level/event и полями."""
        obs.jlog("test_ev", level="WARNING", ip="10.0.0.1", n=5)
        with open(st.cget("obs_jsonl_path"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["event"], "test_ev")
        self.assertEqual(rec["level"], "WARNING")
        self.assertEqual(rec["ip"], "10.0.0.1")
        self.assertEqual(rec["n"], 5)
        self.assertIsInstance(rec["ts"], float)
        self.assertNotIn("trace", rec)

    def test_trace_id(self):
        """202: после set_trace id попадает в записи; base36 короткий."""
        obs.set_trace(1234567890)
        self.assertEqual(obs.get_trace(), obs.b36(1234567890))
        rec = obs.jlog("t")
        self.assertEqual(rec["trace"], obs.b36(1234567890))
        obs.set_trace(None)
        self.assertIsNone(obs.get_trace())
        self.assertEqual(obs.b36(0), "0")
        self.assertEqual(obs.b36(35), "z")
        self.assertEqual(obs.b36(36), "10")

    def test_note_cmd_canonical_and_slow(self):
        """203 + 240: каноническая запись команды; долгая — в slow.log."""
        obs.note_cmd("/diag", "10.0.0.1", 2.5, True, tg_retries=1)
        obs.note_cmd("/find", "10.20.50", 99.0, False, err="Boom", tg_retries=0)
        with open(st.cget("obs_jsonl_path"), encoding="utf-8") as f:
            recs = [json.loads(ln) for ln in f.read().splitlines()]
        cmds = [r for r in recs if r["event"] == "cmd"]
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0]["cmd"], "/diag")
        self.assertTrue(cmds[0]["ok"])
        self.assertEqual(cmds[0]["tg_retries"], 1)
        self.assertFalse(cmds[1]["ok"])
        self.assertIn("Boom", cmds[1]["err"])
        with open(st.cget("slow_log_path"), encoding="utf-8") as f:
            slow = f.read()
        self.assertIn("/find", slow)
        self.assertNotIn("/diag", slow)


class TestRing(Base):
    def test_ring_maxlen_and_dump(self):
        """207: кольцо не растёт бесконечно; дамп валиден."""
        for i in range(obs.RING.maxlen + 100):
            obs.RING.append({"i": i})
        self.assertEqual(len(obs.RING), obs.RING.maxlen)
        self.assertEqual(obs.RING[0]["i"], 100)  # старые вытеснены
        path = os.path.join(self.tmp, "ring.json")
        out = obs.ring_dump(path, reason="test")
        self.assertEqual(out, path)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["reason"], "test")
        self.assertEqual(len(d["events"]), obs.RING.maxlen)


class TestPercentiles(Base):
    def test_pctl(self):
        """213: перцентили ближайшего ранга."""
        self.assertIsNone(obs.pctl([], 95))
        self.assertEqual(obs.pctl([5], 50), 5)
        vals = list(range(1, 101))  # 1..100
        self.assertEqual(obs.pctl(vals, 50), 51)
        self.assertEqual(obs.pctl(vals, 95), 95)
        self.assertEqual(obs.pctl(vals, 99), 99)
        self.assertEqual(obs.pctl([3, 1, 2], 100), 3)  # сортировка внутри


class TestChannelState(Base):
    def _fill(self, over, fail_share, n=40):
        obs._polls.clear()
        fails = int(n * fail_share)
        for _ in range(n - fails):
            obs.note_poll(30 + over, True, timeout_s=30)
        for _ in range(fails):
            obs.note_poll(30, False, timeout_s=30)

    def test_classify(self):
        """214: чистая классификация по p95-перебору и доле неудач."""
        self.assertEqual(obs.classify(0.0, 0.0), "GOOD")
        self.assertEqual(obs.classify(10.0, 0.0), "DEGRADED")
        self.assertEqual(obs.classify(0.0, 0.3), "DEGRADED")
        self.assertEqual(obs.classify(25.0, 0.0), "BAD")
        self.assertEqual(obs.classify(0.0, 0.6), "BAD")
        self.assertEqual(obs.classify(None, 0.0), "GOOD")

    def test_transitions(self):
        """214: GOOD -> DEGRADED -> BAD -> GOOD с алертами на переходах."""
        alerts = []
        with mock.patch.object(obs, "_owner",
                               lambda text, silent=True: alerts.append(text)):
            self._fill(over=0, fail_share=0)
            self.assertEqual(obs._eval_channel(), "GOOD")
            self.assertEqual(alerts, [])
            self._fill(over=12, fail_share=0)
            self.assertEqual(obs._eval_channel(), "DEGRADED")
            self.assertEqual(len(alerts), 1)
            self._fill(over=25, fail_share=0.6)
            self.assertEqual(obs._eval_channel(), "BAD")
            self.assertEqual(len(alerts), 2)
            self._fill(over=0, fail_share=0)
            self.assertEqual(obs._eval_channel(), "GOOD")
            self.assertEqual(len(alerts), 3)

    def test_few_samples_no_flap(self):
        """214: <20 замеров — состояние не меняется."""
        obs._polls.clear()
        for _ in range(5):
            obs.note_poll(60, True, timeout_s=30)
        with mock.patch.object(obs, "_owner", lambda *a, **k: None):
            self.assertEqual(obs._eval_channel(), "GOOD")

    def test_adaptive_timeouts(self):
        """215: множитель и long-poll зависят от состояния."""
        back = st.CFG.get("obs_adapt_timeouts")
        st.CFG["obs_adapt_timeouts"] = True
        try:
            obs._chan["state"] = "GOOD"
            self.assertEqual(obs.timeout_factor(), 1.0)
            self.assertEqual(obs.poll_timeout(), int(st.cget("poll_timeout_s")))
            obs._chan["state"] = "DEGRADED"
            self.assertEqual(obs.timeout_factor(), 1.5)
            self.assertLessEqual(obs.poll_timeout(), 15)
            obs._chan["state"] = "BAD"
            self.assertEqual(obs.timeout_factor(), 2.0)
            self.assertLessEqual(obs.poll_timeout(), 10)
        finally:
            obs._chan["state"] = "GOOD"
            if back is None:
                st.CFG.pop("obs_adapt_timeouts", None)
            else:
                st.CFG["obs_adapt_timeouts"] = back


class TestAudit(Base):
    def test_chain_and_tamper(self):
        """212: hash-chain сходится; подмена строки ломает проверку."""
        obs.audit("/note", "10.0.0.1 текст", "OK")
        obs.audit("/upgrade", "a -> b", "OK")
        obs.audit("/restart", "", "OK")
        ok, n, bad = obs.audit_verify()
        self.assertTrue(ok)
        self.assertEqual(n, 3)
        self.assertIsNone(bad)
        path = st.cget("audit_path")
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        rec = json.loads(lines[1])
        rec["arg"] = "ПОДМЕНЕНО"
        lines[1] = json.dumps(rec, ensure_ascii=False)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        ok, n, bad = obs.audit_verify()
        self.assertFalse(ok)
        self.assertEqual(bad, 3)  # хэш строки 2 изменился -> строка 3 бита


class TestSleepDetect(Base):
    def test_sleep_gap(self):
        """243: скачок wall сверх monotonic > порога = сон."""
        self.assertIsNone(obs.sleep_gap(None, None, 100, 1000))
        # монотонник +60с, часы +60с — сна нет
        self.assertIsNone(obs.sleep_gap(0, 1000, 60, 1060))
        # монотонник +60с, часы +43 мин — машина спала ~42 мин
        gap = obs.sleep_gap(0, 1000, 60, 1000 + 60 + 2580)
        self.assertAlmostEqual(gap, 2580, delta=1)
        # ниже порога 120с — не считаем сном
        self.assertIsNone(obs.sleep_gap(0, 1000, 60, 1060 + 100))


class TestRestartsParser(Base):
    CSV = ("ts;exit_code;life_s;pause_s\n"
           "2026-07-08T10:00:00;1;12;3\n"
           "2026-07-08T10:00:15;1;10;3\n"
           "мусорная строка\n"
           "2026-07-08T10:00:30;9009;5;30\n"
           "2026-07-08T12:00:00;0;7200;3\n")

    def test_parse(self):
        """236: парсер restarts.csv терпит мусор и заголовок."""
        rows = rel.parse_restarts(self.CSV)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["code"], 1)
        self.assertEqual(rows[2]["code"], 9009)
        self.assertEqual(rows[3]["life"], 7200)
        self.assertEqual(rows[1]["pause"], 3)
        self.assertEqual(rel.parse_restarts(""), [])

    def test_flap_count(self):
        """234: рестарты за последний час."""
        import time as _t
        rows = rel.parse_restarts(self.CSV)
        now = _t.mktime(_t.strptime("2026-07-08T10:30:00", "%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(rel.flap_count(rows, now=now), 3)
        now2 = _t.mktime(_t.strptime("2026-07-08T12:10:00", "%Y-%m-%dT%H:%M:%S"))
        self.assertEqual(rel.flap_count(rows, now=now2), 1)


class TestRelease(Base):
    def test_changelog_entries(self):
        """226: секции '## ' в порядке файла."""
        text = ("# CHANGELOG\nпреамбула\n\n## v2 — новое\nстрока а\n\n"
                "## v1 — старое\nстрока б\n")
        ents = rel.changelog_entries(text)
        self.assertEqual(len(ents), 2)
        self.assertTrue(ents[0].startswith("v2"))
        self.assertIn("строка а", ents[0])
        self.assertIn("строка б", ents[1])

    def test_slo_table(self):
        """244: агрегация канонических событий."""
        evs = ([{"cmd": "/diag", "dur": 1.0, "ok": True}] * 8
               + [{"cmd": "/diag", "dur": 9.0, "ok": False}] * 2
               + [{"cmd": "/shot", "dur": 2.0, "ok": True}])
        rows = rel.slo_table(evs)
        self.assertEqual(rows[0][0], "/diag")   # самый частый первым
        self.assertEqual(rows[0][1], 10)
        self.assertAlmostEqual(rows[0][4], 20.0)  # 2 из 10 = 20% ошибок
        self.assertEqual(rows[1][0], "/shot")
        self.assertAlmostEqual(rows[1][2], 2.0)


if __name__ == "__main__":
    unittest.main()
