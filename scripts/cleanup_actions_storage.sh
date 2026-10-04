#!/usr/bin/env bash
#
# GitHub Actions のストレージ（無料枠 0.5GB/月）を空けるため、古い workflow run の
# ログと、残っていればアーティファクトを削除する。
#
# Actions ストレージは「アーティファクト＋実行ログ」で計算される。このリポジトリは
# upload-artifact を使っていないので、実質すべてが実行ログ。publish.yml が毎時動くため
# run 数が増え続け、放置すると無料枠を食い潰す。
#
# 前提:
#   - GH_TOKEN … Actions: read/write（ワークフローからは GITHUB_TOKEN + permissions.actions: write）
#   - TARGET_REPO … "owner/repo"（省略時は下のデフォルト）
#
# 環境変数:
#   RETENTION_DAYS … これより古い run を削除対象にする（既定 14）
#   DRY_RUN        … true なら削除せず対象を一覧表示するだけ（既定 false）
#   MAX_DELETIONS  … 1回の実行で削除する run の上限。API レート制限よけ（既定 500）
set -euo pipefail

REPO="${TARGET_REPO:?TARGET_REPO (owner/repo) を設定してください}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DRY_RUN="${DRY_RUN:-false}"
MAX_DELETIONS="${MAX_DELETIONS:-500}"

CUTOFF="$(date -u -d "${RETENTION_DAYS} days ago" +%Y-%m-%dT%H:%M:%SZ)"
# クエリ文字列に生の "<" を入れないよう %3C にエンコードする
CUTOFF_Q="%3C${CUTOFF}"

echo "repo=${REPO} retention=${RETENTION_DAYS}d cutoff=${CUTOFF} dry_run=${DRY_RUN}"

# ---- 1. アーティファクト（現状は生成していないが、過去分や手動アップロード分の保険） ----
echo "== artifacts =="
artifacts="$(gh api --paginate "/repos/${REPO}/actions/artifacts?per_page=100" \
  --jq '.artifacts[] | [.id, .size_in_bytes, .created_at, .name] | @tsv' || true)"

art_count=0
art_bytes=0
if [ -n "$artifacts" ]; then
  while IFS=$'\t' read -r id size created name; do
    [ -n "${id:-}" ] || continue
    art_count=$((art_count + 1))
    art_bytes=$((art_bytes + ${size:-0}))
    if [ "$DRY_RUN" = "true" ]; then
      echo "would delete artifact ${id} (${size} bytes, ${created}, ${name})"
    else
      gh api -X DELETE "/repos/${REPO}/actions/artifacts/${id}" >/dev/null \
        && echo "deleted artifact ${id} (${size} bytes, ${name})" \
        || echo "::warning::failed to delete artifact ${id}"
    fi
  done <<< "$artifacts"
fi
echo "artifacts targeted: ${art_count} ($((art_bytes / 1024 / 1024)) MiB)"

# ---- 2. カットオフより古い完了済み run（＝ログ本体） ----
echo "== workflow runs older than ${CUTOFF} =="
run_ids="$(gh api --paginate \
  "/repos/${REPO}/actions/runs?status=completed&created=${CUTOFF_Q}&per_page=100" \
  --jq '.workflow_runs[].id' || true)"

run_count=0
deleted=0
if [ -n "$run_ids" ]; then
  while read -r id; do
    [ -n "${id:-}" ] || continue
    run_count=$((run_count + 1))
    if [ "$run_count" -gt "$MAX_DELETIONS" ]; then
      echo "::notice::hit MAX_DELETIONS=${MAX_DELETIONS}; 残りは次回の実行で削除する。"
      break
    fi
    if [ "$DRY_RUN" = "true" ]; then
      echo "would delete run ${id}"
    else
      if gh api -X DELETE "/repos/${REPO}/actions/runs/${id}" >/dev/null 2>&1; then
        deleted=$((deleted + 1))
      else
        echo "::warning::failed to delete run ${id}"
      fi
    fi
  done <<< "$run_ids"
fi

if [ "$DRY_RUN" = "true" ]; then
  echo "runs targeted: ${run_count} (dry run — 何も削除していない)"
  echo "::notice::dry run: artifacts=${art_count}, runs=${run_count} が削除対象"
else
  echo "runs deleted: ${deleted} / targeted ${run_count}"
  echo "::notice::cleanup done (artifacts=${art_count}, runs=${deleted})"
fi
