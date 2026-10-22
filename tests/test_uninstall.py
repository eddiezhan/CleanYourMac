#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应用卸载功能测试
================
覆盖 uninstaller.py 的核心判定与门禁：
  - 应用信息读取（Bundle ID / 名称）
  - 残留数据的三级置信度分类，以及「被其他应用认领」的排除
  - 卸载门禁：系统应用、未知应用路径、越权残留路径、运行中应用
  - 移入废纸篓的实际行为

所有用例都在临时目录里构造假的应用与残留，**不触碰真实的
/Applications 与 ~/Library**。

运行：python3 -m unittest discover -s tests -v
"""

import os
import plistlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uninstaller as U  # noqa: E402


def make_app(root: str, name: str, bundle_id: str, version: str = "1.0") -> str:
    """在 root 下造一个最小可用的 .app（只含 Contents/Info.plist）。"""
    path = os.path.join(root, name + ".app")
    os.makedirs(os.path.join(path, "Contents"), exist_ok=True)
    info = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleName": name,
        "CFBundleShortVersionString": version,
    }
    with open(os.path.join(path, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(info, f)
    return path


def make_dir(base: str, rel: str, payload: bytes = b"x" * 2048) -> str:
    """在 base 下创建 rel 目录并放一个文件，保证 du 能测出体积。"""
    path = os.path.join(base, rel)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "blob.bin"), "wb") as f:
        f.write(payload)
    return path


class TempCase(unittest.TestCase):
    """每个用例一套独立的 apps / library / trash 目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cmm-uninst-")
        self.apps_root = os.path.join(self.tmp, "Applications")
        self.library = os.path.join(self.tmp, "Library")
        self.trash = os.path.join(self.tmp, "Trash")
        for d in (self.apps_root, self.library, self.trash):
            os.makedirs(d, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAppInfo(TempCase):
    def test_reads_bundle_id_and_name(self):
        path = make_app(self.apps_root, "Demo", "com.example.demo", "2.3")
        info = U.read_app_info(path)
        self.assertEqual(info["bundle_id"], "com.example.demo")
        self.assertEqual(info["name"], "Demo")
        self.assertEqual(info["version"], "2.3")
        self.assertEqual(info["path"], path)

    def test_returns_none_for_non_app(self):
        plain = os.path.join(self.apps_root, "not-an-app")
        os.makedirs(plain)
        self.assertIsNone(U.read_app_info(plain))

    def test_list_apps_skips_non_app_entries(self):
        make_app(self.apps_root, "Demo", "com.example.demo")
        os.makedirs(os.path.join(self.apps_root, "loose-folder"))
        apps = U.list_apps([self.apps_root])
        self.assertEqual([a["bundle_id"] for a in apps], ["com.example.demo"])

    def test_system_app_detected(self):
        self.assertTrue(U.is_system_app("com.apple.Safari"))
        self.assertTrue(U.is_system_app("com.apple.TestFlight"))
        self.assertFalse(U.is_system_app("com.google.Chrome"))
        self.assertFalse(U.is_system_app(""))


class TestClassifyLeftover(TempCase):
    def setUp(self):
        super().setUp()
        self.app = {"name": "Demo", "bundle_id": "com.example.demo"}

    def test_own_bundle_id_is_bundle_confidence(self):
        self.assertEqual(
            U.classify_leftover("com.example.demo", self.app, set()), "bundle")

    def test_bundle_id_with_suffix_is_bundle_confidence(self):
        self.assertEqual(
            U.classify_leftover("com.example.demo.helper", self.app, set()), "bundle")

    def test_exact_name_is_name_confidence(self):
        self.assertEqual(U.classify_leftover("Demo", self.app, set()), "name")

    def test_plist_suffix_stripped_before_name_match(self):
        self.assertEqual(
            U.classify_leftover("Demo.plist", self.app, set()), "name")

    def test_substring_is_fuzzy(self):
        self.assertEqual(
            U.classify_leftover("Demo Beta", self.app, set()), "fuzzy")

    def test_unrelated_is_none(self):
        self.assertIsNone(U.classify_leftover("SomethingElse", self.app, set()))

    def test_bundle_id_must_end_at_component_boundary(self):
        """回归：com.example.demo 不得把 com.example.demo2 判成 bundle 级。

        后缀必须以分隔符（. 或 -）开始，否则就是另一个应用的 Bundle ID。
        这类项最多只能落到 fuzzy（低置信度、默认不勾），绝不能自动勾选。
        """
        self.assertNotEqual(
            U.classify_leftover("com.example.demo2", self.app, set()), "bundle")
        self.assertNotEqual(
            U.classify_leftover("com.example.demoExtra", self.app, set()), "bundle")
        # 真正的组件后缀仍然命中
        self.assertEqual(
            U.classify_leftover("com.example.demo.ShipIt", self.app, set()), "bundle")
        self.assertEqual(
            U.classify_leftover("com.example.demo-helper", self.app, set()), "bundle")

    def test_other_app_bundle_id_excluded(self):
        """回归：WeChat 不得认领 WorkBuddy 的 WechatShare 容器。"""
        app = {"name": "WeChat", "bundle_id": "com.tencent.xinWeChat"}
        others = {"com.tencent.workbuddy.mac"}
        self.assertIsNone(
            U.classify_leftover("com.tencent.workbuddy.mac.WechatShare", app, others))

    def test_longest_bundle_id_wins(self):
        """回归：Chrome 不得认领 Chrome for Testing 的偏好文件。"""
        app = {"name": "Google Chrome", "bundle_id": "com.google.Chrome"}
        others = {"com.google.chrome.for.testing"}
        self.assertIsNone(
            U.classify_leftover("com.google.chrome.for.testing.plist", app, others))

    def test_preferences_requires_exact_bundle_id(self):
        """回归：Chrome for Testing 未安装时，也不能被 Chrome 顺走配置。

        Preferences 下的文件名就是 Bundle ID，因此这里要求精确相等。
        真实案例：com.google.chrome.for.testing.plist 曾因前缀匹配被算作
        Chrome 的高置信度残留。
        """
        app = {"name": "Google Chrome", "bundle_id": "com.google.Chrome"}
        self.assertIsNone(
            U.classify_leftover("com.google.chrome.for.testing.plist", app, set(),
                                in_preferences=True))
        # 精确同名仍然命中
        self.assertEqual(
            U.classify_leftover("com.google.Chrome.plist", app, set(),
                                in_preferences=True), "bundle")


class TestFindLeftovers(TempCase):
    def test_finds_bundle_and_name_entries(self):
        make_dir(self.library, "Caches/com.example.demo")
        make_dir(self.library, "Application Support/Demo")
        make_dir(self.library, "Caches/com.unrelated.thing")
        app = {"name": "Demo", "bundle_id": "com.example.demo"}

        found = U.find_leftovers(app, self.library, set())
        by_name = {os.path.basename(f["path"]): f["confidence"] for f in found}
        self.assertEqual(by_name.get("com.example.demo"), "bundle")
        self.assertEqual(by_name.get("Demo"), "name")
        self.assertNotIn("com.unrelated.thing", by_name)

    def test_each_leftover_has_size(self):
        make_dir(self.library, "Caches/com.example.demo")
        app = {"name": "Demo", "bundle_id": "com.example.demo"}
        found = U.find_leftovers(app, self.library, set())
        self.assertGreater(found[0]["size"], 0)

    def test_nested_vendor_child_is_fuzzy(self):
        """Chrome 的真实数据在 Application Support/Google/Chrome，两级嵌套。"""
        make_dir(self.library, "Application Support/Google/Chrome")
        make_dir(self.library, "Application Support/Google/RLZ")
        app = {"name": "Google Chrome", "bundle_id": "com.google.Chrome"}

        found = U.find_leftovers(app, self.library, set())
        paths = {f["path"]: f["confidence"] for f in found}
        chrome = os.path.join(self.library, "Application Support/Google/Chrome")
        rlz = os.path.join(self.library, "Application Support/Google/RLZ")
        self.assertEqual(paths.get(chrome), "fuzzy")
        # 只取命中的子目录，不把整个厂商目录算进来
        self.assertNotIn(rlz, paths)
        self.assertNotIn(os.path.join(self.library, "Application Support/Google"), paths)

    def test_vendor_token_comes_from_bundle_id(self):
        """回归：Chrome 的 CFBundleName 是 "Chrome"，靠名字猜不到厂商 "Google"。

        Bundle ID com.google.Chrome 的第二段就是厂商标识，用它定位
        Application Support/Google/Chrome 这类两级嵌套目录。
        """
        make_dir(self.library, "Application Support/Google/Chrome")
        app = {"name": "Chrome", "bundle_id": "com.google.Chrome"}

        found = U.find_leftovers(app, self.library, set())
        chrome = os.path.join(self.library, "Application Support/Google/Chrome")
        self.assertIn(chrome, {f["path"] for f in found})

    def test_vendor_sibling_variant_not_matched(self):
        """Google/Chrome Beta 是另一个产品，不能被 Chrome 认领。"""
        make_dir(self.library, "Application Support/Google/Chrome Beta")
        app = {"name": "Chrome", "bundle_id": "com.google.Chrome"}

        found = U.find_leftovers(app, self.library, set())
        paths = {f["path"] for f in found}
        self.assertNotIn(
            os.path.join(self.library, "Application Support/Google/Chrome Beta"), paths)

    def test_excludes_other_apps_containers(self):
        make_dir(self.library, "Containers/com.tencent.workbuddy.mac.WechatShare")
        app = {"name": "WeChat", "bundle_id": "com.tencent.xinWeChat"}
        found = U.find_leftovers(app, self.library, {"com.tencent.workbuddy.mac"})
        self.assertEqual(found, [])


class TestScanApps(TempCase):
    def test_default_selection_excludes_fuzzy(self):
        make_app(self.apps_root, "Demo", "com.example.demo")
        make_dir(self.library, "Caches/com.example.demo")
        make_dir(self.library, "Application Support/Demo Beta")

        out = U.scan_apps([self.apps_root], self.library, running_checker=lambda p: False)
        app = out["apps"][0]
        defaults = {os.path.basename(f["path"]): f["default"] for f in app["leftovers"]}
        self.assertTrue(defaults["com.example.demo"])
        self.assertFalse(defaults["Demo Beta"])

    def test_system_and_running_flags(self):
        make_app(self.apps_root, "Safari", "com.apple.Safari")
        make_app(self.apps_root, "Demo", "com.example.demo")
        out = U.scan_apps(
            [self.apps_root], self.library,
            running_checker=lambda p: p.endswith("Demo.app"),
        )
        flags = {a["name"]: (a["system"], a["running"]) for a in out["apps"]}
        self.assertEqual(flags["Safari"], (True, False))
        self.assertEqual(flags["Demo"], (False, True))


class TestUninstallGate(TempCase):
    def _demo(self):
        path = make_app(self.apps_root, "Demo", "com.example.demo")
        left = make_dir(self.library, "Caches/com.example.demo")
        return path, left

    def _run(self, app_path, leftovers, **kw):
        kw.setdefault("roots", [self.apps_root])
        kw.setdefault("library", self.library)
        kw.setdefault("trash_dir", self.trash)
        kw.setdefault("running_checker", lambda p: False)
        return U.uninstall(app_path, leftovers, **kw)

    def test_refuses_system_app(self):
        path = make_app(self.apps_root, "Safari", "com.apple.Safari")
        out = self._run(path, [])
        self.assertFalse(out["ok"])
        self.assertIn("系统", out["error"])
        self.assertTrue(os.path.exists(path))

    def test_refuses_unknown_app_path(self):
        bogus = os.path.join(self.apps_root, "Ghost.app")
        out = self._run(bogus, [])
        self.assertFalse(out["ok"])
        self.assertIn("未知应用", out["error"])

    def test_refuses_leftover_outside_computed_set(self):
        path, _ = self._demo()
        rogue = make_dir(self.library, "Caches/com.somebody.else")
        out = self._run(path, [rogue])
        self.assertFalse(out["ok"])
        self.assertIn("不在该应用的残留清单内", out["error"])
        self.assertTrue(os.path.isdir(rogue))

    def test_refuses_running_app(self):
        path, _ = self._demo()
        out = self._run(path, [], running_checker=lambda p: True)
        self.assertFalse(out["ok"])
        self.assertIn("正在运行", out["error"])
        self.assertTrue(os.path.exists(path))

    def test_moves_app_and_leftovers_to_trash(self):
        path, left = self._demo()
        out = self._run(path, [left])
        self.assertTrue(out["ok"], out)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(left))
        moved = sorted(os.path.basename(m["to"]) for m in out["moved"])
        self.assertEqual(len(moved), 2)
        # 文件名带可辨识前缀，方便用户在废纸篓里找回
        self.assertTrue(all(n.startswith("uninstall__") for n in moved), moved)

    def test_app_moved_with_app_suffix_intact(self):
        """应用本体进废纸篓后仍要能双击恢复，.app 后缀不能丢。"""
        path, _ = self._demo()
        out = self._run(path, [])
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["moved"][0]["to"].endswith(".app"))

    def test_writes_audit_log(self):
        path, _ = self._demo()
        log = os.path.join(self.tmp, "audit.log")
        out = self._run(path, [], audit_log=log)
        self.assertTrue(out["ok"], out)
        with open(log, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("com.example.demo", content)

    def test_duplicate_leftovers_moved_once(self):
        """回归：同一路径重复传入时只移动一次，第二次不应报错。"""
        path, left = self._demo()
        out = self._run(path, [left, left, left])
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(out["moved"]), 2)   # 应用本体 + 残留各一次
        self.assertEqual(out["failed"], [])

    def test_symlink_leftover_not_followed(self):
        """回归：残留是指向外部目录的软链时，绝不顺着软链删掉目标内容。

        find_leftovers 会跳过软链（体积统计不可靠且容易误导），
        因此软链不会出现在允许集合里，卸载应被拒绝、外部目录原封不动。
        """
        path = make_app(self.apps_root, "Demo", "com.example.demo")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "precious.txt"), "wb") as f:
            f.write(b"z" * 1024)
        link = os.path.join(self.library, "Caches", "com.example.demo")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(outside, link)

        out = self._run(path, [link])
        self.assertFalse(out["ok"])
        # 目标目录里的内容必须原封不动
        self.assertTrue(os.path.isfile(os.path.join(outside, "precious.txt")))
        # 应用本体也不能在「残留校验失败」时被顺带删掉
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
