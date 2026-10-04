import os
import time
from collections import namedtuple
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Sequence, Set

import requests
import tweepy
from dotenv import load_dotenv

from utils.gsheets import (COL_PUBLISHED_AT as PUBLISHED_AT_HEADER, append_metrics_log,
                           call_with_retry, get_recorded_checkpoints, get_worksheet,
                           header_column)

# Load environment variables
load_dotenv()

JST = timezone(timedelta(hours=+9), 'JST')

# 投稿から何時間後に計測するか。1投稿につき各チェックポイントを1回だけ記録する。
# 1h=初速（配信の一次判定）、24h=ほぼ本番の値、168h=最終値。
# 値を上書きせず追記するので、投稿ごとの伸び方と、週をまたいだ同条件比較ができる。
MEASUREMENT_CHECKPOINTS_H = (1, 24, 168)

# 1回の実行で計測する上限。既存の投稿が一斉に168hチェックポイントに入る初回実行で、
# Actions の実行時間を使い切らないための安全弁。溢れた分は次回以降の実行で消化される。
MAX_MEASUREMENTS_PER_RUN = 150

# X API Free ではツイートを読めない（401/403）。実測でもX投稿は1件も計測できていないため
# 既定では対象外にする。Basic 以上に上げたら MEASURE_X=true で有効化する。
MEASURE_X = os.environ.get('MEASURE_X', '').strip().lower() == 'true'

SCHEDULED_AT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")

# 投稿管理シートの列（0始まり）
COL_NO, COL_SCHEDULED, COL_CHANNEL, COL_STATUS, COL_POST_URL = 0, 2, 3, 9, 11
# 実公開時刻の列（0始まり）。シート側に列を足すまでは存在しないので、
# main() はヘッダから引き直し、無ければ None のまま予約時刻基準で測る。
COL_PUBLISHED_AT = 17
# 最新スナップショットを書き戻す列（1始まり）
CELL_IMPRESSIONS, CELL_LIKES, CELL_REPLIES, CELL_MEASURED_AT = 14, 15, 16, 17

# 経過時間をどちらの時刻から測ったか。基準が混ざった期間をまたいで比較しないために残す。
BASIS_PUBLISHED = "実公開"
BASIS_SCHEDULED = "予約"

# 引き算の結果が負になったときの記録。0（読者の返信が無かった）と区別する。
MISSING = "欠測"

Measurement = namedtuple(
    "Measurement",
    "row_num no platform media_ids head_id thread_len checkpoint_h age_h scheduled_at basis")


def parse_scheduled_at(value: str) -> Optional[datetime]:
    """シートの日時セル（予約時間・実公開時刻）をJSTのdatetimeとして読む。読めなければ None。"""
    text = (value or "").strip()
    for fmt in SCHEDULED_AT_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def due_checkpoint(age_h: float, recorded: Set[int]) -> Optional[int]:
    """今回記録すべきチェックポイントを返す。無ければ None。

    「通過済みのうち最大のもの」を1つだけ返す。古い投稿を初めて計測するときに
    1h/24h まで遡って同じ値で埋めてしまうと、観測していない数字を捏造することになるため。
    """
    passed = [c for c in MEASUREMENT_CHECKPOINTS_H if age_h >= c]
    if not passed:
        return None
    latest = max(passed)
    return None if latest in recorded else latest


def reader_replies(total_replies: int, thread_len: int):
    """ツリー全体の返信数から自分の続き投稿を引く。

    ツリーの2本目以降は親への返信として数えられるので、引かないと読者の反応を
    水増しする。負になったら 0 ではなく「欠測」（続き投稿が削除された等、
    引けない状態と、読者の返信が0件だった状態は別物）。
    """
    value = total_replies - (thread_len - 1)
    return value if value >= 0 else MISSING


def _media_ids(platform: str, post_url: str) -> List[str]:
    """実際の投稿URL列から、計測に使うIDを取り出す。"""
    parts = [p.strip() for p in post_url.split(",") if p.strip()]
    if platform.lower() in ("x", "twitter"):
        return [p.split("status/")[-1].split("?")[0] for p in parts if "status/" in p]
    return parts


def _published_at(row: Sequence[str], published_at_col: Optional[int]) -> Optional[datetime]:
    """実公開時刻を読む。列がまだ無い・空・読めない場合は None（予約時刻基準に落ちる）。"""
    if published_at_col is None or len(row) <= published_at_col:
        return None
    return parse_scheduled_at(row[published_at_col])


def _is_measurable(platform: str, measure_x: bool) -> bool:
    if platform.lower() in ("x", "twitter"):
        return measure_x
    return platform.lower().startswith("threads")


def plan_measurements(data_rows: Sequence[Sequence[str]], recorded: Dict[int, Set[int]],
                      now: datetime, measure_x: bool = MEASURE_X,
                      limit: int = MAX_MEASUREMENTS_PER_RUN,
                      published_at_col: Optional[int] = None) -> List[Measurement]:
    """シートの行から「今回計測すべき対象」を選ぶ。外部I/Oは行わない純粋関数。

    チェックポイントの小さい順に優先する。1h・24h は取り逃すと二度と観測できないのに対し、
    168h は次回以降の実行でも取れるため。
    """
    planned = []
    for index, row in enumerate(data_rows):
        row_num = index + 2  # ヘッダが1行目
        if len(row) <= COL_POST_URL:
            continue
        if row[COL_STATUS] != "完了" or not row[COL_POST_URL].strip():
            continue

        platform = row[COL_CHANNEL]
        if not _is_measurable(platform, measure_x):
            continue

        scheduled_at = parse_scheduled_at(row[COL_SCHEDULED])
        if scheduled_at is None:
            continue

        published_at = _published_at(row, published_at_col)
        base = published_at or scheduled_at
        basis = BASIS_PUBLISHED if published_at else BASIS_SCHEDULED

        age_h = (now - base).total_seconds() / 3600
        checkpoint = due_checkpoint(age_h, recorded.get(row_num, set()))
        if checkpoint is None:
            continue

        media_ids = _media_ids(platform, row[COL_POST_URL])
        if not media_ids:
            continue

        planned.append(Measurement(
            row_num=row_num, no=row[COL_NO], platform=platform, media_ids=media_ids,
            head_id=media_ids[0], thread_len=len(media_ids),
            checkpoint_h=checkpoint, age_h=age_h, scheduled_at=row[COL_SCHEDULED],
            basis=basis,
        ))

    planned.sort(key=lambda m: (m.checkpoint_h, m.age_h))
    return planned[:limit]


def fetch_twitter_metrics(client, tweet_ids):
    try:
        response = client.get_tweets(ids=tweet_ids, tweet_fields=["public_metrics"])
        if not response.data:
            # 取得できなかっただけで「0閲覧」ではない。None を返せばチェックポイントが
            # 未記録のまま残り、due_checkpoint() が次回も同じ計測を返す。
            print(f"No tweet data returned for {tweet_ids}; leaving the checkpoint unrecorded.")
            return None

        impressions = 0
        likes = 0
        replies = 0

        for tweet in response.data:
            metrics = tweet.public_metrics
            impressions += metrics.get('impression_count', 0)
            likes += metrics.get('like_count', 0)
            replies += metrics.get('reply_count', 0)

        return impressions, likes, replies
    except tweepy.errors.Forbidden:
        print(f"Twitter API Forbidden (403) for {tweet_ids}. Skipping... (Check if you have Basic tier or higher)")
        return None
    except Exception as e:
        print(f"Error fetching Twitter metrics for {tweet_ids}: {e}")
        return None


def fetch_threads_metrics(access_token, media_ids):
    """ツリーの指定IDぶんの (閲覧, いいね, 返信) を合算して返す。

    1つでも取得に失敗したら None を返す。取れた分だけ足して返すと、実際より小さい値を
    「観測した数字」として計測ログに残すことになるため（未記録なら次回そのまま再取得される）。
    """
    impressions = 0
    likes = 0
    replies = 0

    try:
        for media_id in media_ids:
            url = f"https://graph.threads.net/v1.0/{media_id}/insights"
            params = {
                "metric": "views,likes,replies",
                "access_token": access_token
            }
            response = requests.get(url, params=params)
            data = response.json()

            if "data" not in data:
                print(f"Error fetching Threads metrics for {media_id}: {data}")
                return None

            for insight in data["data"]:
                val = insight.get("values", [{}])[0].get("value", 0)
                if insight["name"] == "views":
                    impressions += val
                elif insight["name"] == "likes":
                    likes += val
                elif insight["name"] == "replies":
                    replies += val
            time.sleep(1)  # prevent rate limits

        return impressions, likes, replies
    except Exception as e:
        print(f"Error fetching Threads metrics for {media_ids}: {e}")
        return None


def _build_clients():
    """計測に使う各プラットフォームのクライアント／トークンを用意する。"""
    twitter_keys = (os.environ.get('TWITTER_API_KEY'), os.environ.get('TWITTER_API_SECRET'),
                    os.environ.get('TWITTER_ACCESS_TOKEN'), os.environ.get('TWITTER_ACCESS_SECRET'))
    twitter_client = None
    if MEASURE_X and all(twitter_keys):
        twitter_client = tweepy.Client(
            consumer_key=twitter_keys[0], consumer_secret=twitter_keys[1],
            access_token=twitter_keys[2], access_token_secret=twitter_keys[3]
        )

    threads_tokens = {
        "threads": os.environ.get('THREADS_ACCESS_TOKEN'),
        "threads_shigoto": os.environ.get('THREADS_ACCESS_TOKEN_SHIGOTO'),
        "threads_papa": os.environ.get('THREADS_ACCESS_TOKEN_PAPA'),
    }
    return twitter_client, threads_tokens


def _fetch(measurement, media_ids, twitter_client, threads_tokens):
    """指定IDぶんの計測値を取得する。取れなければ None（チェックポイントは未記録のまま残る）。"""
    platform = measurement.platform.lower()
    if platform in ("x", "twitter"):
        if not twitter_client:
            return None
        return fetch_twitter_metrics(twitter_client, media_ids)

    token = threads_tokens.get(platform)
    if not token:
        print(f"No access token for platform '{measurement.platform}'; skipping row {measurement.row_num}.")
        return None
    return fetch_threads_metrics(token, media_ids)


def _fetch_split(measurement, twitter_client, threads_tokens):
    """(先頭投稿のみ, ツリー合算) を返す。片方でも取れなければ None。

    ツリー全体の合算を「到達人数」と読むと実態より大きく見えるので、先頭投稿の値を
    別に残す。IDごとの取得は1回ずつで、合算のために取り直すことはしない。
    """
    head = _fetch(measurement, measurement.media_ids[:1], twitter_client, threads_tokens)
    if head is None:
        return None
    if measurement.thread_len == 1:
        return head, head

    rest = _fetch(measurement, measurement.media_ids[1:], twitter_client, threads_tokens)
    if rest is None:
        return None
    return head, tuple(h + r for h, r in zip(head, rest))


def main():
    sheet = get_worksheet()
    all_rows = call_with_retry(sheet.get_all_values)

    if len(all_rows) <= 1:
        print("No posts found in the spreadsheet.")
        return

    recorded = get_recorded_checkpoints()
    now_jst = datetime.now(JST)
    # シートに実公開時刻の列が無ければ None のまま（予約時刻基準で測る）。
    published_at_column = header_column(all_rows[0], PUBLISHED_AT_HEADER)
    published_at_col = published_at_column - 1 if published_at_column else None
    plan = plan_measurements(all_rows[1:], recorded, now_jst,
                             published_at_col=published_at_col)

    if not plan:
        print("No post has reached an unrecorded checkpoint. Nothing to do.")
        return
    print(f"{len(plan)} measurement(s) due: " +
          ", ".join(f"row{m.row_num}@{m.checkpoint_h}h" for m in plan[:10]) +
          (" ..." if len(plan) > 10 else ""))

    twitter_client, threads_tokens = _build_clients()

    from gspread import Cell
    log_entries = []
    cells_to_update = []

    for measurement in plan:
        print(f"Fetching row {measurement.row_num} ({measurement.platform}, "
              f"checkpoint {measurement.checkpoint_h}h, {measurement.basis}基準で "
              f"{measurement.age_h:.1f}h)...")
        result = _fetch_split(measurement, twitter_client, threads_tokens)
        if result is None:
            continue

        (head_impressions, head_likes, head_replies), (impressions, likes, replies) = result
        measured_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        log_entries.append([
            measured_at, measurement.row_num, measurement.no, measurement.platform,
            measurement.scheduled_at, measurement.checkpoint_h, round(measurement.age_h, 2),
            impressions, likes, replies,
            head_impressions, head_likes, head_replies,
            impressions, likes, replies,
            reader_replies(replies, measurement.thread_len), measurement.basis,
        ])
        # 投稿管理シートにも最新値を残す（growth-review など既存の読み取り側の互換のため）
        cells_to_update.extend([
            Cell(row=measurement.row_num, col=CELL_IMPRESSIONS, value=impressions),
            Cell(row=measurement.row_num, col=CELL_LIKES, value=likes),
            Cell(row=measurement.row_num, col=CELL_REPLIES, value=replies),
            Cell(row=measurement.row_num, col=CELL_MEASURED_AT, value=measured_at),
        ])
        print(f"  -> head {head_impressions} views / thread {impressions} views, "
              f"{likes} likes, {replies} replies.")
        time.sleep(0.5)

    # 追記を先に行う。ここで落ちても計測ログが正なので、次回は同じ計測を繰り返さない。
    if log_entries:
        print(f"Appending {len(log_entries)} row(s) to the metrics log...")
        append_metrics_log(log_entries)

    if cells_to_update:
        print(f"Updating {len(cells_to_update)} cells in the main sheet...")
        call_with_retry(sheet.update_cells, cells_to_update)

    print("Done.")


if __name__ == "__main__":
    main()
