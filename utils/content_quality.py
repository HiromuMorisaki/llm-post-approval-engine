"""短文SNS（量より質）の編集品質ゲート。

文字数の投稿可否そのものは ``utils.limits`` が担う。このモジュールは、
R1〜R3の短文SNS下書きに適用する、より狭い編集方針を検査する。
"""
from __future__ import annotations

import math
import re
from typing import Any

from utils.limits import URL_RE, as_post_list, is_x_platform, threads_length, x_weight


STRICT_SOCIAL_PATTERNS = {"r1-daily", "r2-weekly", "r3-zenn-announce"}
SHORT_POST_TYPES = {"failure", "parent_dev", "app_use"}
SHORT_CTA_TYPES = {"question", "app_store", "none"}

# X は全角100〜130字相当。X の重みを2で割った値で判定する。
SHORT_X_EQUIVALENT_MIN = 100
SHORT_X_EQUIVALENT_MAX = 130
SHORT_THREADS_MIN = 200
SHORT_THREADS_MAX = 300

# AI最新情報の速報枠（R1）。画像なし・単発で、通常の短文より短くする。
AI_NEWS_PATTERN = "r1-ai-news"
AI_NEWS_POST_TYPE = "ai_news"
AI_NEWS_X_EQUIVALENT_MIN = 60
AI_NEWS_X_EQUIVALENT_MAX = 100
AI_NEWS_THREADS_MIN = 80
AI_NEWS_THREADS_MAX = 150
NEWS_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

GENERIC_PHRASES = (
    "AIツールまとめ",
    "開発の第一歩",
    "の世界へようこそ",
    "いかがでしょうか",
    "みなさんもぜひ",
)


def is_strict_social_post(post: dict[str, Any]) -> bool:
    return post.get("prompt_pattern") in STRICT_SOCIAL_PATTERNS


def x_full_width_equivalent(text: str) -> int:
    """X の重みを全角字数相当に丸める。URLはX仕様の23重みで数える。"""
    return math.ceil(x_weight(text) / 2)


def _is_url_only(text: str) -> bool:
    stripped = text.strip()
    match = URL_RE.fullmatch(stripped)
    return match is not None


def editorial_issues(platform: str, content: Any, media_url: str = "") -> list[str]:
    """公開文と画像だけから確認できる短文SNSの編集上の問題を返す。"""
    parts = [str(part) for part in as_post_list(content)]
    if not parts or not parts[0].strip():
        return ["本文が空"]

    issues: list[str] = []
    main = parts[0]

    if is_x_platform(platform):
        equivalent = x_full_width_equivalent(main)
        if not SHORT_X_EQUIVALENT_MIN <= equivalent <= SHORT_X_EQUIVALENT_MAX:
            issues.append(
                f"X本文が全角{equivalent}字相当（目標{SHORT_X_EQUIVALENT_MIN}〜{SHORT_X_EQUIVALENT_MAX}字）"
            )
        if len(parts) != 1:
            issues.append("Xは単発1本にする")
    else:
        length = threads_length(main)
        if not SHORT_THREADS_MIN <= length <= SHORT_THREADS_MAX:
            issues.append(
                f"Threads本文が{length}字（目標{SHORT_THREADS_MIN}〜{SHORT_THREADS_MAX}字）"
            )
        if len(parts) > 2 or (len(parts) == 2 and not _is_url_only(parts[1])):
            issues.append("Threadsは単発、またはURLだけの2本目までにする")

    media = str(media_url).strip()
    if not media:
        issues.append("投稿内容と対応するメディアURLがない")
    elif URL_RE.fullmatch(media) is None:
        issues.append("media_urlは公開されたhttp(s) URLにする")

    if re.search(r"。[^\n\r）』」]", main):
        issues.append("句点の後で改行されていない箇所がある")

    found = next((phrase for phrase in GENERIC_PHRASES if phrase in main), None)
    if found:
        issues.append(f"一般論・定型句を含む:{found}")

    return issues


def ai_news_quality_issues(post: dict[str, Any]) -> list[str]:
    """AI最新情報の速報枠を検査する。画像は求めず、短さと一次情報の出典を求める。"""
    parts = [str(part) for part in as_post_list(post.get("content", ""))]
    if not parts or not parts[0].strip():
        return ["本文が空"]

    issues: list[str] = []
    main = parts[0]

    if is_x_platform(str(post.get("platform", ""))):
        equivalent = x_full_width_equivalent(main)
        if not AI_NEWS_X_EQUIVALENT_MIN <= equivalent <= AI_NEWS_X_EQUIVALENT_MAX:
            issues.append(
                f"X本文が全角{equivalent}字相当（目標{AI_NEWS_X_EQUIVALENT_MIN}〜{AI_NEWS_X_EQUIVALENT_MAX}字）"
            )
    else:
        length = threads_length(main)
        if not AI_NEWS_THREADS_MIN <= length <= AI_NEWS_THREADS_MAX:
            issues.append(
                f"Threads本文が{length}字（目標{AI_NEWS_THREADS_MIN}〜{AI_NEWS_THREADS_MAX}字）"
            )
    if len(parts) != 1:
        issues.append("AI最新情報は単発1本にする")

    if re.search(r"。[^\n\r）』」]", main):
        issues.append("句点の後で改行されていない箇所がある")

    found = next((phrase for phrase in GENERIC_PHRASES if phrase in main), None)
    if found:
        issues.append(f"一般論・定型句を含む:{found}")

    if post.get("post_type") != AI_NEWS_POST_TYPE:
        issues.append("post_typeはai_newsにする")

    sources = post.get("fact_sources")
    has_url = isinstance(sources, list) and any(
        URL_RE.fullmatch(str(source).strip()) for source in sources
    )
    if not has_url:
        issues.append("fact_sourcesに一次情報のURLを1件以上入れる")

    if NEWS_DATE_RE.fullmatch(str(post.get("news_date", "")).strip()) is None:
        issues.append("news_dateに発表日をYYYY-MM-DDで入れる")

    if post.get("cta_type") not in SHORT_CTA_TYPES:
        issues.append("cta_typeはquestion / app_store / noneのいずれかにする")

    return issues


def strict_social_quality_issues(post: dict[str, Any]) -> list[str]:
    """短文SNS JSONの本文・画像・監査用メタデータをまとめて検査する。"""
    if post.get("prompt_pattern") == AI_NEWS_PATTERN:
        return ai_news_quality_issues(post)
    if not is_strict_social_post(post):
        return []

    issues = editorial_issues(
        platform=str(post.get("platform", "")),
        content=post.get("content", ""),
        media_url=str(post.get("media_url", "")),
    )

    post_type = post.get("post_type")
    if post_type not in SHORT_POST_TYPES:
        issues.append("post_typeはfailure / parent_dev / app_useのいずれかにする")

    sources = post.get("fact_sources")
    if not isinstance(sources, list) or not sources or not all(str(s).strip() for s in sources):
        issues.append("fact_sourcesに事実台帳（ネタ在庫）の出典を1件以上入れる")

    if not str(post.get("visual_description", "")).strip():
        issues.append("visual_descriptionに画像が証明する1点を書く")

    if post.get("cta_type") not in SHORT_CTA_TYPES:
        issues.append("cta_typeはquestion / app_store / noneのいずれかにする")

    return issues
