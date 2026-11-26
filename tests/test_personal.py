import unittest
from omni_service.personal import TelegramPersonal
from omni_service.models import ChannelError


def connection(enabled=True, owner=10, reply=True, delete=True):
    return {"id": "c1", "user": {"id": owner}, "is_enabled": enabled,
            "rights": {"can_reply": reply, "can_delete_sent_messages": delete}}


def message(chat=20, sender=20, **extra):
    return {"update_id": 100, "business_message": {
        "business_connection_id": "c1", "chat": {"id": chat, "type": "private"},
        "from": {"id": sender, "is_bot": False}, "message_id": 5,
        "date": 1000, "text": "Здравствуйте", **extra}}


class PersonalContract(unittest.TestCase):
    def setUp(self):
        self.bridge = TelegramPersonal(expected_owner_id=10, allowed_chat_ids=[20])
        self.bridge.apply_connection(connection())

    def test_default_deny_and_private_chat_filter(self):
        empty = TelegramPersonal(expected_owner_id=10)
        empty.apply_connection(connection())
        self.assertEqual(empty.parse(message()), [])
        self.assertEqual(self.bridge.parse(message(chat=21, sender=21)), [])
        self.assertEqual(self.bridge.parse(message(chat={"id": 20, "type": "group"})), [])
        self.assertEqual(self.bridge.parse(message(business_connection_id="other")), [])

    def test_allowlist_iterator_is_retained_and_validated(self):
        bridge = TelegramPersonal(expected_owner_id=10, allowed_chat_ids=iter([20]))
        bridge.apply_connection(connection())
        self.assertEqual(bridge.parse(message())[0].role, "customer")
        with self.assertRaises(ChannelError):
            TelegramPersonal(expected_owner_id=10, allowed_chat_ids=iter([20, -1]))

    def test_owner_customer_and_echo_are_distinct(self):
        result, = self.bridge.parse(message())
        self.assertEqual((result.role, result.event.account_id, result.event.chat_id),
                         ("customer", "c1", "20"))
        self.assertEqual(result.event.text, "Здравствуйте")
        self.assertEqual(self.bridge.parse(message(sender=10))[0].role, "owner")
        self.assertEqual(self.bridge.parse(message(sender=10, sender_business_bot={"id": 9})), [])

    def test_deletion_and_scope(self):
        event = {"update_id": 101, "deleted_business_messages": {
            "business_connection_id": "c1", "chat": {"id": 20, "type": "private"},
            "message_ids": [5, 6]}}
        self.assertEqual([x.event.message_id for x in self.bridge.parse(event)], ["5", "6"])
        event["deleted_business_messages"]["chat"]["id"] = 21
        self.assertEqual(self.bridge.parse(event), [])

    def test_send_original_account_and_window(self):
        method, body = self.bridge.send_payload("c1", 20, "Привет", last_incoming_at=1000, now=1001)
        self.assertEqual(method, "sendMessage")
        self.assertEqual(body, {"business_connection_id": "c1", "chat_id": 20, "text": "Привет"})
        for target, now in [(21, 1001), (20, 87400), (20, 999)]:
            with self.assertRaises(ChannelError):
                self.bridge.send_payload("c1", target, "x", last_incoming_at=1000, now=now)

    def test_revocation_and_owner_binding(self):
        self.assertFalse(self.bridge.apply_connection(connection(owner=11)))
        self.bridge.apply_connection(connection(enabled=False))
        self.assertEqual(self.bridge.parse(message()), [])
        with self.assertRaises(ChannelError):
            self.bridge.send_payload("c1", 20, "x", last_incoming_at=1000, now=1001)

    def test_recall_exact_receipt_and_right(self):
        self.assertEqual(self.bridge.delete_payload("c1", 20, 5, confirmed_sent=("c1", 20, 5)),
                         ("deleteBusinessMessages", {"business_connection_id": "c1", "message_ids": [5]}))
        with self.assertRaises(ChannelError):
            self.bridge.delete_payload("c1", 20, 5, confirmed_sent=("c1", 21, 5))
        self.bridge.apply_connection(connection(delete=False))
        with self.assertRaises(ChannelError):
            self.bridge.delete_payload("c1", 20, 5, confirmed_sent=("c1", 20, 5))

    def test_poll_only_business_updates(self):
        method, body = self.bridge.poll_payload(101)
        self.assertEqual(method, "getUpdates")
        self.assertEqual(body["offset"], 101)
        self.assertNotIn("message", body["allowed_updates"])
