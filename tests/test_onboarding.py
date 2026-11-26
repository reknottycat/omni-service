import getpass
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from omni_service.models import ChannelError
from omni_service.onboarding import _ask, _feishu_owner, main, paired_connection, read_credentials
from omni_service.personal import Connection
from omni_service.personal_runner import PersonalBridge
from omni_service.intake import Intake
from test_personal_managed import ManagedFeishu, ManagedTelegram


NONCE = "test_pair_nonce_xxxxxxxxxxxxxxxx"


def _start(actor=10, *, timestamp=1001, chat="private"):
    return {"message": {"from": {"id": actor, "is_bot": False},
                        "chat": {"id": actor, "type": chat}, "date": timestamp, "text": "/start " + NONCE}}


def _connection(owner=10, cid="conn", *, reply=True):
    return {"id": cid, "user": {"id": owner}, "is_enabled": True, "rights": {"can_reply": reply}}


class OnboardingContracts(unittest.TestCase):
    def test_callback_requires_fresh_private_proof_and_bound_live_connection(self):
        api = Mock()
        connection = {"business_connection": _connection()}
        wrong = {"business_connection": _connection(20, "foreign")}
        for invalid in ([], [_start(timestamp=999)], [_start(chat="group")], [_start(20)]):
            self.assertIsNone(paired_connection(api, [connection, *invalid], NONCE, 1000))
        api.call.assert_not_called()
        api.call.return_value = _connection()
        verified = paired_connection(api, [_start(), wrong, connection], NONCE, 1000)
        self.assertEqual((verified.owner_id, verified.connection_id), (10, "conn"))
        api.call.assert_called_once_with("getBusinessConnection", {"business_connection_id": "conn"})
        api.call.return_value = _connection(20)
        with self.assertRaisesRegex(ChannelError, "telegram_connection_not_ready"):
            paired_connection(api, [_start(), connection], NONCE, 1000)
        with self.assertRaisesRegex(ChannelError, "telegram_pairing_ambiguous"):
            paired_connection(api, [_start(), _start(20), connection, wrong], NONCE, 1000)

    def test_credentials_are_literal_and_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.env"
            path.write_text("TELEGRAM_BOT_TOKEN=abc:synthetic\nTRANSLATION_API_KEY=\n")
            path.chmod(0o600)
            self.assertEqual(read_credentials(path)["TELEGRAM_BOT_TOKEN"], "abc:synthetic")
            path.write_text("TELEGRAM_BOT_TOKEN=$(touch /tmp/should-never-run)\n")
            with self.assertRaisesRegex(ChannelError, "invalid_credentials_file"):
                read_credentials(path)
            path.chmod(0o644)
            with self.assertRaisesRegex(ChannelError, "unsafe_credentials_file"):
                read_credentials(path)
            symlink = Path(tmp) / "alias.env"
            symlink.symlink_to(path)
            with self.assertRaises(OSError):
                read_credentials(symlink)

    def test_secret_prompt_rejects_echo_fallback(self):
        with patch('omni_service.onboarding.getpass.getpass', side_effect=getpass.GetPassWarning):
            with self.assertRaisesRegex(ChannelError, 'secret_input_requires_terminal'):
                _ask({}, 'TELEGRAM_BOT_TOKEN', 'Telegram Bot Token: ')

    def test_feishu_profile_must_match_and_only_bootstrap_identity_is_retained(self):
        app = "cli_example"
        bot = {"appId": app, "brand": "feishu", "identity": "bot", "available": True}
        profile = {"ok": True, "identity": "user",
                   "data": {"open_id": "ou_admin", "tenant_key": "tenant_1", "mobile": "not-retained"}}
        with tempfile.TemporaryDirectory() as tmp:
            with patch("omni_service.onboarding._lark", side_effect=(None, bot, None, bot, None, profile, {})) as lark:
                self.assertEqual(_feishu_owner(app, "fixture-secret", Path(tmp)), ("ou_admin", "tenant_1"))
                self.assertIn("user_profile", lark.call_args_list[4].args[0])
                self.assertIn("logout", lark.call_args_list[-1].args[0])
                env = lark.call_args_list[0].args[1]
                self.assertEqual(Path(env["LARKSUITE_CLI_CONFIG_DIR"]), Path(tmp) / "lark")
                self.assertEqual(Path(env["LARKSUITE_CLI_DATA_DIR"]), Path(tmp) / "lark-data")
                login_env = lark.call_args_list[4].args[1]
                self.assertNotEqual(login_env["LARKSUITE_CLI_DATA_DIR"], env["LARKSUITE_CLI_DATA_DIR"])
                self.assertFalse(Path(login_env["LARKSUITE_CLI_DATA_DIR"]).exists())
            with patch("omni_service.onboarding._lark",
                       side_effect=(None, bot, None, bot, None, {**profile, "identity": "bot"}, {})) as lark:
                with self.assertRaisesRegex(ChannelError, "feishu_setup_identity_unverified"):
                    _feishu_owner(app, "fixture-secret", Path(tmp))
                self.assertIn("logout", lark.call_args_list[-1].args[0])

    def test_configure_official_receipts_replace_manual_identity_fields(self):
        example = Path(__file__).resolve().parents[1] / "config" / "personal.example.json"
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "personal.json"
            config.write_bytes(example.read_bytes())
            creds = Path(tmp) / "credentials.env"
            creds.touch(mode=0o600)
            args = ["configure", "--config", str(config), "--secrets-file", str(creds),
                    "--feishu-app", "cli_example", "--callback-url", "https://demo.example/admin/auth/callback",
                    "--model-url", "http://127.0.0.1:8085/v1", "--model", "demo",
                    "--chat-scope", "connection", "--port", "9090"]
            token = "123456:fixturexxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            bot = {"appId": "cli_example", "brand": "feishu", "identity": "bot", "available": True}
            user = {"ok": True, "identity": "user", "data": {"open_id": "ou_admin", "tenant_key": "tenant_1"}}
            with patch("omni_service.onboarding.getpass.getpass", side_effect=(token, "test-model-key", "fixture-secret")), \
                    patch("omni_service.onboarding._lark", side_effect=(None, bot, None, bot, None, user, {})) as lark, \
                    patch("omni_service.onboarding._telegram_pair",
                          return_value=({"username": "example_bot"}, Connection("conn", 10, True, True, False))), \
                    patch("omni_service.onboarding.LocalTranslation") as model:
                self.assertEqual(main(args), 0)
                model.return_value.translate.assert_called_once()
                for index in (0, 2):
                    init = lark.call_args_list[index].args[0]
                    self.assertEqual(init[init.index("--name") + 1], "cli_example")
                for index in (1, 3, 4, 5, 6):
                    self.assertEqual(lark.call_args_list[index].args[0][0], "--profile=cli_example")
            actual = json.loads(config.read_text())
            self.assertEqual(actual["mode"], "personal-managed")
            self.assertEqual(actual["management"]["admin_ids"], ["ou_admin"])
            self.assertEqual(actual["management"]["port"], 9090)
            self.assertEqual(actual["telegram"]["expected_owner_id"], 10)
            self.assertEqual(actual["telegram"]["allowed_chat_ids"], [])
            self.assertEqual(actual["telegram"]["peer_scope"], "connection")
            self.assertNotIn("test-model-key", config.read_text())
            self.assertEqual(read_credentials(creds)["TELEGRAM_BOT_TOKEN"], token)
            self.assertEqual(main(args), 1)  # Setup cannot rebind an enabled deployment.

    def test_prepare_feishu_keeps_configured_profile_on_migration(self):
        app_id, profile = "cli_example", "logistics"
        bot = {"appId": app_id, "brand": "feishu", "identity": "bot", "available": True}
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "personal.json"
            config.write_text(json.dumps({"feishu": {"app_id": app_id, "profile": profile}}))
            credentials = Path(tmp) / "credentials.env"
            credentials.write_text("OMNI_FEISHU_APP_SECRET=fixture-secret\n")
            credentials.chmod(0o600)
            args = ["prepare-feishu", "--config", str(config), "--secrets-file", str(credentials)]
            with patch("omni_service.onboarding._lark", side_effect=(None, bot)) as lark:
                self.assertEqual(main(args), 0)
                init = lark.call_args_list[0].args[0]
                self.assertEqual(init[init.index("--name") + 1], profile)
                self.assertEqual(lark.call_args_list[1].args[0], [f"--profile={profile}", "whoami", "--as", "bot"])


class ConnectionScopedBridge(unittest.TestCase):
    def test_only_the_bound_connection_can_establish_new_private_service_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed.sqlite"
            api, feishu = ManagedTelegram(), ManagedFeishu()
            options = dict(owner=10, peer_ids=[], connection_id="c1", bot_id=88, clock=lambda: 1000,
                           intake=Intake(lambda _text, _fields: {}, simulated=True), intake_enabled=True,
                           approval_intake=True)
            service = PersonalBridge(path, api, feishu, **options, connection_scope=True)
            try:
                api.update(1, "Здравствуйте", chat=30, sender=30)
                api.update(2, "Здравствуйте", chat=40, sender=40)
                api.updates[-1]["business_message"]["business_connection_id"] = "foreign"
                api.update(3, "Здравствуйте", chat=50, sender=50)
                api.updates[-1]["business_message"]["chat"]["type"] = "group"
                for event in service.telegram.poll(timeout=0):
                    service.receive_telegram(event)
                service.telegram.ack()
                service.drain()
                self.assertEqual({row["chat_id"] for row in service.list_conversations()}, {"30"})
            finally:
                service.close()
            service = PersonalBridge(path, api, feishu, **options, connection_scope=True)
            service.close()
            with self.assertRaisesRegex(ChannelError, "personal_db_binding_mismatch"):
                PersonalBridge(path, api, feishu, **{**options, "peer_ids": [30]})

    def test_round_robin_persists_across_restart_without_reassigning_existing_customer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rotation.sqlite'
            api, feishu = ManagedTelegram(), ManagedFeishu()
            options = dict(owner=10, peer_ids=[], connection_id='c1', bot_id=88, clock=lambda: 1000,
                           intake=Intake(lambda _text, _fields: {}, simulated=True),
                           intake_enabled=True, approval_intake=True, connection_scope=True)

            def receive(service, number, customer):
                api.update(number, 'Здравствуйте', chat=customer, sender=customer)
                for event in service.telegram.poll(timeout=0):
                    service.receive_telegram(event)
                service.telegram.ack()

            bridge = PersonalBridge(path, api, feishu, **options)
            try:
                bridge.configure_management(['ou_allowed'])
                bridge.admin_update_defaults('ou_allowed', {'staff_ids': ['ou_allowed', 'ou_other'],
                                                             'handoff_strategy': 'round_robin'})
                receive(bridge, 1, 20)
            finally:
                bridge.close()
            bridge = PersonalBridge(path, api, feishu, **options)
            try:
                receive(bridge, 2, 20)
                receive(bridge, 3, 30)
                receive(bridge, 4, 40)
                rooms = {item['id']: item['handoff_staff_id']
                         for item in bridge.admin_state('ou_allowed')['conversations']}
                assigned = {item['chat_id']: rooms[item['id']] for item in bridge.list_conversations()}
                self.assertEqual(assigned, {'20': 'ou_allowed', '30': 'ou_other', '40': 'ou_allowed'})
            finally:
                bridge.close()
