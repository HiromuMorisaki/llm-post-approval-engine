import difflib
import os
import re
import time
import requests
import tweepy
import json
import gspread
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from utils.gsheets import (COL_HEAD_POST_ID, COL_PUBLISHED_AT, COL_THREAD_LENGTH,
                           POST_SHEET_OPTIONAL_HEADER, call_with_retry, get_worksheet,
                           header_column)
from utils.limits import describe_over_length, over_length_positions

# Load environment variables
load_dotenv()

JST = timezone(timedelta(hours=+9), 'JST')

def download_media(media_url):
    try:
        filename = "temp_media_" + str(int(time.time())) + ".jpg"
        r = requests.get(media_url, stream=True)
        if r.status_code == 200:
            with open(filename, 'wb') as f:
                for chunk in r:
                    f.write(chunk)
            return filename
    except Exception as e:
        print(f"Error downloading media: {e}")
    return None

def post_to_twitter(content_list, media_url=""):
    api_key = os.environ.get('TWITTER_API_KEY')
    api_secret = os.environ.get('TWITTER_API_SECRET')
    access_token = os.environ.get('TWITTER_ACCESS_TOKEN')
    access_secret = os.environ.get('TWITTER_ACCESS_SECRET')

    if not all([api_key, api_secret, access_token, access_secret]):
        print("Twitter credentials are not fully set.")
        return False, "", "Twitter credentials are not fully set."

    tweet_urls = []
    try:
        client = tweepy.Client(
            consumer_key=api_key, consumer_secret=api_secret,
            access_token=access_token, access_token_secret=access_secret
        )
        
        media_ids = None
        if media_url:
            local_file = download_media(media_url)
            if local_file:
                auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
                api_v1 = tweepy.API(auth)
                try:
                    media = api_v1.media_upload(filename=local_file)
                    media_ids = [media.media_id]
                except Exception as e:
                    print("Failed to upload media to Twitter v1.1:", e)
                finally:
                    if os.path.exists(local_file):
                        os.remove(local_file)
                        
        previous_id = None
        for i, text in enumerate(content_list):
            kwargs = {"text": text}
            if previous_id:
                kwargs["in_reply_to_tweet_id"] = previous_id
            if i == 0 and media_ids:
                kwargs["media_ids"] = media_ids

            response = client.create_tweet(**kwargs)
            previous_id = response.data['id']
            tweet_urls.append(f"https://x.com/i/status/{previous_id}")
            print(f"Successfully posted to Twitter: {response.data}")
            time.sleep(1)  # Sleep briefly to avoid rate limit

        return True, ",".join(tweet_urls), ""
    except Exception as e:
        # Capture the API's actual reason (e.g. duplicate content vs. cap
        # exceeded) — the generic 403 text alone is not diagnosable.
        detail = str(e)
        api_errors = getattr(e, "api_errors", None) or getattr(e, "api_messages", None)
        if api_errors:
            detail += f" | api: {api_errors}"
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                detail += f" | body: {resp.text[:300]}"
            except Exception:
                pass
        # ツリーの何本目で落ちたかが一番効く診断情報なので先頭に置く。末尾に置くと
        # 手前の body 300字に押し出され、main() の [:200] で必ず消える
        # （エラー163行すべてで消えていた。運用時の403エラー調査）。
        detail = f"failed_at_tweet: {len(tweet_urls) + 1}/{len(content_list)} | {detail}"
        print(f"Failed to post to Twitter: {detail}")
        return False, ",".join(tweet_urls), detail


# Threads media containers need server-side processing before they can be
# published. Publishing immediately (especially reply containers, which also
# wait for the parent post to propagate) fails with code 24 "Media Not Found",
# so poll the container status until it reports FINISHED.
THREADS_STATUS_POLL_INTERVAL_SEC = 3
THREADS_STATUS_MAX_ATTEMPTS = 20  # ~60s total before giving up


def wait_for_threads_container(creation_id, access_token):
    """Poll a Threads media container until it is ready to publish.

    Returns (True, "") once the container reports FINISHED, or
    (False, error_message) on ERROR/EXPIRED status or timeout.
    """
    status_url = f"https://graph.threads.net/v1.0/{creation_id}"
    params = {"fields": "status,error_message", "access_token": access_token}
    for _ in range(THREADS_STATUS_MAX_ATTEMPTS):
        response = requests.get(status_url, params=params)
        data = response.json()
        status = data.get("status")
        if status == "FINISHED":
            return True, ""
        if status in ("ERROR", "EXPIRED"):
            return False, str(data.get("error_message") or data)
        time.sleep(THREADS_STATUS_POLL_INTERVAL_SEC)
    return False, "Timed out waiting for Threads container to be ready."


# A reply container can fail with code 24 / subcode 4279009
# ("The requested resource does not exist") when the parent post has not
# propagated yet. Wait longer between posts and retry the failing node.
THREADS_POST_INTERVAL_SEC = 5  # let a published post propagate before replying to it
THREADS_PUBLISH_RETRY_MAX = 4
THREADS_PUBLISH_RETRY_BACKOFF_SEC = 5


def _is_resource_missing_error(data):
    """True if a Threads API response is the transient code 24 / 4279009
    'resource does not exist' error (typically a propagation lag)."""
    if not isinstance(data, dict):
        return False
    err = data.get("error", data)
    if not isinstance(err, dict):
        return False
    return err.get("code") == 24 or err.get("error_subcode") == 4279009


def _publish_one_thread_post(user_id, access_token, text, media_url="", reply_to_id=None):
    """Create + publish a single Threads post (one node of a thread).

    Retries on the transient code 24 / 4279009 error (parent/container not
    propagated yet). Returns (published_id, "") on success or
    (None, error_detail) on failure.
    """
    create_url = f"https://graph.threads.net/v1.0/{user_id}/threads"
    publish_url = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
    last_err = ""

    for attempt in range(THREADS_PUBLISH_RETRY_MAX):
        payload = {"text": text, "access_token": access_token}
        if media_url:
            payload["media_type"] = "IMAGE"
            payload["image_url"] = media_url
        else:
            payload["media_type"] = "TEXT"
        if reply_to_id:
            payload["reply_to_id"] = reply_to_id

        create_data = requests.post(create_url, data=payload).json()
        if "id" not in create_data:
            last_err = str(create_data.get("error", create_data))
            if _is_resource_missing_error(create_data) and attempt < THREADS_PUBLISH_RETRY_MAX - 1:
                print(f"Threads create transient error (attempt {attempt + 1}): {last_err}")
                time.sleep(THREADS_PUBLISH_RETRY_BACKOFF_SEC * (attempt + 1))
                continue
            print(f"Failed to create Threads container: {create_data}")
            return None, last_err

        creation_id = create_data["id"]

        ready, wait_err = wait_for_threads_container(creation_id, access_token)
        if not ready:
            last_err = wait_err
            print(f"Threads container {creation_id} not ready: {wait_err}")
            time.sleep(THREADS_PUBLISH_RETRY_BACKOFF_SEC * (attempt + 1))
            continue

        publish_data = requests.post(
            publish_url,
            data={"creation_id": creation_id, "access_token": access_token},
        ).json()
        if "id" in publish_data:
            print(f"Successfully posted to Threads: {publish_data}")
            return publish_data["id"], ""

        last_err = str(publish_data.get("error", publish_data))
        if _is_resource_missing_error(publish_data) and attempt < THREADS_PUBLISH_RETRY_MAX - 1:
            print(f"Threads publish transient error (attempt {attempt + 1}): {last_err}")
            time.sleep(THREADS_PUBLISH_RETRY_BACKOFF_SEC * (attempt + 1))
            continue
        print(f"Failed to publish to Threads: {publish_data}")
        return None, last_err

    return None, last_err or "Threads post failed after retries."


def post_to_threads(content_list, media_url="", account="default"):
    if account == "shigoto":
        access_token = os.environ.get('THREADS_ACCESS_TOKEN_SHIGOTO')
        user_id = os.environ.get('THREADS_USER_ID_SHIGOTO')
    elif account == "papa":
        access_token = os.environ.get('THREADS_ACCESS_TOKEN_PAPA')
        user_id = os.environ.get('THREADS_USER_ID_PAPA')
    else:
        access_token = os.environ.get('THREADS_ACCESS_TOKEN')
        user_id = os.environ.get('THREADS_USER_ID')

    if not all([access_token, user_id]):
        print("Threads credentials are not fully set.")
        return False, "", "Threads credentials are not fully set."

    try:
        previous_id = None
        threads_urls = []
        for i, text in enumerate(content_list):
            posted_id, err_detail = _publish_one_thread_post(
                user_id,
                access_token,
                text,
                media_url=(media_url if i == 0 else ""),
                reply_to_id=previous_id,
            )
            if not posted_id:
                # 途中で落ちても、ここまでに公開済みのIDは返す。捨てるとシートに記録が
                # 残らず、Threads上に記録の無い投稿だけが残る。
                detail = f"failed_at_post: {i + 1}/{len(content_list)} | {err_detail}"
                return False, ",".join(threads_urls), detail

            previous_id = posted_id
            threads_urls.append(str(previous_id))  # store the api IDs directly
            time.sleep(THREADS_POST_INTERVAL_SEC)  # let the post propagate before the next reply

        return True, ",".join(threads_urls), ""

    except Exception as e:
        print(f"Failed to post to Threads: {e}")
        return False, "", str(e)

def delete_twitter_posts(urls_string):
    api_key = os.environ.get('TWITTER_API_KEY')
    api_secret = os.environ.get('TWITTER_API_SECRET')
    access_token = os.environ.get('TWITTER_ACCESS_TOKEN')
    access_secret = os.environ.get('TWITTER_ACCESS_SECRET')

    if not all([api_key, api_secret, access_token, access_secret]):
        return False

    try:
        client = tweepy.Client(
            consumer_key=api_key, consumer_secret=api_secret,
            access_token=access_token, access_token_secret=access_secret
        )
        urls = urls_string.split(",")
        # Delete from bottom up (replies first) to avoid orphaned metrics, though API doesn't strictly require it
        for url in reversed(urls):
            if "status/" in url:
                tweet_id = url.split("status/")[-1].split("?")[0]
                client.delete_tweet(id=tweet_id)
                print(f"Deleted tweet {tweet_id}")
                time.sleep(1)
        return True
    except Exception as e:
        print(f"Failed to delete Twitter posts: {e}")
        return False

def delete_threads_posts(urls_string, account="default"):
    if account == "shigoto":
        access_token = os.environ.get('THREADS_ACCESS_TOKEN_SHIGOTO')
    elif account == "papa":
        access_token = os.environ.get('THREADS_ACCESS_TOKEN_PAPA')
    else:
        access_token = os.environ.get('THREADS_ACCESS_TOKEN')
    if not access_token:
        return False
        
    try:
        ids = urls_string.split(",")
        for media_id in reversed(ids):
            media_id = media_id.strip()
            if not media_id:
                continue
            delete_url = f"https://graph.threads.net/v1.0/{media_id}"
            response = requests.delete(delete_url, params={"access_token": access_token})
            print(f"Deleted Threads post {media_id}: {response.text}")
            time.sleep(1)
        return True
    except Exception as e:
        print(f"Failed to delete Threads posts: {e}")
        return False

# X (Twitter) rejects duplicate / near-duplicate tweets with 403
# "You are not permitted to perform this action". The LLM occasionally
# regenerates an almost-identical promo, so guard before spending the API call.
_DEDUP_MIN_CONTAIN_LEN = 20  # normalized chars required for a containment match
_DEDUP_SIMILARITY_THRESHOLD = 0.75  # SequenceMatcher ratio treated as near-duplicate


def _normalize_for_dedup(text: str) -> str:
    """Collapse a post to a comparison key: drop URLs, hashtags, whitespace and
    punctuation so cosmetically-different but substantively-identical promos
    map to the same string."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[#＃]\S+", "", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[、。!！?？「」『』()（）.,:;…\-—~〜・/]", "", text)
    return text.lower()


def is_duplicate_x_content(first_post: str, prior_first_posts: list[str]) -> bool:
    """Return True if first_post is a near-duplicate of any already-posted X
    content (compared on the thread's first post, which is what X dedups on)."""
    candidate = _normalize_for_dedup(first_post)
    if not candidate:
        return False
    for prior in prior_first_posts:
        existing = _normalize_for_dedup(prior)
        if not existing:
            continue
        if candidate == existing:
            return True
        shorter, longer = sorted((candidate, existing), key=len)
        if len(shorter) >= _DEDUP_MIN_CONTAIN_LEN and shorter in longer:
            return True
        # X rejects posts that are merely similar (not just identical) with a
        # 403, so also catch near-matches by character-level similarity.
        if difflib.SequenceMatcher(None, candidate, existing).ratio() >= _DEDUP_SIMILARITY_THRESHOLD:
            return True
    return False


def collect_posted_x_first_posts(data_rows: list[list[str]]) -> list[str]:
    """Gather the first post of every already-completed X row, for dedup checks."""
    posts: list[str] = []
    for row in data_rows:
        if len(row) < 10:
            continue
        platform, content, status = row[3], row[8], row[9]
        if platform.lower() not in ("twitter", "x") or status != "完了":
            continue
        try:
            parsed = json.loads(content)
            first = parsed[0] if isinstance(parsed, list) and parsed else content
        except Exception:
            first = content
        if first:
            posts.append(first)
    return posts


# 投稿管理シートの書き込み先（1始まり）。列は末尾にしか増やさないこと。
CELL_STATUS = 10        # 配信状況
CELL_DELETION = 11      # 削除
CELL_POST_URL = 12      # 実際の投稿URL


def _write_post_url(sheet, row_num, result_url):
    """投稿URL列に RAW で書く。成功時も失敗時（部分投稿）も同じ経路を通す。

    RAW で書くのは、Threads の17〜19桁のメディアIDを Sheets が float64 に丸め、
    カンマ区切りを桁区切りとして解釈して壊すため（USER_ENTERED の既定動作）。
    """
    if not result_url:
        return
    url_cell = gspread.utils.rowcol_to_a1(row_num, CELL_POST_URL)
    call_with_retry(
        sheet.update,
        [[result_url]],
        url_cell,
        value_input_option=gspread.utils.ValueInputOption.raw,
    )


def _record_publish_details(sheet, row_num, optional_cells, content_list, result_url, now):
    """実公開時刻・先頭投稿ID・ツリー本数を残す（AW03）。

    計測の経過時間を予約時刻ではなく実公開時刻で測るために要る。
    シート側にまだ列が無い間は何も書かない（列が無くても落ちないこと）。
    """
    head_id = result_url.split(",")[0] if result_url else ""
    values = {
        COL_PUBLISHED_AT: now.strftime("%Y-%m-%d %H:%M:%S"),
        COL_HEAD_POST_ID: head_id,
        COL_THREAD_LENGTH: len(content_list),
    }
    for name, value in values.items():
        col = optional_cells.get(name)
        if col:
            call_with_retry(sheet.update_cell, row_num, col, value)


def main():
    sheet = get_worksheet()
    all_rows = call_with_retry(sheet.get_all_values)
    
    if len(all_rows) <= 1:
        print("No posts found in the spreadsheet.")
        return
        
    headers = all_rows[0]
    data_rows = all_rows[1:]

    # Snapshot of already-posted X content, used to skip near-duplicates that
    # would otherwise be rejected by X with a 403.
    posted_x_first_posts = collect_posted_x_first_posts(data_rows)

    # AW03 の追加列。シートに無ければ None のままで、書き込みはスキップされる。
    optional_cells = {name: header_column(headers, name)
                      for name in POST_SHEET_OPTIONAL_HEADER}

    # Columns: 0:No, 1:ID, 2:予約時間, 3:投稿チャネル, 4:プロンプトパターン, 5:配信可否, 6:Note記事共有, 7:Note記事No, 8:投稿内容, 9:配信状況, 10:削除, 11:実際の投稿URL, 12:メディアURL, 13:インプレッション数, 14:いいね数, 15:返信数, 16:計測日時
    for i, row in enumerate(data_rows):
        if len(row) < 13:
            row += [""] * (13 - len(row))
            
        row_num = i + 2  # +2 because header is row 1, index 0 is row 2
        no = row[0]
        post_id = row[1]
        scheduled_at = row[2]
        platform = row[3]
        is_publishable = row[5]
        content = row[8]
        status = row[9]
        deletion = row[10]
        post_url = row[11]
        media_url = row[12]
        
        # 1. Check deletion
        if deletion == "削除希望":
            print(f"Deleting post {post_id} from {platform}...")
            del_success = False
            if platform.lower() in ["twitter", "x"]:
                del_success = delete_twitter_posts(post_url)
            elif platform.lower() == "threads":
                del_success = delete_threads_posts(post_url)
            elif platform.lower() == "threads_shigoto":
                del_success = delete_threads_posts(post_url, account="shigoto")
            elif platform.lower() == "threads_papa":
                del_success = delete_threads_posts(post_url, account="papa")
                
            if del_success:
                call_with_retry(sheet.update_cell, row_num, CELL_STATUS, "削除済")
                call_with_retry(sheet.update_cell, row_num, CELL_DELETION, "削除済")
                print(f"Post {post_id} deleted successfully.")
            else:
                call_with_retry(sheet.update_cell, row_num, CELL_DELETION, "エラー")
            continue
            
        if deletion == "削除済":
            continue
            
        # 2. Check publishable
        if is_publishable != "◯":
            continue
        
        # 3. Check pending status
        if status == "未対応" or status.lower() == "pending":
            try:
                now_jst = datetime.now(JST).replace(tzinfo=None)
                
                if len(scheduled_at) > 16:
                    sched_time = datetime.strptime(scheduled_at, "%Y-%m-%d %H:%M:%S")
                elif len(scheduled_at) == 16:
                    sched_time = datetime.strptime(scheduled_at, "%Y-%m-%d %H:%M")
                else:
                    sched_time = now_jst # Fallback
                
                if now_jst < sched_time:
                    print(f"Post {post_id} is scheduled for {sched_time} (JST). It's currently {now_jst} (JST). Skipping for now.")
                    continue
            except Exception as e:
                print(f"Error parsing time for post {post_id}: {e}")
                
            print(f"Publishing post {post_id} to {platform}...")
            
            try:
                content_list = json.loads(content)
                if not isinstance(content_list, list):
                    content_list = [content] # Fallback
            except Exception:
                content_list = [content] # Fallback if not JSON

            # 1要素でも上限を超えていたら1本も投げない。ツリーの途中で 403 になると
            # 手前の投稿だけがタイムラインに残り、シートには記録が無い孤児になる
            # （運用時の403エラー調査）。
            over = over_length_positions(platform, content_list)
            if over:
                reason = "スキップ: " + describe_over_length(platform, over)
                print(f"Post {post_id} skipped: {reason}")
                call_with_retry(sheet.update_cell, row_num, CELL_STATUS, reason)
                continue

            success = False
            result_url = ""
            err = ""
            if platform.lower() in ["twitter", "x"]:
                first_post = content_list[0] if content_list else ""
                if is_duplicate_x_content(first_post, posted_x_first_posts):
                    print(f"Post {post_id} skipped: near-duplicate X content (would trigger 403).")
                    call_with_retry(sheet.update_cell, row_num, CELL_STATUS, "スキップ: 重複の可能性")
                    continue
                success, result_url, err = post_to_twitter(content_list, media_url)
                if success:
                    posted_x_first_posts.append(first_post)
            elif platform.lower() == "threads":
                success, result_url, err = post_to_threads(content_list, media_url)
            elif platform.lower() == "threads_shigoto":
                success, result_url, err = post_to_threads(content_list, media_url, account="shigoto")
            elif platform.lower() == "threads_papa":
                success, result_url, err = post_to_threads(content_list, media_url, account="papa")
            else:
                print(f"Unknown platform: {platform}")
                continue

            if success:
                call_with_retry(sheet.update_cell, row_num, CELL_STATUS, "完了")
                _write_post_url(sheet, row_num, result_url)
                _record_publish_details(sheet, row_num, optional_cells,
                                        content_list, result_url, datetime.now(JST))
                print(f"Post {post_id} published successfully.")
            else:
                status_detail = ("エラー: " + err)[:200] if err else "エラー"
                call_with_retry(sheet.update_cell, row_num, CELL_STATUS, status_detail)
                # 途中まで投稿できていたぶんのURLは残す。捨てると、媒体上には在るのに
                # どこにも記録が無い投稿になる（エラー163行すべてで捨てられていた）。
                _write_post_url(sheet, row_num, result_url)
                print(f"Post {post_id} failed.")
            
            time.sleep(2)

if __name__ == "__main__":
    main()
