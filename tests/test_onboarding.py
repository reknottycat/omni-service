import json
import unittest

from omni_service.onboarding import employee_setup_id, telegram_metadata
from omni_service.personal_simulation import connection_update, customer_update, staff_payload


class SetupDiagnostics(unittest.TestCase):
    def test_metadata_does_not_expose_message_body_or_ack_updates(self):
        class Client:
            def get_me(self):
                return {'id': 900, 'username': 'test', 'can_connect_to_business': True}
            def get_webhook_info(self):
                return {'url': ''}
            def get_updates(self, offset, timeout):
                self.args = (offset, timeout)
                return [connection_update(), customer_update(2, text='NEVER_PRINT_ME')]
        client = Client()
        first = telegram_metadata(client)
        self.assertEqual([], first['private_chat_ids'])
        second = telegram_metadata(client, owner_id=10)
        self.assertEqual([20], second['private_chat_ids'])
        self.assertNotIn('NEVER_PRINT_ME', json.dumps(second))
        self.assertEqual((0, 0), client.args)
        self.assertEqual(0, second['messages_sent'])

    def test_employee_must_voluntarily_send_exact_command_in_app_dm(self):
        event = staff_payload('oc_p2p', text='/omni setup')
        self.assertIsNone(employee_setup_id(event, 'cli_test'))  # Group is ignored.
        event['event']['message']['chat_type'] = 'p2p'
        self.assertEqual('ou_staff_1', employee_setup_id(event, 'cli_test'))
        self.assertIsNone(employee_setup_id(event, 'cli_other'))
        event['event']['message']['content'] = json.dumps({'text': 'private ordinary text'})
        self.assertIsNone(employee_setup_id(event, 'cli_test'))

    def test_setup_does_not_delete_existing_webhook(self):
        class Client:
            def get_me(self):
                return {'id': 900}
            def get_webhook_info(self):
                return {'url': 'https://existing.example/private'}
            def get_updates(self, *_args, **_kwargs):
                raise AssertionError('must not poll conflicting bot')
        with self.assertRaises(Exception) as caught:
            telegram_metadata(Client())
        self.assertEqual('webhook_conflict', caught.exception.code)
