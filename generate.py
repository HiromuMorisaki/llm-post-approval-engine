import difflib
import json
import os
import uuid
from datetime import date, datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Sequence, Union
from dotenv import load_dotenv

from utils.limits import describe_over_length, over_length_positions

# 過去投稿とこの類似度以上なら自動投稿せず ×（人間ゲート）に落とす。
# X は重複気味の投稿を 403 で拒否するため、投げる前に手前で止める。
SIMILARITY_GATE_THRESHOLD = 0.5

# note誘導投稿を出す曜日（月=0）。火・金の週2回に絞る。
# リンク付き投稿は p90=272 View で頭打ち（リンクなしは p90=1,455）。毎日1枠を誘導に使うと、
# 伸びる余地のある枠が週7本潰れるため、頻度を落として通常投稿の母数を増やす。
NOTE_PROMO_WEEKDAYS = (1, 4)

# 1日の投稿枠（JST）。08/12/19時は中央値300前後、07/20/21時は50前後のため勝ち枠のみ使う。
SCHEDULE_TIMES = ("08:00:00", "12:00:00", "19:00:00")

# 3枠目（19:00）だけが note誘導の対象になる。
NOTE_PROMO_SLOT = 2

# ペルソナの事実と矛盾する既知のNGワード。含まれていたら ×（人間ゲート）に落とす。
# LLMの自己ファクトチェックをすり抜けた実例（使っていないモデル名・課金形態の混同）への機械ガード。
# 値は運用者の事実台帳に依存するので、自分のペルソナに合わせて差し替える。
FACT_BANNED_TERMS = [
    "Claude 3.7", "Claude 3.5", "GPT-3.5", "Gemini 1.5",  # 使っていない旧モデル（ニュース言及があり得る語は入れない）
    "API課金", "APIのコスト", "API経由でのコスト",  # 課金はPro定額のみでAPI従量課金はしていない
    "2本契約", "2アカウント", "並列アカウント",  # 事実と異なる契約形態
]


def contains_banned_term(content: Union[str, List[str]]) -> Optional[str]:
    """投稿本文に事実矛盾の既知NGワードが含まれるか判定する。"""
    text = " ".join(content) if isinstance(content, list) else str(content)
    return next((term for term in FACT_BANNED_TERMS if term in text), None)


def _first_text(cell_or_list):
    """シートのセル値（JSON文字列）または content リストから1本目の本文を取り出す。"""
    value = cell_or_list
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    if isinstance(value, list) and value:
        return str(value[0])
    return str(value)


def max_similarity_to_recent(content: Union[str, List[str]], recent_posts: Sequence[str]) -> float:
    """生成した投稿の1本目と、直近投稿それぞれの1本目との最大類似度を返す。"""
    candidate = _first_text(content)[:200]
    best = 0.0
    for recent in recent_posts:
        ratio = difflib.SequenceMatcher(None, candidate, _first_text(recent)[:200]).ratio()
        best = max(best, ratio)
    return best


def is_note_promo_day(when: date) -> bool:
    """その日に note 誘導投稿を出す曜日かを判定する。"""
    return when.weekday() in NOTE_PROMO_WEEKDAYS


def _build_row(post: Dict[str, Any], slot_index: int, platform: str, sched_at: str,
               recent_posts: Sequence[str], promo_pattern: Optional[int],
               target_note: Optional[Dict[str, str]], is_promo_slot: bool) -> Dict[str, Any]:
    """生成結果1件をシート1行分のdictに整形する（既存のdictは変更しない）。"""
    content = post['content']
    over = over_length_positions(platform, content)
    similarity = max_similarity_to_recent(content, recent_posts)
    banned = contains_banned_term(content)
    if over:
        print(f"Warning: {platform} post {slot_index + 1} is over the limit - "
              f"{describe_over_length(platform, over)} -> gated to ×")
    if similarity >= SIMILARITY_GATE_THRESHOLD:
        print(f"Warning: {platform} post {slot_index + 1} is {similarity:.0%} similar to a recent post -> gated to ×")
    if banned:
        print(f"Warning: {platform} post {slot_index + 1} contains banned term '{banned}' -> gated to ×")
    gated = bool(over) or similarity >= SIMILARITY_GATE_THRESHOLD or banned

    return {
        "id": str(uuid.uuid4())[:8],
        "scheduled_at": sched_at,
        "platform": platform,
        "prompt_pattern": f"Pattern {promo_pattern}" if is_promo_slot else "Standard",
        "is_publishable": "×" if gated else "◯",
        "is_note_promo": "◯" if is_promo_slot else "×",
        "note_no": target_note['title'] if (is_promo_slot and target_note) else "",
        "content": content,
        "status": "未対応",
        "deletion": "-",
        "post_url": "",
        "media_url": "",
    }


def build_post_rows(posts_data: Sequence[Dict[str, Any]], recent_posts: Sequence[str],
                    today_str: str, promo_pattern: Optional[int],
                    target_note: Optional[Dict[str, str]], promo_day: bool) -> List[Dict[str, Any]]:
    """生成結果をシート行のリストに整形する。LLM・シートには触れない純粋関数。"""
    by_platform = {
        "X": [p for p in posts_data if p.get('platform') == 'X'],
        "Threads": [p for p in posts_data if p.get('platform') == 'Threads'],
    }

    rows = []
    for slot_index, time_str in enumerate(SCHEDULE_TIMES):
        sched_at = f"{today_str} {time_str}"
        is_promo_slot = promo_day and slot_index == NOTE_PROMO_SLOT
        for platform in ("X", "Threads"):
            posts = by_platform[platform]
            if slot_index >= len(posts):
                continue
            rows.append(_build_row(
                post=posts[slot_index], slot_index=slot_index, platform=platform,
                sched_at=sched_at, recent_posts=recent_posts, promo_pattern=promo_pattern,
                target_note=target_note, is_promo_slot=is_promo_slot,
            ))
    return rows


load_dotenv()

from utils.llm import generate_social_posts
from utils.gsheets import append_posts, get_next_note, get_recent_contents

def main() -> None:
    JST = timezone(timedelta(hours=+9), 'JST')
    now_jst = datetime.now(JST)
    today_str = now_jst.strftime("%Y-%m-%d")

    promo_day = is_note_promo_day(now_jst)
    # 非promo日は get_next_note() を呼ばない。呼ぶとNotesシートの最終投稿日時が進み、
    # 実際には紹介していない記事がローテーション済み扱いになってしまう。
    target_note = None
    promo_pattern = None
    if promo_day:
        print("Fetching next Note promo target...")
        target_note = get_next_note()
        promo_pattern = 1 if now_jst.timetuple().tm_yday % 2 == 0 else 2
        if target_note:
            print(f"Target Note: {target_note['title']} (Pattern {promo_pattern})")
        else:
            print("No Note promo target available; slot 3 falls back to a normal post.")
    else:
        print(f"{now_jst:%a} is not a Note promo day; generating 3 normal posts per platform.")

    print("Fetching recent posts for dedup...")
    try:
        recent_posts = get_recent_contents(limit=40)
    except Exception as e:
        print(f"Warning: failed to fetch recent posts ({e}); continuing without dedup context.")
        recent_posts = []

    print("Generating posts via LLM...")
    posts_data = generate_social_posts(
        target_note=target_note,
        promo_pattern=promo_pattern,
        recent_posts=recent_posts,
    )

    if len(posts_data) != 6:
        print(f"Warning: Expected 6 posts, got {len(posts_data)}")

    formatted_posts = build_post_rows(
        posts_data=posts_data,
        recent_posts=recent_posts,
        today_str=today_str,
        promo_pattern=promo_pattern,
        target_note=target_note,
        # 記事が取れなければ生成されるのは通常投稿なので、promo として記録しない
        promo_day=promo_day and target_note is not None,
    )

    print("Appending to Google Sheets...")
    append_posts(formatted_posts)
    print(f"Successfully generated and appended {len(formatted_posts)} posts.")

if __name__ == "__main__":
    main()
