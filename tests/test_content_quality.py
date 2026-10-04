"""短文SNSの量より質ゲートのテスト。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.content_quality import editorial_issues, strict_social_quality_issues


def valid_r1(platform="X", content=None):
    if content is None:
        content = ["あ" * 100]
    return {
        "platform": platform,
        "prompt_pattern": "r1-daily",
        "post_type": "failure",
        "fact_sources": ["facts:F01"],
        "visual_description": "ダウンロード数の推移を示すアプリ画面",
        "cta_type": "question",
        "media_url": "https://example.com/screenshot.jpg",
        "content": content,
    }


class EditorialIssuesTest(unittest.TestCase):
    def test_accepts_x_at_the_lower_and_upper_target(self):
        self.assertEqual(editorial_issues("X", ["あ" * 100], "https://example.com/a.jpg"), [])
        self.assertEqual(editorial_issues("X", ["あ" * 130], "https://example.com/a.jpg"), [])

    def test_rejects_x_outside_the_short_form_target(self):
        self.assertTrue(any("目標100〜130字" in issue
                            for issue in editorial_issues("X", ["あ" * 99], "https://example.com/a.jpg")))
        self.assertTrue(any("目標100〜130字" in issue
                            for issue in editorial_issues("X", ["あ" * 131], "https://example.com/a.jpg")))

    def test_accepts_threads_between_200_and_300_characters(self):
        self.assertEqual(editorial_issues("Threads", ["あ" * 200], "https://example.com/a.jpg"), [])
        self.assertEqual(editorial_issues("Threads", ["あ" * 300], "https://example.com/a.jpg"), [])

    def test_rejects_missing_visual(self):
        self.assertIn("投稿内容と対応するメディアURLがない",
                      editorial_issues("X", ["あ" * 100]))

    def test_rejects_a_local_media_path(self):
        self.assertIn("media_urlは公開されたhttp(s) URLにする",
                      editorial_issues("X", ["あ" * 100], "assets/screenshot.jpg"))

    def test_allows_only_a_url_as_a_second_threads_post(self):
        main = "あ" * 200
        self.assertEqual(editorial_issues(
            "Threads", [main, "https://example.com/app"], "https://example.com/a.jpg"), [])
        self.assertTrue(any("URLだけ" in issue for issue in editorial_issues(
            "Threads", [main, "詳しくはこちら"], "https://example.com/a.jpg")))

    def test_rejects_unbroken_sentences(self):
        issues = editorial_issues("Threads", ["あ" * 100 + "。" + "い" * 100],
                                  "https://example.com/a.jpg")
        self.assertTrue(any("句点の後" in issue for issue in issues))


class ShortPostMetadataTest(unittest.TestCase):
    def test_accepts_complete_r1_metadata(self):
        self.assertEqual(strict_social_quality_issues(valid_r1()), [])

    def test_requires_one_of_the_three_post_types(self):
        post = valid_r1()
        post["post_type"] = "tips"
        self.assertTrue(any("post_type" in issue for issue in strict_social_quality_issues(post)))

    def test_requires_fact_source_and_visual_description(self):
        post = valid_r1()
        post["fact_sources"] = []
        post["visual_description"] = ""
        issues = strict_social_quality_issues(post)
        self.assertTrue(any("fact_sources" in issue for issue in issues))
        self.assertTrue(any("visual_description" in issue for issue in issues))

    def test_does_not_apply_to_other_routines(self):
        self.assertEqual(strict_social_quality_issues({"prompt_pattern": "app-release"}), [])

    def test_applies_to_r2_and_r3_short_posts(self):
        for pattern in ("r2-weekly", "r3-zenn-announce"):
            post = valid_r1()
            post["prompt_pattern"] = pattern
            self.assertEqual(strict_social_quality_issues(post), [], pattern)


def valid_news(platform="X", content=None):
    if content is None:
        content = ["あ" * 80]
    return {
        "platform": platform,
        "prompt_pattern": "r1-ai-news",
        "post_type": "ai_news",
        "fact_sources": ["https://example.com/official-announcement"],
        "news_date": "2026-10-02",
        "cta_type": "none",
        "content": content,
    }


class AiNewsQualityTest(unittest.TestCase):
    def test_accepts_a_short_news_post_without_an_image(self):
        self.assertEqual(strict_social_quality_issues(valid_news()), [])
        self.assertEqual(strict_social_quality_issues(valid_news("Threads", ["あ" * 150])), [])

    def test_rejects_news_outside_the_short_target(self):
        self.assertTrue(any("目標60〜100字" in i
                            for i in strict_social_quality_issues(valid_news("X", ["あ" * 101]))))
        self.assertTrue(any("目標60〜100字" in i
                            for i in strict_social_quality_issues(valid_news("X", ["あ" * 59]))))
        self.assertTrue(any("目標80〜150字" in i
                            for i in strict_social_quality_issues(valid_news("Threads", ["あ" * 151]))))

    def test_requires_a_primary_source_url_and_news_date(self):
        post = valid_news()
        post["fact_sources"] = ["どこかで見た"]
        post["news_date"] = ""
        issues = strict_social_quality_issues(post)
        self.assertTrue(any("一次情報のURL" in i for i in issues))
        self.assertTrue(any("news_date" in i for i in issues))

    def test_requires_the_ai_news_post_type_and_a_single_post(self):
        post = valid_news(content=["あ" * 80, "い" * 80])
        post["post_type"] = "failure"
        issues = strict_social_quality_issues(post)
        self.assertTrue(any("ai_news" in i for i in issues))
        self.assertTrue(any("単発" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
