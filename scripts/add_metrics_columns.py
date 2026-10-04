"""投稿管理シートの末尾に AW03 の計測用の列を足す（1回だけ実行する移行スクリプト）。

足す列: 実公開時刻 / 先頭投稿ID / ツリー本数

publish.py はこの3列が無くても動く（書き込みをスキップする）ので、実行しなくても壊れない。
実行すると、以降の投稿で実公開時刻が記録され、fetch_metrics.py が経過時間を
予約時刻ではなく実公開時刻で測るようになる。

安全のための約束:
- **行は一切動かさない。** 計測ログは投稿管理シートを行番号で参照しているため、
  1行でもずれると過去の計測が全部別の投稿に紐づく
- **既存列の間に挿入しない。** 末尾にだけ足す
- 冪等。すでに足してあれば何もしない。想定と違う状態なら書き込まずに中止する

使い方:
    python3 scripts/add_metrics_columns.py            # 確認のみ（書き込まない）
    python3 scripts/add_metrics_columns.py --apply    # 実際に書き込む
"""
import os
import sys

# リポジトリルートを import パスに追加（scripts/ から実行されるため）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=".env")
except ImportError:
    pass

import gspread

from utils.gsheets import (POST_SHEET_HEADER, POST_SHEET_OPTIONAL_HEADER,
                           get_gsheets_client)


def inspect(sheet):
    """現在のヘッダと、足すべき列の位置を返す。書き込めない状態なら理由つきで返す。"""
    header = sheet.row_values(1)

    if header[:len(POST_SHEET_HEADER)] != POST_SHEET_HEADER:
        return None, ("ヘッダの先頭17列が想定と違う。手で直してから再実行すること。\n"
                      f"  期待: {POST_SHEET_HEADER}\n  実際: {header[:len(POST_SHEET_HEADER)]}")

    already = [name for name in POST_SHEET_OPTIONAL_HEADER if name in header]
    if already == POST_SHEET_OPTIONAL_HEADER:
        return None, "3列とも追加済み。何もしない。"
    if already:
        return None, f"一部だけ存在する: {already}。手で揃えてから再実行すること。"

    if len(header) > len(POST_SHEET_HEADER):
        extra = header[len(POST_SHEET_HEADER):]
        return None, f"想定外の列が末尾にある: {extra}。上書きしないので手で確認すること。"

    first_col = len(POST_SHEET_HEADER) + 1  # 1始まり
    return first_col, None


def occupied_cells(sheet, first_col):
    """追加先の列に既にデータが入っていないか確かめる（ヘッダ無しで値だけある列を上書きしない）。"""
    last_col = first_col + len(POST_SHEET_OPTIONAL_HEADER) - 1
    rng = (f"{gspread.utils.rowcol_to_a1(1, first_col)}:"
           f"{gspread.utils.rowcol_to_a1(sheet.row_count, last_col)}")
    values = sheet.get(rng)
    return [(index + 1, row) for index, row in enumerate(values) if any(c.strip() for c in row)]


def main():
    apply_changes = "--apply" in sys.argv

    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise SystemExit("SPREADSHEET_ID environment variable not set.")

    sheet = get_gsheets_client().open_by_key(spreadsheet_id).sheet1
    print(f"worksheet: {sheet.title} / rows: {sheet.row_count} / cols: {sheet.col_count}")

    first_col, reason = inspect(sheet)
    if reason:
        print(reason)
        return

    last_col = first_col + len(POST_SHEET_OPTIONAL_HEADER) - 1
    print(f"追加する列: {POST_SHEET_OPTIONAL_HEADER} → {first_col}〜{last_col} 列目（1始まり）")

    occupied = occupied_cells(sheet, first_col)
    if occupied:
        print(f"追加先にデータがある（{len(occupied)}行）。上書きしないので中止する。")
        for row_num, row in occupied[:5]:
            print(f"  行{row_num}: {row}")
        return

    if sheet.col_count < last_col:
        print(f"グリッドの列数が足りない（{sheet.col_count} < {last_col}）。")
        if apply_changes:
            sheet.add_cols(last_col - sheet.col_count)
            print(f"  → {last_col - sheet.col_count} 列を追加した。")

    if not apply_changes:
        print("\n確認のみで終了（書き込んでいない）。実行するには --apply を付ける。")
        return

    header_range = (f"{gspread.utils.rowcol_to_a1(1, first_col)}:"
                    f"{gspread.utils.rowcol_to_a1(1, last_col)}")
    sheet.update([POST_SHEET_OPTIONAL_HEADER], header_range)
    print(f"{header_range} にヘッダを書き込んだ。")

    updated = sheet.row_values(1)
    print(f"確認: 現在のヘッダ {len(updated)} 列 / 末尾3列 = {updated[-3:]}")


if __name__ == "__main__":
    main()
