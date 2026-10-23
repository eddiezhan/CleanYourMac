# CleanYourMac

**一个本地运行的 macOS 磁盘分析工具：扫描 → 分级 → 勾选 → 移入废纸篓，并能把应用连同它的残留数据一起卸干净。**

不做永久删除，所有操作可在废纸篓中撤销。零第三方依赖，只用 Python 标准库。

![Version](https://img.shields.io/badge/version-1.2.0-blue)
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

┌── 卸载应用 ──────────────────────────────────────┐
│  Acme Editor  com.acme.editor     12.4 GB  卸载  │
│  Beta Tools   com.beta.tools       2.1 GB  卸载  │
│  Gamma App    com.gamma.app        1.4 GB  卸载  │
│  Safari       com.apple.Safari       —   系统应用│
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

## 卸载应用（1.1.0 新增，1.1.1 修复）

macOS 直接删掉 `.app` 只会删掉本体，配置、缓存、容器还留在 `~/Library` 里，
几十 GB 就这么被占着，而用户往往不知道它们在哪。

切到「卸载应用」标签，工具会枚举 `/Applications`、`/Applications/Utilities`
和 `~/Applications` 下的所有应用，逐个算出**本体体积 + 残留体积**，点「卸载」
即可把两者一起移入废纸篓。

### 残留数据怎么找

在 `~/Library` 下这 12 个目录里做一级枚举：

```
Application Support  Caches  Preferences  Containers  Group Containers
Saved Application State  HTTPStorages  WebKit  Logs  Application Scripts
LaunchAgents  Cookies
```

每个目录项按**三级置信度**判定归属：

| 置信度 | 判定方式 | 默认勾选 |
|---|---|---|
| **Bundle ID 匹配** | 目录项包含该应用的 Bundle ID | ✅ 是 |
| **名称匹配** | 目录项名（去掉 `.plist`）与应用名完全一致 | ✅ 是 |
| **模糊匹配** | 名字包含应用名，或厂商两级嵌套（`<厂商>/<产品>`） | ❌ **否，需手动勾选** |

低置信度项在弹窗里标黄，默认不选——宁可让用户多点一下，也不替他做误删决定。

### 两处容易误伤的地方，已经处理

**一、别人的数据里有你的名字。** 两家厂商的 Bundle ID 常常互相包含，按名字
或前缀匹配会把别人的容器当成自己的。因此凡命中「另一个已安装应用的 Bundle ID」
的目录项会被直接排除；若有更长（更精确）的他人 Bundle ID 也命中，以更精确的
那个为准——这样同一厂商的正式版不会顺走它测试版的配置。

**二、`Preferences` 里的文件名就是 Bundle ID。** 那里必须精确相等，
不能用前缀匹配——否则 `com.acme.app.for.testing.plist` 会被 `com.acme.app`
顺走，而测试版未必同时装着，无法靠「其他应用」兜底。

### 厂商目录靠 Bundle ID 定位

很多应用的 `CFBundleName` 只有产品名，光看名字猜不到它挂在哪个厂商目录下
（数据实际在 `Application Support/<厂商>/<产品>`）。所以厂商片段同时取自
Bundle ID 的反向域名段（`com.vendor.Product` → `vendor`），并且子目录名必须
与片段**完全相等**才命中——厂商目录下往往并排放着正式版 / Beta / Dev / 测试版，
子串匹配会把兄弟产品一起删掉。

### 无法卸载的情况

| 情况 | 表现 |
|---|---|
| 系统自带应用（`com.apple.*`） | 按钮置灰，标注「系统应用」 |
| 应用正在运行 | 按钮置灰，标注「运行中」，先退出再卸载 |
| root / App Store 安装 | 标注「需管理员权限」，卸载时系统调起 Touch ID / 密码认证放行移入废纸篓 |
| 应用装在扫描范围外 | 列表里不出现，接口也拒绝 |

## 更新记录

### v1.2.0 — 应用真实图标 + 乐观无感移除 + 权限应用卸载支持

- **应用真实图标显示**：利用 macOS 自带的 `sips` 命令行工具从 bundle 内提取 `.icns` 并转为 PNG（64x64），服务端内存缓存；前端无缝优先展示真实高精图标，加载失败时自动平滑 fallback 到首字母头像。
- **乐观局部更新**：卸载应用成功后直接在前端移除卡片行并就地更新统计数值，不再全盘重新触发耗时 5–10 秒的扫描，连续卸载流畅无卡顿。
- **App Store / root 权限应用卸载支持（解决 ServerCat 等应用报错）**：
  - 针对 App Store 及管理员权限安装的应用（如 ServerCat），优先尝试原子 rename；若遇到系统写权限不足，自动调用 Finder 原生废纸篓通道唤起系统 Touch ID / 管理员密码授权完成移入废纸篓。
  - 修复 `~/Library/Containers` 残留移动时触发 `copytree` 导致 `.com.apple.containermanagerd.metadata.plist` 抛出 `Operation not permitted` 的缺陷。
  - 列表行增加「需管理员权限」徽章提示。

### v1.1.1 — 安全修复

独立代码审查发现并修复了以下问题，全部补了回归测试：

| 严重度 | 问题 | 后果 |
|---|---|---|
| 严重 | `~/Applications` 下的应用全部无法卸载 | 对外给 `~` 相对路径，门禁用 `realpath`（不展开 `~`）比较，永远判「未知应用」 |
| 严重 | 任意网页可触发卸载 | 无 Origin / Host / Content-Type 校验，`text/plain` 跨源请求不预检 |
| 重要 | 一个损坏的 `Info.plist` 拖垮整个扫描 | 截断 XML 抛 `ExpatError`，它不是 `ValueError`，原 `except` 捕获不到 |
| 重要 | 废纸篓目标名未清洗 | 应用名含 `/` 会建嵌套目录 |
| 重要 | 同名残留进废纸篓时互相合并 | 只用秒级时间戳消歧，同一秒内多条同名残留被 `move` 进同一目录 |
| 重要 | 应用本体没搬走却返回 `ok` | 前端移除列表行并提示「已卸载」，应用其实还在 |
| 重要 | `is_running` 对含正则元字符的路径失效 | `pgrep -f` 把路径当 ERE，`C++` 之类退出码 2、stdout 为空，被误判「未运行」 |
| 重要 | 发布流程的隐私检查从未真正执行 | macOS 的 bsdtar 不支持 `--wildcards`，报错被 `2>/dev/null` 吞掉，检查「通过」了空输入 |
| 重要 | 同厂商兄弟产品认领厂商子目录 | 两个同厂商产品同时装着时，A 会认领 B 的 profile 目录 |
| 次要 | 重复 Bundle ID 破坏列表行身份 | 同一应用在 `/Applications` 与 `~/Applications` 各一份时，点第二行操作的是第一行 |
| 次要 | 第三方字符串未转义直接进 `innerHTML` | 恶意 `CFBundleName` 可在本页 origin 下执行脚本 |
| 次要 | 测试污染生产审计日志 | 跑测试会往仓库内 `deletes.log` 写临时路径 |

### v1.1.0 — 新增应用卸载

应用本体与残留数据一并移入废纸篓。

## 安全设计

删除是不可逆操作，所以这里的原则是「宁可少删，不可错删」。五层门禁，任何一层不满足都会拒绝：

1. **白名单目标** — 只接受 `TARGETS` 中登记过的 id，无法通过任意路径删除
2. **风险分级拦截** — `risk == "keep"` 的目标一律拒绝，UI 中复选框也是禁用的
3. **允许范围校验** — 展开后的真实路径必须位于家目录或当前用户临时目录之内
4. **保护名单** — `~/.ssh`、`~/Downloads`、`~/Documents`、`~/Library/Keychains`、`/Applications` 等命中即拒绝
5. **移入废纸篓而非删除** — 使用 `shutil.move` 移到 `~/.Trash`，文件名加目标 id 前缀便于辨认，**全程不使用 `rm`**

卸载应用走的是另一套独立门禁（`uninstaller.py`），因为 `/Applications` 本身
在清理功能的保护名单里。四层校验，任何一层不满足都整体拒绝：

1. **应用必须真实存在** — 服务端重新枚举扫描目录，前端传来的路径只是「请求」不是「授权」
2. **系统应用拒绝** — `com.apple.*` 一律不允许卸载
3. **运行中拒绝** — 用 `pgrep` 匹配应用路径前缀，避免删掉正在写数据的应用
4. **残留必须是重算集合的子集** — 每次卸载都重新跑一遍残留发现，只有落在结果里的路径才允许移动

应用本体进废纸篓时保留 `.app` 后缀（`uninstall__<应用名>.app`），
残留则是 `uninstall__<应用名>__<原名>`，拖回原位即可还原。

### 请求来源校验（防 CSRF 与 DNS rebinding）

服务只监听 `127.0.0.1`，但这**不代表**只有你自己能调用它——浏览器允许任意
网页向 localhost 发请求，而 `text/plain` 属于 CORS simple request，**不触发
预检**。也就是说，你随手打开的一个恶意页面就能构造请求删掉你的应用，
它读不到响应也无所谓：应用路径可以猜，`leftovers` 传空就够了。

因此所有 `POST` 在读取 body 之前先过三道校验，任何一条不满足直接拒绝：

| 校验 | 拒绝码 | 防的是 |
|---|---|---|
| `Content-Type` 必须是 `application/json` | 415 | 跨源 simple request 绕过预检 |
| 带 `Origin` 时必须指向本机 | 403 | 其他站点发起的跨源请求 |
| `Host` 必须是本机 | 403 | DNS rebinding |

另外，所有进入页面的第三方字符串（应用名、Bundle ID、路径）都经过 HTML 转义——
一个 `CFBundleName` 为 `<img src=x onerror=...>` 的应用否则能在本页 origin 下
执行脚本，进而调用卸载接口。

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
├── uninstaller.py             卸载器：应用枚举 + 残留发现 + 卸载门禁
├── server.py                  本地服务：/api/scan、/api/trash、/api/apps、/api/uninstall
├── static/index.html          前端界面（原生 JS，无构建步骤）
├── tests/test_safety.py       清理功能安全校验回归测试（13 项）
├── tests/test_uninstall.py    卸载功能回归测试（39 项）
├── tests/test_server_http.py  HTTP 层来源校验回归测试（5 项）
├── targets.local.example.json 自定义目标示例
├── start.command              macOS 双击启动
├── assets/donate/             捐赠二维码
├── data.json                  扫描结果（运行时生成，已忽略）
└── deletes.log                操作审计（运行时生成，已忽略）
```

运行测试（共 63 项）：

```bash
python3 -m unittest discover -s tests -v
```

## 实现说明

- **并发扫描** — 各目标相互独立，8 线程并发执行 `du`。串行版本耗时 32 秒，并发后约 8 秒
- **零依赖** — 只用标准库（`http.server`、`shutil`、`concurrent.futures`），不需要 pip install
- **本地监听** — 只绑定 `127.0.0.1`，不对局域网暴露
- **配置驱动** — 扫描逻辑与目标清单分离，加新目标不用改代码
- **扫描缓存** — 服务端 5 秒（清理）/ 30 秒（应用）结果缓存 + 互斥锁，避免连点重扫拖住机器
- **不执行应用代码** — 卸载只读 `Contents/Info.plist`，不启动应用、不访问网络

## 已知限制

- **仅支持 macOS** — 依赖 `~/.Trash`、`~/Library` 布局和 BSD `du`。Linux 需要改废纸篓路径（`~/.local/share/Trash/files`）与部分目标路径；Windows 不支持
- **不含系统级日志** — `/private/var/db/diagnostics`、`powerlog` 等需要 sudo，本工具不做提权
- **目录级粒度** — 不做重复文件检测、不逐个列出大文件
- **不自动清理** — 不提供定时任务，避免在无人值守时误删
- **残留发现只覆盖 `~/Library`** — 不扫 `/Library`（需 sudo）、`/usr/local`、`/opt` 等系统级位置；装在系统级位置的应用可能留尾巴
- **不清理 LaunchServices 注册** — 移入废纸篓后 Dock 上可能残留一个失效图标，重启或重新登录后消失
- **不卸载 Homebrew 包** — 只处理 `.app`；`brew` 安装的命令行工具请用 `brew uninstall`
- **不做提权** — root 安装的应用会报权限错误，需要用户自行 `sudo` 处理

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

卸载应用时是 `uninstall__<应用名>.app` 和 `uninstall__<应用名>__<原名>`，
同样拖回原位即可。若应用已经从废纸篓清空，需要重新下载安装。

**卸载按钮是灰的**

两种情况：应用是系统自带的（标「系统应用」），或正在运行（标「运行中」）。
后者先退出应用再刷新列表即可。

**卸载后残留数据没删干净**

本工具只扫 `~/Library` 下的 12 个目录，且低置信度项默认不勾选。
如果确认某条标黄的路径确实属于该应用，卸载时手动勾上即可。
装在 `/Library` 或 `/opt` 的组件需要手动清理。

**卸载了一个应用，但另一个应用的数据也没了**

这不应该发生——残留判定会排除「命中其他已安装应用 Bundle ID」的目录项。
若真的遇到，请带上 `deletes.log` 里的记录提 issue，这是需要修的错误。

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

本项目按「现状」提供，不对使用后果作任何担保。清理与卸载操作虽均以移入废纸篓的
方式实现，但仍建议在执行前确认清单内容——尤其是标为「需确认」和「低置信度」的项。
因使用本工具造成的数据丢失或其他损失，由使用者自行承担。

## License

[MIT](./LICENSE)
