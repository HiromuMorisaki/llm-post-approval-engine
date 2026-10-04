"""main() の配線テスト。LLM・シートはすべてモックし、外部I/Oは一切行わない。"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate

JST = timezone(timedelta(hours=+9), 'JST')
NOTE = {"title": "個人開発アプリを5日で作った話", "url": "https://note.com/example/n/xxxx"}
GENERATED = ([{"platform": "X", "content": [f"x{i}"]} for i in range(3)]
             + [{"platform": "Threads", "content": [f"t{i}"]} for i in range(3)])


class MainWiringTest(unittest.TestCase):
    def _run_on(self, when):
        with mock.patch.object(generate, 'get_next_note', return_value=NOTE) as next_note, \
             mock.patch.object(generate, 'get_recent_contents', return_value=[]), \
             mock.patch.object(generate, 'generate_social_posts', return_value=GENERATED) as llm, \
             mock.patch.object(generate, 'append_posts') as append, \
             mock.patch.object(generate, 'datetime', mock.Mock(now=lambda tz: when)):
            generate.main()
        return next_note, llm, append.call_args[0][0]

    def test_does_not_touch_the_note_sheet_on_a_non_promo_day(self):
        next_note, llm, rows = self._run_on(datetime(2026, 9, 21, 22, 30, tzinfo=JST))  # 月
        next_note.assert_not_called()  # Notesシートの最終投稿日時を進めてはいけない
        self.assertIsNone(llm.call_args.kwargs['target_note'])
        self.assertTrue(all(r["is_note_promo"] == "×" for r in rows))

    def test_uses_the_note_sheet_on_a_promo_day(self):
        next_note, llm, rows = self._run_on(datetime(2026, 9, 22, 22, 30, tzinfo=JST))  # 火
        next_note.assert_called_once()
        self.assertEqual(llm.call_args.kwargs['target_note'], NOTE)
        self.assertEqual(sum(1 for r in rows if r["is_note_promo"] == "◯"), 2)

    def test_schedules_for_the_next_day_dates_from_jst_now(self):
        _, _, rows = self._run_on(datetime(2026, 9, 22, 22, 30, tzinfo=JST))
        self.assertTrue(all(r["scheduled_at"].startswith("2026-09-22 ") for r in rows))

    def test_falls_back_to_a_normal_slot_when_no_note_is_available(self):
        with mock.patch.object(generate, 'get_next_note', return_value=None), \
             mock.patch.object(generate, 'get_recent_contents', return_value=[]), \
             mock.patch.object(generate, 'generate_social_posts', return_value=GENERATED), \
             mock.patch.object(generate, 'append_posts') as append, \
             mock.patch.object(generate, 'datetime',
                               mock.Mock(now=lambda tz: datetime(2026, 9, 22, 22, 30, tzinfo=JST))):
            generate.main()
        rows = append.call_args[0][0]
        self.assertTrue(all(r["note_no"] == "" for r in rows))
        # 記事が取れなければ実際には通常投稿が生成されるので、promo として記録してはいけない
        self.assertTrue(all(r["is_note_promo"] == "×" for r in rows))
        self.assertTrue(all(r["prompt_pattern"] == "Standard" for r in rows))


if __name__ == "__main__":
    unittest.main()
