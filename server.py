#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地清理服务
============
职责：
  - 提供静态 UI
  - GET  /api/scan    → 实时扫描结果（只读）
  - POST /api/trash   → 把选中的目标移入系统废纸篓

安全设计（这是本项目最关键的部分）：
  1. 只接受 TARGETS 中登记过的 id，无法通过任意路径删除
  2. risk == "keep" 的目标一律拒绝
  3. 展开后的每个真实路径都必须落在 ALLOWED_ROOTS 之内
  4. 命中 PROTECTED_PATHS 一律拒绝
  5. 删除方式为「移入 ~/.Trash」，可随时恢复，绝不使用 rm
  6. 每次操作写入 deletes.log 审计

对应需求：cleanMyMac 本地空间分析与清理 UI
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scanner import all_targets, expand, scan  # noqa: E402

HOME = os.path.expanduser("~")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TRASH_DIR = os.path.join(HOME, ".Trash")
AUDIT_LOG = os.path.join(BASE_DIR, "deletes.log")

PORT = int(os.environ.get("CMM_PORT", "8765"))

# 允许操作的范围：用户家目录 + 当前用户的临时目录
ALLOWED_ROOTS = [
    HOME,
    os.path.realpath(tempfile.gettempdir()),
]

# 永久保护：即使误配到 TARGETS 也不允许触碰。
# 只列通用路径，不出现具体用户名或特定项目名——否则开源后会误导使用者。
PROTECTED_PATHS = [
    os.path.join(HOME, p) for p in (
        "",                      # 家目录本身（子路径不受此条影响，见 is_protected）
        ".ssh", ".gnupg", ".gitconfig", ".zshrc", ".bashrc", ".profile",
        "Documents", "Desktop", "Pictures", "Movies", "Music",
        "Downloads",             # 下载目录可能含未归档的重要文件
        "Library/Keychains",
        ".claude", ".cursor",    # AI 工具会话与配置
    )
] + [
    "/Applications", "/System", "/Library", "/usr", "/bin", "/sbin",
    "/etc", "/private/etc", "/opt",
]


def _norm(path: str) -> str:
    """规范化路径：展开 ~、解析符号链接、去掉尾部斜杠。

    统一用 realpath 比较，可防止通过软链接绕过保护名单。
    """
    return os.path.realpath(os.path.expanduser(path)).rstrip("/") or "/"


def is_protected(path: str) -> bool:
    """判断路径是否命中保护名单（含父子关系）。

    注意：家目录本身必须保护，但**不能**把家目录当作前缀去保护它的所有子路径，
    否则 ~/.npm/_logs 之类的合法目标会被全部误杀（曾出现过该缺陷）。
    """
    p = _norm(path)
    home = _norm(HOME)

    # 家目录自身不可删
    if p == home:
        return True

    for prot in PROTECTED_PATHS:
        prot = _norm(prot)
        # 家目录只做「相等」判断，跳过前缀匹配
        if prot == home:
            continue
        if p == prot or p.startswith(prot + "/"):
            return True
    return False


def is_allowed(path: str) -> bool:
    """判断路径是否在允许操作范围内。"""
    p = _norm(path)
    for root in ALLOWED_ROOTS:
        root = _norm(root)
        # 必须是 root 的子路径，且不能等于 root 本身
        if p.startswith(root + "/"):
            return True
    return False


def unique_trash_path(name: str) -> str:
    """在废纸篓中生成不冲突的目标路径。"""
    candidate = os.path.join(TRASH_DIR, name)
    if not os.path.exists(candidate):
        return candidate
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(TRASH_DIR, f"{name} {stamp}")


def audit(entry: dict) -> None:
    """写审计日志，失败不影响主流程。"""
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def trash_items(ids: list[str]) -> dict:
    """把指定 id 对应的路径移入废纸篓，返回逐项结果。"""
    # 必须用 all_targets()：仅用内置 TARGETS 会把 targets.local.json 里的目标判成「未知目标」
    targets = {t["id"]: t for t in all_targets()}
    os.makedirs(TRASH_DIR, exist_ok=True)

    results = []
    for item_id in ids:
        t = targets.get(item_id)
        if not t:
            results.append({"id": item_id, "ok": False, "error": "未知目标"})
            continue
        if t["risk"] == "keep":
            results.append({"id": item_id, "ok": False, "error": "该目标被标记为不可删除"})
            continue

        # 展开真实路径（glob 目标可能匹配多个）
        real_paths: list[str] = []
        for pattern in t["paths"]:
            real_paths.extend(expand(pattern))

        if not real_paths:
            results.append({"id": item_id, "ok": False, "error": "路径不存在"})
            continue

        moved, failed = 0, []
        for p in real_paths:
            # 三重校验：允许范围 → 保护名单 → 存在性
            if not is_allowed(p):
                failed.append(f"{p}: 不在允许范围")
                continue
            if is_protected(p):
                failed.append(f"{p}: 命中保护名单")
                continue
            if not os.path.exists(p):
                continue
            try:
                # 废纸篓中带上目标 id 前缀，便于用户日后辨认与恢复
                base = os.path.basename(p.rstrip("/")) or "item"
                dst = unique_trash_path(f"{item_id}__{base}")
                shutil.move(p, dst)
                moved += 1
                audit({
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "id": item_id, "from": p, "to": dst, "result": "ok",
                })
            except OSError as e:
                failed.append(f"{p}: {e}")
                audit({
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "id": item_id, "from": p, "result": "fail", "error": str(e),
                })

        results.append({
            "id": item_id, "ok": moved > 0, "moved": moved,
            "failed": failed,
        })

    return {"results": results}


# ---------------------------------------------------------------------------
# 扫描结果短缓存
# 每次扫描要并发跑几十次 du，耗时约 10 秒。用户连点「重新扫描」会同时触发
# 多轮扫描把机器拖住，因此这里做两件事：
#   1. 用锁串行化扫描，避免并发重扫
#   2. 5 秒内复用同一份结果（force=1 时强制重扫，用于删除后刷新）
# ---------------------------------------------------------------------------
_SCAN_TTL = 5.0
_scan_cache: dict[str, object] = {"at": 0.0, "data": None}
_scan_lock = threading.Lock()


def get_scan(force: bool = False) -> dict:
    """返回扫描结果，必要时重新扫描。"""
    with _scan_lock:
        now = time.time()
        if not force and _scan_cache["data"] is not None and now - _scan_cache["at"] < _SCAN_TTL:
            return _scan_cache["data"]  # type: ignore[return-value]
        data = scan()
        _scan_cache["at"] = time.time()
        _scan_cache["data"] = data
        return data


class Handler(BaseHTTPRequestHandler):
    server_version = "CleanMyMacUI/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/scan":
            # force=1 时跳过缓存（删除后刷新、手动重新扫描）
            force = parse_qs(parsed.query).get("force", ["0"])[0] in ("1", "true", "yes")
            try:
                self._json(200, get_scan(force))
            except Exception as e:  # 扫描失败也要给前端可读信息
                self._json(500, {"error": str(e)})
            return

        if path in ("/", "/index.html"):
            file_path = os.path.join(STATIC_DIR, "index.html")
            ctype = "text/html; charset=utf-8"
        else:
            # 只允许访问 static 下的文件，防止路径穿越
            safe = os.path.normpath(path).lstrip("/")
            file_path = os.path.join(STATIC_DIR, safe)
            if not os.path.abspath(file_path).startswith(STATIC_DIR):
                self._send(403, b"forbidden", "text/plain")
                return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(os.path.splitext(file_path)[1], "application/octet-stream")

        if not os.path.isfile(file_path):
            self._send(404, b"not found", "text/plain")
            return
        with open(file_path, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_POST(self):
        if urlparse(self.path).path != "/api/trash":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "请求格式错误"})
            return

        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not ids:
            self._json(400, {"error": "未选择任何项目"})
            return
        if len(ids) > 50:
            self._json(400, {"error": "单次最多处理 50 项"})
            return

        started = time.time()
        self._json(200, {
            **trash_items(ids),
            "elapsed": round(time.time() - started, 2),
        })


def main() -> None:
    print(f"清理面板已启动: http://127.0.0.1:{PORT}")
    print(f"废纸篓目录: {TRASH_DIR}")
    print(f"审计日志: {AUDIT_LOG}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
