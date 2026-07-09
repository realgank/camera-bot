# -*- coding: utf-8 -*-
"""Волна D: журнал проблем (164-166), snooze (161), тихие часы (162),
парсер сроков /remind (197), lifecycle (196), JSON-стор."""
import sys
import os
import time
import datetime
import tempfile
import unittest

sys.path.insert(0, r"C:\Users\1\camera")

import bot_state as st  # noqa: E402
import bot_store as store  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="botD_")
        self._old = dict(st.CFG)
        for key, fn in (("issues_path", "_issues.json"),
                        ("lifecycle_path", "_lifecycle.json"),
                        ("reminders_path", "_reminders.json"),
                        ("maint_path", "_maint.json"),
                        ("ppr_path", "ppr.json")):
            st.CFG[key] = os.path.join(self.tmp, fn)
        self.addCleanup(self._restore)

    def _restore(self):
        st.CFG.clear()
        st.CFG.update(self._old)


class TestStore(Base):
    def test_roundtrip(self):
        p = os.path.join(self.tmp, "x.json")
        self.assertEqual(store.jload(p, {"a": 1}), {"a": 1})
        store.jsave(p, {"b": 2})
        self.assertEqual(store.jload(p, {}), {"b": 2})

    def test_jupdate(self):
        p = os.path.join(self.tmp, "y.json")
        store.jupdate(p, {"n": 0}, lambda d: {**d, "n": d["n"] + 1})
        store.jupdate(p, {"n": 0}, lambda d: {**d, "n": d["n"] + 1})
        self.assertEqual(store.jload(p, {})["n"], 2)

    def test_broken_json(self):
        p = os.path.join(self.tmp, "bad.json")
        with open(p, "w") as f:
            f.write("{oops")
        self.assertEqual(store.jload(p, {"ok": True}), {"ok": True})


class TestIssues(Base):
    def test_open_take_fix_mttr(self):
        import bot_issues as iss
        t0 = time.time() - 7200
        it = iss.open_issue("10.20.50.1", now=t0)
        self.assertEqual(it["status"], "open")
        # повторное открытие не плодит дублей
        it2 = iss.open_issue("10.20.50.1")
        self.assertEqual(it2["id"], it["id"])
        self.assertTrue(iss.set_status("10.20.50.1", "in_progress"))
        self.assertEqual(iss.get_open("10.20.50.1")["status"], "in_progress")
        self.assertTrue(iss.set_status("10.20.50.1", "fixed"))
        self.assertIsNone(iss.get_open("10.20.50.1"))
        avg, n = iss.mttr(30)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(avg, 7200, delta=120)
        self.assertEqual(iss.repairs_count("10.20.50.1"), 1)
        self.assertFalse(iss.is_chronic("10.20.50.1"))

    def test_chronic(self):
        import bot_issues as iss
        for _i in range(3):
            iss.open_issue("10.20.50.2")
            iss.set_status("10.20.50.2", "fixed")
        self.assertTrue(iss.is_chronic("10.20.50.2"))

    def test_snooze(self):
        import bot_issues as iss
        iss.open_issue("10.20.50.3")
        iss.snooze("10.20.50.3", int(time.time()) + 3600)
        self.assertTrue(iss.snoozed("10.20.50.3"))
        self.assertEqual(iss.get_open("10.20.50.3")["status"], "in_progress")
        iss.snooze("10.20.50.3", int(time.time()) - 10)
        self.assertFalse(iss.snoozed("10.20.50.3"))


class TestQuiet(Base):
    def test_in_quiet(self):
        import bot_ops as ops
        st.CFG["quiet_hours"] = [23, 8]
        st.CFG["quiet_enabled"] = True
        def at(h):
            return datetime.datetime(2026, 7, 8, h, 30).timestamp()
        self.assertTrue(ops.in_quiet(at(23)))
        self.assertTrue(ops.in_quiet(at(3)))
        self.assertFalse(ops.in_quiet(at(12)))
        st.CFG["quiet_enabled"] = False
        self.assertFalse(ops.in_quiet(at(3)))


class TestLifecycle(Base):
    def test_status_and_monitoring(self):
        import bot_lifecycle as lc
        self.assertEqual(lc.status_of("10.20.50.5"), "active")
        self.assertTrue(lc.is_monitored("10.20.50.5"))
        lc.set_status("10.20.50.5", "dismantled")
        self.assertFalse(lc.is_monitored("10.20.50.5"))
        lc.set_status("10.20.50.5", "in_repair")
        self.assertTrue(lc.is_monitored("10.20.50.5"))
        self.assertEqual(len(lc.lc_events("10.20.50.5")), 2)


class TestParseWhen(unittest.TestCase):
    NOW = datetime.datetime(2026, 7, 8, 12, 0).timestamp()

    def p(self, s):
        from bot_lifecycle import parse_when
        return parse_when(s.split(), now=self.NOW)

    def test_tomorrow(self):
        ts, used = self.p("завтра 10:00 проверить")
        self.assertEqual(used, 2)
        dt = datetime.datetime.fromtimestamp(ts)
        self.assertEqual((dt.day, dt.hour, dt.minute), (9, 10, 0))

    def test_tomorrow_default_time(self):
        ts, used = self.p("завтра проверить")
        self.assertEqual(used, 1)
        self.assertEqual(datetime.datetime.fromtimestamp(ts).hour, 10)

    def test_relative(self):
        ts, used = self.p("через 2ч замена")
        self.assertEqual(used, 2)
        self.assertAlmostEqual(ts - self.NOW, 7200, delta=1)
        ts, used = self.p("через 30 мин глянуть")
        self.assertEqual(used, 3)
        self.assertAlmostEqual(ts - self.NOW, 1800, delta=1)

    def test_date(self):
        ts, used = self.p("15.07 14:30 ппр")
        self.assertEqual(used, 2)
        dt = datetime.datetime.fromtimestamp(ts)
        self.assertEqual((dt.month, dt.day, dt.hour, dt.minute), (7, 15, 14, 30))

    def test_bare_time_rolls_over(self):
        ts, _u = self.p("18:00 вечером")
        self.assertEqual(datetime.datetime.fromtimestamp(ts).day, 8)
        ts, _u = self.p("09:00 утром")   # уже прошло -> завтра
        self.assertEqual(datetime.datetime.fromtimestamp(ts).day, 9)

    def test_garbage(self):
        ts, used = self.p("когда-нибудь потом")
        self.assertIsNone(ts)
        self.assertEqual(used, 0)


if __name__ == "__main__":
    unittest.main()
