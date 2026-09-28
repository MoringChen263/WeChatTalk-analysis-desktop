#!/usr/bin/env bash
# 把本地构建好的发布包挂到 GitHub Releases。
#
# 为什么不用 `git push` / `gh release create`：
#   本机（Windows + 本地代理）对 github.com 的 POST 会被拦，git 协议经代理也会 TLS 握手失败；
#   而直连 api.github.com 是通的。所以这里走 REST API 手工构造请求，效果与 git push 一致。
#
# 前置：
#   export GITHUB_TOKEN='ghp_xxx'      # classic token，需 repo 权限
#   bash scripts/publish_release.sh <zip路径> [版本号]
#
# 产物：
#   https://github.com/<owner>/<repo>/releases/tag/<版本号>
#   （单文件 >100MB 不能进 git tree，所以发布包只能走 Releases —— 这是硬限制，不是选填）

set -euo pipefail

OWNER="MoringChen263"
REPO="jev-chat-analyzer"
API="https://api.github.com"

ZIP="${1:-}"
TAG="${2:-v0.1.0}"

if [ -z "$ZIP" ] || [ ! -f "$ZIP" ]; then
  echo "用法: GITHUB_TOKEN=xxx bash scripts/publish_release.sh <zip路径> [版本号]"
  exit 1
fi
if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "缺少 GITHUB_TOKEN 环境变量"
  exit 1
fi

# 本机环境：本地代理会拦 github.com 的 POST，全部直连（不走代理）
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
export no_proxy='*' NO_PROXY='*'

NAME="$(basename "$ZIP")"
SIZE="$(wc -c < "$ZIP" | tr -d ' ')"

echo "== [1/5] 隐私门：发布包不得含用户数据 =="
# 打包脚本内部已有这道门，这里再挡一次：包是给人下载的， Leak 就是事故。
BAD="$(python - "$ZIP" <<'PY'
import re, sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
bad = [n for n in z.namelist()
       if re.search(r'(^|/)config\.json$|\.sqlite3$|\.log$|_staging/|/(logs|memory)/', n)]
print("\n".join(bad))
PY
)"
if [ -n "$BAD" ]; then
  echo ">>> 隐私门失败，包内含敏感文件："
  echo "$BAD"
  exit 1
fi
echo "PASS：无 config.json / 记忆库 / 日志"

echo "== [2/5] 校验 =="
echo "文件: $NAME  大小: $((SIZE / 1048576)) MB"
if [ "$SIZE" -gt 2147483647 ]; then
  echo "超过 GitHub 单文件上限"; exit 1
fi
echo "SHA256: $(sha256sum "$ZIP" | cut -d' ' -f1)"

echo "== [3/5] 创建 tag =="
curl -sS --max-time 30 -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d "{\"ref\":\"refs/tags/$TAG\",\"object\":\"$(git rev-parse HEAD)\",\"type\":\"commit\"}" \
  "$API/repos/$OWNER/$REPO/git/refs" > /dev/null 2>&1 \
  && echo "tag $TAG 已创建" || echo "tag 已存在，跳过"

echo "== [4/5] 创建 Release =="
REL_JSON="$(curl -sS --max-time 30 -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d "{\"tag_name\":\"$TAG\",\"name\":\"jev-chat-analyzer $TAG\",\"body\":\"见下方 Assets\",\"draft\":false,\"prerelease\":false}" \
  "$API/repos/$OWNER/$REPO/releases")"
REL_ID="$(printf '%s' "$REL_JSON" | grep -o '"id": *[0-9]*' | head -1 | grep -o '[0-9]*')"
if [ -z "$REL_ID" ]; then
  echo "创建失败，响应："; printf '%s\n' "$REL_JSON" | head -c 400; exit 1
fi
echo "release id=$REL_ID"

echo "== [5/5] 上传附件（大文件，可能较慢） =="
curl -sS --max-time 1800 -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Content-Type: application/octet-stream" \
  --data-binary "@$ZIP" \
  "$API/repos/$OWNER/$REPO/releases/$REL_ID/assets?name=$NAME" \
  | grep -o '"browser_download_url": *"[^"]*"' | head -1

echo "完成：https://github.com/$OWNER/$REPO/releases/tag/$TAG"
