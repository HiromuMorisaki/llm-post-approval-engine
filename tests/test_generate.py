"""generate.py の純粋ロジック（曜日判定・行整形）のテスト。外部I/Oには触れない。"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate import build_post_rows, is_note_promo_day

NOTE = {"title": "個人開発アプリを5日で作った話", "url": "https://note.com/example/n/xxxx"}


def _posts(x_texts, th_texts):
    return ([{"platform": "X", "content": [t]} for t in x_texts]
            + [{"platform": "Threads", "content": [t]} for t in th_texts])


THREE = _posts(["x1", "x2", "x3"], ["t1", "t2", "t3"])


class IsNotePromoDayTest(unittest.TestCase):
    def test_returns_true_on_tuesday_and_friday(self):
        self.assertTrue(is_note_promo_day(date(2026, 9, 22)))  # 火
        self.assertTrue(is_note_promo_day(date(2026, 9, 25)))  # 金

    def test_returns_false_on_the_other_five_days(self):
        for day in (21, 23, 24, 26, 27):  # 月水木土日
            self.assertFalse(is_note_promo_day(date(2026, 9, day)), day)

    def test_marks_exactly_two_days_per_week(self):
        week = [date(2026, 9, d) for d in range(21, 28)]
        self.assertEqual(sum(1 for d in week if is_note_promo_day(d)), 2)


class BuildPostRowsTest(unittest.TestCase):
    def _rows(self, promo_day, posts=THREE, recent=()):
        return build_post_rows(
            posts_data=posts, recent_posts=list(recent), today_str="2026-09-22",
            promo_pattern=1, target_note=NOTE if promo_day else None, promo_day=promo_day,
        )

    def test_schedules_three_slots_per_platform(self):
        rows = self._rows(promo_day=True)
        self.assertEqual(len(rows), 6)
        self.assertEqual(
            sorted({r["scheduled_at"] for r in rows}),
            ["2026-09-22 08:00:00", "2026-09-22 12:00:00", "2026-09-22 19:00:00"],
        )

    def test_third_slot_is_note_promo_on_a_promo_day(self):
        rows = self._rows(promo_day=True)
        promo = [r for r in rows if r["is_note_promo"] == "◯"]
        self.assertEqual(len(promo), 2)  # X と Threads で1本ずつ
        for row in promo:
            self.assertEqual(row["scheduled_at"], "2026-09-22 19:00:00")
            self.assertEqual(row["note_no"], NOTE["title"])
            self.assertEqual(row["prompt_pattern"], "Pattern 1")

    def test_gates_a_post_that_exceeds_the_platform_limit(self):
        # X の上限は280重み。全角150字＝300重みで、投げれば403になる
        over = [{"platform": "X", "content": ["あ" * 150]}]
        rows = self._rows(promo_day=False, posts=over)
        self.assertTrue(all(r["is_publishable"] == "×" for r in rows if r["platform"] == "X"))

    def test_no_row_is_a_note_promo_on_a_non_promo_day(self):
        rows = self._rows(promo_day=False)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r["is_note_promo"] == "×" for r in rows))
        self.assertTrue(all(r["note_no"] == "" for r in rows))
        self.assertTrue(all(r["prompt_pattern"] == "Standard" for r in rows))

    def test_still_fills_the_third_slot_on_a_non_promo_day(self):
        rows = self._rows(promo_day=False)
        self.assertEqual(sum(1 for r in rows if r["scheduled_at"].endswith("19:00:00")), 2)

    def test_gates_a_post_that_repeats_a_recent_one(self):
        posts = _posts(["まったく同じ本文です", "x2", "x3"], ["t1", "t2", "t3"])
        rows = self._rows(promo_day=False, posts=posts, recent=["まったく同じ本文です"])
        self.assertEqual(rows[0]["is_publishable"], "×")
        self.assertEqual(rows[1]["is_publishable"], "◯")

    def test_gates_a_post_containing_a_fact_banned_term(self):
        posts = _posts(["x1", "x2", "x3"], ["Claude 3.7 を使っている", "t2", "t3"])
        rows = self._rows(promo_day=False, posts=posts)
        gated = [r for r in rows if r["is_publishable"] == "×"]
        self.assertEqual([r["platform"] for r in gated], ["Threads"])

    def test_tolerates_fewer_posts_than_slots(self):
        rows = build_post_rows(
            posts_data=_posts(["x1"], ["t1", "t2"]), recent_posts=[], today_str="2026-09-22",
            promo_pattern=2, target_note=None, promo_day=False,
        )
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()
