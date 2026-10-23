# -*- coding: utf-8 -*-
"""纯逻辑单测：不启动 ComfyUI，也不连导演台。"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_package():
    name = "h3d_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pkg = load_package()
config = sys.modules["h3d_under_test.config"]
client = sys.modules["h3d_under_test.director_client"]
nodes = sys.modules["h3d_under_test.nodes"]


class ConfigTests(unittest.TestCase):
    def test_env_override(self):
        old = os.environ.get("H3_DIRECTOR_URL")
        os.environ["H3_DIRECTOR_URL"] = "http://example.test:9999/"
        try:
            cfg = config.load_config()
            self.assertEqual(cfg["director_url"], "http://example.test:9999")
        finally:
            if old is None:
                os.environ.pop("H3_DIRECTOR_URL", None)
            else:
                os.environ["H3_DIRECTOR_URL"] = old

    def test_bad_name(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(config.ConfigError):
                config.write_film_json("a/b", "s", "zh", "x", raw_dir=d)

    def test_empty_lines(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(config.ConfigError):
                config.write_film_json("ok", "s", "zh", "   \n  ", raw_dir=d)

    def test_write_json_and_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            p = config.write_film_json(
                "测试片", "3D 卡通", "zh",
                "第一句\n\n第二句\n", reuse_text="seg_00.mp4\nseg_01.mp4",
                raw_dir=d)
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            self.assertEqual(data["lines"], ["第一句", "第二句"])
            self.assertEqual(data["reuse"], ["seg_00.mp4", "seg_01.mp4"])
            self.assertEqual(data["lang"], "zh")
            with self.assertRaises(config.ConfigError):
                config.write_film_json("测试片", "s", "zh", "x", raw_dir=d)
            p2 = config.write_film_json("测试片", "s2", "zh", "x", raw_dir=d,
                                        overwrite=True)
            self.assertEqual(json.loads(Path(p2).read_text(encoding="utf-8"))["style"], "s2")


class StatusTests(unittest.TestCase):
    def test_summary(self):
        data = {
            "overall": {"phase": "busy", "text": "处理中 · 字幕合成"},
            "tasks": [{"name": "a.mp4", "state": "running", "stage": "字幕合成"},
                      {"name": "b.mp4", "state": "done"}],
            "films": [{"name": "f1"}],
        }
        s = client.summarize_status(data)
        self.assertIn("处理中 · 字幕合成", s)
        self.assertIn("进行中 1", s)
        self.assertIn("a.mp4", s)
        self.assertIn("字幕合成", s)

    def test_last_result(self):
        self.assertEqual(client.get_last_result({}), {})
        self.assertEqual(client.get_last_result({"last_result": {"file": "x.mp4"}}),
                         {"file": "x.mp4"})

    def test_download_url_encodes(self):
        u = client.download_url("中文 片.mp4", "http://127.0.0.1:8898/")
        self.assertTrue(u.startswith("http://127.0.0.1:8898/download?name="))
        self.assertNotIn(" ", u)


class NodeContractTests(unittest.TestCase):
    def test_nodes_registered(self):
        for key in ("H3DirectorStatus", "H3DirectorLastResult", "H3DirectorSubmitFilm"):
            self.assertIn(key, nodes.NODE_CLASS_MAPPINGS)
            cls = nodes.NODE_CLASS_MAPPINGS[key]
            self.assertTrue(cls.INPUT_TYPES())
            self.assertTrue(cls.RETURN_TYPES)
            self.assertEqual(cls.CATEGORY, "H3/Director")


if __name__ == "__main__":
    unittest.main(verbosity=2)
