"""Deterministic language fixtures for offline workflow acceptance.

This provider proves routing and failure handling, not translation quality.
Unknown translations fail closed instead of pretending the source is a translation.
"""

import re
from .models import ChannelError


PHRASES = [
    {'zh': '你好', 'ru': 'Здравствуйте', 'en': 'Hello', 'es': 'Hola'},
    {'zh': '您好，请问有什么可以帮您？', 'ru': 'Здравствуйте! Чем я могу вам помочь?', 'en': 'Hello! How can I help you?', 'es': '¡Hola! ¿En qué puedo ayudarle?'},
    {'zh': '我需要人工帮助', 'ru': 'Мне нужна помощь сотрудника', 'en': 'I need a human agent', 'es': 'Necesito un agente humano'},
    {'zh': '请安排人工客服', 'ru': 'Соедините меня с оператором', 'en': 'Please connect me to an agent', 'es': 'Conécteme con un agente'},
    {'zh': '您好，我来帮您处理。', 'ru': 'Здравствуйте! Я помогу вам.', 'en': 'Hello! I will help you.', 'es': '¡Hola! Le ayudaré.'},
    {'zh': '请问您需要什么帮助？', 'ru': 'Чем я могу вам помочь?', 'en': 'How can I help you?', 'es': '¿En qué puedo ayudarle?'},
    {'zh': '我们会尽快回复您。', 'ru': 'Мы ответим вам как можно скорее.', 'en': 'We will reply as soon as possible.', 'es': 'Le responderemos lo antes posible.'},
    {'zh': '请问您计划什么时候来中国？', 'ru': 'Когда вы планируете приехать в Китай?', 'en': 'When do you plan to visit China?', 'es': '¿Cuándo planea visitar China?'},
    {'zh': '请提供订单号。', 'ru': 'Пожалуйста, сообщите номер заказа.', 'en': 'Please provide your order number.', 'es': 'Indique su número de pedido.'},
    {'zh': '您的订单已确认。', 'ru': 'Ваш заказ подтверждён.', 'en': 'Your order is confirmed.', 'es': 'Su pedido está confirmado.'},
    {'zh': '谢谢', 'ru': 'Спасибо', 'en': 'Thank you', 'es': 'Gracias'},
    {'zh': '不客气，有问题随时联系。', 'ru': 'Пожалуйста! Обращайтесь, если возникнут вопросы.', 'en': 'You are welcome. Contact us if you have questions.', 'es': 'De nada. Contáctenos si tiene preguntas.'},
    {'zh': '已为您安排人工客服，请稍候。', 'ru': 'Я подключаю сотрудника. Пожалуйста, подождите.', 'en': 'I am connecting you to an agent. Please wait.', 'es': 'Le estoy conectando con un agente. Espere, por favor.'},
    {'zh': '你们的营业时间是什么？', 'ru': 'Какой у вас график работы?', 'en': 'What are your opening hours?', 'es': '¿Cuál es su horario de atención?'},
    {'zh': '我们的人工客服工作时间为周一至周五，09:00至18:00（北京时间）。', 'ru': 'Наши сотрудники работают с понедельника по пятницу с 09:00 до 18:00 по пекинскому времени.', 'en': 'Our agents work Monday to Friday, 09:00–18:00 Beijing time.', 'es': 'Nuestros agentes trabajan de lunes a viernes, de 09:00 a 18:00, hora de Pekín.'},
]


def _normal(text):
    return re.sub(r'[\s!！?？。.,，]+', ' ', text.strip().lower()).strip()


class SimulationLanguage:
    provider_name = 'deterministic-fixtures'
    supported = ('zh', 'ru', 'en', 'es')

    def suggest_reply(self, _transcript):
        return '您好，我来帮您处理。'

    def detect(self, text, previous='und', hint=''):
        value = _normal(text)
        if not value or value in ('ok', 'okay', 'да', 'нет', 'yes', 'no') or not any(c.isalpha() for c in value):
            return previous if previous in self.supported else self._hint(hint)
        for row in PHRASES:
            for lang, phrase in row.items():
                if _normal(phrase) == value:
                    return lang
        if re.search(r'[\u0400-\u04ff]', text):
            return 'ru'
        if re.search(r'[\u4e00-\u9fff]', text):
            return 'zh'
        if re.search(r'[¿¡ñáéíóú]', text, re.I):
            return 'es'
        if re.search(r'\b(hello|please|need|human|agent|thank|help|order|opening|hours)\b', text, re.I):
            return 'en'
        return previous if previous in self.supported else self._hint(hint)

    def _hint(self, hint):
        lang = hint.lower().split('-')[0].split('_')[0]
        return lang if lang in self.supported else 'und'

    def translate(self, text, source, target):
        if not text:
            return ''
        if source == target and target in self.supported:
            return text
        value = _normal(text)
        if not any(c.isalpha() for c in value):
            return text
        for row in PHRASES:
            if _normal(row.get(source, '\x00')) == value and target in row:
                return row[target]
        if value in ('ok', 'okay') and target in self.supported:
            return {'zh': '好的', 'ru': 'Хорошо', 'en': 'OK', 'es': 'De acuerdo'}[target]
        raise ChannelError('translation_unavailable', 'This phrase has no simulated translation fixture.')

    def reply(self, text, language):
        try:
            chinese = self.translate(text, language, 'zh')
        except ChannelError:
            return {'action': 'handoff', 'text': ''}
        if _normal(chinese) == _normal('你好'):
            answer = '您好，请问有什么可以帮您？'
        elif _normal(chinese) == _normal('谢谢'):
            answer = '不客气，有问题随时联系。'
        elif _normal(chinese) == _normal('你们的营业时间是什么？'):
            answer = '我们的人工客服工作时间为周一至周五，09:00至18:00（北京时间）。'
        else:
            return {'action': 'handoff', 'text': ''}
        return {'action': 'reply', 'text': self.translate(answer, 'zh', language)}
