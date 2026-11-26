"""Bounded intake questions and handoff messages for new conversations."""

from dataclasses import dataclass
import re

from .models import ChannelError


FIELDS = ("company", "cargo", "quantity", "origin", "destination")

# Templates keep customer messages independent of the extractor's output.
_COPY = {
    "zh": {
        "welcome": "您好！我是自动接待助手。",
        "transfer": "我会把您的需求转给人工客服，请稍候。",
        ("company",): "请问您来自哪家公司？",
        ("cargo", "quantity"): "需要运输什么货物？数量和单位是多少？",
        ("cargo",): "需要运输什么货物？",
        ("quantity",): "货物的数量和单位是多少？",
        ("origin", "destination"): "货物从哪里发出，要运到哪里？",
        ("origin",): "货物从哪里发出？",
        ("destination",): "货物要运到哪里？",
    },
    "ru": {
        "welcome": "Здравствуйте! Я виртуальный помощник службы поддержки.",
        "transfer": "Я передам ваш запрос сотруднику. Пожалуйста, подождите.",
        ("company",): "Какую компанию вы представляете?",
        ("cargo", "quantity"): "Какой груз нужно перевезти? Уточните количество и единицу измерения.",
        ("cargo",): "Какой груз нужно перевезти?",
        ("quantity",): "Уточните количество груза и единицу измерения.",
        ("origin", "destination"): "Откуда нужно забрать груз и куда его доставить?",
        ("origin",): "Откуда нужно забрать груз?",
        ("destination",): "Куда нужно доставить груз?",
    },
    "en": {
        "welcome": "Hello! I'm the automated support assistant.",
        "transfer": "I'll pass your request to a support agent. Please wait a moment.",
        ("company",): "Which company are you contacting us from?",
        ("cargo", "quantity"): "What cargo do you need to ship? Please include the quantity and unit.",
        ("cargo",): "What cargo are you shipping?",
        ("quantity",): "What is the quantity and unit?",
        ("origin", "destination"): "Where should the cargo be picked up and delivered?",
        ("origin",): "Where should the cargo be picked up?",
        ("destination",): "Where should the cargo be delivered?",
    },
    "es": {
        "welcome": "¡Hola! Soy el asistente automático de atención al cliente.",
        "transfer": "Pasaré su solicitud a un agente de atención al cliente. Por favor, espere un momento.",
        ("company",): "¿A qué empresa representa?",
        ("cargo", "quantity"): "¿Qué mercancía necesita transportar? Indique la cantidad y la unidad.",
        ("cargo",): "¿Qué mercancía necesita transportar?",
        ("quantity",): "¿Cuál es la cantidad y la unidad de medida?",
        ("origin", "destination"): "¿Dónde debemos recoger la mercancía y dónde debemos entregarla?",
        ("origin",): "¿Dónde debemos recoger la mercancía?",
        ("destination",): "¿Dónde debemos entregar la mercancía?",
    },
}
_QUESTION_GROUPS = (("company",), ("cargo", "quantity"), ("origin", "destination"))
_HUMAN_REQUEST = re.compile(
    r"转人工|人工客服|真人客服|找个人|"
    r"\b(?:human|live) (?:agent|support|representative)\b|\b(?:talk|speak) to a human\b|"
    r"\bhuman please\b|\bi need a human[.!?\s]*$|"
    r"\b(?:помощь сотрудника|мне нужен оператор|мне нужен человек|"
    r"соедините меня с оператором|переключите на оператора|позовите оператора|оператор, пожалуйста)\b|"
    r"^\s*оператор[.!?\s]*$|"
    r"\b(?:hablar con un agente|agente humano|persona real|agente por favor)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IntakeDecision:
    facts: dict[str, str]
    text: str
    reason: str | None


class Intake:
    def __init__(self, extract, *, max_replies=5, simulated=False):
        if (not callable(extract) or type(max_replies) is not int or not 3 <= max_replies <= 5
                or type(simulated) is not bool):
            raise ChannelError("invalid_intake_config")
        self.extract = extract
        self.max_replies = max_replies
        self.simulated = simulated

    def evaluate(self, text, language, facts, replies, *, first):
        """Use English customer copy when the language is undetermined or unsupported."""
        facts = dict(facts)
        if replies >= self.max_replies:
            return IntakeDecision(facts, "", "reply_limit")

        copy = _COPY.get(language, _COPY["en"])
        parts = [copy["welcome"]] if first else []
        transfer = " ".join([*parts, copy["transfer"]])
        if _HUMAN_REQUEST.search(text):
            return IntakeDecision(facts, transfer, "requested_human")

        missing = tuple(field for field in FIELDS if field not in facts)
        if missing:
            try:
                proposed = self.extract(text, missing)
                if not isinstance(proposed, dict):
                    raise ChannelError("invalid_intake_extraction")
            except ChannelError:
                return IntakeDecision(facts, transfer, "extract_unavailable")
            for field in missing:
                value = proposed.get(field)
                if isinstance(value, str) and (value := value.strip()) and value in text:
                    facts[field] = value

        if all(field in facts for field in FIELDS):
            return IntakeDecision(facts, transfer, "complete")
        if replies == self.max_replies - 1:
            return IntakeDecision(facts, transfer, "reply_limit")

        for group in _QUESTION_GROUPS:
            remaining = tuple(field for field in group if field not in facts)
            if remaining:
                parts.append(copy[remaining])
        return IntakeDecision(facts, " ".join(parts), None)
