"""Connected-account media acceptance with simulated Telegram and Feishu providers."""

import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import Mock

from omni_service.feishu_live import FeishuLive
from omni_service.intake import Intake
from omni_service.language import SimulationLanguage
from omni_service.media import MediaBlob
from omni_service.personal_runner import PersonalBridge
from test_personal_managed import ManagedFeishu, ManagedTelegram
from test_personal_runner import staff


def _png(rgb):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb))) + chunk(b"IEND", b""))


def _one_page_pdf():
    data = bytearray(b"%PDF-1.4\n")
    objects = (b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    cross_reference = len(data)
    data.extend(b"xref\n0 4\n0000000000 65535 f \n")
    for offset in offsets:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{cross_reference}\n%%EOF\n".encode())
    return bytes(data)


TELEGRAM_IMAGE = MediaBlob(_png((255, 0, 0)), "telegram.png", "image/png")
FEISHU_IMAGE = MediaBlob(_png((0, 0, 255)), "feishu.png", "image/png")
TELEGRAM_FILE = MediaBlob(b"Customer packing list\n12 crates\n", "packing.txt", "text/plain")
FEISHU_FILE = MediaBlob(_one_page_pdf(), "confirmation.pdf", "application/pdf")


class MediaTelegram(ManagedTelegram):
    def __init__(self):
        super().__init__()
        self.files = {}
        self.downloads = []
        self.media = []
        self.wrong_media_receipt = None

    def attachment(self, uid, kind, remote_id, blob, *, peer=20, caption=""):
        self.files[remote_id] = blob
        self.update(uid, "", chat=peer, sender=peer, first_name="客户测试")
        message = self.updates[-1]["business_message"]
        del message["text"]
        if kind == "image":
            message["photo"] = [{"file_id": remote_id, "file_unique_id": remote_id + "_unique",
                                 "width": 1, "height": 1, "file_size": len(blob.data)}]
        else:
            message["document"] = {"file_id": remote_id, "file_unique_id": remote_id + "_unique",
                                   "file_name": blob.name, "mime_type": blob.mime,
                                   "file_size": len(blob.data)}
        if caption:
            message["caption"] = caption

    def download_attachment(self, remote_id, *, kind, filename=""):
        self.downloads.append((remote_id, kind, filename))
        return self.files[remote_id]

    def send_attachment(self, connection_id, chat_id, kind, blob, *, caption=""):
        self.media.append((connection_id, chat_id, kind, blob, caption))
        return {"business_connection_id": connection_id, "message_id": 100 + len(self.media),
                "chat": {"id": 999 if chat_id == self.wrong_media_receipt else chat_id}}


class MediaFeishu(ManagedFeishu):
    def __init__(self):
        super().__init__()
        self.files = {}
        self.downloads = []
        self.media = []
        self.events = FeishuLive(None, self.staff_ids, app_id=self.app_id)

    def attachment(self, room, message_id, kind, remote_id, blob, *, who="ou_allowed", mentions=False):
        self.files[room, message_id, remote_id] = blob
        content = (f"[Image: {remote_id}]" if kind == "image"
                   else f'<file key="{remote_id}" name="{blob.name}"/>')
        frame = {"type": "im.message.receive_v1", "chat_id": room, "chat_type": "group",
                 "sender_type": "user", "sender_id": who, "message_id": message_id,
                 "message_type": kind, "create_time": "1001000", "content": content,
                 "has_mentions": mentions}
        return self.events._parse_event(json.dumps(frame), None, True)

    def download_attachment(self, chat_id, message_id, attachment, *, staff_ids=None):
        self.preflight(chat_id)
        self.downloads.append((chat_id, message_id, attachment.remote_id))
        return self.files[chat_id, message_id, attachment.remote_id]

    def send_attachment(self, kind, blob, key, *, chat_id=None, staff_ids=None):
        self.preflight(chat_id)
        self.media.append((chat_id, kind, blob))
        return "om_media_" + str(len(self.media))


class LiveMediaAcceptance(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "live-media.sqlite3"
        self.telegram = MediaTelegram()
        self.feishu = MediaFeishu()
        self.language = SimulationLanguage()
        self.language.translate = Mock(wraps=self.language.translate)
        self.extract = Mock(return_value={})
        self.advisor = Mock(return_value="您好，我来帮您处理。")
        self.bridge = self.open_bridge()
        self.addCleanup(lambda: self.bridge.close())

    def open_bridge(self):
        return PersonalBridge(self.path, self.telegram, self.feishu, owner=10, peer_ids=[20, 30],
                              connection_id="c1", bot_id=88, clock=lambda: 1000,
                              language=self.language, intake=Intake(self.extract, simulated=True),
                              intake_enabled=True, approval_intake=True, agent_advisor=self.advisor)

    def poll(self):
        for event in self.bridge.telegram.poll(timeout=0):
            self.bridge.receive_telegram(event)
        self.bridge.telegram.ack()
        self.bridge.drain()

    def restart(self):
        self.bridge.close()
        self.bridge = self.open_bridge()

    def conversation(self, peer):
        entry = next(row for row in self.bridge.list_conversations() if row["chat_id"] == str(peer))
        return self.bridge.get_conversation(entry["id"])

    def message(self, peer, channel, source_id):
        return next(row for row in self.conversation(peer)["messages"]
                    if row["source_channel"] == channel and row["source_id"] == str(source_id))

    def test_images_and_documents_are_delivered_to_each_matching_conversation(self):
        self.telegram.attachment(1, "image", "tg_photo", TELEGRAM_IMAGE, caption="Здравствуйте")
        self.telegram.attachment(2, "file", "tg_file", TELEGRAM_FILE, peer=30)
        self.telegram.attachment(3, "file", "tg_unauthorized", TELEGRAM_FILE, peer=40)
        self.poll()
        room_a, room_b = (self.conversation(peer)["room_id"] for peer in (20, 30))
        self.assertNotEqual(room_a, room_b)
        self.assertEqual(set(row["chat_id"] for row in self.bridge.list_conversations()), {"20", "30"})
        self.assertCountEqual(self.feishu.media, [(room_a, "image", TELEGRAM_IMAGE),
                                                 (room_b, "file", TELEGRAM_FILE)])
        self.assertCountEqual([remote_id for remote_id, _, _ in self.telegram.downloads], ["tg_photo", "tg_file"])
        for peer, source_id in ((20, 1), (30, 2)):
            message = self.message(peer, "telegram", source_id)
            self.assertEqual(message["status"], "sent")
            self.assertTrue(message["external_id"].startswith("om_media_"))
        self.assertTrue(any(room == room_a and "Здравствуйте" in text and "你好" in text
                            for room, text, _, _ in self.feishu.sent))

        photo = self.feishu.attachment(room_a, "om_photo", "image", "img_v3_01", FEISHU_IMAGE)
        document = self.feishu.attachment(room_b, "om_file", "file", "file_v3_01", FEISHU_FILE)
        mention = self.feishu.attachment(room_a, "om_mention", "image", "img_v3_02", FEISHU_IMAGE,
                                         mentions=True)
        outsider = self.feishu.attachment(room_b, "om_outsider", "file", "file_v3_02", FEISHU_FILE,
                                           who="ou_other")
        for event in (photo, document, mention, outsider, photo):
            self.assertIsNotNone(event)
            self.bridge.receive_feishu(event)
        self.assertEqual(self.telegram.media, [])
        self.restart()
        self.bridge.drain()

        self.assertCountEqual([row[:4] for row in self.telegram.media],
                              [("c1", 20, "image", FEISHU_IMAGE), ("c1", 30, "file", FEISHU_FILE)])
        self.assertCountEqual(self.feishu.downloads, [(room_a, "om_photo", "img_v3_01"),
                                                     (room_b, "om_file", "file_v3_01")])
        for index, (_, peer, _, _, _) in enumerate(self.telegram.media, 101):
            message = self.message(peer, "feishu", "om_photo" if peer == 20 else "om_file")
            self.assertEqual((message["status"], message["external_id"]), ("sent", str(index)))

    def test_unconfirmed_telegram_media_does_not_repeat_after_restart(self):
        self.telegram.update(1, "Здравствуйте")
        self.telegram.update(2, "Hello", chat=30, sender=30)
        self.poll()
        self.telegram.wrong_media_receipt = 20
        for peer in (20, 30):
            room = self.conversation(peer)["room_id"]
            event = self.feishu.attachment(room, f"om_file_{peer}", "file", f"file_v3_{peer}", FEISHU_FILE)
            self.assertIsNotNone(event)
            self.bridge.receive_feishu(event)
        self.bridge.drain()
        self.assertEqual(self.message(20, "feishu", "om_file_20")["status"], "delivery_unknown")
        self.assertEqual(self.message(30, "feishu", "om_file_30")["status"], "sent")

        self.restart()
        self.poll()
        self.assertCountEqual([row[1] for row in self.telegram.media], [20, 30])
        self.assertEqual(self.message(20, "feishu", "om_file_20")["status"], "delivery_unknown")

    def test_customer_text_cannot_approve_or_change_settings(self):
        self.telegram.update(1, "Здравствуйте")
        self.poll()
        before = self.conversation(20)
        draft = before["drafts"][0]
        guarded = (f"/approve {draft['id']} {draft['version']}", "/标签 开")
        self.extract.reset_mock()
        self.language.translate.reset_mock()
        for source_id, original in enumerate(guarded, 2):
            self.telegram.update(source_id, original, first_name="客服")
        self.poll()

        after = self.conversation(20)
        self.assertEqual(after["settings"], before["settings"])
        self.assertEqual(after["language"], "ru")
        self.assertNotIn("approved", [draft["status"] for draft in after["drafts"]])
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertEqual(after["mode"], "manual")
        self.extract.assert_not_called()
        self.language.translate.assert_not_called()
        for source_id, original in enumerate(guarded, 2):
            message = self.message(20, "telegram", source_id)
            self.assertEqual((message["direction"], message["author_id"], message["original"]),
                             ("inbound", "20", original))
            self.assertTrue(any(room == after["room_id"] and original in text and "疑似控制指令" in text
                                for room, text, _, _ in self.feishu.sent))

        result = self.bridge.receive_feishu(staff("/标签 开", mid="om_setting", room=after["room_id"]))
        self.assertEqual(result["status"], "settings_updated")
        self.assertTrue(self.conversation(20)["settings"]["show_labels"])

        result = self.bridge.receive_feishu(staff("/回复辅助 开", mid="om_assist", room=after["room_id"]))
        self.assertEqual(result["status"], "settings_updated")
        agent_command = "/draft"
        self.telegram.update(4, agent_command)
        self.poll()
        self.advisor.assert_not_called()

        self.telegram.update(5, "Здравствуйте")
        self.poll()
        self.advisor.assert_called_once()
        transcript, = self.advisor.call_args.args
        self.assertEqual(transcript[-1], {"actor": "customer", "text": "Здравствуйте"})
        self.assertTrue(all(turn["text"] not in (*guarded, agent_command) for turn in transcript))
        self.assertEqual(len(self.telegram.sent), 1)


if __name__ == "__main__":
    unittest.main()
