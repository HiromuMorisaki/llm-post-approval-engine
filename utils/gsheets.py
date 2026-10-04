import os
import json
import functools
import random
import time
import gspread
import requests
from google.oauth2.service_account import Credentials

# Google Sheets API の一時障害に対するリトライ設定。
# 2026-08-19、publish が起動直後の row_values(1) で
# `APIError: [503] The service is currently unavailable.` を食らって落ち、
# その回の予約投稿を丸ごと取りこぼした。この手の 5xx / 429 は数秒〜十数秒で
# 回復するので、数回待って叩き直せば次の実行を待たずに済む。
# 恒久的なエラー（権限不足の403、シート不在の404 等）はリトライせず即座に落とす。
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
RETRY_ATTEMPTS = 4        # 初回 + リトライ3回
RETRY_BASE_DELAY = 2.0    # 2秒 → 4秒 → 8秒（+ジッタ）。最悪でも約14秒の上乗せ


def _error_status_code(exc):
    """例外から HTTP ステータスコードを取り出す。判別できなければ None。"""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code > 0:
        return code
    # gspread の APIError はレスポンスJSONのパースに失敗すると code=-1 になるため、
    # その場合は生のレスポンスから拾う。
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def is_transient_error(exc):
    """Google 側の一時障害（5xx / レート制限）や接続断なら True。"""
    if isinstance(exc, gspread.exceptions.APIError):
        return _error_status_code(exc) in TRANSIENT_STATUS_CODES
    return isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout))


def call_with_retry(func, *args, **kwargs):
    """Sheets API 呼び出しを一時障害に強くする。

    リトライ対象は「同じ呼び出しを繰り返しても結果が変わらない」操作に限ること。
    セルの上書き（update_cell / update_cells）や読み取りは対象にしてよいが、
    append_row / append_rows のような追記は重複行を生むので通してはいけない。
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == RETRY_ATTEMPTS or not is_transient_error(e):
                raise
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.1)  # 同時実行が揃って再試行しないようジッタ
            print(f"Sheets API transient error ({e}); "
                  f"retrying in {delay:.1f}s [{attempt}/{RETRY_ATTEMPTS - 1}]")
            time.sleep(delay)


def with_retry(func):
    """関数まるごとを一時障害リトライで包むデコレータ（冪等な処理にのみ使う）。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return call_with_retry(func, *args, **kwargs)
    return wrapper


def get_gsheets_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    # 1. Check for credentials in environment variable first (for GitHub Actions)
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    if creds_json:
        creds_dict = json.loads(creds_json)
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        # 2. Fallback to local credentials.json (for local testing)
        if not os.path.exists('credentials.json'):
            raise Exception("No GOOGLE_CREDENTIALS env var and credentials.json not found.")
        credentials = Credentials.from_service_account_file('credentials.json', scopes=scopes)
    
    return gspread.authorize(credentials)

# 投稿管理シートの必須列。ここまでが揃っていれば、シートは初期化済みとみなす。
POST_SHEET_HEADER = ["No", "ID", "予約時間", "投稿チャネル", "プロンプトパターン", "配信可否",
                     "Note記事共有", "Note記事No", "投稿内容", "配信状況", "削除",
                     "実際の投稿URL", "メディアURL", "インプレッション数", "いいね数",
                     "返信数", "計測日時"]

# 末尾に足す計測用の列（AW03）。シート側に足してもらうまでは存在しないので、
# publish.py / fetch_metrics.py は列が無ければ書き込みをスキップする。
POST_SHEET_OPTIONAL_HEADER = ["実公開時刻", "先頭投稿ID", "ツリー本数"]

COL_PUBLISHED_AT = "実公開時刻"
COL_HEAD_POST_ID = "先頭投稿ID"
COL_THREAD_LENGTH = "ツリー本数"


def header_column(header, name):
    """ヘッダ行から列番号（1始まり）を引く。無ければ None。

    列は末尾に増える前提なので、index 直書きではなくこれで引く。
    """
    try:
        return list(header).index(name) + 1
    except ValueError:
        return None


@with_retry
def get_worksheet():
    client = get_gsheets_client()
    spreadsheet_id = os.environ.get('SPREADSHEET_ID')
    if not spreadsheet_id:
        raise Exception("SPREADSHEET_ID environment variable not set.")
        
    sheet = client.open_by_key(spreadsheet_id).sheet1

    # Initialize headers if sheet is empty or row 1 is not the header.
    # 比較は必須列だけの前方一致にする。完全一致にすると、末尾に列を1つ足しただけで
    # insert_row が走り、実行のたびに全行が1つずつ下へずれる。計測ログは投稿管理シートを
    # 行番号で参照しているので、ずれると過去の計測が全部別の投稿に紐づく。
    current_header = sheet.row_values(1)
    if current_header[:len(POST_SHEET_HEADER)] != POST_SHEET_HEADER:
        sheet.insert_row(POST_SHEET_HEADER, index=1)

    return sheet

@with_retry
def get_notes_worksheet():
    client = get_gsheets_client()
    spreadsheet_id = os.environ.get('SPREADSHEET_ID')
    spreadsheet = client.open_by_key(spreadsheet_id)
    
    try:
        sheet = spreadsheet.worksheet("Notes")
    except gspread.exceptions.WorksheetNotFound:
        # Create it if it doesn't exist
        sheet = spreadsheet.add_worksheet(title="Notes", rows="100", cols="5")
        sheet.append_row(["Title", "URL", "Last Posted At"])
        
    return sheet

def get_next_note():
    sheet = get_notes_worksheet()
    all_rows = sheet.get_all_values()

    if len(all_rows) <= 1:
        return None  # No notes found

    data_rows = all_rows[1:]

    oldest_idx = -1
    oldest_date = "9999-99-99"
    target_note = None

    from datetime import datetime

    # Notesタブの実列: A=No, B=タイトル, C=NoteのURL, D=投稿日時, E=ローテ対象(◯/×)
    # 発信の軸に沿った記事だけを自動誘導の対象にするため E="×" は除外する。
    for i, row in enumerate(data_rows):
        if len(row) < 3:
            continue
        title = row[1]
        url = row[2]
        last_posted = row[3] if len(row) > 3 else ""
        rotate_flag = row[4].strip() if len(row) > 4 else "◯"

        if rotate_flag == "×":
            continue  # 背骨外Noteは自動ローテから除外（手動promoのみ）
        if not title or not url:
            continue

        if not last_posted:
            # Never posted, pick immediately
            oldest_idx = i
            target_note = {"title": title, "url": url}
            break
        elif last_posted < oldest_date:
            oldest_date = last_posted
            oldest_idx = i
            target_note = {"title": title, "url": url}

    if target_note and oldest_idx >= 0:
        row_num = oldest_idx + 2
        from datetime import timezone, timedelta
        JST = timezone(timedelta(hours=+9), 'JST')
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        # D列(投稿日時=4列目)に最終投稿時刻を記録する
        try:
            sheet.update_cell(row_num, 4, now)
        except Exception as e:
            print(f"Failed to update note timestamp: {e}")

    return target_note

@with_retry
def get_zenn_draft_worksheet():
    client = get_gsheets_client()
    spreadsheet_id = os.environ.get('SPREADSHEET_ID')
    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        sheet = spreadsheet.worksheet("Zenn下書き")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="Zenn下書き", rows="100", cols="6")
        sheet.append_row(["投稿日時", "タイトル", "本文（Markdown）", "ステータス", "公開URL"])

    return sheet


def append_zenn_draft(title, body, status="下書き"):
    """Zenn記事下書きを1行追記する（投稿日時 / タイトル / 本文 / ステータス / 公開URL）。"""
    from datetime import datetime, timezone, timedelta
    sheet = get_zenn_draft_worksheet()
    JST = timezone(timedelta(hours=+9), 'JST')
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([now, title, body, status, ""])


def delete_zenn_draft_row(row_number):
    """「Zenn下書き」シートの指定行（1-indexed、ヘッダ=1行目）を削除する。"""
    sheet = get_zenn_draft_worksheet()
    sheet.delete_rows(row_number)


@with_retry
def get_recent_zenn_drafts(limit=40):
    """「Zenn下書き」シートの直近本文（3列目）を新しい順に返す。重複投入ゲート用。"""
    sheet = get_zenn_draft_worksheet()
    all_rows = sheet.get_all_values()
    if len(all_rows) <= 1:
        return []

    bodies = []
    for row in reversed(all_rows[1:]):
        if len(row) > 2 and row[2].strip():
            bodies.append(row[2])
        if len(bodies) >= limit:
            break
    return bodies


@with_retry
def get_recent_contents(limit=18):
    """直近の投稿内容（9列目）を新しい順に返す。マンネリ防止のためLLMに渡す用。"""
    sheet = get_worksheet()
    all_rows = sheet.get_all_values()
    if len(all_rows) <= 1:
        return []

    contents = []
    for row in reversed(all_rows[1:]):
        if len(row) > 8 and row[8].strip():
            contents.append(row[8])
        if len(contents) >= limit:
            break
    return contents


METRICS_LOG_SHEET = "計測ログ"
# 既存の10列。R6 のスクリプトが index 6（実経過h）を直接見ているので、並びは変えない。
METRICS_LOG_BASE_HEADER = ["計測日時", "投稿行", "投稿No", "投稿チャネル", "予約時間",
                           "チェックポイントh", "実経過h", "インプレッション数", "いいね数", "返信数"]
# AW03 で末尾に足す列。先頭投稿とツリー合算を分け、自分の続き投稿を除いた読者返信と、
# 実経過h がどちらの時刻を基準にしたかを残す。
# ツリー* の3列は既存の「インプレッション数/いいね数/返信数」と同じ値。既存列を
# 消すと過去分と R6 の index 参照が壊れるので残し、名前で引ける別名として足している
# （合算を「到達人数」と読まないために、先頭とツリーを列名で区別するのが目的）。
METRICS_LOG_EXTRA_HEADER = ["先頭閲覧", "先頭いいね", "先頭返信",
                            "ツリー閲覧", "ツリーいいね", "ツリー返信", "読者返信", "基準"]
METRICS_LOG_HEADER = METRICS_LOG_BASE_HEADER + METRICS_LOG_EXTRA_HEADER


def ensure_metrics_log_header(sheet):
    """末尾に増えた列名をヘッダ行に書き足す。追記の直前にだけ呼ぶ（R6 は読み取り専用のため）。

    行の挿入はしない（計測ログは投稿管理シートを行番号で参照しているため、
    1行でもずらすと過去の計測が別の投稿に紐づく）。想定と違う並びなら何もしない。
    """
    current = sheet.row_values(1)
    if current == METRICS_LOG_HEADER:
        return
    if current != METRICS_LOG_HEADER[:len(current)]:
        print(f"Warning: 計測ログのヘッダが想定と異なるため自動更新しない: {current}")
        return

    # シートの列数が足りないと、ヘッダの書き込みも追記もグリッド外で失敗する。
    missing_cols = len(METRICS_LOG_HEADER) - sheet.col_count
    if missing_cols > 0:
        sheet.add_cols(missing_cols)

    last_cell = gspread.utils.rowcol_to_a1(1, len(METRICS_LOG_HEADER))
    sheet.update([METRICS_LOG_HEADER], f"A1:{last_cell}")
    print(f"計測ログのヘッダに {len(METRICS_LOG_HEADER) - len(current)} 列を追記した。")


@with_retry
def get_metrics_log_worksheet():
    """計測ログシートを返す（無ければ作る）。1投稿×1チェックポイント＝1行の追記専用。"""
    client = get_gsheets_client()
    spreadsheet_id = os.environ.get('SPREADSHEET_ID')
    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        sheet = spreadsheet.worksheet(METRICS_LOG_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(
            title=METRICS_LOG_SHEET, rows="2000", cols=str(len(METRICS_LOG_HEADER)))
        sheet.append_row(METRICS_LOG_HEADER)
        return sheet

    return sheet


@with_retry
def get_recorded_checkpoints():
    """計測ログから {投稿管理の行番号: {記録済みチェックポイントh}} を組み立てる。

    投稿管理シートの行番号をキーにしている（No列は過去に重複があり一意でないため）。
    行を削除すると以降の対応がずれるので、投稿管理シートは追記のみで運用すること。
    """
    rows = get_metrics_log_worksheet().get_all_values()
    recorded = {}
    for row in rows[1:]:
        if len(row) < 6:
            continue
        row_num, checkpoint = row[1].strip(), row[5].strip()
        if not row_num.isdigit() or not checkpoint.isdigit():
            continue
        recorded.setdefault(int(row_num), set()).add(int(checkpoint))
    return recorded


def append_metrics_log(entries):
    """計測結果を計測ログに追記する。

    追記は再実行すると行が重複するため、call_with_retry では包まない（gsheets の方針どおり）。
    """
    if not entries:
        return
    sheet = get_metrics_log_worksheet()
    ensure_metrics_log_header(sheet)
    sheet.append_rows(entries)


def append_posts(posts):
    sheet = get_worksheet()
    current_rows = sheet.get_all_values()
    current_no = len(current_rows) if current_rows else 1
    
    rows = []
    for post in posts:
        content = post['content']
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        rows.append([
            str(current_no),
            post.get('id', ''),
            post.get('scheduled_at', ''),
            post.get('platform', ''),
            post.get('prompt_pattern', ''),
            post.get('is_publishable', '◯'),
            post.get('is_note_promo', '×'),
            post.get('note_no', ''),
            content,
            post.get('status', '未対応'),
            post.get('deletion', '-'),
            post.get('post_url', ''),
            post.get('media_url', '')
        ])
        current_no += 1
        
    if rows:
        sheet.append_rows(rows)
