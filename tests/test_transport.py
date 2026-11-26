import unittest

from omni_service.models import ChannelError, Request
from omni_service.simulation import FakePlatforms
from omni_service.transport import HttpTransport


class TransportAcceptance(unittest.TestCase):
    def setUp(self):
        self.platforms = self.enterContext(FakePlatforms())
        self.transport = HttpTransport(self.platforms.urls)
        self.request = Request("POST", "/botsimulation-token/sendMessage", {"chat_id": 123, "text": "Hello"})

    def assert_error(self, code, operation):
        with self.assertRaises(ChannelError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_real_http_send_reaches_platform(self):
        result = self.transport.send("telegram", self.request)
        self.assertEqual(result["result"]["message_id"], 1)
        self.assertEqual(self.platforms.messages[0]["text"], "Hello")
        self.assertEqual(self.platforms.messages[0]["target"], "123")

    def test_non_loopback_and_credential_urls_are_rejected(self):
        for url in ("https://api.telegram.org", "http://192.168.0.1", "http://example.com",
                    "http://127.0.0.1.example.com", "http://token@127.0.0.1", "file:///tmp/test"):
            with self.subTest(url=url):
                self.assert_error("simulation_only", lambda: HttpTransport({"telegram": url}))

    def test_relative_request_cannot_escape_loopback(self):
        for path in ("https://example.com", "//example.com/send", "/send\r\nInjected: yes"):
            with self.subTest(path=path):
                self.assert_error("invalid_request", lambda: self.transport.send("telegram", Request("POST", path)))
        self.assertEqual(self.platforms.requests, [])

    def test_redirect_is_not_followed(self):
        self.platforms.fail_next("telegram", "send", status=302, body={"redirect": True},
                                 headers={"Location": "https://example.com"})
        self.assert_error("redirect_rejected", lambda: self.transport.send("telegram", self.request))
        self.assertEqual(len(self.platforms.requests), 1)
        self.assertEqual(self.platforms.messages, [])

    def test_rate_limit_preserves_retry_after(self):
        self.platforms.fail_next("telegram", "send", status=429,
                                 body={"ok": False, "parameters": {"retry_after": 7}})
        error = self.assert_error("rate_limited", lambda: self.transport.send("telegram", self.request))
        self.assertEqual(error.retry_after, 7)

    def test_auth_denial_does_not_become_retryable_or_success(self):
        self.platforms.fail_next("telegram", "send", status=403, body={"error": "forbidden"})
        self.assert_error("authorization_failed", lambda: self.transport.send("telegram", self.request))
        self.assertEqual(self.platforms.messages, [])

    def test_server_failure_is_an_unconfirmed_delivery(self):
        self.platforms.fail_next("telegram", "send", status=503, body={"error": "unavailable"})
        self.assert_error("delivery_unknown", lambda: self.transport.send("telegram", self.request))
        self.assertEqual(len(self.platforms.requests), 1)

    def test_nondict_rate_limit_body_is_a_channel_error(self):
        self.platforms.fail_next("telegram", "send", status=429, body=["invalid response"])
        self.assert_error("delivery_unknown", lambda: self.transport.send("telegram", self.request))

    def test_null_or_array_retry_parameters_use_header_without_crashing(self):
        for parameters in (None, []):
            with self.subTest(parameters=parameters):
                self.platforms.fail_next("telegram", "send", status=429,
                    body={"parameters": parameters}, headers={"Retry-After": "4"})
                error = self.assert_error("rate_limited", lambda: self.transport.send("telegram", self.request))
                self.assertEqual(error.retry_after, 4)


if __name__ == "__main__":
    unittest.main()
