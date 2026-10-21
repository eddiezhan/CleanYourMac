#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
磁盘空间扫描器（只读）
======================
职责：
  1. 读取磁盘容量
  2. 按「配置驱动」的目标清单逐个测量体积
  3. 输出结构化 JSON 供前端 UI 消费

安全约束（重要）：
  - 本模块**只做读取与统计**，绝不删除、移动、修改任何文件
  - 所有可操作目标必须显式登记在 TARGETS 中，不存在「扫描到就删」的路径

扩展方式：
  内置清单覆盖 macOS 上通用的缓存/日志/构建产物。个人环境特有的目标
  （小众工具、自建服务、特定项目）请写入同目录的 `targets.local.json`，
  该文件默认被 .gitignore 忽略，不会随仓库分发。格式见 targets.local.example.json。

对应需求：cleanMyMac 本地空间分析与清理 UI
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

HOME = os.path.expanduser("~")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 用户临时目录（macOS 每用户独立），动态获取而非硬编码
_TMP_ROOT = os.path.realpath(tempfile.gettempdir())


def du_bytes(path: str) -> int:
    """测量路径占用字节数。

    优先使用系统 du（快，且能正确处理硬链接去重）；
    路径不存在或无权限时返回 0，不抛异常。
    """
    if not os.path.exists(path) and not os.path.islink(path):
        return 0
    try:
        out = subprocess.run(
            ["du", "-sk", path],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode == 0 and out.stdout.strip():
            # du -sk 返回的是 1024 字节块数
            return int(out.stdout.split()[0]) * 1024
    except (subprocess.SubprocessError, ValueError):
        pass
    # 兜底：Python 递归统计（慢但可靠）
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def expand(pattern: str) -> list[str]:
    """展开路径模式，返回实际存在的路径列表（支持 ~ 与通配符）。"""
    p = os.path.expanduser(pattern)
    if any(c in p for c in "*?["):
        return sorted(glob.glob(p))
    return [p] if os.path.exists(p) else []


# ---------------------------------------------------------------------------
# 内置目标清单：每项都经过人工判定，写明「能不能删、为什么」
# risk: safe(可安全删除) / caution(需确认，可能影响功能) / keep(不要删，仅展示)
#
# 新增条目的硬性要求：
#   1. paths 必须是 ~ 相对路径或系统路径，禁止出现具体用户名
#   2. desc 必须说明「删了会怎样」，而不是只描述它是什么
#   3. 拿不准的一律标 caution，不要标 safe
# ---------------------------------------------------------------------------
TARGETS: list[dict[str, Any]] = [
    # ======================= 开发缓存（可重建，safe）=======================
    {
        "id": "npm-cacache", "name": "npm 包缓存", "category": "开发缓存",
        "paths": ["~/.npm/_cacache"], "risk": "safe",
        "desc": "npm 下载过的包副本。删除后 npm install 会重新下载，功能不受影响。",
    },
    {
        "id": "npm-npx", "name": "npx 临时包", "category": "开发缓存",
        "paths": ["~/.npm/_npx"], "risk": "safe",
        "desc": "npx 运行时缓存的临时工具包，下次使用会自动重装。",
    },
    {
        "id": "pnpm-store", "name": "pnpm 全局存储", "category": "开发缓存",
        "paths": ["~/Library/pnpm", "~/.local/share/pnpm/store"], "risk": "safe",
        "desc": "pnpm content-addressable 存储。删除后已用 pnpm 的项目需重新 install。",
    },
    {
        "id": "yarn-cache", "name": "Yarn 缓存", "category": "开发缓存",
        "paths": ["~/Library/Caches/Yarn", "~/.cache/yarn"], "risk": "safe",
        "desc": "Yarn 包缓存，删除后重新 install 会自动下载。",
    },
    {
        "id": "pip-cache", "name": "pip 缓存", "category": "开发缓存",
        "paths": ["~/Library/Caches/pip", "~/.cache/pip"], "risk": "safe",
        "desc": "pip 下载的 wheel 缓存，可安全清理。",
    },
    {
        "id": "uv-cache", "name": "uv Python 缓存", "category": "开发缓存",
        "paths": ["~/.cache/uv"], "risk": "safe",
        "desc": "uv 的 wheel 与源码构建缓存。删除后首次安装变慢，不影响已装环境。",
    },
    {
        "id": "poetry-cache", "name": "Poetry 缓存", "category": "开发缓存",
        "paths": ["~/Library/Caches/pypoetry", "~/.cache/pypoetry"], "risk": "safe",
        "desc": "Poetry 的依赖包缓存，可安全删除。",
    },
    {
        "id": "cargo-registry", "name": "Cargo 依赖缓存", "category": "开发缓存",
        "paths": ["~/.cargo/registry/cache", "~/.cargo/registry/src"], "risk": "safe",
        "desc": "Rust 依赖源码与包缓存。删除后 cargo build 会重新下载编译。",
    },
    {
        "id": "go-modcache", "name": "Go 模块缓存", "category": "开发缓存",
        "paths": ["~/go/pkg/mod/cache"], "risk": "safe",
        "desc": "Go module 下载缓存，删除后 go mod download 会重新拉取。",
    },
    {
        "id": "gradle-cache", "name": "Gradle 缓存", "category": "开发缓存",
        "paths": ["~/.gradle/caches"], "risk": "safe",
        "desc": "Gradle 依赖与构建缓存。删除后首次构建变慢。",
    },
    {
        "id": "maven-repo", "name": "Maven 本地仓库", "category": "开发缓存",
        "paths": ["~/.m2/repository"], "risk": "safe",
        "desc": "Maven 依赖缓存，删除后重新构建会再次下载。",
    },
    {
        "id": "node-gyp", "name": "node-gyp 头文件缓存", "category": "开发缓存",
        "paths": ["~/Library/Caches/node-gyp", "~/.cache/node-gyp"], "risk": "safe",
        "desc": "编译原生模块时下载的 Node 头文件，可安全删除。",
    },
    {
        "id": "playwright-cache", "name": "Playwright 浏览器缓存", "category": "开发缓存",
        "paths": ["~/Library/Caches/ms-playwright", "~/.cache/ms-playwright"], "risk": "safe",
        "desc": "自动化测试用的浏览器二进制。删除后需重新执行 playwright install。",
    },
    {
        "id": "deno-cache", "name": "Deno 缓存", "category": "开发缓存",
        "paths": ["~/Library/Caches/deno", "~/.cache/deno"], "risk": "safe",
        "desc": "Deno 的模块与编译缓存，可安全删除。",
    },

    # ======================= 构建产物 =======================
    {
        "id": "xcode-derived", "name": "Xcode 编译中间产物", "category": "构建产物",
        "paths": ["~/Library/Developer/Xcode/DerivedData"], "risk": "safe",
        "desc": "索引与编译缓存。删除后首次编译变慢，不影响源码与已打包产物。",
    },
    {
        "id": "xcode-archives", "name": "Xcode 归档包", "category": "构建产物",
        "paths": ["~/Library/Developer/Xcode/Archives"], "risk": "caution",
        "desc": "用于上架与符号化崩溃日志的历史归档。删除后无法再上传旧版本或解析其崩溃报告。",
    },
    {
        "id": "xcode-devicesupport", "name": "iOS 设备支持文件", "category": "构建产物",
        "paths": ["~/Library/Developer/Xcode/iOS DeviceSupport"], "risk": "caution",
        "desc": "真机调试所需的系统符号文件。删除后重新连接设备会自动生成。",
    },
    {
        "id": "android-sdk", "name": "Android SDK / 模拟器", "category": "构建产物",
        "paths": ["~/Library/Android"], "risk": "caution",
        "desc": "SDK 与模拟器镜像。不做 Android 开发时可删，重建需重新下载数 GB。",
    },
    {
        "id": "simulator-runtime", "name": "iOS 模拟器数据", "category": "构建产物",
        "paths": ["~/Library/Developer/CoreSimulator/Caches",
                  "~/Library/Developer/CoreSimulator/Devices"], "risk": "caution",
        "desc": "模拟器缓存与设备实例。删除会清空模拟器内的应用与数据。",
    },

    # ======================= 浏览器缓存 =======================
    {
        "id": "chrome-cache", "name": "Chrome 缓存", "category": "应用缓存",
        "paths": ["~/Library/Caches/Google"], "risk": "safe",
        "desc": "网页缓存与代码缓存。删除后首次访问网站稍慢，登录状态保留。",
    },
    {
        "id": "safari-cache", "name": "Safari 缓存", "category": "应用缓存",
        "paths": ["~/Library/Caches/com.apple.Safari"], "risk": "safe",
        "desc": "Safari 网页缓存，可安全删除。",
    },
    {
        "id": "firefox-cache", "name": "Firefox 缓存", "category": "应用缓存",
        "paths": ["~/Library/Caches/Firefox"], "risk": "safe",
        "desc": "Firefox 网页缓存，可安全删除。",
    },
    {
        "id": "edge-cache", "name": "Edge 缓存", "category": "应用缓存",
        "paths": ["~/Library/Caches/Microsoft Edge"], "risk": "safe",
        "desc": "Edge 网页缓存，可安全删除。",
    },

    # ======================= 日志与临时文件 =======================
    {
        "id": "user-logs", "name": "用户应用日志", "category": "日志",
        "paths": ["~/Library/Logs"], "risk": "safe",
        "desc": "应用运行日志，仅用于排障。删除后应用会重新生成。",
    },
    {
        "id": "diagnostic-reports", "name": "崩溃报告", "category": "日志",
        "paths": ["~/Library/Logs/DiagnosticReports"], "risk": "safe",
        "desc": "历史崩溃与卡顿报告。如需保留排障线索请先备份。",
    },
    {
        "id": "pytest-tmpdir", "name": "pytest 临时目录", "category": "临时文件",
        "paths": [f"{_TMP_ROOT}/pytest-of-*"], "risk": "safe",
        "desc": "pytest 为当前用户创建的临时工作目录，测试结束后不再需要。",
    },

    # ======================= 应用缓存与数据 =======================
    {
        "id": "user-caches", "name": "全量用户缓存", "category": "应用缓存",
        "paths": ["~/Library/Caches"], "risk": "caution",
        "desc": "所有应用的缓存总目录。体积最大但内容混杂，"
                "建议优先清理上面的单项，而非整体删除。",
    },
    {
        "id": "trash", "name": "废纸篓", "category": "临时文件",
        "paths": ["~/.Trash"], "risk": "caution",
        "desc": "废纸篓中的文件。清空后无法恢复，请确认其中没有待找回的内容。",
    },
    {
        "id": "telegram-data", "name": "Telegram 本地数据", "category": "应用数据",
        "paths": ["~/Library/Application Support/Telegram Desktop"], "risk": "caution",
        "desc": "聊天缓存与媒体文件。删除后需重新从服务器同步。",
    },
    {
        "id": "slack-data", "name": "Slack 本地数据", "category": "应用数据",
        "paths": ["~/Library/Application Support/Slack"], "risk": "caution",
        "desc": "消息缓存与工作区数据。删除后需重新登录并同步。",
    },
    {
        "id": "discord-data", "name": "Discord 本地数据", "category": "应用数据",
        "paths": ["~/Library/Application Support/discord"], "risk": "caution",
        "desc": "消息与媒体缓存，删除后需重新同步。",
    },

    # ======================= AI 编码工具（主流，通用）=======================
    {
        "id": "ai-tool-cache", "name": "AI 编码工具缓存与日志", "category": "日志",
        "paths": ["~/.cache/codex-runtimes", "~/Library/Logs/Claude",
                  "~/.cache/claude"], "risk": "safe",
        "desc": "AI 助手运行时缓存与运行日志，仅用于排障，可清理。",
    },

    # ======================= 不要删：仅展示 =======================
    {
        "id": "keep-documents", "name": "文档", "category": "用户数据",
        "paths": ["~/Documents"], "risk": "keep",
        "desc": "个人文档目录。",
    },
    {
        "id": "keep-desktop", "name": "桌面", "category": "用户数据",
        "paths": ["~/Desktop"], "risk": "keep",
        "desc": "桌面文件。",
    },
    {
        "id": "keep-pictures", "name": "图片", "category": "用户数据",
        "paths": ["~/Pictures"], "risk": "keep",
        "desc": "照片图库与图片文件。",
    },
    {
        "id": "keep-movies", "name": "影片", "category": "用户数据",
        "paths": ["~/Movies"], "risk": "keep",
        "desc": "视频文件。",
    },
    {
        "id": "keep-music", "name": "音乐", "category": "用户数据",
        "paths": ["~/Music"], "risk": "keep",
        "desc": "音乐资料库。",
    },
    {
        "id": "keep-ssh", "name": "SSH 密钥", "category": "安全凭据",
        "paths": ["~/.ssh"], "risk": "keep",
        "desc": "远程登录密钥，删除将导致无法连接服务器。",
    },
    {
        "id": "keep-keychain", "name": "钥匙串", "category": "安全凭据",
        "paths": ["~/Library/Keychains"], "risk": "keep",
        "desc": "系统密码与证书存储。",
    },
    {
        "id": "keep-apps", "name": "已安装应用", "category": "应用程序",
        "paths": ["/Applications"], "risk": "keep",
        "desc": "应用本体。如需卸载请用应用自带卸载器，不要直接删除目录。",
    },
]


def load_local_targets() -> list[dict[str, Any]]:
    """加载 targets.local.json 中的自定义目标。

    该文件不入仓库，用于登记个人环境特有目标。任何解析异常都只警告，
    不影响内置清单工作——扫描器不应因为一个配置笔误而整体失败。
    """
    path = os.path.join(BASE_DIR, "targets.local.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("targets", data) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise ValueError("targets 必须是数组")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"[warn] targets.local.json 解析失败，已忽略：{e}", file=sys.stderr)
        return []

    valid = []
    for t in entries:
        if not isinstance(t, dict):
            continue
        if not {"id", "name", "paths"} <= t.keys():
            print(f"[warn] 跳过缺少 id/name/paths 的条目：{t}", file=sys.stderr)
            continue
        # 补齐可选字段默认值，降低手写配置的出错概率
        t.setdefault("category", "自定义")
        t.setdefault("risk", "caution")   # 自定义项默认「需确认」，宁严勿松
        t.setdefault("desc", "自定义目标（来自 targets.local.json）")
        valid.append(t)
    return valid


def all_targets() -> list[dict[str, Any]]:
    """内置清单 + 本地扩展。id 冲突时以本地配置为准，便于覆盖默认判定。"""
    local = load_local_targets()
    local_ids = {t["id"] for t in local}
    return [t for t in TARGETS if t["id"] not in local_ids] + local


def scan() -> dict[str, Any]:
    """执行扫描，返回结构化结果。

    各目标之间相互独立，使用线程池并发测量（du 为 IO 密集型），
    总耗时约等于最慢的单个目标，而非全部相加。
    """
    usage = shutil.disk_usage(HOME)

    def measure(t: dict[str, Any]) -> dict[str, Any] | None:
        """测量单个目标，返回条目或 None（体积为 0 时跳过）。"""
        matched: list[str] = []
        for pattern in t["paths"]:
            matched.extend(expand(pattern))

        total = 0
        detail: list[dict[str, Any]] = []
        for p in matched:
            size = du_bytes(p)
            if size <= 0:
                continue
            total += size
            # 只保留 ~ 相对路径：输出文件可能被分享或截图，不落绝对路径
            # （删除时 server.py 用 expand() 重新展开，不依赖这里的结果）
            detail.append({
                "path": p.replace(HOME, "~"),
                "size": size,
            })

        if total <= 0:
            return None

        return {
            "id": t["id"],
            "name": t["name"],
            "category": t["category"],
            "risk": t["risk"],
            "desc": t["desc"],
            "size": total,
            "paths": [d["path"] for d in detail],
            "file_count": len(detail),
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        measured = list(pool.map(measure, all_targets()))

    items = [m for m in measured if m]
    # 按体积降序，前端直接渲染
    items.sort(key=lambda x: x["size"], reverse=True)

    def sum_risk(risk: str) -> int:
        return sum(i["size"] for i in items if i["risk"] == risk)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "disk": {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round(usage.used / usage.total * 100, 1),
        },
        "summary": {
            "safe": sum_risk("safe"),
            "caution": sum_risk("caution"),
            "keep": sum_risk("keep"),
        },
        "items": items,
    }


if __name__ == "__main__":
    result = scan()
    out_path = os.path.join(BASE_DIR, "data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({
        "out": out_path,
        "safe_gb": round(result["summary"]["safe"] / 2**30, 2),
        "caution_gb": round(result["summary"]["caution"] / 2**30, 2),
        "items": len(result["items"]),
    }, ensure_ascii=False, indent=2))
