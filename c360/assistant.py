"""Ask C360 - a plain-English answer to an RM's question about one customer, written
only from that customer's Customer 360 record.

What goes to the model is what the RM can already see on the page: the header fields
in ``_CUSTOMER_FIELDS`` and the insight blocks (products, loans, repayments, digital
loans, facilities, activity, profile, revenue, cash flow). Contact details, ID
numbers, address and date of birth are never sent. The model is told to answer from
that record alone, to say plainly when the record does not hold the answer, and to
name the section each figure comes from, so an RM can check it on the screen.

Off unless ``C360_ASSISTANT_ENABLED`` is set: the record leaves the bank for
Anthropic's API, so switching it on is IT Risk's decision. The SDK reads its key
from ``ANTHROPIC_API_KEY`` in the server env file.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger('c360')

MAX_QUESTION_CHARS = 500

#: Header fields sent with the record. An allowlist: a new field on the customer
#: record is not sent until it is added here.
_CUSTOMER_FIELDS = ('cust_id', 'name', 'segment', 'branch', 'rm_name', 'relationship_since',
                    'risk_class', 'crb_status', 'kyc_status', 'active')
_BIO_FIELDS = ('customer_type', 'account_open_date')
_BLOCKS = ('products', 'loans', 'repayments', 'digital_loans', 'facilities', 'activity',
           'profile', 'revenue', 'cashflow')

SYSTEM = """You answer a relationship manager's question about one bank customer at HFCB (a Kenyan bank), using only the customer record in the message.

Rules:
- Use only figures and facts that are in the record. Never estimate, assume or fill a gap from general knowledge. If the record does not hold the answer, say so in one sentence and name what is missing.
- Each block in the record has a status: "live" means read today, "none" means the customer has nothing there, "unavailable" means it could not be read. Never treat "unavailable" as nothing.
- Amounts are in Kenya shillings; write them as KES 1,234,567. Dates as 29 Oct 2026.
- After each figure, name the section it comes from in brackets, e.g. (Repayment track record) or (Revenue), so the manager can check it on the screen. Section names: Products, Loans in detail, Repayment track record, Digital loans, Facilities, Activity, Profile, Revenue, Cash flow.
- Mobile/Whizz loan limits are not in the record; if asked about a limit, say so.
- Answer in plain English, short paragraphs or a short list, under 200 words. No investment or credit advice beyond what the record shows."""


def enabled() -> bool:
    return bool(getattr(settings, 'C360_ASSISTANT_ENABLED', False))


def record(customer: dict[str, Any], insights: dict[str, Any]) -> dict[str, Any]:
    """The customer record sent with the question (allowlisted, see module doc)."""
    bio = customer.get('bio') or {}
    head = {k: customer.get(k) for k in _CUSTOMER_FIELDS if customer.get(k) not in (None, '')}
    head.update({k: bio.get(k) for k in _BIO_FIELDS if bio.get(k) not in (None, '')})
    return {'customer': head, 'as_of': insights.get('as_of'),
            **{b: insights.get(b) for b in _BLOCKS if b in insights}}


def ask(customer: dict[str, Any], insights: dict[str, Any], question: str) -> dict[str, Any]:
    """{'answer', 'model'} or {'error'}. Never raises."""
    question = ' '.join(str(question or '').split())[:MAX_QUESTION_CHARS]
    if not question:
        return {'error': 'Type a question about this customer.'}
    import anthropic                                         # only needed when switched on

    payload = json.dumps(record(customer, insights), default=str, sort_keys=True, separators=(',', ':'))
    try:
        client = anthropic.Anthropic(timeout=90.0)
        resp = client.beta.messages.create(
            model=getattr(settings, 'C360_ASSISTANT_MODEL', 'claude-opus-5-5'),
            max_tokens=16000,
            system=SYSTEM,
            output_config={'effort': 'medium'},
            betas=['server-side-fallback-2026-07-01'],
            fallbacks='default',
            messages=[{'role': 'user', 'content': f'<customer_record>\n{payload}\n</customer_record>\n\nQuestion: {question}'}],
        )
    except anthropic.RateLimitError:
        return {'error': 'The assistant is busy. Try again in a minute.'}
    except anthropic.APIStatusError as exc:
        logger.warning('assistant API error %s (request %s)', exc.status_code,
                       getattr(exc, 'request_id', None))
        return {'error': 'The assistant could not answer just now.'}
    except anthropic.APIConnectionError:
        return {'error': 'The assistant could not be reached from the server.'}
    if resp.stop_reason == 'refusal':
        return {'error': 'The assistant declined this question. Rephrase it about the customer record.'}
    text = '\n'.join(b.text for b in resp.content if b.type == 'text').strip()
    if not text:
        return {'error': 'The assistant returned no answer. Try again.'}
    return {'answer': text, 'model': resp.model, 'truncated': resp.stop_reason == 'max_tokens'}
