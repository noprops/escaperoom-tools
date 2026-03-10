#!/bin/bash
# deploy.sh — package フォルダの変更を git add / commit / push する

set -e

cd "$(dirname "$0")"

if [ -z "$(git status --porcelain)" ]; then
    echo "変更なし。コミットするものはありません。"
    exit 0
fi

echo ""
echo "=== 変更ファイル ==="
git status --short
echo ""

read -rp "コミットメッセージ (空欄で自動生成): " MSG
if [ -z "$MSG" ]; then
    MSG="Update package $(date '+%Y-%m-%d %H:%M')"
fi

git add .
git commit -m "$MSG"
git push

echo ""
echo "✅ push 完了: $(git remote get-url origin)"
