"""Contracts for first-run CLI setup without desktop interaction."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from omni_service import onboarding, telegram_tdlib
from omni_service.models import ChannelError


BOT = {"id": 88, "username": "logistics_fixture_bot", "is_bot": True,
       "can_connect_to_business": True}
OWNER = {"id": 10, "type": {"@type": "userTypeRegular"},
         "usernames": {"active_usernames": ["fixture_owner"]}}


class AccountClient:
    """Synthetic TDLib account with an observable server-side connection."""

    def __init__(self, owner=OWNER, existing=None, unwanted_rights=False):
        self.owner = owner
        self.connection = existing
        self.unwanted_rights = unwanted_rights
        self.sent = []

    def call(self, name, **request):
        if name == "getMe":
            return self.owner
        if name in ("searchPublicChat", "createPrivateChat"):
            peer_id = request.get("user_id", BOT["id"])
            return {"id": peer_id, "type": {"@type": "chatTypePrivate", "user_id": peer_id}}
        if name == "getBusinessConnectedBot":
            return ({"@type": "businessConnectedBotInfo", "bot": self.connection}
                    if self.connection is not None else None)
        if name == "setBusinessConnectedBot":
            self.connection = request["bot"]
            if self.unwanted_rights:
                self.connection["rights"]["can_delete_all_messages"] = True
            return {"@type": "ok"}
        raise AssertionError(name)

    def send_start_confirmed(self, **request):
        self.sent.append(request)


class TelegramCommandContracts(unittest.TestCase):
    def test_cli_configuration_requires_same_verified_platform_owner(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            config = directory / "personal.json"
            config.write_bytes((root / "config" / "personal.example.json").read_bytes())
            credentials = directory / "credentials.env"
            credentials.write_text("TELEGRAM_BOT_TOKEN=88:mock\nOMNI_FEISHU_APP_SECRET=fixture-secret\n"
                                   "TRANSLATION_API_KEY=\n")
            credentials.chmod(0o600)
            api_application = directory / "telegram-application.json"
            api_application.write_text(json.dumps({"api_id": 123, "api_hash": "a" * 32}))
            api_application.chmod(0o600)
            library = directory / "libtdjson.so"
            library.touch()
            args = ["configure", "--config", str(config), "--secrets-file", str(credentials),
                    "--feishu-app", "cli_fixture", "--callback-url", "https://example.test/admin/auth/callback",
                    "--model-url", "http://127.0.0.1:8085/v1", "--model", "fixture",
                    "--chat-scope", "allowlist", "--test-chat-id", "30",
                    "--tdlib-library", str(library), "--tdlib-app-credentials", str(api_application),
                    "--telegram-owner", "@fixture_owner"]
            account = AccountClient()

            class BotAPI:
                peer = 20

                def call(self, method, payload=None):
                    if method == "getMe":
                        return BOT
                    if method == "getWebhookInfo":
                        return {"url": ""}
                    result = {"id": "conn", "user": {"id": self.peer},
                              "is_enabled": True, "rights": {"can_reply": True}}
                    if method == "getBusinessConnection":
                        return result
                    if method == "getUpdates":
                        nonce = account.sent[-1]["parameter"]
                        return [{"message": {"text": "/start " + nonce, "date": 1001,
                                 "from": {"id": self.peer, "is_bot": False},
                                 "chat": {"id": self.peer, "type": "private"}}},
                                {"business_connection": result}]
                    raise AssertionError(method)

            api = BotAPI()

            def connect(library, directory, *, bot, expected_owner, nonce, included,
                        excluded, selectors, login, show_qr, proxy, application):
                self.assertEqual(login, "qr")
                self.assertEqual(application, (123, "a" * 32))
                return telegram_tdlib._bind(account, bot, expected_owner, nonce, included, excluded, selectors)

            with (patch("omni_service.onboarding.TelegramAPI", return_value=api),
                  patch("omni_service.onboarding.time.time", return_value=1000),
                  patch("omni_service.onboarding.LocalTranslation"),
                  patch("omni_service.onboarding._feishu_owner", return_value=("ou_fixture", "tenant_1")) as owner,
                  patch("omni_service.onboarding._prepare_feishu") as reuse,
                  patch("omni_service.onboarding.connect_account", side_effect=connect) as binding,
                  patch("omni_service.onboarding.getpass.getpass",
                        side_effect=AssertionError("preconfigured setup should not ask for secrets")),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(onboarding.main(args), 1)
                self.assertEqual(json.loads(config.read_text())["mode"], "disabled")
                api.peer = 10
                self.assertEqual(onboarding.main(args), 0)
                owner.assert_called_once()
                reuse.assert_called_once()
                binding.assert_called_once()
            telegram = json.loads(config.read_text())["telegram"]
            self.assertEqual(telegram["expected_owner_id"], 10)
            self.assertEqual(telegram["allowed_chat_ids"], [30])
            self.assertEqual(telegram["connection_id"], "conn")
            self.assertNotIn("setup_pairing", telegram)
            self.assertNotIn("a" * 32, credentials.read_text() + config.read_text())
            self.assertEqual(account.connection["recipients"]["chat_ids"], [30])
            self.assertEqual(account.connection["recipients"]["select_new_chats"], False)
            self.assertEqual(account.sent[-1]["chat_id"], 88)

    def test_wrong_account_or_existing_other_bot_cannot_be_changed(self):
        existing = {"@type": "businessConnectedBot", "bot_user_id": 99}
        variants = [(AccountClient(owner={**OWNER, "id": 20}), "10", "telegram_setup_wrong_owner"),
                    (AccountClient(existing=existing), "10", "telegram_setup_other_bot_connected")]
        for account, owner, error in variants:
            original = account.connection
            with self.assertRaisesRegex(ChannelError, error):
                telegram_tdlib._bind(account, BOT, owner, "challenge", (), (), ("new",))
            self.assertIs(account.connection, original)
            self.assertEqual(account.sent, [])

    def test_scope_readback_rejects_extra_rights_and_honors_exclusions(self):
        account = AccountClient(unwanted_rights=True)
        with self.assertRaisesRegex(ChannelError, "telegram_setup_binding_unverified"):
            telegram_tdlib._bind(account, BOT, "10", "challenge", (), (20,), ("new",))
        self.assertEqual(account.sent, [])
        self.assertEqual(account.connection["recipients"]["excluded_chat_ids"], [20])

    def test_qr_login_uses_ephemeral_database_and_waits_for_authorization(self):
        class LoginClient:
            commands = []

            def call(self, method, **args):
                self.commands.append((method, args))
                return {"@type": "authorizationStateWaitTdlibParameters"}

            def next_state(self, previous, **args):
                return next(self.states)

        td = LoginClient()
        td.states = iter([{"@type": "authorizationStateWaitPhoneNumber"},
                          {"@type": "authorizationStateWaitOtherDeviceConfirmation",
                           "link": "tg://login?token=fixture"},
                          {"@type": "authorizationStateReady"}])
        links = []
        with patch("omni_service.telegram_tdlib.private_input",
                   side_effect=AssertionError("operator credentials must not trigger prompts")):
            telegram_tdlib._sign_in(td, "/private/fixture", login="qr", show_qr=links.append,
                                    proxy=("127.0.0.1", 8899), application=(123, "a" * 32))
        self.assertEqual([key for key, _ in td.commands], [
            "getAuthorizationState", "setTdlibParameters", "addProxy", "requestQrCodeAuthentication"])
        setting = td.commands[1][1]
        self.assertEqual(setting["database_directory"], "/private/fixture")
        self.assertFalse(setting["use_message_database"])
        self.assertTrue(setting["database_encryption_key"])
        self.assertEqual(links, ["tg://login?token=fixture"])


class FeishuRegistrationCommand(unittest.TestCase):
    def test_register_and_retry_without_creating_another_app(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "personal.json"
            config.write_bytes((root / "config" / "personal.example.json").read_bytes())
            secrets = Path(tmp) / "credentials.env"
            secrets.touch(mode=0o600)
            using = ["register-feishu", "--config", str(config), "--secrets-file", str(secrets),
                     "--feishu-app-name", "OmniService 物流"]
            output = io.StringIO()
            with (patch("omni_service.onboarding.provision_feishu",
                        return_value=("cli_fixture", "fixture-secret")) as register,
                  contextlib.redirect_stdout(output)):
                self.assertEqual(onboarding.main(using), 0)
                self.assertEqual(onboarding.main(using), 0)
            register.assert_called_once()
            self.assertEqual(register.call_args.kwargs["app_name"], "OmniService 物流")
            actual = json.loads(config.read_text())
            self.assertEqual(actual["mode"], "disabled")
            self.assertEqual(actual["feishu"]["app_id"], "cli_fixture")
            self.assertEqual(onboarding.read_credentials(secrets)["OMNI_FEISHU_APP_SECRET"], "fixture-secret")
            self.assertNotIn("fixture-secret", output.getvalue() + config.read_text())
