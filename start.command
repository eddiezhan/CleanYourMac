#!/bin/bash
# 双击启动本地空间清理面板
# 作用：检查环境 → 启动本地服务 → 自动打开浏览器
cd "$(dirname "$0")" || exit 1

PORT="${CMM_PORT:-8765}"

# 定位 python3（不写死路径，任何机器都能用）
PY="$(command -v python3)"
if [ -z "$PY" ]; then
  echo "未找到 python3，请先安装 Python 3.9 或更高版本"
  read -r -p "按回车退出..."
  exit 1
fi

# 版本门禁：代码用到 dict[str, Any] 等 3.9+ 语法
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "当前 Python 版本过低：$("$PY" -V 2>&1)，需要 3.9+"
  read -r -p "按回车退出..."
  exit 1
fi

# 端口已被占用时，直接打开已有实例，避免重复启动
if command -v lsof >/dev/null 2>&1 && lsof -i ":$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "面板已在运行，正在打开浏览器"
  open "http://127.0.0.1:$PORT"
  exit 0
fi

echo "启动中… http://127.0.0.1:$PORT"
echo "提示：浏览器会自动打开；关闭此终端窗口即停止服务。"
echo "-----------------------------------------------"
( sleep 1; open "http://127.0.0.1:$PORT" ) &
exec "$PY" server.py
