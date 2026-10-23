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


class TestIsRunning(unittest.TestCase):
    def test_regex_metachar_path_is_conservative(self):
        """回归：路径含正则元字符时 pgrep 会以退出码 2 失败。

        原实现只看 stdout，于是把「探测失败」当成「未运行」，
        正在运行的 C++ / [beta] 之类应用会被误判为可卸载。
        """
        # 造一个名字带 + 的路径；pgrep 会把它当 ERE 而报错
        self.assertTrue(U.is_running("/Applications/C++.app"))

    def test_missing_pgrep_is_conservative(self):
        """pgrep 不存在时也应当保守地认为「可能在运行」。"""
        original = U.subprocess.run

        def boom(*a, **kw):
            raise OSError("no pgrep")

        U.subprocess.run = boom
        try:
            self.assertTrue(U.is_running("/Applications/Demo.app"))
        finally:
            U.subprocess.run = original


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

    def test_bundle_id_requires_start_boundary_too(self):
        """回归：org.other.com.example.demo.tool 不能判成 bundle 级。

        起点也必须是组件边界——否则别人的 Bundle ID 恰好把本应用 ID
        夹在中间时，无关数据会被默认勾选。
        """
        self.assertNotEqual(
            U.classify_leftover("org.other.com.example.demo.tool", self.app, set()),
            "bundle")

    def test_other_app_bundle_id_excluded(self):
        """回归：本应用不得认领「另一个已安装应用」的容器。

        真实案例：两家厂商的 Bundle ID 互相包含，按名字匹配时 A 会命中
        B 的共享容器（B 的 ID 里恰好含有 A 的名字）。
        """
        app = {"name": "Alpha", "bundle_id": "com.acme.alpha"}
        others = {"com.acme.beta.mac"}
        self.assertIsNone(
            U.classify_leftover("com.acme.beta.mac.AlphaShare", app, others))

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
    def test_sibling_product_does_not_claim_vendor_child(self):
        """回归：同厂商的兄弟产品不能认领对方的厂商子目录。

        两个同厂商产品同时装着时，例如 Chrome Remote Desktop 与 Google Chrome，
        Application Support/Google/Chrome 属于后者，前者不能以 fuzzy 认领——
        用户勾上后卸载就会删掉另一个产品的 profile。
        """
        make_dir(self.library, "Application Support/Google/Chrome")
        sibling = {"name": "Chrome Remote Desktop",
                   "bundle_id": "com.google.chrome.remote_desktop"}
        others = {"com.google.Chrome"}

        found = U.find_leftovers(sibling, self.library, others)
        chrome = os.path.join(self.library, "Application Support/Google/Chrome")
        self.assertNotIn(chrome, {f["path"] for f in found})

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
        make_dir(self.library, "Containers/com.acme.beta.mac.AlphaShare")
        app = {"name": "Alpha", "bundle_id": "com.acme.alpha"}
        found = U.find_leftovers(app, self.library, {"com.acme.beta.mac"})
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

    def test_scan_path_round_trips_into_uninstall(self):
        """回归：scan_apps 返回的 path 必须能原样回传给 uninstall。

        曾因对外给 ~ 相对路径、而门禁用 realpath 比较（不展开 ~），
        导致 ~/Applications 下的应用全部无法卸载。
        """
        path = make_app(self.apps_root, "Demo", "com.example.demo")
        out = U.scan_apps([self.apps_root], self.library, running_checker=lambda p: False)
        api_path = out["apps"][0]["path"]

        result = U.uninstall(
            api_path, [], roots=[self.apps_root], library=self.library,
            trash_dir=self.trash, running_checker=lambda p: False, audit_log=None,
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(os.path.exists(path))

    def test_corrupt_plist_does_not_break_scan(self):
        """回归：某个应用的 Info.plist 损坏时，不能拖垮整个扫描。

        截断的 XML 抛 ExpatError，它不是 ValueError，原实现的 except 捕获不到。
        """
        make_app(self.apps_root, "Good", "com.example.good")
        bad = os.path.join(self.apps_root, "Bad.app", "Contents")
        os.makedirs(bad)
        with open(os.path.join(bad, "Info.plist"), "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0"?><plist><dict><key>a</key>')

        out = U.scan_apps([self.apps_root], self.library, running_checker=lambda p: False)
        self.assertEqual([a["bundle_id"] for a in out["apps"]], ["com.example.good"])


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
        # 必须显式指定临时审计日志：不传会回落到仓库内的 deletes.log，
        # 让跑测试变成往生产审计日志里写垃圾记录。
        kw.setdefault("audit_log", os.path.join(self.tmp, "audit.log"))
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

    def test_app_failure_is_not_reported_as_success(self):
        """回归：应用本体没搬走、只有残留成功时，不能报 ok。

        前端只判断 ok，会把该行从列表里移除并提示「已卸载」，
        但应用其实还在——用户以为删掉了。
        """
        path, left = self._demo()
        os.chmod(self.apps_root, 0o500)   # 让应用本体无法移动
        try:
            out = self._run(path, [left])
        finally:
            os.chmod(self.apps_root, 0o700)
        self.assertFalse(out["ok"])
        self.assertTrue(os.path.exists(path))
        self.assertTrue(out["partial"], "应标记为部分完成")

    def test_same_name_leftovers_do_not_collide_in_trash(self):
        """回归：三个同名残留曾因秒级时间戳相同而在废纸篓里被合并成一个。

        合并后拖回恢复会放错位置，审计日志也看不出发生过合并。
        """
        path = make_app(self.apps_root, "Demo", "com.example.demo")
        leftovers = []
        for sub in ("Caches", "Containers", "WebKit"):
            d = make_dir(self.library, f"{sub}/Demo")
            leftovers.append(d)

        out = self._run(path, leftovers)
        self.assertTrue(out["ok"], out)
        # 应用本体 + 3 条残留 = 4 个独立条目，一个都不能少
        self.assertEqual(len(os.listdir(self.trash)), 4, sorted(os.listdir(self.trash)))
        self.assertEqual(out["failed"], [])

    def test_trash_target_stays_inside_trash(self):
        """回归：应用名含 / 或 .. 时，废纸篓目标路径必须仍落在废纸篓内。"""
        path = make_app(self.apps_root, "Evil", "com.evil.x")
        # 直接改 Info.plist 里的名字，模拟恶意 bundle
        import plistlib as _pl
        info = os.path.join(path, "Contents", "Info.plist")
        with open(info, "wb") as f:
            _pl.dump({"CFBundleIdentifier": "com.evil.x",
                      "CFBundleName": "../../escape/name"}, f)
        left = make_dir(self.library, "Caches/com.evil.x")

        out = self._run(path, [left])
        trash_real = os.path.realpath(self.trash)
        for m in out["moved"]:
            self.assertTrue(
                os.path.realpath(m["to"]).startswith(trash_real + os.sep),
                m["to"])
        # 废纸篓之外不能凭空多出目录（audit.log 是本用例自己的审计日志）
        self.assertEqual(
            sorted(os.listdir(self.tmp)),
            ["Applications", "Library", "Trash", "audit.log"], os.listdir(self.tmp))

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


    def test_is_admin_required(self):
        """测试管理员权限判定。"""
        import uninstaller
        # 不存在的路径返回 False
        self.assertFalse(uninstaller.is_admin_required("/tmp/non_existent.app"))
        # 临时目录创建的应用，当前用户所有且有写权限 -> False
        path = make_app(self.apps_root, "AdminTest", "com.example.admin")
        self.assertFalse(uninstaller.is_admin_required(path))
        # 移除写权限后 -> True
        try:
            os.chmod(path, 0o555)
            self.assertTrue(uninstaller.is_admin_required(path))
        finally:
            os.chmod(path, 0o755)

    def test_get_app_icon_invalid_path(self):
        """非法路径读取图标应返回 None，不抛出异常。"""
        import uninstaller
        self.assertIsNone(uninstaller.get_app_icon_png("/tmp/non_existent.app"))
        self.assertIsNone(uninstaller.get_app_icon_png(self.tmp))

    def test_scan_apps_includes_needs_admin(self):
        """扫描结果中必须包含 needs_admin 字段。"""
        import uninstaller
        path = make_app(self.apps_root, "AdminTest2", "com.example.admin2")
        res = uninstaller.scan_apps(roots=[self.apps_root], library=self.library)
        self.assertEqual(len(res["apps"]), 1)
        self.assertIn("needs_admin", res["apps"][0])
        self.assertIsInstance(res["apps"][0]["needs_admin"], bool)


if __name__ == "__main__":
    unittest.main(verbosity=2)
