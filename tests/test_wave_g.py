# -*- coding: utf-8 -*-
"""Волна G (301-350): SQLite-слой метрик (347), risk-score (342),
спарклайны (348), парсер SDP (315), детект залипания по MD5 (301),
чёрный кадр (302), скачок часов (312), парсер ping (317-319),
дефолтный hostname / gateway / дрейф энкодера (332/333/340), MTBF (343).
Всё на синтетике и временных файлах — сеть и прод-файлы не трогаем."""
import os
import sys
import time
import tempfile
import unittest

sys.path.insert(0, r"C:\Users\1\camera")

import bot_state as st  # noqa: E402


class Base(unittest.TestCase):
    KEYS = ("metrics_db_path", "health_history_path", "health_state_path",
            "camtime_state_path", "imgqa_state_path", "secaudit_state_path",
            "sdp_facts_path", "encoders_facts_path", "maint_path")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wg_test_")
        self._back = {}
        for k in self.KEYS:
            self._back[k] = st.CFG.get(k)
            st.CFG[k] = os.path.join(self.tmp, k + ".dat")
        import bot_metrics as mx
        self.mx = mx
        mx.close_db()

    def tearDown(self):
        self.mx.close_db()
        for k, v in self._back.items():
            if v is None:
                st.CFG.pop(k, None)
            else:
                st.CFG[k] = v


class TestMetricsDB(Base):
    def test_on_run_and_queries(self):
        now = time.time()
        results = [("10.9.0.1", True), ("10.9.0.2", False), ("10.9.1.3", True)]
        ups = [{"ts": int(now), "ip": "10.9.0.1", "ev": "up", "dur": 120}]
        self.mx.on_run(results, ["10.9.0.2"], ups, now)
        self.assertEqual(self.mx.downs_count(1).get("10.9.0.2"), 1)
        # 341: up с dur=120 < 180 c = микро-ребут
        self.assertEqual(self.mx.micro_reboots(1).get("10.9.0.1"), 1)
        trs = self.mx.transitions(ip="10.9.0.2", days=1)
        self.assertEqual([e["ev"] for e in trs], ["down"])

    def test_metrics_series_and_median(self):
        for i, v in enumerate([10, 20, 30, 40, 50]):
            self.mx.metric_add("10.9.0.5", "rtt", v,
                               ts=int(time.time()) - 100 + i)
        self.assertEqual(self.mx.med("10.9.0.5", "rtt", 1), 30)
        self.assertEqual(self.mx.last_value("10.9.0.5", "rtt"), 50)
        self.assertEqual(len(self.mx.series("10.9.0.5", "rtt", 1)), 5)

    def test_event_cooldown(self):
        self.assertTrue(self.mx.event_add("10.9.0.7", "frozen"))
        # повтор в окне cooldown — НЕ «свежее» (алерт не спамим)
        self.assertFalse(self.mx.event_add("10.9.0.7", "frozen"))
        self.assertEqual(len(self.mx.events(ip="10.9.0.7", kind="frozen")), 2)

    def test_retention_cleanup(self):
        old = int(time.time() - 200 * 86400)
        self.mx.metric_add("10.9.0.8", "rtt", 1.0, ts=old)
        self.mx.metric_add("10.9.0.8", "rtt", 2.0)
        n = self.mx.cleanup()
        self.assertGreaterEqual(n, 1)
        self.assertEqual(len(self.mx.series("10.9.0.8", "rtt", 365)), 1)

    def test_uptime_and_downtime(self):
        now = time.time()
        # лежала 1 час, поднялась 2 часа назад
        self.mx.on_run([("10.9.0.9", True)], [],
                       [{"ts": int(now - 7200), "ip": "10.9.0.9",
                         "ev": "up", "dur": 3600}], now)
        dt = self.mx.downtime_s("10.9.0.9", now - 86400, now)
        self.assertAlmostEqual(dt, 3600, delta=5)
        # исключение окна работ (344): всё падение внутри /maint
        dt2 = self.mx.downtime_s("10.9.0.9", now - 86400, now,
                                 exclude=[(now - 11000, now - 7000)])
        self.assertAlmostEqual(dt2, 0, delta=5)
        self.assertGreater(self.mx.uptime_pct("10.9.0.9", 1), 95)

    def test_kv(self):
        self.assertIsNone(self.mx.kv_get("x"))
        self.mx.kv_set("x", "42")
        self.mx.kv_set("x", "43")
        self.assertEqual(self.mx.kv_get("x"), "43")


class TestPredict(Base):
    def test_spark(self):
        import bot_predict as bp
        s = bp.spark([0, 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(len(s), 8)
        self.assertEqual(s[0], "▁")
        self.assertEqual(s[-1], "█")
        self.assertEqual(bp.spark([]), "")
        self.assertIn("·", bp.spark([1, None, 2]))

    def test_bucketize(self):
        import bot_predict as bp
        ser = [(i * 10, float(i)) for i in range(100)]
        b = bp.bucketize(ser, 10)
        self.assertEqual(len(b), 10)
        self.assertLess(b[0], b[-1])

    def test_risk_components(self):
        import bot_predict as bp
        c = bp.risk_components(downs=0, micro=0, rtt_ratio=1, jitter=0,
                               events_n=0)
        self.assertEqual(bp.risk_total(c), 0)
        c = bp.risk_components(downs=10, micro=10, rtt_ratio=5, jitter=100,
                               events_n=10)
        self.assertEqual(bp.risk_total(c), 100)  # потолок
        c = bp.risk_components(downs=2, micro=1, rtt_ratio=2.5)
        self.assertEqual(c["флапы"], 16)
        self.assertEqual(c["микро-ребуты"], 5)
        self.assertEqual(c["RTT"], 8)

    def test_risk_score_from_db(self):
        import bot_predict as bp
        now = time.time()
        self.mx.on_run([("10.9.2.1", False)], ["10.9.2.1"], [], now)
        s, comp = bp.risk_score("10.9.2.1")
        self.assertEqual(comp["флапы"], 8)
        self.assertGreaterEqual(s, 8)

    def test_parse_ping_ru_en(self):
        import bot_predict as bp
        ru = ("Ответ от 10.20.50.51: число байт=32 время=3мс TTL=64\r\n"
              "Ответ от 10.20.50.51: число байт=32 время<1мс TTL=64\r\n")
        times, ttl = bp.parse_ping(ru)
        self.assertEqual(times, [3, 1])
        self.assertEqual(ttl, 64)
        en = "Reply from 8.8.8.8: bytes=32 time=12ms TTL=117"
        times, ttl = bp.parse_ping(en)
        self.assertEqual((times, ttl), ([12], 117))
        self.assertEqual(bp.parse_ping(""), ([], None))

    def test_mtbf(self):
        import bot_predict as bp
        now = time.time()
        self.mx.on_run([("10.9.2.5", True)], ["10.9.2.5"],
                       [{"ts": int(now - 3600), "ip": "10.9.2.5",
                         "ev": "up", "dur": 1800}], now)
        m = bp.mtbf_mttr("10.9.2.5", days=30)
        self.assertEqual(m["downs"], 1)
        self.assertAlmostEqual(m["mttr_min"], 30, delta=1)
        self.assertGreater(m["mtbf_h"], 700)

    def test_season_hist(self):
        import bot_predict as bp
        now = time.time()
        self.mx.on_run([("10.9.2.7", False)], ["10.9.2.7"], [], now)
        by_h, by_wd = bp.season_hist(1)
        self.assertEqual(sum(by_h.values()), 1)
        self.assertEqual(sum(by_wd.values()), 1)


SDP_SAMPLE = """v=0
o=- 1 1 IN IP4 10.20.50.51
s=Media Presentation
c=IN IP4 0.0.0.0
t=0 0
m=video 0 RTP/AVP 96
a=rtpmap:96 H264/90000
a=framerate:25
a=framesize:96 1920-1080
a=control:rtsp://10.20.50.51:554/trackID=1
m=audio 0 RTP/AVP 8
a=rtpmap:8 PCMA/8000
"""


class TestRtsp(Base):
    def test_parse_sdp(self):
        import bot_rtsp as rt
        s = rt.parse_sdp(SDP_SAMPLE)
        self.assertEqual(s["codec"], "H264")
        self.assertEqual(s["fps"], 25)
        self.assertEqual(s["res"], "1920x1080")
        self.assertEqual(s["tracks"], 2)
        self.assertTrue(s["control"].startswith("rtsp://"))
        self.assertEqual(rt.parse_sdp("")["tracks"], 0)

    def test_sdp_drift(self):
        import bot_rtsp as rt
        ref = {"codec": "H264", "res": "1920x1080", "fps": 25}
        cur = {"codec": "H264", "res": "704x576", "fps": 25}
        d = rt.sdp_drift(ref, cur)
        self.assertEqual(d, [("res", "1920x1080", "704x576")])
        # незаполненные поля не считаются дрейфом
        self.assertEqual(rt.sdp_drift(ref, {"codec": "H264", "res": None,
                                            "fps": None}), [])

    def test_store_sdp_ref_and_drift(self):
        import bot_rtsp as rt
        s1 = {"codec": "H264", "res": "1920x1080", "fps": 25}
        self.assertEqual(rt._store_sdp("10.9.3.1", s1, "rtsp://x/"), [])
        s2 = dict(s1, res="704x576")
        drifts = rt._store_sdp("10.9.3.1", s2, "rtsp://x/")
        self.assertEqual(drifts, [("res", "1920x1080", "704x576")])


class TestImgqa(Base):
    def test_md5_frozen_synthetic(self):
        import bot_imgqa as iq
        a = b"\xff\xd8" + os.urandom(5000) + b"\xff\xd9"
        self.assertTrue(iq.md5_frozen(a, bytes(a)))     # 301: залип
        b2 = b"\xff\xd8" + os.urandom(5000) + b"\xff\xd9"
        self.assertFalse(iq.md5_frozen(a, b2))          # живой поток
        self.assertFalse(iq.md5_frozen(a, b""))

    def test_is_black(self):
        import bot_imgqa as iq
        self.assertTrue(iq.is_black(20_000, 200_000, ratio=0.35))   # 302
        self.assertFalse(iq.is_black(150_000, 200_000, ratio=0.35))
        self.assertFalse(iq.is_black(100, 0, ratio=0.35))  # нет медианы

    def test_baseline_cmp(self):
        import bot_imgqa as iq
        base = b"\xff\xd8" + b"A" * 100_000
        r = iq.baseline_cmp(base, bytes(base))
        self.assertIn("байт-в-байт", r["verdict"])
        r = iq.baseline_cmp(base, b"\xff\xd8" + b"B" * 20_000)
        self.assertLess(r["ratio"], 0.5)
        self.assertIn("расходится", r["verdict"])

    def test_size_trend(self):
        import bot_imgqa as iq
        self.assertLess(iq.size_trend([100] * 5 + [50] * 5), -30)  # 308
        self.assertEqual(iq.size_trend([100, 100]), 0.0)  # мало данных


class TestCamtime(Base):
    def test_is_jump(self):
        import bot_camtime as ct
        self.assertTrue(ct.is_jump(0, 1000, jump_s=300))     # 312
        self.assertFalse(ct.is_jump(10, 40, jump_s=300))
        self.assertFalse(ct.is_jump(None, 1000, jump_s=300))

    def test_fmt_off(self):
        import bot_camtime as ct
        self.assertEqual(ct._fmt_off(5), "+5 c")
        self.assertEqual(ct._fmt_off(-7200), "-2.0 ч")
        self.assertEqual(ct._fmt_off(None), "?")


class TestSecaudit(Base):
    def test_default_hostname(self):
        import bot_secaudit as sa
        pats = ["ipc", "apix", "localhost"]
        self.assertTrue(sa.is_default_host("IPC123456", pats))   # 332
        self.assertTrue(sa.is_default_host("APIX-Bullet", pats))
        self.assertFalse(sa.is_default_host("AS-7C.01", pats))
        self.assertFalse(sa.is_default_host("", pats))

    def test_gw_expected(self):
        import bot_secaudit as sa
        self.assertEqual(sa.gw_expected("10.20.50.51"), "10.20.50.254")  # 333

    def test_enc_drift(self):
        import bot_secaudit as sa
        ref = [{"name": "main", "codec": "H264", "res": "1920x1080",
                "fps": 25, "kbps": 4096}]
        cur = [{"name": "main", "codec": "H264", "res": "704x576",
                "fps": 25, "kbps": 1024}]
        d = sa.enc_drift(ref, cur)                                # 340
        self.assertIn(("main", "res", "1920x1080", "704x576"), d)
        self.assertIn(("main", "kbps", 4096, 1024), d)
        self.assertEqual(sa.enc_drift(ref, ref), [])


if __name__ == "__main__":
    unittest.main()
