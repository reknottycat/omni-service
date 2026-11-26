import unittest

from omni_service.language import SimulationLanguage
from omni_service.models import ChannelError


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.language = SimulationLanguage()

    def test_text_wins_over_profile_hint(self):
        self.assertEqual(self.language.detect('Здравствуйте', hint='en-US'), 'ru')
        self.assertEqual(self.language.detect('Hello', previous='ru', hint='ru'), 'en')

    def test_short_ambiguous_message_keeps_previous_language(self):
        self.assertEqual(self.language.detect('OK', previous='ru', hint='en'), 'ru')
        self.assertEqual(self.language.detect('12345', previous='es'), 'es')
        self.assertEqual(self.language.detect('👍', hint='ru-RU'), 'ru')

    def test_unknown_translation_never_returns_source_as_success(self):
        with self.assertRaises(ChannelError) as error:
            self.language.translate('这句话没有任何模拟译文', 'zh', 'ru')
        self.assertEqual(error.exception.code, 'translation_unavailable')

    def test_chinese_employee_phrases_translate_to_customer_language(self):
        self.assertEqual(self.language.translate('您好，我来帮您处理。', 'zh', 'ru'), 'Здравствуйте! Я помогу вам.')
        self.assertEqual(self.language.translate('请提供订单号。', 'zh', 'en'), 'Please provide your order number.')
        self.assertEqual(self.language.translate('您的订单已确认。', 'zh', 'es'), 'Su pedido está confirmado.')

    def test_known_knowledge_answer_and_unknown_escalation(self):
        reply = self.language.reply('What are your opening hours?', 'en')
        self.assertEqual(reply['action'], 'reply')
        self.assertIn('09:00', reply['text'])
        self.assertEqual(self.language.reply('Please confirm a custom complex order', 'en')['action'], 'handoff')


if __name__ == '__main__':
    unittest.main()
