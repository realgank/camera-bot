# -*- coding: utf-8 -*-
"""R46 (Волна C): bot_health — дебаунс переходов, алерты со второго прогона,
восстановление с длительностью, uptime/top_flaky. Всё на моках, сеть не трогаем."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, r"C:\Users\1\camera")

import bot_state as st  # noqa: E402
import bot_health as bh  # noqa: E402
import onvif_snap  # noqa: E402


class TestHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bh_test_")
        self._cfg_back = {}
        for k, v in {
            "health_state_path": os.path.join(self.tmp, "state.json"),
            "health_history_path": os.path.join(self.tmp, "hist.json"),
            # Волна G (347): метрики — во времянку, не в прод _metrics.db
            "metrics_db_path": os.path.join(self.tmp, "metrics.db"),
            # Волна D: _alert_downs заводит проблемы (164) — пишем во времянку
            "issues_path": os.path.join(self.tmp, "issues.json"),
            "maint_path": os.path.join(self.tmp, "maint.json"),
            "lifecycle_path": os.path.join(self.tmp, "lc.json"),
            "quiet_enabled": False,   # тихие часы не должны глотать алерты теста
            "health_fail_threshold": 2,
            "health_workers": 4,
            "health_alerts_max": 8,
            "health_mass_threshold": 99,   # массовую группировку не триггерим
            "health_factory_probe": False,  # без сетевых проб
            "watch_ips": [],
        }.items():
            self._cfg_back[k] = st.CFG.get(k)
            st.CFG[k] = v
        # чистое состояние модуля
        bh._state.update({"ips": {}, "last_run": None, "runs": 0,
                          "factory_ok": False, "last_daily": ""})
        bh._state["ips"].clear()
        bh._loaded[0] = True
        bh.RUNS_IN_PROC[0] = 0
        self.alerts = []
        patches = [
            mock.patch.object(bh, "_alert",
                              lambda text, markup=None, silent=False:
                              self.alerts.append(text)),
            mock.patch.object(bh, "target_ips",
                              lambda: ["10.0.0.1", "10.0.0.2"]),
            mock.patch.object(onvif_snap, "get_snapshot",
                              lambda *a, **k: (None, "mock")),
            mock.patch.object(st, "save_cfg", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        import bot_metrics
        bot_metrics.close_db()  # чтобы открылся временный metrics.db

    def tearDown(self):
        import bot_metrics
        bot_metrics.close_db()  # не держать хэндл на времянку
        for k, v in self._cfg_back.items():
            if v is None:
                st.CFG.pop(k, None)
            else:
                st.CFG[k] = v

    def _run(self, alive: dict):
        with mock.patch.object(bh, "probe", lambda ip: alive[ip]):
            return bh.run_once(alerts=True)

    def test_debounce_and_alert_after_second_run(self):
        # прогон 1: все живы — тихо (первый прогон, база)
        r1 = self._run({"10.0.0.1": True, "10.0.0.2": True})
        self.assertEqual((r1["online"], r1["offline"]), (2, 0))
        self.assertEqual(self.alerts, [])
        # прогон 2: .2 провалилась 1 раз — дебаунс, ещё онлайн, без алерта
        r2 = self._run({"10.0.0.1": True, "10.0.0.2": False})
        self.assertEqual(self.alerts, [])
        self.assertTrue(bh.snapshot()["ips"]["10.0.0.2"]["ok"])
        # прогон 3: .2 провалилась 2-й раз — переход в офлайн + алерт
        r3 = self._run({"10.0.0.1": True, "10.0.0.2": False})
        self.assertIn("10.0.0.2", r3["downs"])
        self.assertFalse(bh.snapshot()["ips"]["10.0.0.2"]["ok"])
        self.assertTrue(any("упала" in a and "10.0.0.2" in a for a in self.alerts))
        # прогон 4: .2 ожила — алерт восстановления с длительностью
        n_prev = len(self.alerts)
        r4 = self._run({"10.0.0.1": True, "10.0.0.2": True})
        self.assertEqual(len(r4["ups"]), 1)
        self.assertTrue(any("ожила" in a for a in self.alerts[n_prev:]))
        self.assertEqual(bh.offline_ips(), [])
        # история и метрики
        ev = bh.history_events()
        self.assertEqual([e["ev"] for e in ev if e["ip"] == "10.0.0.2"],
                         ["down", "up"])
        pct, downs, _dt = bh.uptime("10.0.0.2", days=1)
        self.assertEqual(downs, 1)
        self.assertLessEqual(pct, 100.0)
        self.assertEqual(bh.top_flaky(days=1), [("10.0.0.2", 1)])

    def test_first_run_down_is_baseline_not_alert(self):
        """Камера, офлайн с самого первого прогона, — база, а не событие."""
        for _ in range(3):
            self._run({"10.0.0.1": True, "10.0.0.2": False})
        self.assertFalse(bh.snapshot()["ips"]["10.0.0.2"]["ok"])
        self.assertEqual(self.alerts, [])  # перехода True->False не было
        self.assertEqual(bh.history_events(), [])

    def test_watch_alerts_on_first_fail(self):
        """U30: /watch-камера падает без дебаунса (порог 1)."""
        st.CFG["watch_ips"] = ["10.0.0.2"]
        self._run({"10.0.0.1": True, "10.0.0.2": True})
        self._run({"10.0.0.1": True, "10.0.0.2": False})  # 1-й провал
        self.assertFalse(bh.snapshot()["ips"]["10.0.0.2"]["ok"])
        self.assertTrue(any("10.0.0.2" in a for a in self.alerts))

    def test_report_text(self):
        self._run({"10.0.0.1": True, "10.0.0.2": True})
        txt = bh.report_text()
        self.assertIn("2/2", txt)
        self.assertIn("10.0.0.x", txt)


if __name__ == "__main__":
    unittest.main()
