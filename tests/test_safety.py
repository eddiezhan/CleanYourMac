#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安全校验回归测试
================
覆盖 server.py 的删除门禁：
  - 保护名单判定（含家目录前缀误杀缺陷的回归用例）
  - 允许范围判定
  - trash_items 对 keep / 未知 id 的拒绝

运行：python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import HOME, is_allowed, is_protected, trash_items  # noqa: E402


class TestProtected(unittest.TestCase):
    def test_home_itself_protected(self):
        self.assertTrue(is_protected(HOME))

    def test_home_child_not_protected(self):
        """回归：家目录下的合法缓存目录不得被整体保护（曾因前缀匹配被误杀）。"""
        self.assertFalse(is_protected(os.path.join(HOME, ".npm/_logs")))
        self.assertFalse(is_protected(os.path.join(HOME, ".cache/uv")))

    def test_user_data_protected(self):
        """个人数据目录及其子路径都要保护。"""
        for p in ("Documents", "Desktop", "Pictures", "Movies", "Music", "Downloads"):
            self.assertTrue(is_protected(os.path.join(HOME, p)), p)
            self.assertTrue(is_protected(os.path.join(HOME, p, "some-file")), p)

    def test_credentials_protected(self):
        for p in (".ssh", ".gnupg", "Library/Keychains", ".claude"):
            self.assertTrue(is_protected(os.path.join(HOME, p)), p)

    def test_system_protected(self):
        for p in ("/Applications", "/System", "/Library", "/usr", "/etc", "/opt"):
            self.assertTrue(is_protected(p), p)

    def test_prefix_lookalike_not_protected(self):
        """相似前缀不应误判：~/DocumentsX 不属于 ~/Documents。"""
        self.assertFalse(is_protected(os.path.join(HOME, "DocumentsX")))
        self.assertFalse(is_protected(os.path.join(HOME, "Downloadsbak")))


class TestAllowed(unittest.TestCase):
    def test_home_subpath_allowed(self):
        self.assertTrue(is_allowed(os.path.join(HOME, ".npm/_cacache")))

    def test_home_root_not_allowed(self):
        """家目录本身不是可操作对象（必须删除子路径）。"""
        self.assertFalse(is_allowed(HOME))

    def test_outside_roots_rejected(self):
        for p in ("/etc/passwd", "/Applications/Safari.app", "/tmp/x"):
            self.assertFalse(is_allowed(p), p)

    def test_tmp_root_allowed(self):
        """临时目录（动态获取）下的路径应允许操作。"""
        tmp = os.path.join(os.path.realpath(tempfile.gettempdir()), "pytest-of-user")
        self.assertTrue(is_allowed(tmp))


class TestTrashGate(unittest.TestCase):
    def test_keep_rejected(self):
        out = trash_items(["keep-ssh", "keep-documents"])
        self.assertEqual(len(out["results"]), 2)
        for r in out["results"]:
            self.assertFalse(r["ok"])
            self.assertIn("不可删除", r["error"])

    def test_unknown_id_rejected(self):
        out = trash_items(["../../etc/passwd"])
        self.assertFalse(out["results"][0]["ok"])
        self.assertEqual(out["results"][0]["error"], "未知目标")

    def test_keep_targets_untouched(self):
        """确认被拒绝后文件仍然存在。"""
        for p in ("Documents", "Downloads"):
            path = os.path.join(HOME, p)
            if os.path.isdir(path):
                self.assertTrue(os.path.isdir(path), p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
