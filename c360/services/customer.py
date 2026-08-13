"""Customer-level services — the header (Contract A) and cross-domain value summary.

This is where each field is stamped with its provenance. Identity and current
snapshot values are LIVE; risk / CRB / KYC / relationship-since are TO_SOURCE and
returned with an explicit "not yet sourced" status so the UI badges them honestly
instead of rendering a bare ``--``.
"""
from __future__ import annotations

from typing import Any

from ..warehouse.gateway import WarehouseGateway
from ..warehouse.provenance import Provenance, derived, live, to_source


# Bio fields carried as ISO dates so the UI can format them; the rest are strings.
_BIO_DATE_FIELDS = {'date_of_birth', 'account_open_date'}


def _build_bio(bio: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in bio.items():
        unit = 'date' if key in _BIO_DATE_FIELDS else None
        out[key] = (live(val, unit=unit) if unit else live(val)).to_dict()
    return out


def build_customer_header(gateway: WarehouseGateway, cust_id: str) -> dict[str, Any] | None:
    c = gateway.get_customer(cust_id)
    if not c:
        return None

    # Risk & KYC are DERIVED from live data (identity completeness + loan
    # performance), not read from a dedicated feed — so they're real and shown,
    # badged 'derived' with the basis in the note. CRB genuinely needs an external
    # bureau feed we don't have, so it stays honestly 'not sourced'.
    try:
        profile = gateway.get_risk_profile(cust_id)
    except Exception:
        profile = None
    if profile:
        risk_metric = derived(profile['risk']['class'], note=profile['risk']['note'] + ' Factors: '
                              + '; '.join(profile['risk']['factors']) + '.').to_dict()
        kyc_metric = derived(profile['kyc']['status'], note=profile['kyc']['note']).to_dict()
        risk_metric['detail'] = profile['risk']['factors']
        kyc_metric['detail'] = profile['kyc']['checks']
    else:
        risk_metric = to_source(note='Risk profile unavailable for this customer.').to_dict()
        kyc_metric = to_source(note='KYC profile unavailable for this customer.').to_dict()

    return {
        'cust_id': c['cust_id'],
        'identity': {
            'name': live(c['name']).to_dict(),
            'segment': live(c['segment']).to_dict(),
            'branch': live(c['branch']).to_dict(),
            'rm_name': live(c.get('rm_name')).to_dict(),
            'sales_code': live(c.get('sales_code')).to_dict(),
            'mobile': live(c.get('mobile')).to_dict(),
            'email': live(c.get('email')).to_dict(),
            'id_no': live(c.get('id_no')).to_dict(),
            'active': live(c.get('active', True)).to_dict(),
        },
        # Bio & identification (backlog item #1) — DOB, ID details and account
        # details, sourced live from dim_customer. Personal fields (DOB / gender /
        # birthplace) are individual-only, so an organisation carries them as null
        # and the UI simply omits them (a legitimate N/A, not a silent gap).
        'bio': _build_bio(c.get('bio') or {}),
        'risk': {
            'risk_class': risk_metric,
            'crb_status': to_source(note='CRB score needs an external credit-bureau feed — not derivable from held data.').to_dict(),
            'kyc_status': kyc_metric,
            # Sourced live from dim_customer.account_opening_date; unsourced in mock.
            'relationship_since': (
                live(c['relationship_since'], unit='date')
                if c.get('relationship_since')
                else to_source(unit='date', note='Relationship-since not yet sourced.')
            ).to_dict(),
        },
    }


def build_value_summary(gateway: WarehouseGateway, cust_id: str) -> dict[str, Any]:
    """Cross-domain value summary. Core banking is LIVE; the other three domains
    are declared but PREVIEW/TO_SOURCE until their pipelines land, so the donut
    never shows a phantom slice as if it were real."""
    v = gateway.get_relationship_value(cust_id)
    return {
        'headline': {
            'relationship_value': live(v['relationship_value'], unit='KES').to_dict(),
            'deposits': live(v['deposits'], unit='KES').to_dict(),
            'loans': live(v['loans'], unit='KES').to_dict(),
            'revenue': live(v['revenue'], unit='KES').to_dict(),
        },
        # value-by-domain: HFCB is real; others are placeholders flagged as such.
        'by_domain': [
            {'domain': 'HFCB', 'value': v['relationship_value'], 'status': Provenance.LIVE.value},
            {'domain': 'Whizz', 'value': None, 'status': Provenance.TO_SOURCE.value,
             'note': 'Whizz value pending pipeline from the Kocela MySQL estate.'},
            {'domain': 'Properties', 'value': None, 'status': Provenance.TO_SOURCE.value,
             'note': 'Properties value pending HFDI CRM integration.'},
            {'domain': 'Bancassurance', 'value': None, 'status': Provenance.TO_SOURCE.value,
             'note': 'Customer-level bancassurance value is a known gap (§7A.5).'},
        ],
    }
