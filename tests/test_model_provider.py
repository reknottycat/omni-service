from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import unittest

from omni_service.model_provider import LocalModels, ModelEndpoint, MAX_RESPONSE_BYTES
from omni_service.models import ChannelError


KB = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge"


def completion(value, *, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {
        "role": "assistant", "content": json.dumps(value, ensure_ascii=False)}}]}


def translated(text="Здравствуйте", source="zh", target="ru"):
    return {"translation": text, "source_language": source, "target_language": target}


class ModelServer:
    def __init__(self):
        self.responses = deque()
        self.calls = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                owner.calls.append({"path": self.path, "body": json.loads(raw),
                                    "authorization": self.headers.get("Authorization")})
                item = owner.responses.popleft() if owner.responses else (500, {"error": "no fixture"}, 0)
                status, body, delay = item
                if delay:
                    time.sleep(delay)
                raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    if status == 302:
                        self.send_header("Location", owner.url + "/credential-trap")
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def add(self, response, status=200, delay=0):
        self.responses.append((status, response, delay))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class LocalModelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ModelServer()
        self.config = {"api_kind": "openai-compatible", "base_url": self.server.url + "/v1",
                       "model": "synthetic-translation-model", "api_key_env": "MODEL_TEST_KEY",
                       "timeout_seconds": 2}
        self.env = {"MODEL_TEST_KEY": "synthetic-model-key"}

    def tearDown(self):
        self.server.close()

    def models(self, *, llm=True, **config):
        translation = {**self.config, **config}
        chat = {**self.config, "model": "synthetic-ai-model"} if llm else None
        return LocalModels(translation, chat, KB, environ=self.env)

    def test_openai_translation_uses_real_http_and_keeps_input_as_data(self):
        self.server.add(completion(translated()))
        models = self.models()
        self.assertEqual(models.translate("你好", "zh", "ru"), "Здравствуйте")
        request = self.server.calls[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["authorization"], "Bearer synthetic-model-key")
        body = request["body"]
        self.assertEqual(body["model"], "synthetic-translation-model")
        self.assertFalse(body["stream"])
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual([part["role"] for part in body["messages"]], ["system", "user"])
        self.assertEqual(json.loads(body["messages"][1]["content"])["text"], "你好")
        self.assertNotIn("synthetic-model-key", json.dumps(body))

    def test_detect_uses_text_not_profile_and_preserves_ambiguous_context(self):
        models = self.models()
        self.server.add(completion({"language": "ru", "confidence": 0.99}))
        self.assertEqual(models.detect("Какие документы нужны?", "en", "zh"), "ru")
        self.assertEqual(models.detect("ok", "ru", "en"), "ru")
        self.assertEqual(models.detect("12345", "und", "en-US"), "en")
        self.assertEqual(len(self.server.calls), 1)
        supplied = json.loads(self.server.calls[0]["body"]["messages"][1]["content"])
        self.assertEqual(set(supplied), {"text"})

    def test_unsupported_language_low_confidence_or_model_failure_return_unknown(self):
        models = self.models()
        for answer in ({"language": "russian", "confidence": 0.99},
                       {"language": "ru", "confidence": 0.2},
                       {"language": "en", "confidence": True},
                       {"language": "ru", "confidence": 100}):
            self.server.add(completion(answer))
            self.assertEqual(models.detect("A new substantive message", "ru"), "und")
        self.server.add({"error": "synthetic-model-key"}, status=500)
        self.assertEqual(models.detect("Bonjour à tous", "ru"), "und")

    def test_failed_wrong_language_unchanged_or_changed_quantity_never_falls_back(self):
        models = self.models()
        for answer in (translated("你好"), translated("Hello", target="en"),
                       translated("Hello", target="ru"), translated("Вес 100 кг")):
            self.server.add(completion(answer))
            with self.assertRaises(ChannelError) as error:
                models.translate("重量 10 公斤", "zh", "ru")
            self.assertEqual(error.exception.code, "translation_unavailable")
            self.assertNotIn("synthetic-model-key", str(error.exception))

    def test_numbers_and_order_ids_are_preserved_in_valid_translation(self):
        self.server.add(completion(translated("Номер заказа AB123, вес 10 кг")))
        self.assertEqual(self.models().translate("订单号 AB123，重量 10 公斤", "zh", "ru"),
                         "Номер заказа AB123, вес 10 кг")

    def test_same_language_and_neutral_symbols_do_not_call_model(self):
        models = self.models()
        self.assertEqual(models.translate("你好", "zh", "zh"), "你好")
        self.assertEqual(models.translate("12345 👍", "zh", "ru"), "12345 👍")
        self.assertEqual(models.translate("", "und", "und"), "")
        self.assertEqual(self.server.calls, [])
        with self.assertRaises(ChannelError):
            models.translate("你好", "zh", "und")

    def test_ollama_nonstreaming_json_contract(self):
        answer = {"message": {"role": "assistant", "content": json.dumps(translated())},
                  "done": True, "done_reason": "stop"}
        self.server.add(answer)
        self.assertEqual(self.models(api_kind="ollama", base_url=self.server.url).translate("你好", "zh", "ru"),
                         "Здравствуйте")
        call = self.server.calls[0]
        self.assertEqual(call["path"], "/api/chat")
        self.assertIs(call["body"]["stream"], False)
        self.assertEqual(call["body"]["format"]["additionalProperties"], False)
        self.assertEqual(call["body"]["options"]["temperature"], 0)

    def test_libretranslate_detection_and_translation_contract(self):
        self.server.add([{"language": "ru", "confidence": 98}])
        self.server.add({"translatedText": "请提供重量。"})
        models = self.models(api_kind="libretranslate", base_url=self.server.url)
        self.assertEqual(models.detect("Укажите вес"), "ru")
        self.assertEqual(models.translate("Укажите вес", "ru", "zh"), "请提供重量。")
        first, second = self.server.calls
        self.assertEqual(first["path"], "/detect")
        self.assertEqual(second["path"], "/translate")
        self.assertEqual(second["body"]["source"], "ru")
        self.assertEqual(second["body"]["target"], "zh")
        self.assertEqual(second["body"]["format"], "text")
        self.assertEqual(second["body"]["api_key"], "synthetic-model-key")
        self.assertIsNone(second["authorization"])

    def test_ai_emits_approved_knowledge_answer_and_evidence(self):
        self.server.add(completion({"action": "reply", "answer_id": "common-documents",
                                    "source_ids": ["common-documents"]}))
        models = self.models()
        answer = models.reply("Какие документы нужны?", "ru")
        self.assertEqual(answer["action"], "reply")
        self.assertEqual(answer["text"], models.knowledge.get("common-documents").answers["ru"])
        self.assertEqual(answer["source_ids"], ["common-documents"])
        self.assertTrue(answer["synthetic_knowledge"])
        self.assertEqual(answer["knowledge_revision"], models.knowledge.revision)
        sent = self.server.calls[0]["body"]
        self.assertEqual(sent["model"], "synthetic-ai-model")
        self.assertTrue(json.loads(sent["messages"][1]["content"])["knowledge_is_synthetic"])

    def test_quote_request_only_emits_approved_details_request(self):
        self.server.add(completion({"action": "reply", "answer_id": "quote-details",
                                    "source_ids": ["quote-details"]}))
        answer = self.models().reply("Сколько стоит доставка из Китая во Владивосток?", "ru")
        self.assertEqual(answer["action"], "reply")
        self.assertIn("вес", answer["text"])
        self.assertIn("готовой цены у меня нет", answer["text"])

    def test_unknown_sources_and_free_form_price_payloads_are_rejected(self):
        models = self.models()
        attempts = [
            {"action": "reply", "answer_id": "not-in-the-knowledge-base", "source_ids": ["not-in-the-knowledge-base"]},
            {"action": "reply", "answer_id": "common-documents", "source_ids": ["quote-details"]},
            {"action": "reply", "answer_id": "quote-details", "source_ids": ["quote-details"]},
            {"action": "reply", "answer_id": "common-documents", "source_ids": ["common-documents"],
             "text": "Guaranteed next-day delivery for 500 rubles"},
        ]
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                self.server.add(completion(attempt))
                self.assertEqual(models.reply("Какие документы нужны?", "ru")["action"], "handoff")

    def test_unknown_human_and_special_cargo_requests_need_no_model_call(self):
        models = self.models()
        for question in ("Find the orbital period of Venus", "Мне нужна помощь сотрудника",
                         "保证几天到货？", "Можно отправить опасный груз?", "Shipping lithium batteries"):
            self.assertEqual(models.reply(question, "ru")["action"], "handoff")
        self.assertEqual(self.server.calls, [])

    def test_unconfigured_ai_hands_off_without_using_translation_as_ai(self):
        self.assertEqual(self.models(llm=False).reply("Какие документы нужны?", "ru")["reason"],
                         "model_not_configured")
        self.assertEqual(self.server.calls, [])

    def test_other_language_uses_local_translation_of_approved_answer(self):
        self.server.add(completion({"action": "reply", "answer_id": "common-documents",
                                    "source_ids": ["common-documents"]}))
        self.server.add(completion(translated("Los documentos habituales incluyen el contrato y la factura; "
                                                "un empleado debe confirmar los requisitos.", target="es")))
        answer = self.models().reply("documents", "es")
        self.assertEqual(answer["action"], "reply")
        self.assertTrue(answer["text"].startswith("Los documentos"))
        second = self.server.calls[1]["body"]
        self.assertEqual(second["model"], "synthetic-translation-model")
        translated_input = json.loads(second["messages"][1]["content"])
        self.assertEqual(translated_input["target_language"], "es")
        self.assertIn("常见资料", translated_input["text"])

    def test_redirect_invalid_json_truncated_and_large_responses_fail_closed(self):
        responses = [({}, 302), (b"not JSON", 200), (completion(translated(), finish="length"), 200),
                     (b" " * (MAX_RESPONSE_BYTES + 1), 200)]
        for response, status in responses:
            self.server.add(response, status)
            with self.assertRaises(ChannelError) as error:
                self.models().translate("你好", "zh", "ru")
            self.assertEqual(error.exception.code, "translation_unavailable")
        self.assertEqual(len(self.server.calls), len(responses))
        self.assertNotIn("/credential-trap", [call["path"] for call in self.server.calls])

    def test_timeout_is_bounded_and_error_never_exposes_response_or_secret(self):
        self.server.add({"error": "synthetic-model-key and private message"}, delay=0.3)
        start = time.monotonic()
        with self.assertRaises(ChannelError) as error:
            self.models(timeout_seconds=0.05).translate("你好", "zh", "ru")
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertNotIn("synthetic-model-key", str(error.exception))
        self.assertNotIn("private message", str(error.exception))

    def test_credentials_and_remote_endpoint_policy(self):
        for base in ("http://8.8.8.8/v1", "http://169.254.169.254/v1", "http://user:key@127.0.0.1/v1",
                     self.server.url + "/v1?token=key", "file:///tmp/model"):
            with self.subTest(base=base), self.assertRaises(ChannelError):
                self.models(base_url=base)
        with self.assertRaises(ChannelError):
            self.models(allow_remote=True)
        with self.assertRaises(ChannelError):
            LocalModels({**self.config, "require_api_key": True}, None, KB, environ={})
        # LAN and Tailscale endpoint validation does not open a network connection.
        for address in ("http://192.168.1.10:8000/v1", "http://100.101.102.103:8000/v1"):
            endpoint = ModelEndpoint({**self.config, "base_url": address}, environ={})
            self.assertFalse(endpoint.allow_remote)
        self.assertEqual(self.server.calls, [])

    def test_oversized_inputs_and_duplicate_json_keys_are_rejected(self):
        models = self.models()
        with self.assertRaises(ChannelError):
            models.translate("你好" * 20_000, "zh", "ru")
        self.assertEqual(self.server.calls, [])
        self.server.add({"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
            "content": '{"language":"ru","language":"en","confidence":0.99}'}}]})
        self.assertEqual(models.detect("Здравствуйте"), "und")


if __name__ == "__main__":
    unittest.main()
