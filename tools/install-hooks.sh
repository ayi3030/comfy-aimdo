#!/bin/sh
# 安装 pre-push 强制审查钩子（ComfyUI Intel XPU 适配团 / 解耦林）
#
# 用法（在仓库根目录执行）：
#   sh tools/install-hooks.sh
#
# 作用：
#   把 tools/pre-push-check.py 接到 .git/hooks/pre-push，
#   今后任何 `git push` 都会先跑审查门禁（action 存在性 / 浮动引用警告 /
#   Python 语法 / URL 可达性），存在阻断项时拒绝推送。
#
# 注意：.git/hooks 不被版本管理，故每位协作者 clone 后需各自跑一次本脚本。

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_DIR="$ROOT/.git/hooks"
HOOK="$HOOK_DIR/pre-push"
CHECKER="$ROOT/tools/pre-push-check.py"

if [ ! -f "$CHECKER" ]; then
  echo "✗ 找不到 $CHECKER" >&2
  exit 1
fi

# 选一个可用的 python：优先 PATH 里的，回退到本机托管 python
PY=""
if command -v python >/dev/null 2>&1; then
  PY=python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY="E:/HE/WorkBuddyData/binaries/python/versions/3.13.12/python.exe"
fi

mkdir -p "$HOOK_DIR"

cat > "$HOOK" <<EOF
#!/bin/sh
# 自动生成，请勿手改。来源：tools/install-hooks.sh
exec "$PY" "$CHECKER" "\$@"
EOF

chmod +x "$HOOK"

echo "✅ 已安装 pre-push 钩子 -> $HOOK"
echo "   审查逻辑见 tools/pre-push-check.py"
echo "   浮动 action 引用可用 tools/pin-actions.py 钉到 SHA"
echo ""
echo "试用："
echo "   git push --dry-run fork master   # 触发审查但不真正推送"
