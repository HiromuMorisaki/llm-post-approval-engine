"""fetch_metrics.main() の配線テスト。シート・外部APIはすべてモックする。"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch_metrics
from utils.gsheets import METRICS_LOG_HEADER

JST = timezone(timedelta(hours=+9), 'JST')
NOW = datetime(2026, 9, 22, 13, 10, tzinfo=JST)
HEADER = ["No", "ID", "予約時間", "投稿チャネル", "", "", "", "", "", "配信状況",
          "", "実際の投稿URL", "", "", "", "", ""]
EXTENDED_HEADER = HEADER + ["実公開時刻", "先頭投稿ID", "ツリー本数"]


def posted_row(no="1", scheduled="2026-09-22 12:00:00", url="17900000000000001",
               published=None):
    row = [""] * 17
    row[0], row[2], row[3], row[9], row[11] = no, scheduled, "Threads", "完了", url
    if published is not None:
        row += [published, "", ""]
    return row


class MainWiringTest(unittest.TestCase):
    def _run(self, rows, recorded=None, metrics=(1234, 5, 2), header=HEADER):
        sheet = mock.Mock()
        with mock.patch.object(fetch_metrics, 'get_worksheet', return_value=sheet), \
             mock.patch.object(fetch_metrics, 'call_with_retry',
                               side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             mock.patch.object(fetch_metrics, 'get_recorded_checkpoints',
                               return_value=recorded or {}), \
             mock.patch.object(fetch_metrics, 'fetch_threads_metrics',
                               **({'side_effect': metrics} if isinstance(metrics, list)
                                  else {'return_value': metrics})), \
             mock.patch.object(fetch_metrics, 'append_metrics_log') as append_log, \
             mock.patch.object(fetch_metrics, 'time', mock.Mock(sleep=lambda s: None)), \
             mock.patch.dict(os.environ, {'THREADS_ACCESS_TOKEN': 'tok'}), \
             mock.patch.object(fetch_metrics, 'datetime',
                               mock.Mock(now=lambda tz: NOW, strptime=datetime.strptime)):
            sheet.get_all_values.return_value = [header] + rows
            fetch_metrics.main()
        return sheet, append_log

    def test_appends_one_log_row_per_measurement(self):
        _, append_log = self._run([posted_row()])
        entries = append_log.call_args[0][0]
        self.assertEqual(len(entries), 1)
        measured_at, row_num, no, channel, scheduled, checkpoint, age, imp, likes, reps = entries[0][:10]
        self.assertEqual((row_num, checkpoint, imp, likes, reps), (2, 1, 1234, 5, 2))
        self.assertEqual(channel, "Threads")
        self.assertAlmostEqual(age, 1.17, places=2)

    def test_log_row_matches_the_metrics_log_header(self):
        _, append_log = self._run([posted_row()])
        self.assertEqual(len(append_log.call_args[0][0][0]), len(METRICS_LOG_HEADER))

    def test_records_the_head_post_separately_from_the_thread_total(self):
        # 先頭 1000 views / 2本目 200 views のツリー
        row = posted_row(url="1701,1702")
        _, append_log = self._run([row], metrics=[(1000, 5, 3), (200, 1, 0)])
        entry = dict(zip(METRICS_LOG_HEADER, append_log.call_args[0][0][0]))
        self.assertEqual(entry["先頭閲覧"], 1000)
        self.assertEqual(entry["ツリー閲覧"], 1200)
        self.assertEqual(entry["インプレッション数"], 1200)

    def test_subtracts_the_authors_own_follow_up_from_the_reply_count(self):
        row = posted_row(url="1701,1702")
        _, append_log = self._run([row], metrics=[(1000, 5, 3), (200, 1, 0)])
        entry = dict(zip(METRICS_LOG_HEADER, append_log.call_args[0][0][0]))
        self.assertEqual(entry["ツリー返信"], 3)
        self.assertEqual(entry["読者返信"], 2)  # 3 - (2本のツリー - 1)

    def test_records_the_basis_of_the_elapsed_time(self):
        _, append_log = self._run([posted_row()])
        entry = dict(zip(METRICS_LOG_HEADER, append_log.call_args[0][0][0]))
        self.assertEqual(entry["基準"], "予約")

    def test_uses_the_actual_publish_time_when_the_sheet_has_that_column(self):
        row = posted_row(published="2026-09-22 12:10:00")
        _, append_log = self._run([row], header=EXTENDED_HEADER)
        entry = dict(zip(METRICS_LOG_HEADER, append_log.call_args[0][0][0]))
        self.assertEqual(entry["基準"], "実公開")
        self.assertAlmostEqual(entry["実経過h"], 1.0, places=2)

    def test_does_not_record_the_checkpoint_when_the_rest_of_the_thread_fails(self):
        row = posted_row(url="1701,1702")
        _, append_log = self._run([row], metrics=[(1000, 5, 3), None])
        append_log.assert_not_called()

    def test_also_writes_the_latest_value_back_to_the_main_sheet(self):
        sheet, _ = self._run([posted_row()])
        cells = sheet.update_cells.call_args[0][0]
        self.assertEqual([(c.row, c.col, c.value) for c in cells][:3],
                         [(2, 14, 1234), (2, 15, 5), (2, 16, 2)])

    def test_does_nothing_when_no_checkpoint_is_due(self):
        sheet, append_log = self._run([posted_row()], recorded={2: {1}})
        append_log.assert_not_called()
        sheet.update_cells.assert_not_called()

    def test_does_not_record_a_checkpoint_when_the_fetch_fails(self):
        sheet, append_log = self._run([posted_row()], metrics=None)
        append_log.assert_not_called()
        sheet.update_cells.assert_not_called()

    def test_handles_an_empty_sheet(self):
        sheet = mock.Mock()
        sheet.get_all_values.return_value = [HEADER]
        with mock.patch.object(fetch_metrics, 'get_worksheet', return_value=sheet), \
             mock.patch.object(fetch_metrics, 'call_with_retry',
                               side_effect=lambda fn, *a, **kw: fn(*a, **kw)), \
             mock.patch.object(fetch_metrics, 'get_recorded_checkpoints') as recorded:
            fetch_metrics.main()
        recorded.assert_not_called()

    def test_skips_a_platform_with_no_token(self):
        row = posted_row()
        row[3] = "threads_shigoto"
        _, append_log = self._run([row])
        append_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
