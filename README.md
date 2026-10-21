# CleanYourMac

**一个本地运行的 macOS 磁盘分析工具：扫描 → 分级 → 勾选 → 移入废纸篓。**

不做永久删除，所有操作可在废纸篓中撤销。零第三方依赖，只用 Python 标准库。

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/Platform-macOS-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)
[![CI](https://github.com/eddiezhan/CleanYourMac/actions/workflows/ci.yml/badge.svg)](https://github.com/eddiezhan/CleanYourMac/actions/workflows/ci.yml)

```
┌─────────────────────────────────────────────────┐
│  456 GB / 512 GB      已使用 89.1%               │
│  ███████████████████████░░░  可用 56.0 GB        │
│  可用 56.0 GB → 清理后 96.6 GB                   │
├─────────────────────────────────────────────────┤
│  可安全清理 40.6 GB │ 需确认 18.2 GB │ 已保护     │
├─────────────────────────────────────────────────┤
│  ☑ npm 包缓存        7.87 GB   可安全删          │
│  ☑ Xcode 编译产物    3.12 GB   可安全删          │
│  ☐ Chrome 缓存       2.86 GB   可安全删          │
│  ☐ Xcode 归档包      1.40 GB   需确认            │
│  ☐ 文档              2.10 GB   已保护 🔒         │
└─────────────────────────────────────────────────┘
```

## 它解决什么问题

macOS 自带的存储管理只告诉你「其他」占了多少，不告诉你是哪个目录、能不能删。
本工具把常见的空间黑洞逐项量化，并给出**明确的删除判定**——而不是让你自己猜。

三类判定，来自人工逐项评估，不是按目录名瞎猜：

| 等级 | 含义 | 默认勾选 |
|---|---|---|
| **可安全删** | 缓存、日志、可重建的构建产物。删后自动重建或完全无用 | 是 |
| **需确认** | 可能影响功能，或含不可再生数据（归档、会话记录、设备数据） | 否 |
| **已保护** | 个人文件、凭据、应用本体、系统目录 | 否（复选框禁用） |

内置清单覆盖 npm / pnpm / yarn / pip / uv / poetry / cargo / go / gradle / maven
等开发缓存，Xcode 与 Android 构建产物，主流浏览器缓存，以及日志与临时文件。
清单外的目标可通过 `targets.local.json` 自行扩展。

## 快速开始

要求：**macOS** + **Python 3.9 及以上**（macOS 12 起需先装 Xcode 命令行工具，见 FAQ）。

### 方式一：git clone（推荐）

```bash
git clone https://github.com/eddiezhan/CleanYourMac.git
cd CleanYourMac
python3 server.py
```

浏览器打开 `http://127.0.0.1:8765` 即可。

### 方式二：双击启动

在 Finder 中双击 `start.command`，会自动启动服务并打开浏览器。

注意两点：

- **终端窗口必须保持打开**，关掉窗口等于停止服务
- 通过 git clone 获取时可直接双击；通过 **ZIP 下载**的则需先做一次授权（见下）

### ZIP 下载用户必读

从浏览器下载的 ZIP 会丢失脚本的执行权限，并被 Gatekeeper 标记。双击会提示
「无法打开」或「Permission denied」。执行一次即可修复：

```bash
cd CleanYourMac
chmod +x start.command
xattr -d com.apple.quarantine start.command 2>/dev/null
```

之后就能正常双击。也可以在文件上**右键 → 打开**，在弹窗中点「打开」放行。

### 使用流程

1. 页面加载后自动扫描（约 8–12 秒，取决于目录规模）
2. 用顶部筛选切换「可安全删 / 需确认 / 已保护」
3. 勾选目标，或点「选中全部安全项」
4. 点右下角「移入废纸篓」→ 二次确认 → 执行
5. 文件进入废纸篓，随时可拖回原位恢复

磁盘卡片右侧的「清理后」数字会跟随勾选实时变化，未选择时不显示预测值。

## 安全设计

删除是不可逆操作，所以这里的原则是「宁可少删，不可错删」。五层门禁，任何一层不满足都会拒绝：

1. **白名单目标** — 只接受 `TARGETS` 中登记过的 id，无法通过任意路径删除
2. **风险分级拦截** — `risk == "keep"` 的目标一律拒绝，UI 中复选框也是禁用的
3. **允许范围校验** — 展开后的真实路径必须位于家目录或当前用户临时目录之内
4. **保护名单** — `~/.ssh`、`~/Downloads`、`~/Documents`、`~/Library/Keychains`、`/Applications` 等命中即拒绝
5. **移入废纸篓而非删除** — 使用 `shutil.move` 移到 `~/.Trash`，文件名加目标 id 前缀便于辨认，**全程不使用 `rm`**

所有操作写入 `deletes.log` 审计：

```json
{"time":"2026-10-21T22:06:36","id":"npm-logs","from":"~/.npm/_logs","to":"~/.Trash/npm-logs__logs","result":"ok"}
```

想恢复误删的内容，直接打开废纸篓把文件拖回原位即可。

## 自定义目标

内置清单覆盖通用的开发缓存、构建产物、浏览器缓存与日志。个人环境特有的目标
（小众工具、自建服务、特定项目产物）写进 `targets.local.json`：

```bash
cp targets.local.example.json targets.local.json
```

```json
{
  "targets": [
    {
      "id": "my-build-output",
      "name": "本地构建输出",
      "category": "构建产物",
      "paths": ["~/work/my-project/dist"],
      "risk": "safe",
      "desc": "可重新构建的产物目录，源码不受影响。"
    }
  ]
}
```

该文件已被 `.gitignore` 忽略，不会误提交。字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 唯一标识，与内置项冲突时覆盖内置项 |
| `name` | 是 | 显示名称 |
| `paths` | 是 | 支持 `~` 与通配符 `*` |
| `risk` | 否 | `safe` / `caution` / `keep`，默认 `caution`（宁严勿松） |
| `category` | 否 | 分组标签，默认「自定义」 |
| `desc` | 否 | 删除后果说明，会显示在列表里 |

## 项目结构

```
CleanYourMac/
├── scanner.py                 扫描器：目标清单 + 并发体积测量（只读）
├── server.py                  本地服务：/api/scan、/api/trash + 五层门禁
├── static/index.html          前端界面（原生 JS，无构建步骤）
├── tests/test_safety.py       安全校验回归测试（13 项）
├── targets.local.example.json 自定义目标示例
├── start.command              macOS 双击启动
├── assets/donate/             捐赠二维码
├── data.json                  扫描结果（运行时生成，已忽略）
└── deletes.log                操作审计（运行时生成，已忽略）
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 实现说明

- **并发扫描** — 各目标相互独立，8 线程并发执行 `du`。串行版本耗时 32 秒，并发后约 8 秒
- **零依赖** — 只用标准库（`http.server`、`shutil`、`concurrent.futures`），不需要 pip install
- **本地监听** — 只绑定 `127.0.0.1`，不对局域网暴露
- **配置驱动** — 扫描逻辑与目标清单分离，加新目标不用改代码
- **扫描缓存** — 服务端 5 秒结果缓存 + 互斥锁，避免连点重扫拖住机器

## 已知限制

- **仅支持 macOS** — 依赖 `~/.Trash`、`~/Library` 布局和 BSD `du`。Linux 需要改废纸篓路径（`~/.local/share/Trash/files`）与部分目标路径；Windows 不支持
- **不含系统级日志** — `/private/var/db/diagnostics`、`powerlog` 等需要 sudo，本工具不做提权
- **目录级粒度** — 不做重复文件检测、不逐个列出大文件
- **不自动清理** — 不提供定时任务，避免在无人值守时误删

## FAQ

**页面提示「无法连接本地服务」或「扫描失败」**

九成是服务没在运行。浏览器页面可以停留在已加载状态，但服务一旦停止，
「重新扫描」就会连不上 `127.0.0.1:8765`。

检查两件事：

```bash
# 1. 服务是否在监听
lsof -i :8765 -sTCP:LISTEN

# 2. 接口是否正常（应返回一大段 JSON）
curl -s http://127.0.0.1:8765/api/scan | head -c 120
```

若第一条无输出，重新启动即可。**注意：关闭启动脚本的终端窗口会同时停止服务。**

**提示「未找到 python3」或「版本过低」**

macOS 已不再预装 Python。安装命令行工具即可获得 `python3`：

```bash
xcode-select --install
```

**双击后终端一闪就没了**

说明脚本报错退出。在终端里手动运行可以看到具体原因：

```bash
cd CleanYourMac && ./start.command
```

**端口 8765 被占用**

换一个端口：

```bash
CMM_PORT=9000 ./start.command
```

**扫描很慢或点了没反应**

单次扫描约 8–12 秒（并发遍历几十个目录）。服务端有 5 秒结果缓存，
连点「重新扫描」不会重复触发扫描，但带 `?force=1` 会强制重扫。

**想恢复误删的文件**

打开废纸篓，文件名为 `<目标id>__<原名>`，拖回原位置即可。`deletes.log` 里记录了每次操作的原始路径。

## 捐赠支持

CleanYourMac 永久免费开源。如果它帮你清出了几十 GB 空间，请作者喝杯咖啡：

| 方式 | 说明 |
|---|---|
| 爱发电 | [afdian.com/a/eddiezhan](https://afdian.com/a/eddiezhan) |

**微信赞赏**

<img src="./assets/donate/wechat_qr.png" width="200" alt="微信赞赏码">

**支付宝**

<img src="./assets/donate/alipay_qr.png" width="200" alt="支付宝收款码">

**USDT (TRC20)**

<img src="./assets/donate/usdt_qr.png" width="200" alt="USDT 收款码">

## 免责声明

本项目按「现状」提供，不对使用后果作任何担保。清理操作虽以移入废纸篓的方式实现，
但仍建议在执行前确认清单内容。因使用本工具造成的数据丢失或其他损失，由使用者自行承担。

## License

[MIT](./LICENSE)
