"""scripts/insert_drafts.py の投入ゲートのテスト。シート・外部APIには触れない。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import insert_drafts

# X の上限は280重み（全角140字）。全角150字は300重みで超過する。
OVER_X = "あ" * 150
WITHIN_X = "あ" * 140
OVER_THREADS = "あ" * 501
WITHIN_THREADS = "あ" * 500


def draft(content, platform="X", is_publishable="◯"):
    return {"platform": platform, "content": content, "is_publishable": is_publishable}


class GatedPublishableTest(unittest.TestCase):
    def test_keeps_the_requested_value_when_every_gate_passes(self):
        self.assertEqual(insert_drafts.gated_publishable(draft(WITHIN_X), []), "◯")

    def test_gates_an_x_draft_that_exceeds_the_weight_limit(self):
        self.assertEqual(insert_drafts.gated_publishable(draft(OVER_X), []), "×")

    def test_gates_a_threads_draft_that_exceeds_the_character_limit(self):
        post = draft(OVER_THREADS, platform="Threads")
        self.assertEqual(insert_drafts.gated_publishable(post, []), "×")

    def test_allows_a_threads_draft_at_the_limit(self):
        post = draft(WITHIN_THREADS, platform="Threads")
        self.assertEqual(insert_drafts.gated_publishable(post, []), "◯")

    def test_gates_a_thread_whose_later_element_exceeds_the_limit(self):
        # ツリーの2本目で超過すると、1本目だけ投稿されてから403になる
        post = draft([WITHIN_X, OVER_X])
        self.assertEqual(insert_drafts.gated_publishable(post, []), "×")

    def test_checks_length_for_the_threads_sub_accounts_too(self):
        post = draft(OVER_THREADS, platform="threads_shigoto")
        self.assertEqual(insert_drafts.gated_publishable(post, []), "×")

    def test_still_gates_on_similarity(self):
        post = draft(WITHIN_X)
        self.assertEqual(insert_drafts.gated_publishable(post, [WITHIN_X]), "×")

    def test_still_gates_on_a_banned_term(self):
        post = draft("Claude 3.7 で書いた")
        self.assertEqual(insert_drafts.gated_publishable(post, []), "×")


class DraftQualityFailuresTest(unittest.TestCase):
    def test_rejects_an_r1_post_without_visual_and_audit_metadata(self):
        post = draft("あ" * 100)
        post["prompt_pattern"] = "r1-daily"
        failures = insert_drafts.draft_quality_failures([post])
        self.assertEqual(failures[0][0], 1)
        self.assertTrue(any("メディアURL" in issue for issue in failures[0][1]))

    def test_leaves_non_short_social_posts_out_of_the_strict_gate(self):
        post = draft("短い本文")
        post["prompt_pattern"] = "app-release"
        self.assertEqual(insert_drafts.draft_quality_failures([post]), [])


if __name__ == "__main__":
    unittest.main()
