import contextlib
import io
import json
import unittest
from unittest.mock import patch

from omni_service import onboarding, telegram_tdlib
from omni_service.models import ChannelError


def message(mid, chat=88, **fields):
    return {"@type": "message", "id": mid, "chat_id": chat, "is_outgoing": True, **fields}


def update(kind, old=123, chat=88):
    return {"@type": "updateMessageSend" + kind, "old_message_id": old, "message": message(456, chat)}


class Wire:
    def __init__(self, events):
        self.events = iter(events)
        self.sent = []

    def td_send(self, client, request):
        self.sent.append(json.loads(request))

    def td_receive(self, timeout):
        try:
            event = next(self.events)
        except StopIteration:
            raise ChannelError("tdlib_timeout") from None
        return json.dumps({"@client_id": 3, **event}).encode()


class ChallengeDelivery(unittest.TestCase):
    def verify(self, *events):
        td = telegram_tdlib.TDLib.__new__(telegram_tdlib.TDLib)
        td.client, td.sequence, td.authorization = 3, 0, None
        td._lib = Wire(events)
        try:
            td.send_start_confirmed(bot_user_id=88, chat_id=88, parameter="fixture")
        finally:
            self.assertEqual(len(td._lib.sent), 1)

    def pending(self):
        return {"@extra": 1, **message(123, sending_state={"@type": "messageSendingStatePending"})}

    def test_waits_for_matching_success_and_ignores_other_chat_id_and_client(self):
        self.verify(self.pending(), update("Succeeded", chat=99), update("Succeeded", old=999),
                    {**update("Failed"), "@client_id": 4}, update("Succeeded"))

    def test_confirmation_before_the_rpc_response(self):
        self.verify(update("Succeeded"), self.pending())

    def test_already_delivered_uses_tdlib_nullable_field_omission(self):
        self.verify({"@extra": 1, **message(456)})

    def test_failed_deleted_and_unknown_are_distinct(self):
        for event, code in [
            (update("Failed"), "telegram_setup_send_failed"),
            ({"@type": "updateDeleteMessages", "chat_id": 88, "message_ids": [123]}, "telegram_setup_send_failed"),
            (update("Succeeded", old=999), "telegram_setup_send_unknown"),
        ]:
            with self.subTest(event=event), self.assertRaisesRegex(ChannelError, code):
                self.verify(self.pending(), event)

    def test_non_outgoing_does_not_confirm(self):
        with self.assertRaisesRegex(ChannelError, "telegram_setup_send_unknown"):
            self.verify({"@extra": 1, **message(123), "is_outgoing": False})

    def test_legacy_checkpoint_cannot_skip_new_verification_and_failed_send_does_not_save(self):
        old = {"bot_id": 88, "issued_at": 999, "owner": 10, "nonce": "a" * 32}
        with (patch.object(onboarding, "enable_secretary", return_value={"id": 88}),
              patch.object(onboarding, "telegram_ready"), patch.object(onboarding.time, "time", return_value=1000)):
            from unittest.mock import Mock
            blocked = Mock(side_effect=ChannelError("telegram_setup_send_failed"))
            saved, api = Mock(), Mock()
            with self.assertRaisesRegex(ChannelError, "telegram_setup_send_failed"):
                onboarding._telegram_pair(api, {}, bind_account=blocked, pending=old, save_pending=saved)
            blocked.assert_called_once()
            saved.assert_not_called()
            api.call.assert_not_called()


class FeishuPermissions(unittest.TestCase):
    def test_missing_app_scopes_produce_exact_official_request_and_qr(self):
        from urllib.parse import parse_qs, urlsplit
        app = "cli_fixture"
        with (patch.object(onboarding, "_prepare_feishu", return_value=("--profile=fixture", {})),
              patch.object(onboarding, "_lark", return_value={"appId": app, "brand": "feishu",
                           "scopes": list(onboarding.BRIDGE_APP_SCOPES[:-1])}) as call,
              patch.object(onboarding, "_render_qr") as qr, contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaisesRegex(ChannelError, "feishu_app_scopes_missing"):
                onboarding._check_feishu_scopes(app, "fixture", None)
            url = urlsplit(qr.call_args.args[0])
            self.assertEqual(url.netloc, "open.feishu.cn")
            self.assertEqual(url.path, "/page/scope-apply")
            self.assertEqual(parse_qs(url.query), {"clientID": [app], "scopes": [onboarding.BRIDGE_APP_SCOPES[-1]]})
            call.assert_called_once_with(["--profile=fixture", "auth", "scopes", "--json"], {})

    def test_user_permissions_never_count_as_application_grant(self):
        with (patch.object(onboarding, "_prepare_feishu", return_value=("--profile=fixture", {})),
              patch.object(onboarding, "_lark", return_value={"appId": "cli_fixture", "brand": "feishu",
                           "userScopes": list(onboarding.BRIDGE_APP_SCOPES)}),
              patch.object(onboarding, "_render_qr") as qr):
            with self.assertRaisesRegex(ChannelError, "feishu_app_scopes_unverified"):
                onboarding._check_feishu_scopes("cli_fixture", "fixture", None)
            qr.assert_not_called()

    def test_complete_scope_list_does_not_request_permissions(self):
        with (patch.object(onboarding, "_prepare_feishu", return_value=("--profile=fixture", {})),
              patch.object(onboarding, "_lark", return_value={"appId": "cli_fixture", "brand": "feishu",
                           "scopes": list(onboarding.BRIDGE_APP_SCOPES)}),
              patch.object(onboarding, "_render_qr") as qr, contextlib.redirect_stdout(io.StringIO())):
            onboarding._check_feishu_scopes("cli_fixture", "fixture", None)
            qr.assert_not_called()


class PrivateTools(unittest.TestCase):
    def test_extracts_only_verified_cli_and_rejects_digest_mismatch(self):
        import hashlib
        from pathlib import Path
        import tarfile
        import tempfile
        from omni_service import onboarding_tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, installed = root / "fixture.gz", root / "cli"
            with tarfile.open(archive, "w:gz") as bundle:
                for name in ("lark-cli", "../not-extracted"):
                    payload = b"fixture"
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    bundle.addfile(member, io.BytesIO(payload))
            with self.assertRaisesRegex(ChannelError, "lark_cli_package_unverified"):
                onboarding_tools._extract_cli(archive, "0" * 64, installed)
            self.assertFalse(installed.exists())
            onboarding_tools._extract_cli(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), installed)
            self.assertEqual(installed.read_bytes(), b"fixture")
            self.assertEqual(installed.stat().st_mode & 0o777, 0o700)
            self.assertFalse((root.parent / "not-extracted").exists())
