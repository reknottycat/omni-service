"""Test employee selection through Feishu login, invitation, and real queue boundaries."""

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from omni_service.admin_auth import FeishuAdminAuth
from omni_service.admin_web import create_admin_server
from omni_service.channels import parse_events
from omni_service.intake import Intake
from omni_service.service import Service
from omni_service.simulation import FakePlatforms, customer_payload
from omni_service.transport import HttpTransport


ADMIN = 'ou_owner'
EMPLOYEE = 'ou_hired'
OTHER = 'ou_other'
ORIGIN = 'https://ops.example'


def feishu_identity_fixture(method, _url, body, headers):
    if method == 'POST':
        return {'code': 0, 'access_token': 'fixture-' + body['code']}
    open_id = headers['Authorization'].removeprefix('Bearer fixture-')
    return {'code': 0, 'data': {'tenant_key': 'fixture-team', 'open_id': open_id,
                                'name': '飞书员工\u202e' if open_id == EMPLOYEE else '租户账号'}}


class StaffInvites(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(tmp) / 'invite.sqlite3'
        self.platforms = self.enterContext(FakePlatforms())
        self.now = 2_000_000_000
        self.service = self.open_service()
        self.deliveries = []
        self.addCleanup(self.service.close)
        self.auth = FeishuAdminAuth(app_id='cli_fixture', app_secret='fixture-secret', tenant_key='fixture-team',
                                    callback_url=ORIGIN + '/admin/auth/callback', signing_key='A' * 32,
                                    request_json=feishu_identity_fixture, clock=lambda: self.now)
        self.server = create_admin_server(self.service, self.auth, templates=None, invite_delivery=self.deliver_invite)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def open_service(self):
        service = Service(self.path, HttpTransport(self.platforms.urls), clock=lambda: self.now,
                          members=(ADMIN,), intake=Intake(lambda _text, _fields: {}, simulated=True),
                          approval_intake=True, approval_cards=True)
        service.configure_management([ADMIN])
        return service

    def deliver_invite(self, target, url, operation_id):
        self.deliveries.append((target, url, operation_id))
        return 'om_confirmed'

    def test_admin_direct_invite_uses_only_explicit_target_and_server_origin(self):
        request = {'expected_user_id': EMPLOYEE, 'send': True}
        self.assertEqual(self.request('staff/invite', request, actor=EMPLOYEE)[0], 403)
        self.assertEqual(self.request('staff/invite', {'send': True}, actor=ADMIN)[0], 400)
        self.assertEqual(self.deliveries, [])
        status, created = self.request('staff/invite', request, actor=ADMIN)
        self.assertEqual((status, created['delivery']), (200, 'sent'))
        self.assertEqual(self.deliveries, [(EMPLOYEE, ORIGIN + '/admin/#invite=' + created['token'],
                                            created['invite']['id'])])

    def signed_session(self, actor):
        consent, state_header = self.auth.begin()
        state = parse_qs(urlsplit(consent).query)['state'][0]
        state_cookie = SimpleCookie(state_header)[self.auth.state_cookie_name].value
        signed, _clear = self.auth.finish(code=actor, state=state, state_cookie=state_cookie)
        signed_cookie = SimpleCookie(signed)[self.auth.session_cookie_name].value
        return signed.split(';', 1)[0], self.auth.read_session(signed_cookie).csrf_token

    def request(self, url, body=None, *, actor=None, origin=ORIGIN, csrf=None):
        headers = {'Origin': origin, 'Content-Type': 'application/json'}
        if actor:
            cookie, token = self.signed_session(actor)
            headers.update({'Cookie': cookie, 'X-Omni-CSRF': token if csrf is None else csrf})
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('GET' if body is None else 'POST', '/admin/api/' + url,
                               body=None if body is None else json.dumps(body), headers=headers)
            with connection.getresponse() as response:
                return response.status, json.load(response)
        finally:
            connection.close()

    def invite(self, expected=None):
        status, created = self.request('staff/invite', {'expected_user_id': expected}, actor=ADMIN)
        self.assertEqual(status, 200)
        return created

    def new_conversation(self, peer):
        self.now += 1
        incoming, = parse_events('telegram', 'fixture', customer_payload(
            'telegram', 'Здравствуйте!', customer=peer, sequence=self.now, timestamp=self.now, language_hint='ru'))
        cid = self.service.ingest(incoming)['conversation_id']
        self.service.drain()
        return cid

    def test_targeted_invite_requires_matching_authenticated_employee_and_csrf(self):
        self.assertEqual(self.request('session'), (200, {'authenticated': False}))
        code, session = self.request('session', actor=EMPLOYEE)
        self.assertEqual((code, session['user_id'], session['display_name']), (200, EMPLOYEE, '飞书员工'))
        self.assertEqual(self.request('state', actor=EMPLOYEE)[0], 403)
        self.assertEqual(self.request('staff/invite', {}, actor=EMPLOYEE)[0], 403)
        created = self.invite(EMPLOYEE)
        body = {'token': created['token'], 'user_id': EMPLOYEE}
        self.assertEqual(self.request('staff/claim', body)[0], 401)
        self.assertEqual(self.request('staff/claim', body, actor=OTHER)[0], 403)
        self.assertEqual(self.request('staff/claim', body, actor=EMPLOYEE, origin='https://other.example')[0], 403)
        self.assertEqual(self.request('staff/claim', body, actor=EMPLOYEE, csrf='wrong')[0], 403)
        code, done = self.request('staff/claim', body, actor=EMPLOYEE)
        self.assertEqual((code, done['status'], done['invite']['user_id']), (200, 'approved', EMPLOYEE))
        self.assertEqual(self.request('staff/claim', body, actor=EMPLOYEE), (200, done))
        self.assertEqual(self.request('staff/claim', body, actor=OTHER)[0], 410)
        self.assertEqual(self.request('state', actor=EMPLOYEE)[1]['role'], 'staff')
        self.assertNotIn('staff_invites', self.request('state', actor=EMPLOYEE)[1])
        state = self.request('state', actor=ADMIN)[1]
        self.assertEqual(state['staff_ids'], [ADMIN, EMPLOYEE])
        self.assertEqual(state['staff_invites'], [done['invite']])
        with self.service.store.lock:
            db = self.service.store.db
            stored = db.execute('SELECT token_hash FROM staff_invites').fetchone()[0]
            self.assertEqual(stored, hashlib.sha256(created['token'].encode()).hexdigest())
            self.assertFalse(any(created['token'] in row['detail'] for row in db.execute('SELECT detail FROM audit')))
        self.assertNotIn(stored, json.dumps(state))
        self.assertNotIn(created['token'], json.dumps(state))

    def test_unbound_claim_requires_admin_and_only_changes_new_groups(self):
        old = self.new_conversation('101')
        created = self.invite()
        body = {'token': created['token']}
        code, pending = self.request('staff/claim', body, actor=EMPLOYEE)
        self.assertEqual((code, pending['status']), (200, 'pending'))
        self.assertEqual(self.request('state', actor=EMPLOYEE)[0], 403)
        self.assertEqual(self.request('state', actor=ADMIN)[1]['staff_ids'], [ADMIN])
        decision = {'invite_id': pending['invite']['id']}
        self.assertEqual(self.request('staff/approve', decision, actor=EMPLOYEE)[0], 403)
        reopened = self.open_service()
        try:
            self.assertEqual(reopened.admin_state(ADMIN)['staff_invites'], [pending['invite']])
        finally:
            reopened.close()
        code, result = self.request('staff/approve', decision, actor=ADMIN)
        self.assertEqual((code, result['status']), (200, 'approved'))
        self.assertEqual(self.request('staff/approve', decision, actor=ADMIN), (200, result))
        self.assertEqual(self.request('defaults', {'handoff_staff_id': EMPLOYEE}, actor=ADMIN)[0], 200)
        new = self.new_conversation('102')
        state = self.request('state', actor=ADMIN)[1]
        rooms = {room['id']: room for room in state['conversations']}
        self.assertEqual(self.platforms.rooms[rooms[old]['room_id']]['members'], [ADMIN])
        self.assertCountEqual(self.platforms.rooms[rooms[new]['room_id']]['members'], [ADMIN, EMPLOYEE])
        _, employee = self.request('state', actor=EMPLOYEE)
        self.assertEqual([room['id'] for room in employee['conversations']], [new])
        self.assertEqual(rooms[old]['handoff_staff_id'], ADMIN)
        self.assertEqual(rooms[new]['handoff_staff_id'], EMPLOYEE)

    def test_concurrent_unbound_claims_cannot_grant_access_and_revocation_denies_retry(self):
        created = self.invite()
        body = {'token': created['token']}
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda actor: self.request('staff/claim', body, actor=actor), (EMPLOYEE, OTHER)))
        self.assertEqual(sorted(code for code, _ in results), [200, 410])
        decision = {'invite_id': created['invite']['id']}
        self.assertEqual(self.request('staff/revoke', decision, actor=ADMIN)[1]['status'], 'revoked')
        self.assertEqual(self.request('staff/approve', decision, actor=ADMIN)[0], 410)
        self.assertEqual(self.request('staff/claim', body, actor=EMPLOYEE)[0], 410)
        self.assertEqual(self.request('staff/claim', body, actor=OTHER)[0], 410)
        self.assertEqual(self.request('state', actor=ADMIN)[1]['staff_ids'], [ADMIN])

    def test_neither_expired_link_nor_expired_pending_claim_can_grant_access(self):
        opened = self.invite()
        pending = self.invite()
        self.request('staff/claim', {'token': pending['token']}, actor=EMPLOYEE)
        self.now = opened['invite']['expires_at']
        self.assertEqual(self.request('staff/claim', {'token': opened['token']}, actor=OTHER)[0], 410)
        decision = {'invite_id': pending['invite']['id']}
        self.assertEqual(self.request('staff/approve', decision, actor=ADMIN)[0], 410)
        state = self.request('state', actor=ADMIN)[1]
        self.assertEqual([row['status'] for row in state['staff_invites']], ['expired', 'expired'])
        self.assertEqual(state['staff_ids'], [ADMIN])


if __name__ == '__main__':
    unittest.main()
