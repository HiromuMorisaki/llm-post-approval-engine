"""
ニュースオピニオン下書きをスプレッドシートへ投入するスクリプト。

JSONファイル（drafts/*.json）の posts をそのまま投稿管理シートへ append する。
generate.py の自動生成とは別系統で、レンズ②(AI仕事術・翻訳家)のような
「手動で作った下書きを × (配信可否=不可) として仕込む」用途に使う。

使い方:
    DRAFTS_FILE=drafts/example.json python scripts/insert_drafts.py
    # または
    python scripts/insert_drafts.py drafts/example.json

配信可否は各 post の "is_publishable" に従う（未指定なら安全側の "×"）。
publish.py は 配信可否=="◯" の行のみ投稿するため、× の行は自動投稿されない。
"""
import os
import sys
import json
import uuid

# リポジトリルートを import パスに追加（scripts/ から実行されるため）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from utils.gsheets import append_posts, get_recent_contents
from utils.limits import describe_over_length, over_length_positions
from utils.content_quality import strict_social_quality_issues
from generate import max_similarity_to_recent, contains_banned_term, SIMILARITY_GATE_THRESHOLD


def gated_publishable(post, recent_posts):
    """文字数・類似度・NGワードの機械ゲート。引っかかった投稿は ×（人間ゲート）に降格する。

    字数ゲートはここに置く。重み方式のチェックは R1 と承認ルーチンのプロンプト内にしか
    無く、R2・R3 が作った下書きは素通りしていた（運用時の403エラー調査）。
    投入経路はこのスクリプトに集約されているので、ここで塞げばどのルーティンでも効く。
    """
    content = post.get("content", "")
    platform = post.get("platform", "")
    over = over_length_positions(platform, content)
    if over:
        print(f"Warning: draft is over the limit - {describe_over_length(platform, over)} -> gated to ×")
        return "×"
    sim = max_similarity_to_recent(content, recent_posts)
    if sim >= SIMILARITY_GATE_THRESHOLD:
        print(f"Warning: draft is {sim:.0%} similar to a recent post -> gated to ×")
        return "×"
    banned = contains_banned_term(content)
    if banned:
        print(f"Warning: draft contains banned term '{banned}' -> gated to ×")
        return "×"
    return post.get("is_publishable", "×")


def load_drafts(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    posts = data.get("posts", [])
    if not posts:
        raise SystemExit(f"No 'posts' found in {path}")
    return data, posts


def draft_quality_failures(posts):
    """R1〜R3短文SNSの厳格ルール違反を返す。ほかの下書きには適用しない。"""
    return [
        (index, issues)
        for index, post in enumerate(posts, 1)
        if (issues := strict_social_quality_issues(post))
    ]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DRAFTS_FILE")
    if not path:
        raise SystemExit("Usage: python scripts/insert_drafts.py <drafts.json> (or set DRAFTS_FILE)")
    if not os.path.exists(path):
        raise SystemExit(f"Drafts file not found: {path}")

    data, posts = load_drafts(path)
    print(f"Loaded {len(posts)} draft(s) from {path} (lens: {data.get('lens', 'n/a')})")

    failures = draft_quality_failures(posts)
    if failures:
        for index, issues in failures:
            print(f"Short social quality gate: post {index} -> " + " / ".join(issues))
        raise SystemExit("Short social quality gate failed; no rows were appended.")

    try:
        recent_posts = get_recent_contents(limit=40)
    except Exception as e:
        print(f"Warning: failed to fetch recent posts ({e}); gates run without dedup context.")
        recent_posts = []

    formatted = []
    for p in posts:
        formatted.append({
            "id": p.get("id") or str(uuid.uuid4())[:8],
            "scheduled_at": p.get("scheduled_at", ""),
            "platform": p.get("platform", "X/Threads"),
            "prompt_pattern": p.get("prompt_pattern", "news-opinion"),
            # 下書きは安全側の × をデフォルトに。◯指定でも品質ゲートに引っかかれば × に降格
            "is_publishable": gated_publishable(p, recent_posts),
            "is_note_promo": p.get("is_note_promo", "×"),
            "note_no": p.get("note_no", ""),
            "content": p.get("content", ""),
            "status": p.get("status", "未対応"),
            "deletion": p.get("deletion", "-"),
            "post_url": p.get("post_url", ""),
            "media_url": p.get("media_url", ""),
        })

    append_posts(formatted)
    print(f"Successfully appended {len(formatted)} draft row(s) to the spreadsheet.")


if __name__ == "__main__":
    main()
