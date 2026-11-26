import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from omni_service.local_translation import LocalTranslation
from omni_service.models import ChannelError


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.responses = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.requests.append((self.path, self.headers.get("Authorization"), body))
                status, headers, raw = test.responses.pop(0)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_port
        self.translator = LocalTranslation(f"http://localhost:{self.port}/upstream/model/v1/", "sample",
                                          api_key="sample-key")

    def add_response(self, text, *, finish_reason="stop", fenced=False):
        self.add_fields_response({"translation": text}, finish_reason=finish_reason, fenced=fenced)

    def add_fields_response(self, fields, *, finish_reason="stop", fenced=False):
        content = json.dumps(fields, ensure_ascii=False)
        if fenced:
            content = f"```json\n{content}\n```"
        response = {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}
        self.responses.append((200, {}, json.dumps(response).encode()))

    def test_fresh_messages_use_the_model_in_both_directions(self):
        russian = "Проверьте, пожалуйста, заказ Л-815. Нужна доставка до пятницы."
        chinese = "请核对订单 Л-815。需要在周五前送达。"
        reply = "已核实，货物将于周四 16:00 前送达。"
        translated_reply = "Проверили: груз будет доставлен до 16:00 в четверг."
        self.add_response(chinese)
        self.add_response(translated_reply, fenced=True)
        self.assertEqual(self.translator.detect(russian), "ru")
        self.assertEqual(self.translator.translate(russian, "ru", "zh"), chinese)
        self.assertEqual(self.translator.translate(reply, "zh", "ru"), translated_reply)
        self.assertEqual(len(self.requests), 2)
        path, authorization, payload = self.requests[0]
        self.assertEqual((path, authorization), ("/upstream/model/v1/chat/completions", "Bearer sample-key"))
        self.assertEqual(payload["messages"][1], {"role": "user", "content": russian})
        self.assertIn("Russian into Simplified Chinese", payload["messages"][0]["content"])
        self.assertEqual(self.requests[1][2]["messages"][1]["content"], reply)

    def test_rejects_external_urls_and_redirects(self):
        for url in ("http://example.com/v1", "https://8.8.8.8/v1", "http://172.32.0.1/v1",
                    "http://user:pass@127.0.0.1", "http://127.0.0.1/v1?next=other", "http://127.0.0.1/a/../b"):
            with self.subTest(url=url), self.assertRaisesRegex(ChannelError, "^invalid_translation_config$"):
                LocalTranslation(url, "sample")
        for host in ("10.13.3.2", "172.16.0.2", "192.168.1.2", "100.99.88.77"):
            LocalTranslation(f"http://{host}:8080/v1", "sample")
        self.responses.append((302, {"Location": f"http://127.0.0.1:{self.port}/unexpected"}, b""))
        with self.assertRaisesRegex(ChannelError, "^translation_unavailable$"):
            self.translator.translate("机柜发货了。", "zh", "ru")
        self.assertEqual(len(self.requests), 1)

    def test_refuses_truncation_invalid_envelopes_and_untranslated_text(self):
        source = "机柜已经安排发货。"
        self.add_response(source)
        self.add_response("The cabinet is scheduled to ship.")
        self.add_response("Шкаф", finish_reason="length")
        self.responses.append((200, {}, b'{"choices":[[]]}'))
        self.responses.append((503, {}, b"service error containing sample-key"))
        for _ in range(5):
            with self.assertRaisesRegex(ChannelError, "^translation_unavailable$"):
                self.translator.translate(source, "zh", "ru")
        self.assertEqual(len(self.requests), 5)

    def test_same_language_and_number_only_messages_do_not_call_the_endpoint(self):
        self.assertEqual(self.translator.translate("订单已到。", "zh", "zh"), "订单已到。")
        self.assertEqual(self.translator.translate("16:00", "zh", "ru"), "16:00")
        with self.assertRaisesRegex(ChannelError, "^translation_unavailable$"):
            self.translator.translate("Please confirm.", "en", "ru")
        self.assertEqual(self.requests, [])

    def test_extracts_requested_intake_fields_without_translating_them(self):
        message = 'Мы ООО «Амур». Нужно перевезти 18 тонн кукурузы из Хабаровска.'
        extracted = {"company": "ООО «Амур»", "cargo": "кукурузы", "quantity": "18 тонн", "origin": "Хабаровска"}
        fields = (*extracted, "destination")
        self.add_fields_response({**extracted, "destination": None}, fenced=True)
        self.assertEqual(self.translator.extract_intake(message, fields), extracted)
        path, authorization, payload = self.requests[0]
        self.assertEqual((path, authorization), ("/upstream/model/v1/chat/completions", "Bearer sample-key"))
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", payload)
        self.assertIn(", ".join(fields), payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][1], {"role": "user", "content": message})

    def test_rejects_intake_output_with_wrong_shape_and_sanitizes_errors(self):
        self.add_fields_response({"cargo": "玉米"})
        self.add_fields_response({"quantity": 18})
        self.add_fields_response({"quantity": "18 吨"}, finish_reason="length")
        self.responses.append((503, {}, b"upstream error containing sample-key"))
        for _ in range(4):
            with self.assertRaisesRegex(ChannelError, "^intake_unavailable$"):
                self.translator.extract_intake("需要 18 吨玉米。", ("quantity",))
        self.assertEqual(len(self.requests), 4)
