#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP 层安全测试
================
覆盖 server.py 的请求来源校验：
  - 只接受本机 Origin / Host（防 CSRF 与 DNS rebinding）
  - 只接受 application/json（迫使跨源请求触发预检而失败）
  - 校验失败时不得执行任何文件操作

对应需求：cleanMyMac 卸载应用（v1.1.0）
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


class ServerCase(unittest.TestCase):
    """在随机端口起一个真实的 HTTP 服务，用真实请求验证门禁。"""

    @classmethod
    def setUpClass(cls):
        # 审计日志改到临时文件：否则跑测试会往仓库内的 deletes.log 写垃圾记录
        cls._tmpdir = tempfile.mkdtemp(prefix="cmm-http-")
        cls._orig_audit = server.AUDIT_LOG
        server.AUDIT_LOG = os.path.join(cls._tmpdir, "deletes.log")

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.AUDIT_LOG = cls._orig_audit
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def post(self, path, body, headers=None):
        """发一个原始 POST，返回 (status, 解析后的 JSON 或 None)。"""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, None


class TestOriginGate(ServerCase):
    """跨源请求必须在进入业务逻辑之前被拒绝。"""

    def test_cross_origin_rejected(self):
        """回归：任意网页都能用 text/plain 触发卸载（CORS simple request 不预检）。"""
        status, body = self.post(
            "/api/uninstall", {"app": "/Applications/Ghost.app", "leftovers": []},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403, body)
        self.assertIn("来源", body.get("error", ""))

    def test_cross_origin_rejected_for_trash(self):
        status, _ = self.post(
            "/api/trash", {"ids": ["npm-cacache"]},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_localhost_origin_allowed(self):
        """本机页面自己发起的请求必须放行（否则 UI 无法工作）。"""
        for origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"):
            status, body = self.post(
                "/api/uninstall", {"app": "/Applications/Ghost.app", "leftovers": []},
                headers={"Origin": origin})
            # 放行后走到业务门禁，报「未知应用」而不是 403
            self.assertEqual(status, 200, body)
            self.assertFalse(body["ok"])

    def test_foreign_host_rejected(self):
        """DNS rebinding：Host 头不是本机时拒绝。"""
        status, body = self.post(
            "/api/uninstall", {"app": "/Applications/Ghost.app", "leftovers": []},
            headers={"Host": "evil.example"})
        self.assertEqual(status, 403, body)

    def test_non_json_content_type_rejected(self):
        """text/plain 属于 CORS simple request，不触发预检，必须显式拒绝。"""
        data = json.dumps({"app": "/Applications/Ghost.app", "leftovers": []}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/uninstall", data=data, method="POST")
        req.add_header("Content-Type", "text/plain")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, body = r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            status, body = e.code, json.loads(e.read() or b"{}")
        self.assertEqual(status, 415, body)


    def test_app_icon_missing_path(self):
        """缺少 path 参数应返回 400。"""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/app-icon")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 400)

    def test_app_icon_unauthorized_path(self):
        """试图读取非扫描范围或非应用路径应返回 403。"""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/app-icon?path=/etc/shadow")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
