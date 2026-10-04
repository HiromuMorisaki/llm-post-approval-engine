"""fetch_metrics.py の計測プラン決定ロジックのテスト。シート・外部APIには触れない。"""
import os
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch_metrics
from fetch_metrics import (BASIS_PUBLISHED, BASIS_SCHEDULED, COL_PUBLISHED_AT,
                           MEASUREMENT_CHECKPOINTS_H, due_checkpoint, parse_scheduled_at,
                           plan_measurements, reader_replies)

JST = timezone(timedelta(hours=+9), 'JST')
NOW = datetime(2026, 9, 22, 13, 10, tzinfo=JST)


def sheet_row(no="1", scheduled="2026-09-22 12:00:00", channel="Threads",
              status="完了", url="17900000000000001", published=None):
    """投稿管理シートの1行を組み立てる（published を渡すと AW03 の拡張列つき）。"""
    row = [""] * 17
    row[0], row[2], row[3], row[9], row[11] = no, scheduled, channel, status, url
    if published is not None:
        row += [published, "", ""]
    return row


class DueCheckpointTest(unittest.TestCase):
    def test_returns_none_before_the_first_checkpoint(self):
        self.assertIsNone(due_checkpoint(0.5, set()))

    def test_returns_the_latest_checkpoint_already_passed(self):
        self.assertEqual(due_checkpoint(1.2, set()), 1)
        self.assertEqual(due_checkpoint(25.0, set()), 24)
        self.assertEqual(due_checkpoint(170.0, set()), 168)

    def test_returns_none_once_that_checkpoint_is_recorded(self):
        self.assertIsNone(due_checkpoint(1.2, {1}))
        self.assertIsNone(due_checkpoint(200.0, {168}))

    def test_does_not_backfill_checkpoints_that_were_missed(self):
        # 3ヶ月前の投稿を初めて計測しても、1h/24h の値はもう観測できない
        self.assertEqual(due_checkpoint(2000.0, set()), 168)

    def test_moves_on_to_the_next_checkpoint(self):
        self.assertEqual(due_checkpoint(25.0, {1}), 24)
        self.assertEqual(due_checkpoint(170.0, {1, 24}), 168)


class ParseScheduledAtTest(unittest.TestCase):
    def test_reads_the_sheet_datetime_as_jst(self):
        self.assertEqual(parse_scheduled_at("2026-09-22 12:00:00"),
                         datetime(2026, 9, 22, 12, 0, tzinfo=JST))

    def test_accepts_a_value_without_seconds(self):
        self.assertIsNotNone(parse_scheduled_at("2026-09-22 12:00"))

    def test_returns_none_for_an_unparseable_value(self):
        for bad in ("", "   ", "未定", "2026/13/45"):
            self.assertIsNone(parse_scheduled_at(bad), bad)


class PlanMeasurementsTest(unittest.TestCase):
    def test_selects_a_completed_post_that_has_passed_a_checkpoint(self):
        plan = plan_measurements([sheet_row()], recorded={}, now=NOW)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0].checkpoint_h, 1)
        self.assertEqual(plan[0].row_num, 2)
        self.assertEqual(plan[0].media_ids, ["17900000000000001"])
        self.assertAlmostEqual(plan[0].age_h, 1.1666, places=3)

    def test_skips_a_post_that_is_not_yet_published(self):
        rows = [sheet_row(status="未対応"), sheet_row(status="エラー: 403 Forbidden", url="")]
        self.assertEqual(plan_measurements(rows, recorded={}, now=NOW), [])

    def test_skips_a_post_whose_checkpoint_is_already_recorded(self):
        plan = plan_measurements([sheet_row()], recorded={2: {1}}, now=NOW)
        self.assertEqual(plan, [])

    def test_skips_x_posts_unless_measurement_is_enabled(self):
        row = sheet_row(channel="X", url="https://x.com/h/status/123")
        self.assertEqual(plan_measurements([row], recorded={}, now=NOW), [])
        enabled = plan_measurements([row], recorded={}, now=NOW, measure_x=True)
        self.assertEqual(len(enabled), 1)
        self.assertEqual(enabled[0].media_ids, ["123"])

    def test_splits_a_thread_into_its_media_ids(self):
        plan = plan_measurements([sheet_row(url="111, 222 ,333")], recorded={}, now=NOW)
        self.assertEqual(plan[0].media_ids, ["111", "222", "333"])

    def test_skips_a_row_with_an_unreadable_scheduled_time(self):
        self.assertEqual(plan_measurements([sheet_row(scheduled="")], recorded={}, now=NOW), [])

    def test_prioritises_time_sensitive_checkpoints_over_old_backfill(self):
        rows = [
            sheet_row(no="old", scheduled="2026-06-01 08:00:00", url="old1"),   # 168h 期限切れ
            sheet_row(no="new", scheduled="2026-09-22 12:00:00", url="new1"),   # 1h
            sheet_row(no="mid", scheduled="2026-09-21 08:00:00", url="mid1"),   # 24h
        ]
        plan = plan_measurements(rows, recorded={}, now=NOW, limit=2)
        self.assertEqual([m.checkpoint_h for m in plan], [1, 24])

    def test_caps_the_number_of_measurements_per_run(self):
        rows = [sheet_row(no=str(i), url=f"id{i}") for i in range(10)]
        self.assertEqual(len(plan_measurements(rows, recorded={}, now=NOW, limit=3)), 3)

    def test_tolerates_short_rows(self):
        short = ["1", "", "2026-09-22 12:00:00", "Threads"]
        self.assertEqual(plan_measurements([short], recorded={}, now=NOW), [])

    def test_checkpoints_are_ordered_and_cover_1h_24h_7d(self):
        self.assertEqual(MEASUREMENT_CHECKPOINTS_H, (1, 24, 168))

    def test_records_the_head_post_and_the_thread_length(self):
        plan = plan_measurements([sheet_row(url="111,222,333")], recorded={}, now=NOW)
        self.assertEqual(plan[0].head_id, "111")
        self.assertEqual(plan[0].thread_len, 3)


class MeasurementBasisTest(unittest.TestCase):
    """経過時間の基準（実公開／予約）の選び方。"""

    def test_uses_the_scheduled_time_when_the_column_does_not_exist(self):
        plan = plan_measurements([sheet_row()], recorded={}, now=NOW)
        self.assertEqual(plan[0].basis, BASIS_SCHEDULED)
        self.assertAlmostEqual(plan[0].age_h, 1.1666, places=3)

    def test_uses_the_actual_publish_time_when_it_is_recorded(self):
        # 予約12:00に対し実公開は12:10。経過は予約基準の1.17hではなく実公開からの1.0h
        row = sheet_row(published="2026-09-22 12:10:00")
        plan = plan_measurements([row], recorded={}, now=NOW,
                                 published_at_col=COL_PUBLISHED_AT)
        self.assertEqual(plan[0].basis, BASIS_PUBLISHED)
        self.assertAlmostEqual(plan[0].age_h, 1.0, places=3)

    def test_a_late_publish_delays_the_first_checkpoint(self):
        # 予約から1h過ぎていても、実公開が30分前なら1hチェックポイントはまだ来ない
        row = sheet_row(published="2026-09-22 12:40:00")
        self.assertEqual(
            plan_measurements([row], recorded={}, now=NOW, published_at_col=COL_PUBLISHED_AT), [])

    def test_falls_back_to_the_scheduled_time_when_the_cell_is_empty(self):
        plan = plan_measurements([sheet_row(published="")], recorded={}, now=NOW,
                                 published_at_col=COL_PUBLISHED_AT)
        self.assertEqual(plan[0].basis, BASIS_SCHEDULED)

    def test_falls_back_to_the_scheduled_time_when_the_cell_is_unreadable(self):
        plan = plan_measurements([sheet_row(published="不明")], recorded={}, now=NOW,
                                 published_at_col=COL_PUBLISHED_AT)
        self.assertEqual(plan[0].basis, BASIS_SCHEDULED)

    def test_tolerates_a_row_that_has_not_been_extended_yet(self):
        plan = plan_measurements([sheet_row()], recorded={}, now=NOW,
                                 published_at_col=COL_PUBLISHED_AT)
        self.assertEqual(plan[0].basis, BASIS_SCHEDULED)


class FetchThreadsMetricsTest(unittest.TestCase):
    """取得できなかった投稿を0として記録しないこと。"""

    def _response(self, payload):
        return mock.Mock(json=mock.Mock(return_value=payload))

    def _insights(self, views, likes, replies):
        return {"data": [{"name": "views", "values": [{"value": views}]},
                         {"name": "likes", "values": [{"value": likes}]},
                         {"name": "replies", "values": [{"value": replies}]}]}

    def test_sums_the_given_media_ids(self):
        with mock.patch.object(fetch_metrics.requests, 'get',
                               side_effect=[self._response(self._insights(100, 2, 1)),
                                            self._response(self._insights(40, 1, 0))]), \
             mock.patch.object(fetch_metrics, 'time', mock.Mock(sleep=lambda s: None)):
            self.assertEqual(fetch_metrics.fetch_threads_metrics("tok", ["1", "2"]), (140, 3, 1))

    def test_returns_none_when_one_of_the_ids_cannot_be_read(self):
        # 取れた分だけ足して返すと、実際より小さい値を観測値として記録してしまう
        with mock.patch.object(fetch_metrics.requests, 'get',
                               side_effect=[self._response(self._insights(100, 2, 1)),
                                            self._response({"error": {"code": 100}})]), \
             mock.patch.object(fetch_metrics, 'time', mock.Mock(sleep=lambda s: None)):
            self.assertIsNone(fetch_metrics.fetch_threads_metrics("tok", ["1", "2"]))


class ReaderRepliesTest(unittest.TestCase):
    """ツリー全体の返信から自分の続き投稿を引いた「読者返信」。"""

    def test_subtracts_the_authors_own_follow_up_posts(self):
        self.assertEqual(reader_replies(5, thread_len=3), 3)

    def test_returns_the_raw_count_for_a_single_post(self):
        self.assertEqual(reader_replies(4, thread_len=1), 4)

    def test_marks_a_negative_result_as_missing_rather_than_zero(self):
        # 続き投稿が消えている等。0（読者の返信が無かった）と区別する
        self.assertEqual(reader_replies(0, thread_len=3), "欠測")


if __name__ == "__main__":
    unittest.main()
