#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应用卸载器
==========
职责：
  1. 枚举已安装的 .app，读取 Bundle ID / 名称 / 版本 / 体积
  2. 发现该应用散落在 ~/Library 下的残留数据，并按置信度分级
  3. 把「应用本体 + 用户确认的残留」整体移入废纸篓

安全约束（与 scanner/server 一致：宁可少删，不可错删）：
  - 只接受扫描结果中真实存在的应用路径，服务端重新枚举校验，
    绝不接受前端传来的任意路径
  - com.apple.* 系统应用一律拒绝卸载
  - 正在运行的应用一律拒绝卸载
  - 残留路径必须是「本次重新计算出的集合」的子集，越权一律拒绝
  - 全程使用 shutil.move 移入废纸篓，**绝不使用 rm**
  - 每次操作写审计日志

对应需求：cleanMyMac 应用卸载（含残留数据）

残留匹配的三级置信度（为什么需要分级）：
  bundle  目录项包含本应用的 Bundle ID —— 最可靠，默认勾选
  name    目录项名与应用名完全一致 —— 可靠，默认勾选
  fuzzy   名字包含、或厂商两级嵌套（如 Application Support/<厂商>/<产品>）
          —— 可能误伤，**默认不勾选**，必须用户手动确认

  另外，命中「另一个已安装应用的 Bundle ID」的目录项会被直接排除。
  典型情形：两家厂商的 Bundle ID 互相包含（如 A 家的 ID 是 B 家 ID 的前缀），
  只按前缀匹配就会把别人的容器当成自己的。
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from xml.parsers.expat import ExpatError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scanner import du_bytes  # noqa: E402

HOME = os.path.expanduser("~")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 默认扫描范围：标准应用目录 + 当前用户的 ~/Applications
DEFAULT_ROOTS = [
    "/Applications",
    "/Applications/Utilities",
    os.path.join(HOME, "Applications"),
]

DEFAULT_LIBRARY = os.path.join(HOME, "Library")
DEFAULT_TRASH = os.path.join(HOME, ".Trash")
DEFAULT_AUDIT_LOG = os.path.join(BASE_DIR, "deletes.log")

# 残留数据可能出现的 ~/Library 子目录（只做一级枚举，不递归整棵树）
LIBRARY_SUBDIRS = [
    "Application Support",
    "Caches",
    "Preferences",
    "Containers",
    "Group Containers",
    "Saved Application State",
    "HTTPStorages",
    "WebKit",
    "Logs",
    "Application Scripts",
    "LaunchAgents",
    "Cookies",
]

# 名字过短时不参与模糊匹配，否则 "X"、"Go" 这类会命中一大片
MIN_FUZZY_LEN = 3

# 应用名分词后，用于识别厂商目录（如 Vendor/Product 里的 "Vendor"）的最小长度
MIN_VENDOR_TOKEN_LEN = 4


# ---------------------------------------------------------------------------
# 应用信息
# ---------------------------------------------------------------------------
def read_app_info(app_path: str) -> dict | None:
    """读取 .app 的基本信息；不是合法的 .app 时返回 None。

    只读 Contents/Info.plist，不执行应用、不访问网络。
    """
    if not app_path.endswith(".app") or not os.path.isdir(app_path):
        return None
    info_path = os.path.join(app_path, "Contents", "Info.plist")
    if not os.path.isfile(info_path):
        return None
    try:
        with open(info_path, "rb") as f:
            plist = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError, ExpatError):
        # 截断/损坏的 XML plist 抛的是 ExpatError，它不是 ValueError；
        # 漏掉它会让一个坏 bundle 拖垮整轮扫描（GET /api/apps 直接 500）。
        return None
    except Exception:
        # 任何其他解析异常同样只影响这一个应用，不应中断整轮扫描
        return None
    if not isinstance(plist, dict):
        return None

    bundle_id = str(plist.get("CFBundleIdentifier") or "")
    # CFBundleName 有时缺失或为空，退回文件名（去掉 .app）
    name = str(plist.get("CFBundleName") or "").strip()
    if not name:
        name = os.path.basename(app_path)[:-4]
    return {
        "name": name,
        "bundle_id": bundle_id,
        "version": str(plist.get("CFBundleShortVersionString") or ""),
        "path": app_path,
    }


def list_apps(roots: list[str]) -> list[dict]:
    """枚举给定目录下的所有 .app（不递归，避免把嵌套的 Helper.app 当成独立应用）。"""
    apps = []
    seen: set[str] = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".app"):
                continue
            path = os.path.join(root, entry)
            real = os.path.realpath(path)
            if real in seen:          # 同一应用被软链到多个目录时只保留一份
                continue
            info = read_app_info(path)
            if not info:
                continue
            seen.add(real)
            apps.append(info)
    return apps


def is_system_app(bundle_id: str) -> bool:
    """系统自带应用（com.apple.*）不允许卸载。

    删掉 Safari / TestFlight 这类应用会破坏系统签名完整性，
    且无法通过本工具恢复。
    """
    return bundle_id.lower().startswith("com.apple.")


def is_running(app_path: str) -> bool:
    """判断应用是否正在运行。

    用 pgrep 匹配「应用路径前缀」，比按名字匹配更准：
    名字可能同时出现在无关进程的命令行里。
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", os.path.join(app_path, "")],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # 探测失败时保守地认为「可能在运行」，宁可拒绝也不误删正在写数据的应用
        return True
    # pgrep 退出码：0 = 有匹配，1 = 无匹配，其他 = 出错。
    # 路径含正则元字符（C++、[beta] 等）时 pgrep 会以 2 失败，
    # 此时 stdout 为空——只看 stdout 会把它误判成「未运行」。
    if out.returncode not in (0, 1):
        return True
    return out.returncode == 0 and bool(out.stdout.strip())


# ---------------------------------------------------------------------------
# 应用图标与权限辅助
# ---------------------------------------------------------------------------
def is_admin_required(app_path: str) -> bool:
    """判断应用是否需要管理员权限才能卸载（如 App Store 下载或 root 安装）。"""
    real_path = os.path.realpath(os.path.expanduser(app_path))
    if not os.path.exists(real_path):
        return False
    try:
        st = os.stat(real_path)
        # 非当前用户所有，或者当前用户无写权限
        if st.st_uid != os.getuid() or not os.access(real_path, os.W_OK):
            return True
        parent = os.path.dirname(real_path)
        if not os.access(parent, os.W_OK):
            return True
        return False
    except OSError:
        return True


def get_app_icon_png(app_path: str, size: int = 64) -> bytes | None:
    """提取 .app 的图标并转换为 PNG 字节流。

    利用 macOS 自带的 sips 工具转换 .icns，零第三方依赖。
    找不到图标或转换失败时返回 None。
    """
    real_path = os.path.realpath(os.path.expanduser(app_path))
    if not os.path.isdir(real_path) or not real_path.endswith(".app"):
        return None
    info_path = os.path.join(real_path, "Contents", "Info.plist")
    if not os.path.isfile(info_path):
        return None
    try:
        with open(info_path, "rb") as f:
            plist = plistlib.load(f)
    except Exception:
        return None
    if not isinstance(plist, dict):
        return None

    icon_name = str(plist.get("CFBundleIconFile") or plist.get("CFBundleIconName") or "").strip()
    candidates: list[str] = []
    if icon_name:
        if not icon_name.endswith(".icns"):
            candidates.append(icon_name + ".icns")
        candidates.append(icon_name)
    candidates.append("AppIcon.icns")

    res_dir = os.path.join(real_path, "Contents", "Resources")
    icns_path = None
    for cand in candidates:
        cand_path = os.path.join(res_dir, cand)
        if os.path.isfile(cand_path):
            icns_path = cand_path
            break
    if not icns_path and os.path.isdir(res_dir):
        import glob
        all_icns = sorted(glob.glob(os.path.join(res_dir, "*.icns")))
        if all_icns:
            icns_path = all_icns[0]

    if not icns_path:
        return None

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_png = tmp.name
    try:
        cmd = ["sips", "-s", "format", "png", "-Z", str(size), icns_path, "--out", tmp_png]
        subprocess.run(cmd, capture_output=True, timeout=5, check=True)
        with open(tmp_png, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        if os.path.exists(tmp_png):
            try:
                os.remove(tmp_png)
            except OSError:
                pass


def _move_via_finder(posix_path: str, timeout: int = 30) -> tuple[bool, str]:
    """通过 Finder 把文件移入废纸篓。

    对于 root 安装或 App Store 下载的应用（当前用户无直接写入/重命名权限），
    Finder 会调起系统原生的 Touch ID / 管理员密码认证窗口完成移入废纸篓。
    """
    escaped_path = posix_path.replace(chr(92), chr(92) + chr(92)).replace(chr(34), chr(92) + chr(34))
    script = f'tell application "Finder" to delete POSIX file "{escaped_path}"'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0:
            return True, ""
        err = proc.stderr.strip()
        if "-128" in err or "User canceled" in err:
            return False, "用户已取消管理员授权"
        import re
        # 清除 AppleScript 行号前缀，如 29:104: execution error:
        cleaned_err = re.sub(r'^\d+:\d+:\s*(execution error:\s*)?', '', err).strip()
        return False, cleaned_err or "Finder 移入废纸篓失败"
    except subprocess.TimeoutExpired:
        return False, "等待管理员授权超时"
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"调用 Finder 失败: {e}"


# ---------------------------------------------------------------------------
# 残留匹配
# ---------------------------------------------------------------------------
def _app_name_tokens(app: dict) -> list[str]:
    """收集用于识别「厂商目录」的片段。

    两个来源缺一不可：
      - Bundle ID 的反向域名段（com.vendor.App → vendor / app）
      - 应用名分词（"Vendor App" → vendor / app）
    只看名字会漏：很多应用的 CFBundleName 只有产品名（如 "Chrome"），
    猜不到厂商，而真实数据在 Application Support/<厂商>/<产品>。
    """
    tokens: list[str] = []
    for raw in (app.get("name") or "").replace("-", " ").replace("_", " ").split():
        tok = raw.strip().lower()
        if len(tok) >= MIN_VENDOR_TOKEN_LEN:
            tokens.append(tok)

    # 反向域名的前两段：com.example.App → com / example
    # 取 "example" 这一级别，正好对应 macOS 常见的 Application Support/<厂商> 布局
    parts = (app.get("bundle_id") or "").lower().split(".")
    for seg in parts[:2]:
        if len(seg) >= MIN_VENDOR_TOKEN_LEN and seg not in ("com", "org", "net", "io", "dev", "local"):
            tokens.append(seg)

    # 去重但保持顺序，便于调试时看懂来源
    return list(dict.fromkeys(tokens))


def _bundle_id_matches(entry_lower: str, bundle_id: str) -> bool:
    """判断目录项名是否属于该 Bundle ID，两端都要求落在组件边界上。

    com.example.demo 应当命中 com.example.demo、com.example.demo.ShipIt、
    com.example.demo-helper，以及带团队 ID 前缀的
    5A4RE8SF68.com.example.demo；但**不能**命中 com.example.demo2
    （共享前缀）或 org.other.com.example.demo.tool（别人的 ID 把本应用 ID
    夹在中间，那整条是另一个 Bundle ID）。
    """
    if not bundle_id:
        return False
    start = 0
    while True:
        idx = entry_lower.find(bundle_id, start)
        if idx < 0:
            return False
        end = idx + len(bundle_id)
        end_ok = end == len(entry_lower) or entry_lower[end] in ".-"
        if end_ok and _prefix_ok(entry_lower[:idx]):
            return True
        start = idx + 1


def _prefix_ok(prefix: str) -> bool:
    """匹配位置之前的前缀是否合法。

    允许两种：空，或单个标签（团队 ID / group 之类，如 5A4RE8SF68.、group.）。
    前缀本身还是域名形式（org.other.）说明这是别人的完整 Bundle ID，
    本应用 ID 只是被夹在中间，不应认领。
    """
    p = prefix.rstrip(".-")
    return "." not in p


def classify_leftover(
    entry_name: str,
    app: dict,
    other_bundle_ids: set[str],
    in_preferences: bool = False,
) -> str | None:
    """判定一个 ~/Library 目录项是否属于该应用，返回置信度或 None。

    other_bundle_ids 是「其他已安装应用」的 Bundle ID 集合，用于排除
    「名字里恰好含有本应用名」的他人数据。
    in_preferences 表示该项位于 ~/Library/Preferences——那里文件名就是
    Bundle ID，判定必须精确相等而不是前缀包含。
    """
    is_prefs = in_preferences
    entry_lower = entry_name.lower()
    own = (app.get("bundle_id") or "").lower()
    app_name = (app.get("name") or "").strip().lower()
    # Preferences 下是 "xxx.plist"，比较名字前先去掉后缀
    entry_stem = entry_lower[:-len(".plist")] if entry_lower.endswith(".plist") else entry_lower

    # 1) 本应用 Bundle ID 命中（结束在组件边界上，排除 com.x.demo2 这类共享前缀的他人 ID）；
    #    但若有更长（更精确）的他人 Bundle ID 也命中，说明这一项属于那个应用
    #    （同一厂商的产品线变体尤其常见）
    if _bundle_id_matches(entry_lower, own):
        longer_other = any(
            _bundle_id_matches(entry_lower, o.lower()) and len(o) > len(own)
            for o in other_bundle_ids if o
        )
        # Preferences 下的文件名**就是** Bundle ID，不是前缀匹配的场合。
        # 要求精确相等，否则同一厂商的变体产品（如 com.x.chrome 之于
        # com.x.chrome.for.testing）的配置会被顺走；而该变体未必同时装着，
        # 无法靠 other_bundle_ids 兜底。
        if not longer_other and (not is_prefs or entry_stem == own):
            return "bundle"

    # 2) 命中其他已安装应用的 Bundle ID —— 明确不是本应用的数据
    if any(o and _bundle_id_matches(entry_lower, o.lower()) for o in other_bundle_ids):
        return None

    # Preferences 里只剩「精确同名」和「名字完全一致」两种情况是可信的，
    # 不做模糊匹配：那里的文件名彼此高度相似，模糊匹配必然误伤。
    if is_prefs:
        return "name" if app_name and entry_stem == app_name else None

    # 3) 与应用名完全一致
    if app_name and entry_stem == app_name:
        return "name"

    # 4) 名字包含应用名 —— 可能误伤，标记为低置信度
    if len(app_name) >= MIN_FUZZY_LEN and app_name in entry_lower:
        return "fuzzy"

    return None


def _classify_nested(child_name: str, app: dict, other_bundle_ids: set[str]) -> str | None:
    """厂商目录下的子项（如 Vendor/Product）：只认「与应用的某一段完全一致」。

    这里刻意不用子串匹配——厂商目录下往往并排放着多个产品
    （正式版 / Beta / Dev / 测试版），子串匹配会把兄弟产品一起删掉。
    """
    if any(o and _bundle_id_matches(child_name.lower(), o.lower()) for o in other_bundle_ids):
        return None
    # 兄弟产品保护：子项名若等于「另一个已安装应用的 Bundle ID 末段」，
    # 说明这个厂商子目录属于那个产品（如 Chrome 之于 Chrome Remote Desktop），
    # 不能由本应用认领。
    child_lower = child_name.lower()
    for o in other_bundle_ids:
        if o and o.lower().split(".")[-1] == child_lower:
            return None
    # 子项名与应用名片段（来自名字 + Bundle ID）完全相等才算命中
    return "fuzzy" if child_lower in _app_name_tokens(app) else None


def find_leftovers(app: dict, library: str, other_bundle_ids: set[str]) -> list[dict]:
    """枚举 ~/Library 下属于该应用的残留，返回带体积与置信度的清单。

    只做一级枚举；仅当某个目录名本身就是应用名的片段（厂商目录）时，
    再往下看一层，避免遍历整棵 Library 树。
    """
    found: list[dict] = []
    seen: set[str] = set()
    tokens = set(_app_name_tokens(app))

    def add(path: str, confidence: str) -> None:
        real = os.path.realpath(path)
        if real in seen:
            return
        seen.add(real)
        size = du_bytes(path)
        if size <= 0:
            return
        found.append({
            # 对外只给 ~ 相对路径：结果可能被截图分享，不落家目录绝对路径。
            # 卸载时用 os.path.expanduser 还原，不依赖这里的形式。
            "path": path.replace(HOME, "~"),
            "size": size,
            "confidence": confidence,
            # fuzzy 一律默认不勾选：宁可让用户多点一下，也不替他做误删决定
            "default": confidence in ("bundle", "name"),
        })

    for sub in LIBRARY_SUBDIRS:
        base = os.path.join(library, sub)
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue          # 目录不存在或没权限：跳过，不影响其他子目录
        is_prefs = (sub == "Preferences")
        for entry in entries:
            full = os.path.join(base, entry)
            conf = classify_leftover(entry, app, other_bundle_ids, in_preferences=is_prefs)
            if conf:
                add(full, conf)
                continue
            # 厂商两级嵌套：父目录名是应用名片段时，看一层子目录
            if entry.lower() not in tokens or os.path.islink(full):
                continue
            try:
                children = sorted(os.listdir(full))
            except OSError:
                continue
            for child in children:
                child_conf = _classify_nested(child, app, other_bundle_ids)
                if child_conf:
                    add(os.path.join(full, child), child_conf)
    return found


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
def scan_apps(
    roots: list[str] | None = None,
    library: str | None = None,
    running_checker=is_running,
) -> dict:
    """扫描已安装应用及其残留，返回前端可直接消费的结构。"""
    roots = roots if roots is not None else DEFAULT_ROOTS
    library = library if library is not None else DEFAULT_LIBRARY

    apps = list_apps(roots)
    all_ids = {a["bundle_id"] for a in apps if a["bundle_id"]}

    items = []
    for app in apps:
        others = all_ids - {app["bundle_id"]}
        leftovers = find_leftovers(app, library, others)
        app_size = du_bytes(app["path"])
        leftover_size = sum(f["size"] for f in leftovers)
        items.append({
            **app,
            # 路径只保留 ~ 相对形式：结果可能被截图分享，不落家目录绝对路径。
            # 卸载时由服务端展开并重新枚举校验，前端拿到的路径不能直接当权限用。
            "path": app["path"].replace(HOME, "~"),
            "size": app_size,
            "leftover_size": leftover_size,
            "total_size": app_size + leftover_size,
            "system": is_system_app(app["bundle_id"]),
            "needs_admin": is_admin_required(app["path"]),
            "running": running_checker(app["path"]),
            "leftovers": leftovers,
        })

    items.sort(key=lambda x: x["total_size"], reverse=True)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "apps": items,
        "summary": {
            "count": len(items),
            "total": sum(i["total_size"] for i in items),
            "leftover": sum(i["leftover_size"] for i in items),
        },
    }


# ---------------------------------------------------------------------------
# 卸载
# ---------------------------------------------------------------------------
def _safe_component(name: str) -> str:
    """把任意字符串清洗成单个安全的路径片段。

    应用名来自 bundle 的 CFBundleName，是不可信输入：可能含 "/"、
    ".."、NUL。直接拼进废纸篓路径会建出嵌套目录，甚至逃出废纸篓。
    """
    cleaned = (name or "").replace("\x00", "").replace("/", "_").replace(os.sep, "_")
    cleaned = cleaned.replace("..", "_").strip()
    return cleaned or "item"


def _unique_trash_path(trash_dir: str, name: str) -> str:
    """在废纸篓中生成不冲突的目标路径。

    同一秒内可能处理多个同名残留（Caches/Demo、Containers/Demo…），
    只靠秒级时间戳会撞车，导致 shutil.move 把它们合并进同一个目录。
    这里用「探测后原子创建目录」的方式抢占目标名，抢不到就加序号重试。
    """
    base = _safe_component(name)
    for i in range(1, 1000):
        candidate = os.path.join(trash_dir, base if i == 1 else f"{base} {i}")
        # 目标必须是废纸篓内的直接子项，不能是嵌套路径
        if os.path.dirname(candidate) != trash_dir:
            raise OSError(f"非法的废纸篓目标名: {name}")
        # lexists：目标即使是失效软链也算占用，不能用 exists
        if not os.path.lexists(candidate):
            return candidate
    raise OSError(f"废纸篓中同名条目过多: {base}")


def _audit(audit_log: str | None, entry: dict) -> None:
    """写审计日志；失败不影响主流程（与 server.py 保持一致）。"""
    if not audit_log:
        return
    try:
        with open(audit_log, "a", encoding="utf-8") as f:
            f.write(_json_line(entry))
    except OSError:
        pass


def _json_line(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False) + "\n"


_UNSET = object()


def uninstall(
    app_path: str,
    leftovers: list[str],
    roots: list[str] | None = None,
    library: str | None = None,
    trash_dir: str | None = None,
    running_checker=is_running,
    audit_log: str | None = _UNSET,  # type: ignore[assignment]
) -> dict:
    """把应用本体与指定的残留移入废纸篓。

    leftovers 是用户勾选的残留路径；服务端会重新计算一遍合法集合，
    只接受其中的路径——这样即使前端被篡改也无法删到别处。

    audit_log 不传时写默认位置；显式传 None 表示**不写审计日志**
    （测试用，避免污染仓库内的 deletes.log）。
    """
    roots = roots if roots is not None else DEFAULT_ROOTS
    library = library if library is not None else DEFAULT_LIBRARY
    trash_dir = trash_dir if trash_dir is not None else DEFAULT_TRASH
    if audit_log is _UNSET:
        audit_log = DEFAULT_AUDIT_LOG

    def fail(error: str) -> dict:
        _audit(audit_log, {
            "time": datetime.now().isoformat(timespec="seconds"),
            "action": "uninstall", "app": app_path, "result": "fail", "error": error,
        })
        return {"ok": False, "error": error, "moved": []}

    # 门禁 1：应用必须是扫描范围内真实存在的 .app
    # 注意：scan_apps 对外给的是 ~ 相对路径，前端会原样回传，
    # 而 realpath 不展开 ~——这里必须先 expanduser，否则 ~/Applications
    # 下的每个应用都会被判成「未知应用」而永远无法卸载。
    installed = {os.path.realpath(a["path"]): a for a in list_apps(roots)}
    app = installed.get(os.path.realpath(os.path.expanduser(app_path)))
    if not app:
        return fail("未知应用：不在扫描范围内，或不是合法的 .app")

    # 门禁 2：系统应用不卸载
    if is_system_app(app["bundle_id"]):
        return fail("系统自带应用不允许卸载（会破坏系统完整性）")

    # 门禁 3：运行中的应用不卸载，否则可能写回残留或损坏数据
    if running_checker(app["path"]):
        return fail("该应用正在运行，请先退出后再卸载")

    # 门禁 4：残留路径必须是重新计算出的集合的子集。
    # find_leftovers 对外给的是 ~ 相对路径，比较前统一还原成真实路径。
    all_ids = {a["bundle_id"] for a in installed.values() if a["bundle_id"]}
    allowed = {
        os.path.realpath(os.path.expanduser(f["path"]))
        for f in find_leftovers(app, library, all_ids - {app["bundle_id"]})
    }
    # 去重：同一路径重复传入时只处理一次，否则第二次 move 会因源已不存在而报错
    checked: list[str] = []
    seen_paths: set[str] = set()
    for path in leftovers:
        real_path = os.path.realpath(os.path.expanduser(path))
        if real_path not in allowed:
            return fail(f"{path}: 不在该应用的残留清单内")
        if real_path in seen_paths:
            continue
        seen_paths.add(real_path)
        checked.append(path)

    os.makedirs(trash_dir, exist_ok=True)
    moved: list[dict] = []
    failed: list[str] = []

    # 应用本体进废纸篓时保留 .app 后缀，否则用户无法双击还原
    targets = [(app["path"], f"uninstall__{os.path.basename(app['path'])}")]
    for path in checked:
        real_path = os.path.expanduser(path)
        base = os.path.basename(real_path.rstrip("/")) or "item"
        targets.append((real_path, f"uninstall__{app['name']}__{base}"))

    app_moved = False
    for index, (src, name) in enumerate(targets):
        is_app = (index == 0)
        dst = _unique_trash_path(trash_dir, name)
        moved_ok = False
        err_msg = ""

        # 1. 优先尝试直接原子 rename 到废纸篓（最快、最安全、绝不使用 rm）
        try:
            os.rename(src, dst)
            moved_ok = True
        except OSError as e:
            err_msg = str(e)

        # 2. 仅应用本体且遇权限问题时（如 App Store / root 安装），借助 Finder 调起系统 Touch ID/密码认证
        # 注意：残留数据在 ~/Library 下属当前用户所有，绝不调用 Finder（避免 Finder 弹系统错误弹窗或报 -5000）
        if not moved_ok and is_app and trash_dir == DEFAULT_TRASH:
            finder_ok, finder_err = _move_via_finder(src)
            if finder_ok:
                moved_ok = True
                dst = os.path.join(trash_dir, os.path.basename(src))
                # 应用移走后稍等片刻，让系统 containermanagerd / LaunchServices 完成注销，避免残留容器被瞬时锁定
                import time
                time.sleep(0.3)
            else:
                err_msg = finder_err or err_msg

        # 3. 残留数据重试：若遇瞬时锁定（如 containermanagerd 正在同步），重试 3 次
        if not moved_ok and not is_app:
            import time
            for _ in range(3):
                time.sleep(0.3)
                if not os.path.exists(src):
                    moved_ok = True
                    break
                try:
                    os.rename(src, dst)
                    moved_ok = True
                    break
                except OSError as e:
                    err_msg = str(e)

        # 4. 兜底：非默认废纸篓（如测试环境临时目录跨卷）
        if not moved_ok and trash_dir != DEFAULT_TRASH:
            try:
                shutil.move(src, dst)
                moved_ok = True
            except OSError as e:
                err_msg = str(e)

        if moved_ok:
            moved.append({"from": src, "to": dst})
            if is_app:
                app_moved = True
            _audit(audit_log, {
                "time": datetime.now().isoformat(timespec="seconds"),
                "action": "uninstall", "app": app["bundle_id"],
                "from": src, "to": dst, "result": "ok",
            })
        else:
            failed.append(f"{src}: {err_msg}")
            _audit(audit_log, {
                "time": datetime.now().isoformat(timespec="seconds"),
                "action": "uninstall", "app": app["bundle_id"],
                "from": src, "result": "fail", "error": err_msg,
            })

    # ok 只认「应用本体确实被移走」：只删掉残留而应用还在时，
    # 前端会把该行从列表移除并提示「已卸载」，用户会以为删干净了。
    return {
        "ok": app_moved,
        "partial": bool(moved) and not app_moved,
        "app_moved": app_moved,
        "moved": moved,
        "failed": failed,
        "error": "" if app_moved else (
            "应用本体未能移除" + (f"：{failed[0]}" if failed else "")
        ),
    }


if __name__ == "__main__":
    import json
    result = scan_apps()
    print(json.dumps({
        "apps": len(result["apps"]),
        "total_gb": round(result["summary"]["total"] / 2**30, 2),
        "leftover_gb": round(result["summary"]["leftover"] / 2**30, 2),
    }, ensure_ascii=False, indent=2))
