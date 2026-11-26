import json
from pathlib import Path
import tempfile
import unittest

from omni_service.knowledge import KnowledgeBase
from omni_service.models import ChannelError


ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "fixtures" / "knowledge"


class KnowledgeTests(unittest.TestCase):
    def test_chinese_russian_and_english_retrieval(self):
        knowledge = KnowledgeBase(KB)
        for question in ("需要哪些单证？", "Какие документы нужны?", "Which documents do you need?"):
            with self.subTest(question=question):
                self.assertEqual(knowledge.retrieve(question)[0].id, "common-documents")
        self.assertEqual(knowledge.retrieve("Сколько стоит доставка из Китая во Владивосток?")[0].id,
                         "quote-details")
        self.assertTrue(knowledge.synthetic)
        self.assertEqual(len(knowledge.revision), 64)

    def test_unknown_returns_no_candidates_and_hi_does_not_match_shipping(self):
        knowledge = KnowledgeBase(KB)
        self.assertEqual(knowledge.retrieve("Calculate the orbit of Venus"), [])
        self.assertNotIn("greeting", [entry.id for entry in knowledge.retrieve("shipping")])

    def test_markdown_companion_uses_versioned_json(self):
        readable = KnowledgeBase(KB / "logistics-zh-ru.md")
        executable = KnowledgeBase(KB / "logistics-zh-ru.json")
        self.assertEqual(readable.revision, executable.revision)
        self.assertTrue(readable.get("special-cargo").requires_human)
        self.assertEqual(set(readable.get("quote-details").answers), {"zh", "ru", "en"})

    def test_duplicate_ids_and_invalid_schema_fail_closed(self):
        original = json.loads((KB / "logistics-zh-ru.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "knowledge.json"
            for invalid in ({"schema": 2}, {**original, "entries": original["entries"] * 2},
                            {**original, "entries": []}, {**original, "synthetic": "yes"}):
                with self.subTest(invalid=invalid.get("schema")):
                    path.write_text(json.dumps(invalid))
                    with self.assertRaises(ChannelError) as error:
                        KnowledgeBase(path)
                    self.assertEqual(error.exception.code, "invalid_knowledge")

    def test_changed_answers_change_revision(self):
        original = json.loads((KB / "logistics-zh-ru.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "knowledge.json"
            path.write_text(json.dumps(original))
            first = KnowledgeBase(path)
            original["entries"][0]["answers"]["zh"] = "你好，请提供货物资料。"
            path.write_text(json.dumps(original))
            self.assertNotEqual(first.revision, KnowledgeBase(path).revision)


if __name__ == "__main__":
    unittest.main()
