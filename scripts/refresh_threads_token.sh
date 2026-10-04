#!/usr/bin/env bash
#
# Threads の long-lived アクセストークンを th_refresh_token で延長し、
# 新しいトークンを同名の Actions Secret に書き戻す。
#
# 前提:
#   - GH_TOKEN … 対象リポジトリの "Secrets: read/write" を持つ PAT（gh secret set 用）
#   - THREADS_ACCESS_TOKEN 系 … 現在の各トークン（repo secrets を env で渡す。未設定はスキップ）
#   - TARGET_REPO … "owner/repo"（省略時は下のデフォルト）
#
# 注意:
#   - リフレッシュは「有効・long-lived・発行から24時間以上経過」のトークンにのみ効く。
#     生成直後(24h以内)に実行すると失敗するので、その場合は翌日以降に。
#   - トークン値はログに出さない（set -x は使わない）。GitHub は登録済みSecretを自動マスクする。
set -euo pipefail

REPO="${TARGET_REPO:?TARGET_REPO (owner/repo) を設定してください}"

json_get() { python3 -c 'import sys,json; print(json.load(sys.stdin).get(sys.argv[1],""))' "$1"; }

refresh_one() {
  local name="$1" cur="$2"
  if [ -z "$cur" ]; then
    echo "::notice::${name} is not set — skipping."
    return 0
  fi
  echo "== ${name} =="
  local resp new expires
  resp="$(curl -sS "https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token=${cur}")" \
    || { echo "::error::${name}: refresh request failed."; return 1; }
  new="$(printf '%s' "$resp" | json_get access_token || true)"
  expires="$(printf '%s' "$resp" | json_get expires_in || true)"
  if [ -z "$new" ]; then
    local msg
    msg="$(printf '%s' "$resp" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("error",{}).get("message","(no message)"))' 2>/dev/null || echo "(unparseable response)")"
    echo "::error::${name}: no access_token returned — ${msg}"
    return 1
  fi
  # 新トークンの健全性チェック（/me で id が解決できるか。トークンは出力しない）
  local meid
  meid="$(curl -sS "https://graph.threads.net/v1.0/me?fields=id&access_token=${new}" | json_get id || true)"
  if [ -z "$meid" ]; then
    echo "::error::${name}: refreshed token failed /me validation."
    return 1
  fi
  local days=$(( ${expires:-0} / 86400 ))
  echo "${name}: OK (account id=${meid}, expires in ~${days} days). Updating secret..."
  printf '%s' "$new" | gh secret set "$name" --repo "$REPO"
  echo "${name}: secret updated."
}

rc=0
refresh_one "THREADS_ACCESS_TOKEN"         "${THREADS_ACCESS_TOKEN:-}"         || rc=1
refresh_one "THREADS_ACCESS_TOKEN_SHIGOTO" "${THREADS_ACCESS_TOKEN_SHIGOTO:-}" || rc=1
refresh_one "THREADS_ACCESS_TOKEN_PAPA"    "${THREADS_ACCESS_TOKEN_PAPA:-}"    || rc=1

if [ "$rc" -ne 0 ]; then
  echo "::error::One or more Threads tokens failed to refresh. Check logs above."
fi
exit "$rc"
