import unittest

from omni_service.intake import FIELDS, Intake
from omni_service.models import ChannelError


class IntakePolicy(unittest.TestCase):
    def test_welcome_collects_all_three_groups_and_asks_only_for_missing_fields(self):
        answers = iter([
            {}, {"company": "ООО «Амур»", "origin": "Хабаровск"},
            {"cargo": "кукурузу", "quantity": "18 тонн", "destination": "Тунцзян"},
        ])
        requested = []

        def extract(_text, fields):
            requested.append(fields)
            return next(answers)

        intake = Intake(extract, simulated=True)
        self.assertTrue(intake.simulated)
        first = intake.evaluate("Здравствуйте!", "ru", {}, 0, first=True)
        self.assertIsNone(first.reason)
        for phrase in ("Здравствуйте", "компанию", "груз", "количество", "Откуда"):
            self.assertIn(phrase, first.text)

        second = intake.evaluate("ООО «Амур», Хабаровск", "ru", first.facts, 1, first=False)
        self.assertEqual(second.facts, {"company": "ООО «Амур»", "origin": "Хабаровск"})
        for phrase in ("Здравствуйте", "компанию", "Откуда"):
            self.assertNotIn(phrase, second.text)
        self.assertIn("Куда нужно доставить груз?", second.text)
        third = intake.evaluate("18 тонн, отправляем кукурузу в Тунцзян", "ru", second.facts, 2, first=False)
        self.assertEqual(third.reason, "complete")
        self.assertEqual(third.facts["destination"], "Тунцзян")
        self.assertEqual(third.facts["quantity"], "18 тонн")
        self.assertIn("сотруднику", third.text)
        self.assertNotIn("?", third.text)
        self.assertEqual(requested, [FIELDS, FIELDS, ("cargo", "quantity", "destination")])
        self.assertEqual(first.facts, {})

    def test_complete_first_message_still_gets_welcome(self):
        facts = {"company": "兴业", "cargo": "玉米", "quantity": "18吨", "origin": "同江", "destination": "佳木斯"}
        intake = Intake(lambda _t, _f: facts)
        result = intake.evaluate("兴业要运18吨玉米，从同江到佳木斯。", "zh", {}, 0, first=True)
        self.assertEqual(result.facts, facts)
        self.assertEqual(result.reason, "complete")
        self.assertTrue(result.text.startswith("您好！"))
        self.assertNotIn("？", result.text)

    def test_hard_limit_reserves_final_reply_for_handoff(self):
        for limit in (3, 4, 5):
            with self.subTest(limit=limit):
                intake = Intake(lambda _t, _f: {}, max_replies=limit)
                for replies in range(limit - 1):
                    decision = intake.evaluate("Please help", "und", {}, replies, first=replies == 0)
                    self.assertIsNone(decision.reason)
                    self.assertIn("Which company", decision.text)
                final = intake.evaluate("Please help", "und", {}, limit - 1, first=False)
                self.assertEqual(final.reason, "reply_limit")
                self.assertIn("support agent", final.text)
                self.assertNotIn("?", final.text)
                after = intake.evaluate("Please help", "und", {}, limit, first=False)
                self.assertEqual(after.text, "")
                self.assertEqual(after.reason, "reply_limit")

    def test_human_requests_skip_extraction(self):
        def extract(_text, _fields):
            self.fail("An explicit transfer request must skip extraction")

        intake = Intake(extract)
        for language, message, agent in (
            ("zh", "请安排人工客服", "人工客服"),
            ("ru", "Соедините меня с оператором", "сотруднику"),
            ("en", "I need a human agent", "support agent"),
            ("es", "Quiero hablar con un agente", "agente de atención"),
        ):
            with self.subTest(language=language):
                decision = intake.evaluate(message, language, {}, 0, first=True)
                self.assertEqual(decision.reason, "requested_human")
                self.assertIn(agent, decision.text)

    def test_accepts_only_literal_evidence_for_requested_fields(self):
        intake = Intake(lambda _t, _f: {
            "company": "另一家公司", "cargo": "  玉米  ", "quantity": 18,
            "origin": "临时地点", "destination": "", "external": "玉米",
        })
        decision = intake.evaluate("临时地点现有一批玉米。", "zh", {"origin": "同江"}, 1, first=False)
        self.assertEqual(decision.facts, {"cargo": "玉米", "origin": "同江"})
        self.assertIsNone(decision.reason)
        self.assertNotIn("临时地点", decision.text)

    def test_model_unavailable_or_unstructured_output_transfers_with_known_facts(self):
        def unavailable(_text, _fields):
            raise ChannelError("model_unavailable")

        for extract in (unavailable, lambda _t, _f: "not a structured result"):
            with self.subTest(extract=extract):
                known = {"company": "兴业"}
                result = Intake(extract).evaluate("您好", "zh", known, 1, first=False)
                self.assertEqual(result.facts, known)
                self.assertEqual(result.reason, "extract_unavailable")
                self.assertIn("人工客服", result.text)

    def test_rejects_invalid_configuration(self):
        for limit in (None, False, 2, 6, 3.0, "5"):
            with self.subTest(limit=limit), self.assertRaisesRegex(ChannelError, "invalid_intake_config"):
                Intake(lambda _t, _f: {}, max_replies=limit)
        with self.assertRaisesRegex(ChannelError, "invalid_intake_config"):
            Intake(None)
        with self.assertRaisesRegex(ChannelError, "invalid_intake_config"):
            Intake(lambda _t, _f: {}, simulated="false")
