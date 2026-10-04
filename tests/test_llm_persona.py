"""utils/llm.py のペルソナ読み込みのテスト。LLM API には触れない。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import llm


class LoadPersonaTest(unittest.TestCase):
    def test_falls_back_to_example_when_private_persona_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "persona.example.md"), "w", encoding="utf-8") as f:
                f.write("EXAMPLE")
            with mock.patch.object(llm, "_PROMPTS_DIR", tmp):
                self.assertEqual(llm.load_persona(), "EXAMPLE")

    def test_prefers_private_persona_over_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (("persona.md", "PRIVATE"), ("persona.example.md", "EXAMPLE")):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write(body)
            with mock.patch.object(llm, "_PROMPTS_DIR", tmp):
                self.assertEqual(llm.load_persona(), "PRIVATE")

    def test_raises_when_no_persona_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(llm, "_PROMPTS_DIR", tmp):
                with self.assertRaises(FileNotFoundError):
                    llm.load_persona()

    def test_bundled_example_persona_is_loadable(self):
        self.assertIn("事実台帳", llm.load_persona())


if __name__ == "__main__":
    unittest.main()
