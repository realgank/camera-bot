# -*- coding: utf-8 -*-
"""Волна D: парсер имён камер (151), этажи, зоны (153) и маршрут (184)."""
import sys
import tempfile
import os
import unittest
from unittest import mock

sys.path.insert(0, r"C:\Users\1\camera")

import bot_state as st  # noqa: E402
import bot_zones as bz  # noqa: E402
import bot_store as store  # noqa: E402


def rec(name, ip=None, location=None):
    import bot_inventory as inv
    return {"name": name, "ip": ip, "location": location,
            "nname": inv.norm_name(name), "nmac": ""}


FAKE = [rec("AS-7C.01", "10.20.50.1", "Корпус 7C, 3 этаж, коридор"),
        rec("AS-7C.02", "10.20.50.2", "Корпус 7C, этаж -1, паркинг"),
        rec("AS-5D.03", "10.20.51.3", "лестница"),
        rec("KO-7", "10.20.52.7", None),
        rec("новая (найдена в сети)", "10.20.53.9", None)]


class TestParser(unittest.TestCase):
    def test_basic(self):
        p = bz.parse_cam_name("AS-7C.01")
        self.assertEqual((p["sys"], p["bld"], p["num"]), ("AS", "7C", 1))

    def test_outdoor_suffix(self):
        p = bz.parse_cam_name("ASo-5D.03")
        self.assertEqual((p["bld"], p["num"]), ("5D", 3))

    def test_space_variants(self):
        self.assertEqual(bz.parse_cam_name("AS-8A. 3")["num"], 3)
        self.assertEqual(bz.parse_cam_name("AS- 7C.01")["bld"], "7C")

    def test_lowercase(self):
        self.assertEqual(bz.parse_cam_name("As-7A.4")["bld"], "7A")

    def test_no_match(self):
        self.assertIsNone(bz.parse_cam_name("новая (найдена в сети)"))
        self.assertIsNone(bz.parse_cam_name(""))
        self.assertIsNone(bz.parse_cam_name(None))

    def test_floor_of(self):
        self.assertEqual(bz.floor_of({"location": "Корпус 7C, 3 этаж"}), 3)
        self.assertEqual(bz.floor_of({"location": "этаж -1, паркинг"}), -1)
        self.assertIsNone(bz.floor_of({"location": "лестница"}))
        self.assertIsNone(bz.floor_of({"location": None}))


class TestZones(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="botD_")
        self.zpath = os.path.join(self.tmp, "zones.json")
        self.lcpath = os.path.join(self.tmp, "_lifecycle.json")
        self._old = dict(st.CFG)
        st.CFG["zones_path"] = self.zpath
        st.CFG["lifecycle_path"] = self.lcpath
        self.addCleanup(self._restore)
        p = mock.patch.object(bz.inv, "cams", lambda: [dict(r) for r in FAKE])
        p.start()
        self.addCleanup(p.stop)

    def _restore(self):
        st.CFG.clear()
        st.CFG.update(self._old)

    def test_zone_mask(self):
        store.jsave(self.zpath, {"атриум": {"items": ["AS-7C.*"]}})
        ips = bz.zone_ips("атриум")
        self.assertEqual(sorted(ips), ["10.20.50.1", "10.20.50.2"])

    def test_zone_bld_and_ip(self):
        store.jsave(self.zpath, {"z": {"items": ["7C", "10.20.52.7"]}})
        ips = bz.zone_ips("z")
        self.assertEqual(sorted(ips), ["10.20.50.1", "10.20.50.2", "10.20.52.7"])

    def test_zone_exact_name(self):
        store.jsave(self.zpath, {"z": {"items": ["as7c01"]}})
        self.assertEqual(bz.zone_ips("z"), ["10.20.50.1"])

    def test_resolve_zone_case(self):
        store.jsave(self.zpath, {"Атриум": {"items": []}})
        self.assertEqual(bz.resolve_zone("атриум"), "Атриум")
        self.assertIsNone(bz.resolve_zone("нет такой"))

    def test_lifecycle_excluded(self):
        store.jsave(self.zpath, {"z": {"items": ["AS-7C.*"]}})
        import bot_lifecycle as lc
        lc.set_status("10.20.50.2", "dismantled")
        self.assertEqual(bz.zone_ips("z"), ["10.20.50.1"])

    def test_cams_by_arg_bld(self):
        what, recs = bz.cams_by_arg("7c")
        self.assertIn("7C", what)
        self.assertEqual(len(recs), 2)

    def test_cams_by_arg_zone(self):
        store.jsave(self.zpath, {"паркинг": {"items": ["AS-7C.02"]}})
        what, recs = bz.cams_by_arg("паркинг")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["ip"], "10.20.50.2")


if __name__ == "__main__":
    unittest.main()
