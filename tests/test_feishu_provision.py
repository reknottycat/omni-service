"""Feishu one-scan provisioning stays within the app's required capabilities."""

import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from omni_service.feishu_provision import provision_feishu
from omni_service.models import ChannelError


VERIFY = 'https://accounts.feishu.cn/oauth/v1/app/registration?device=opaque%2Ffixture'


class FeishuProvision(unittest.TestCase):
    def sdk(self, register_app):
        async def register(**options):
            return register_app(**options)

        return patch.dict(sys.modules, {'lark_oapi': SimpleNamespace(aregister_app=register)})

    def test_scan_uses_vendor_link_and_only_bridge_capabilities(self):
        calls, displayed = [], []

        def register(**options):
            calls.append(options)
            options['on_qr_code']({'url': VERIFY, 'expire_in': 600})
            return {'client_id': 'cli_fixture', 'client_secret': 'fixture-secret'}

        with self.sdk(register):
            result = provision_feishu(show_verification_url=displayed.append)
        self.assertEqual(result, ('cli_fixture', 'fixture-secret'))
        self.assertEqual(displayed, [VERIFY])
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertTrue(request['create_only'])
        self.assertEqual(request['app_preset']['name'], 'OmniService')
        addons = request['addons']
        self.assertFalse(addons['preset'])
        self.assertEqual(set(addons['scopes']), {'tenant'})
        self.assertCountEqual(addons['scopes']['tenant'], [
            'im:chat:create', 'im:chat:read', 'im:chat.members:read', 'im:message.group_msg',
            'im:message:readonly', 'im:message:send_as_bot', 'im:resource',
        ])
        self.assertEqual(addons['events'], {'items': {'tenant': ['im.message.receive_v1']}})
        self.assertEqual(addons['callbacks'], {'items': ['card.action.trigger']})

    def test_non_vendor_and_malformed_links_never_reach_the_owner(self):
        shown = []
        for url in ('http://accounts.feishu.cn/verify', 'https://accounts.feishu.cn@other.example/verify',
                    'https://other.example/verify', 'https://accounts.feishu.cn/verify\n', None):
            with self.subTest(url=url), self.sdk(lambda **request: request['on_qr_code']({'url': url})):
                with self.assertRaisesRegex(ChannelError, '^feishu_provision_invalid_verification$'):
                    provision_feishu(show_verification_url=shown.append)
        self.assertEqual(shown, [])

    def test_no_sdk_and_provider_failure_report_stable_codes(self):
        with patch.dict(sys.modules, {'lark_oapi': None}):
            with self.assertRaisesRegex(ChannelError, '^feishu_provision_sdk_missing$'):
                provision_feishu(show_verification_url=lambda _: None)

        def failed(**_kwargs):
            raise RuntimeError('device and secret must stay private')

        with self.sdk(failed), self.assertRaisesRegex(ChannelError, '^feishu_provision_unavailable$'):
            provision_feishu(show_verification_url=lambda _: None)
        with self.sdk(lambda **_kwargs: {'client_id': 'wrong', 'client_secret': 'fixture'}):
            with self.assertRaisesRegex(ChannelError, '^feishu_provision_invalid_credentials$'):
                provision_feishu(show_verification_url=lambda _: None)


if __name__ == '__main__':
    unittest.main()
