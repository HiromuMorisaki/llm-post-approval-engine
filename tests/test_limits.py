"""utils/limits.py の文字数判定のテスト。外部I/Oには触れない。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.limits import (THREADS_CHAR_LIMIT, X_WEIGHT_LIMIT,
                          describe_over_length, over_length_positions,
                          threads_length, x_weight)


class XWeightTest(unittest.TestCase):
    def test_counts_full_width_characters_as_two(self):
        self.assertEqual(x_weight("あ"), 2)
        self.assertEqual(x_weight("あいう"), 6)

    def test_counts_ascii_as_one(self):
        self.assertEqual(x_weight("abc"), 3)

    def test_ignores_newlines(self):
        self.assertEqual(x_weight("あ\nい"), 4)

    def test_counts_a_url_as_twenty_three_regardless_of_length(self):
        short = x_weight("https://x.co/a")
        long = x_weight("https://example.com/" + "a" * 200)
        self.assertEqual(short, 23)
        self.assertEqual(long, 23)

    def test_140_full_width_characters_is_exactly_at_the_limit(self):
        self.assertEqual(x_weight("あ" * 140), X_WEIGHT_LIMIT)

    def test_141_full_width_characters_is_over(self):
        self.assertGreater(x_weight("あ" * 141), X_WEIGHT_LIMIT)


class ThreadsLengthTest(unittest.TestCase):
    def test_counts_characters_without_newlines(self):
        self.assertEqual(threads_length("あい\nうえ"), 4)


class OverLengthPositionsTest(unittest.TestCase):
    def test_returns_empty_when_every_post_fits(self):
        self.assertEqual(over_length_positions("X", ["あ" * 100, "い" * 100]), [])

    def test_reports_the_position_of_an_over_length_post_in_a_thread(self):
        over = over_length_positions("X", ["あ" * 10, "い" * 200])
        self.assertEqual(over, [(2, 400)])

    def test_reports_every_over_length_post(self):
        over = over_length_positions("X", ["あ" * 200, "い" * 10, "う" * 300])
        self.assertEqual([position for position, _ in over], [1, 3])

    def test_accepts_a_bare_string_as_a_single_post(self):
        self.assertEqual(over_length_positions("X", "あ" * 141), [(1, 282)])

    def test_uses_the_threads_limit_for_threads(self):
        self.assertEqual(over_length_positions("Threads", ["あ" * THREADS_CHAR_LIMIT]), [])
        self.assertEqual(over_length_positions("Threads", ["あ" * 501]), [(1, 501)])

    def test_treats_twitter_as_x(self):
        self.assertEqual(over_length_positions("twitter", ["あ" * 141]), [(1, 282)])

    def test_threads_sub_accounts_use_the_threads_limit(self):
        self.assertEqual(over_length_positions("threads_shigoto", ["あ" * 200]), [])


class DescribeOverLengthTest(unittest.TestCase):
    def test_names_the_failing_position_and_the_limit(self):
        message = describe_over_length("X", [(2, 312)])
        self.assertIn("2本目312重み", message)
        self.assertIn("280", message)


if __name__ == "__main__":
    unittest.main()
