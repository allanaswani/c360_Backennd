"""Every brand name this service puts in front of a user, in one place.

The group has rebranded, and a superseded entity name on a screen or in an emailed
report is a regulatory exposure rather than a cosmetic one. Before this module the
old names were typed literally into payload labels, error messages, report subjects
and email bodies — which is the shape of change that gets ninety percent applied and
then ships the old name in a PDF footer nobody re-read.

So a rename is an edit to this file, in both repos (the frontend has the mirror of
it at ``frontend/src/lib/brand.ts``), and it lands everywhere at once.

What is deliberately NOT here, and must not be changed with the brand:

* ``delta.gold_db.hfdi_client_data`` and every other warehouse table name.
* The ``hfcb`` domain key in the API path and payloads, and the ``HFCBDomain`` type.
* The ``the property register-`` customer-id prefix (``c360.property_register.PREFIX``) — it is in URLs, in the
  audit trail and in logged recommendation outcomes going back months.
* The ``hfdi_admin`` role name, which arrives in portfolio-issued JWT claims.
* ``RecommendationFeedback.domain``, whose stored values are historical records of
  what was pitched under the name in force at the time.

Those are contracts with the warehouse, the portfolio and our own history. Renaming
them would break the wire format to rename something no user ever sees, and the two
kinds of name drift apart anyway.

``DOMAIN_LABELS`` is the one place the two meet: the by-domain rows are labelled
with display names that the frontend also matches on, so both sides read them from
their brand module rather than from a literal that silently stops matching.
"""
from __future__ import annotations

#: The banking entity - core banking.
BANK = 'HFCB'

#: The property arm, whose client register the property-clients page lists.
PROPERTY = 'HFCB Properties'

#: The bancassurance arm behind the insurance CRM panel.
INSURANCE = 'HFBI'

#: The parent group.
GROUP = 'HF Group'

#: The digital / mobile product.
DIGITAL = 'Whizz'

#: Display labels for the cross-domain value rows. The frontend's colour map and
#: domain lookups key off these exact strings (see frontend/src/lib/brand.ts,
#: DOMAIN_KEY), so the two modules must be renamed together.
DOMAIN_LABELS = {
    'bank': BANK,
    'digital': DIGITAL,
    'property': 'Properties',
    'insurance': 'Bancassurance',
}

#: How a property client's segment reads on screen. Built from PROPERTY so the
#: entity name and the segment label can never disagree.
PROPERTY_CLIENT_SEGMENT = f'{PROPERTY} client'

#: Sender name on emailed reports and alerts.
REPORT_SENDER_NAME = f'{BANK} Customer 360 Reports'
