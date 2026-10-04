"""publish.py の投稿前ゲートと失敗時の記録のテスト。シート・外部APIはすべてモックする。"""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import publish
from utils import gsheets
from utils.gsheets import METRICS_LOG_HEADER

JST = timezone(timedelta(hours=+9), 'JST')
NOW = datetime(2026, 9, 20, 9, 0, tzinfo=JST)

BASE_HEADER = ["No", "ID", "予約時間", "投稿チャネル", "プロンプトパターン", "配信可否",
               "Note記事共有", "Note記事No", "投稿内容", "配信状況", "削除", "実際の投稿URL",
               "メディアURL", "インプレッション数", "いいね数", "返信数", "計測日時"]
EXTENDED_HEADER = BASE_HEADER + ["実公開時刻", "先頭投稿ID", "ツリー本数"]

OVER_X = "あ" * 150      # 300重み（上限280）
WITHIN_X = "あ" * 100     # 200重み


def queued_row(content, platform="X", scheduled="2026-09-20 08:00:00"):
    row = [""] * len(BASE_HEADER)
    row[0], row[1], row[2], row[3] = "1", "id1", scheduled, platform
    row[5], row[8], row[9], row[10] = "◯", json.dumps(content, ensure_ascii=False), "未対応", "-"
    return row


def status_writes(sheet):
    """update_cell で配信状況列に書かれた値を順に返す。"""
    return [c.args[2] for c in sheet.update_cell.call_args_list
            if c.args[1] == publish.CELL_STATUS]


class MainTestBase:
    """main() をシート・APIモックの上で1回実行するヘルパー。"""

    def _run(self, rows, header=BASE_HEADER, post_result=(True, "https://x.com/i/status/1", "")):
        sheet = mock.Mock()
        sheet.get_all_values.return_value = [header] + rows
        with mock.patch.object(publish, 'get_worksheet', return_value=sheet), \
             mock.patch.object(publish, 'call_with_retry',
                               side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             mock.patch.object(publish, 'post_to_twitter',
                               return_value=post_result) as to_twitter, \
             mock.patch.object(publish, 'post_to_threads',
                               return_value=post_result) as to_threads, \
             mock.patch.object(publish, 'time', mock.Mock(sleep=lambda s: None)), \
             mock.patch.object(publish, 'datetime',
                               mock.Mock(now=lambda tz: NOW, strptime=datetime.strptime)):
            publish.main()
        return sheet, to_twitter, to_threads


class MainLengthGateTest(MainTestBase, unittest.TestCase):
    def test_does_not_post_any_element_when_one_exceeds_the_limit(self):
        # 現状は1本目を投稿してから2本目で403になり、タイムラインに孤児が残る
        sheet, to_twitter, _ = self._run([queued_row([WITHIN_X, OVER_X])])
        to_twitter.assert_not_called()

    def test_records_the_skip_reason_with_the_failing_position(self):
        sheet, _, _ = self._run([queued_row([WITHIN_X, OVER_X])])
        written = status_writes(sheet)
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].startswith("スキップ: 文字数超過"), written[0])
        self.assertIn("2本目", written[0])

    def test_checks_the_threads_limit_for_threads_rows(self):
        sheet, _, to_threads = self._run([queued_row(["あ" * 501], platform="Threads")])
        to_threads.assert_not_called()
        self.assertTrue(status_writes(sheet)[0].startswith("スキップ: 文字数超過"))

    def test_publishes_normally_when_every_element_is_within_the_limit(self):
        sheet, to_twitter, _ = self._run([queued_row([WITHIN_X, WITHIN_X])])
        to_twitter.assert_called_once()
        self.assertEqual(status_writes(sheet), ["完了"])


class MainFailureRecordTest(MainTestBase, unittest.TestCase):
    def test_keeps_the_partial_post_url_when_the_thread_fails_midway(self):
        partial = "https://x.com/i/status/1"
        sheet, _, _ = self._run([queued_row([WITHIN_X, WITHIN_X])],
                                post_result=(False, partial, "failed_at_tweet: 2/2 | 403"))
        self.assertIn(partial, [c.args[0][0][0] for c in sheet.update.call_args_list])

    def test_keeps_the_failure_reason_in_the_status_cell(self):
        sheet, _, _ = self._run([queued_row([WITHIN_X])],
                                post_result=(False, "", "failed_at_tweet: 1/1 | 403"))
        self.assertTrue(status_writes(sheet)[0].startswith("エラー: failed_at_tweet: 1/1"))


class MainPublishDetailsTest(MainTestBase, unittest.TestCase):
    def test_records_published_at_head_id_and_thread_length_when_the_columns_exist(self):
        sheet, _, _ = self._run(
            [queued_row([WITHIN_X, WITHIN_X])], header=EXTENDED_HEADER,
            post_result=(True, "https://x.com/i/status/11,https://x.com/i/status/12", ""))
        written = {c.args[1]: c.args[2] for c in sheet.update_cell.call_args_list}
        self.assertEqual(written[18], "2026-09-20 09:00:00")
        self.assertEqual(written[19], "https://x.com/i/status/11")
        self.assertEqual(written[20], 2)

    def test_does_not_write_those_columns_when_the_sheet_has_not_been_extended(self):
        sheet, _, _ = self._run([queued_row([WITHIN_X])], header=BASE_HEADER)
        touched = {c.args[1] for c in sheet.update_cell.call_args_list}
        self.assertEqual(touched, {publish.CELL_STATUS})


class PostToTwitterFailureTest(unittest.TestCase):
    def test_uploads_media_with_the_configured_access_secret(self):
        client = mock.Mock()
        client.create_tweet.return_value = mock.Mock(data={'id': 11})
        api_v1 = mock.Mock()
        api_v1.media_upload.return_value = mock.Mock(media_id=22)
        with mock.patch.object(publish.tweepy, 'Client', return_value=client), \
             mock.patch.object(publish.tweepy, 'OAuth1UserHandler') as oauth_handler, \
             mock.patch.object(publish.tweepy, 'API', return_value=api_v1) as api_factory, \
             mock.patch.object(publish, 'download_media', return_value='/tmp/test-media.jpg'), \
             mock.patch.object(publish.os.path, 'exists', return_value=False), \
             mock.patch.object(publish, 'time', mock.Mock(sleep=lambda s: None)), \
             mock.patch.dict(os.environ, {'TWITTER_API_KEY': 'k', 'TWITTER_API_SECRET': 's',
                                          'TWITTER_ACCESS_TOKEN': 't',
                                          'TWITTER_ACCESS_SECRET': 'ts'}):
            success, url, detail = publish.post_to_twitter(['image post'], 'https://example.com/image.jpg')

        self.assertTrue(success, detail)
        self.assertEqual(url, 'https://x.com/i/status/11')
        oauth_handler.assert_called_once_with('k', 's', 't', 'ts')
        api_factory.assert_called_once_with(oauth_handler.return_value)
        api_v1.media_upload.assert_called_once_with(filename='/tmp/test-media.jpg')
        client.create_tweet.assert_called_once_with(text='image post', media_ids=[22])

    def test_puts_failed_at_tweet_at_the_head_of_the_detail(self):
        # detail は main() が [:200] で切るので、末尾にあると必ず消える
        client = mock.Mock()
        client.create_tweet.side_effect = [
            mock.Mock(data={'id': 11}),
            Exception("403 Forbidden " + "x" * 400),
        ]
        with mock.patch.object(publish.tweepy, 'Client', return_value=client), \
             mock.patch.object(publish, 'time', mock.Mock(sleep=lambda s: None)), \
             mock.patch.dict(os.environ, {'TWITTER_API_KEY': 'k', 'TWITTER_API_SECRET': 's',
                                          'TWITTER_ACCESS_TOKEN': 't',
                                          'TWITTER_ACCESS_SECRET': 'ts'}):
            success, url, detail = publish.post_to_twitter(["a", "b"])
        self.assertFalse(success)
        self.assertTrue(detail.startswith("failed_at_tweet: 2/2"), detail[:80])
        self.assertIn("failed_at_tweet: 2/2", ("エラー: " + detail)[:200])

    def test_still_returns_the_urls_that_were_posted_before_the_failure(self):
        client = mock.Mock()
        client.create_tweet.side_effect = [mock.Mock(data={'id': 11}), Exception("403")]
        with mock.patch.object(publish.tweepy, 'Client', return_value=client), \
             mock.patch.object(publish, 'time', mock.Mock(sleep=lambda s: None)), \
             mock.patch.dict(os.environ, {'TWITTER_API_KEY': 'k', 'TWITTER_API_SECRET': 's',
                                          'TWITTER_ACCESS_TOKEN': 't',
                                          'TWITTER_ACCESS_SECRET': 'ts'}):
            _, url, _ = publish.post_to_twitter(["a", "b"])
        self.assertEqual(url, "https://x.com/i/status/11")


class PostToThreadsFailureTest(unittest.TestCase):
    def test_returns_the_ids_posted_before_the_failing_element(self):
        with mock.patch.object(publish, '_publish_one_thread_post',
                               side_effect=[("1701", ""), (None, "code 24")]), \
             mock.patch.object(publish, 'time', mock.Mock(sleep=lambda s: None)), \
             mock.patch.dict(os.environ, {'THREADS_ACCESS_TOKEN': 'tok',
                                          'THREADS_USER_ID': 'uid'}):
            success, url, detail = publish.post_to_threads(["a", "b"])
        self.assertFalse(success)
        self.assertEqual(url, "1701")

    def test_puts_the_failing_position_at_the_head_of_the_detail(self):
        with mock.patch.object(publish, '_publish_one_thread_post',
                               side_effect=[("1701", ""), (None, "code 24 " + "x" * 400)]), \
             mock.patch.object(publish, 'time', mock.Mock(sleep=lambda s: None)), \
             mock.patch.dict(os.environ, {'THREADS_ACCESS_TOKEN': 'tok',
                                          'THREADS_USER_ID': 'uid'}):
            _, _, detail = publish.post_to_threads(["a", "b"])
        self.assertTrue(detail.startswith("failed_at_post: 2/2"), detail[:80])


class MetricsLogHeaderTest(unittest.TestCase):
    """計測ログのヘッダ補完。行は動かさない（行番号で投稿管理シートを参照しているため）。"""

    def _sheet(self, current_header, col_count=10):
        sheet = mock.Mock()
        sheet.row_values.return_value = current_header
        sheet.col_count = col_count
        return sheet

    def test_appends_the_new_column_names_to_the_existing_header(self):
        sheet = self._sheet(METRICS_LOG_HEADER[:10])
        gsheets.ensure_metrics_log_header(sheet)
        self.assertEqual(sheet.update.call_args[0][0], [METRICS_LOG_HEADER])
        sheet.insert_row.assert_not_called()

    def test_widens_the_sheet_when_it_has_too_few_columns(self):
        sheet = self._sheet(METRICS_LOG_HEADER[:10], col_count=10)
        gsheets.ensure_metrics_log_header(sheet)
        sheet.add_cols.assert_called_once_with(len(METRICS_LOG_HEADER) - 10)

    def test_does_nothing_when_the_header_is_already_current(self):
        sheet = self._sheet(list(METRICS_LOG_HEADER), col_count=len(METRICS_LOG_HEADER))
        gsheets.ensure_metrics_log_header(sheet)
        sheet.update.assert_not_called()

    def test_leaves_an_unexpected_header_alone(self):
        sheet = self._sheet(["何か別の列", "投稿行"])
        gsheets.ensure_metrics_log_header(sheet)
        sheet.update.assert_not_called()
        sheet.add_cols.assert_not_called()


if __name__ == "__main__":
    unittest.main()
