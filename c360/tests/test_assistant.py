"""Ask C360 (c360/assistant.py): what is sent, what comes back, and that it stays off
until switched on. The Anthropic client is mocked - no test calls the API."""
import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from c360 import assistant

CUSTOMER = {'cust_id': '37565', 'name': 'A CUSTOMER', 'segment': 'PRIVATE', 'branch': 'REHANI BRANCH',
            'rm_name': 'An RM', 'mobile': '0700000000', 'email': 'x@example.com', 'id_no': '12345678',
            'bio': {'customer_type': 'Individual', 'date_of_birth': '1980-01-01', 'address': 'Somewhere',
                    'alt_phone': '0711111111', 'id_no': '12345678'}}
INSIGHTS = {'as_of': '2026-10-30', 'revenue': {'status': 'live', 'data': {'totals': {'net': 1544460}}},
            'products': {'status': 'unavailable', 'data': None}}


def _resp(text='Net revenue is KES 1,544,460 (Revenue).', stop='end_turn'):
    return SimpleNamespace(stop_reason=stop, model='claude-opus-5-5',
                           content=[SimpleNamespace(type='thinking', thinking=''),
                                    SimpleNamespace(type='text', text=text)])


class RecordTests(SimpleTestCase):
    def test_contact_and_id_details_are_never_sent(self):
        sent = json.dumps(assistant.record(CUSTOMER, INSIGHTS))
        for secret in ('0700000000', 'x@example.com', '12345678', '1980-01-01', 'Somewhere', '0711111111'):
            self.assertNotIn(secret, sent)
        self.assertIn('REHANI BRANCH', sent)
        self.assertIn('1544460', sent)
        self.assertIn('unavailable', sent)          # status travels, so "could not load" != "none"


@override_settings(C360_ASSISTANT_ENABLED=True)
class AskTests(SimpleTestCase):
    def _ask(self, resp, question='How much has the bank earned?'):
        with mock.patch('anthropic.Anthropic') as cls:
            cls.return_value.beta.messages.create.return_value = resp
            out = assistant.ask(CUSTOMER, INSIGHTS, question)
            return out, cls.return_value.beta.messages.create.call_args

    def test_answer_and_request_shape(self):
        out, call = self._ask(_resp())
        self.assertEqual(out['answer'], 'Net revenue is KES 1,544,460 (Revenue).')
        kw = call.kwargs
        self.assertEqual((kw['model'], kw['fallbacks'], kw['betas']),
                         ('claude-opus-5-5', 'default', ['server-side-fallback-2026-07-01']))
        self.assertNotIn('0700000000', kw['messages'][0]['content'])

    def test_refusal_and_empty_question(self):
        out, _ = self._ask(_resp(text='', stop='refusal'))
        self.assertIn('declined', out['error'])
        self.assertIn('Type a question', assistant.ask(CUSTOMER, INSIGHTS, '   ')['error'])


class SwitchTests(SimpleTestCase):
    def test_off_by_default(self):
        self.assertFalse(assistant.enabled())
        client = APIClient()
        self.assertEqual(client.get('/api/customers/37565/ask/').json(), {'enabled': False})
        r = client.post('/api/customers/37565/ask/', {'question': 'hi'}, format='json').json()
        self.assertFalse(r['enabled'])
