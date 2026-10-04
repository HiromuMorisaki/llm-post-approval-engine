"""投稿の文字数上限の判定。

X は「重み」方式で数える（全角2・半角1・URLは実長によらず23、上限280 = 全角140字相当）。
Threads は改行を除いた文字数で500字。

2026-09-20 の調査（運用時の403エラー調査）で、X の403エラー142件が
すべて280重み超過だったことが分かった。X API v2 は280を超える投稿を Premium の機能として
扱うため、権限の無いアカウントには重複エラーと同じ 403 "You are not permitted to
perform this action." を返す。メッセージから区別できないので、投げる前に弾く。

判定式は prompts/daily-drafts.example.md などのエージェント向けプロンプトの
機械チェックと同じもの。基準を変えるときはここだけを直す。
"""
import re
import unicodedata

X_WEIGHT_LIMIT = 280
THREADS_CHAR_LIMIT = 500

URL_RE = re.compile(r'https?://\S+')
URL_WEIGHT = 23

# X の重み付け設定で「1」として数える符号位置の範囲（それ以外は2）。
_LIGHT_RANGES = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))
# 異体字セレクタとZWJは0として数える（絵文字の合字で二重に数えないため）。
_ZERO_WEIGHT = (0xFE0F, 0x200D)


def _char_weight(char: str) -> int:
    code = ord(char)
    if code in _ZERO_WEIGHT:
        return 0
    return 1 if any(lo <= code <= hi for lo, hi in _LIGHT_RANGES) else 2


def x_weight(text: str) -> int:
    """X の重み。改行は数えない（ペルソナ規範の「改行除く」に合わせる）。"""
    normalized = unicodedata.normalize('NFC', text.replace('\n', ''))
    body = sum(_char_weight(c) for part in URL_RE.split(normalized) for c in part)
    return body + URL_WEIGHT * len(URL_RE.findall(normalized))


def threads_length(text: str) -> int:
    """Threads の文字数。改行は数えない。"""
    return len(text.replace('\n', '').replace('\r', ''))


def is_x_platform(platform: str) -> bool:
    return (platform or "").strip().lower() in ("x", "twitter")


def limit_for(platform: str) -> int:
    return X_WEIGHT_LIMIT if is_x_platform(platform) else THREADS_CHAR_LIMIT


def measure(platform: str, text: str) -> int:
    return x_weight(text) if is_x_platform(platform) else threads_length(text)


def as_post_list(content):
    """シート/下書きの content を投稿の配列にそろえる。"""
    if isinstance(content, list):
        return content
    return [content]


def over_length_positions(platform: str, content) -> list[tuple[int, int]]:
    """上限を超えた投稿の (何本目, 実測値) を返す。1本目は 1。

    ツリーの途中が超過していると、そこまでは投稿が成功してから失敗するため、
    送信前に必ず全要素を調べる。
    """
    limit = limit_for(platform)
    return [(index + 1, measure(platform, text))
            for index, text in enumerate(as_post_list(content))
            if measure(platform, text) > limit]


def describe_over_length(platform: str, over: list[tuple[int, int]]) -> str:
    """スキップ理由としてシートに残す短い説明。"""
    limit = limit_for(platform)
    unit = "重み" if is_x_platform(platform) else "字"
    parts = ", ".join(f"{position}本目{value}{unit}" for position, value in over)
    return f"文字数超過({parts} / 上限{limit}{unit})"
