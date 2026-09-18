"""Customer-level services — the header (Contract A) and cross-domain value summary.

This is where each field is stamped with its provenance. Identity and current
snapshot values are LIVE; risk / CRB / KYC / relationship-since are TO_SOURCE and
returned with an explicit "not yet sourced" status so the UI badges them honestly
instead of rendering a bare ``--``.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .. import property_register as prop_reg
from .. import brand
from ..warehouse.gateway import WarehouseGateway
from ..warehouse.provenance import Provenance, derived, live, to_source


# Bio fields carried as ISO dates so the UI can format them; the rest are strings.
_BIO_DATE_FIELDS = {'date_of_birth', 'account_open_date'}


def _kes_short(n: float | int | None) -> str:
    """Human KES with a K/M/B/T rollup, mirroring the frontend `kes()`—for the
    server-authored relationship summary sentence."""
    v = float(n or 0)
    a = abs(v)
    if a >= 1e12:
        body = f'{v / 1e12:.1f}T'
    elif a >= 1e9:
        body = f'{v / 1e9:.1f}B'
    elif a >= 1e6:
        body = f'{v / 1e6:.1f}M'
    elif a >= 1e3:
        body = f'{v / 1e3:.0f}K'
    else:
        body = f'{v:.0f}'
    return f'KES {body}'


def _seg_human(seg: str | None) -> str:
    s = (seg or 'Unsegmented').strip()
    return s.title() if s.isupper() else s


def _join_human(items: list[str]) -> str:
    """'A', 'A and B', 'A, B and C' — a list a person would read aloud."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ''
    return ', '.join(items[:-1]) + ' and ' + items[-1]


def relationship_summary(header: dict[str, Any], value: dict[str, Any]) -> str:
    """A single plain-language sentence an RM can read at a glance — 'who is this
    customer', composed from the same facts already on the page (no new query, no
    invented number). Every clause is dropped when its source is missing rather than
    printed as a bare gap, keeping the never-a-silent-dash rule.

    e.g. 'Mass customer of 8 years with the bank. Holds KES 2.1M in deposits against
    KES 1.3M in loans — KES 0.8M net. Low risk.'"""
    # An property client has no bank relationship for this sentence to describe.
    # The generic template produces 'property client customer.' - true of nobody
    # and useful to no one - so these get a sentence about what they DO hold.
    ins = header.get('insurance_client')
    if ins:
        if ins.get('bank_cust_id'):
            lead = 'On the insurance register, and a bank customer in their own right.'
        elif ins.get('receipts_only'):
            # No client record at all - only the premiums they have paid.
            n = ins.get('receipts') or 0
            return (f'Known only from {n} premium '
                    f'{"receipt" if n == 1 else "receipts"} in the insurance system. '
                    f'No client record on the register and no account with the bank.')
        else:
            lead = 'On the insurance register only, with no account here.'
        total = ins.get('policies') or 0
        if total:
            active = ins.get('active_policies') or 0
            state = f'{active} active' if active else 'none currently active'
            return (f'{lead} Holds {total} {"policy" if total == 1 else "policies"} '
                    f'({state}).')
        n = ins.get('receipts') or 0
        if n:
            return (f'{lead} No policy on file, but {n} premium '
                    f'{"receipt" if n == 1 else "receipts"} are recorded.')
        return f'{lead} No policy on file.'

    h = header.get('property_client')
    if h:
        if h.get('bank_cust_id'):
            lead = (f'On the {brand.PROPERTY} register, and a bank customer in '
                    f'their own right.')
        else:
            lead = (f'On the {brand.PROPERTY} register only, with no account here.')
        if h.get('units'):
            where = f' in {_join_human(h.get("projects") or [])}' if h.get('projects') else ''
            paid = h.get('paid_pct')
            paid_txt = f', {round(paid * 100)}% paid' if paid is not None else ''
            unit_word = 'unit' if h['units'] == 1 else 'units'
            return (f'{lead} Holds {h["units"]} {unit_word} worth '
                    f'{_kes_short(h["units_value"])}{where}{paid_txt}.')
        return f'{lead} No unit on the register yet.'

    idy, risk = header['identity'], header['risk']
    hl = value.get('headline', {})
    dep = hl.get('deposits', {}).get('value') or 0
    loan = hl.get('loans', {}).get('value') or 0
    net = dep - loan

    lead = f'{_seg_human(idy["segment"]["value"])} customer'
    since = risk.get('relationship_since', {}).get('value')
    if since:
        try:
            tenure = date.today().year - int(str(since)[:4])
        except (TypeError, ValueError):
            tenure = None
        if tenure and tenure >= 1:
            lead += f' of {tenure} year{"s" if tenure != 1 else ""} with the bank'
        else:
            lead += f' since {str(since)[:4]}'
    parts = [lead + '.']

    if dep > 0 and loan > 0:
        net_txt = f'{_kes_short(net)} net' if net >= 0 else f'{_kes_short(-net)} net borrowing'
        parts.append(f'Holds {_kes_short(dep)} in deposits against {_kes_short(loan)} in loans, {net_txt}.')
    elif dep > 0:
        parts.append(f'Holds {_kes_short(dep)} in deposits, no active lending.')
    elif loan > 0:
        parts.append(f'Carries {_kes_short(loan)} in loans, no deposit balance.')

    rc = risk.get('risk_class', {}).get('value')
    if rc in ('Low', 'Medium', 'High'):
        parts.append(f'{rc} risk.')

    if idy.get('active', {}).get('value') is False:
        parts.append('Currently dormant.')

    # Voice a retention risk in the one-liner — it's the most action-worthy signal.
    ret = header.get('retention')
    if ret and ret.get('flag') == 'at_risk' and ret.get('note'):
        parts.append(ret['note'])

    return ' '.join(parts)


def _credit_bureau(gateway: WarehouseGateway, cust_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Fetch the customer's TransUnion (CRB) record and produce (panel, crb_chip).

    ``panel`` is the full bureau card data (or None when there's nothing to show);
    ``crb_chip`` is the header 'CRB status' Metric dict, kept consistent with the panel.

    Provenance: the bureau is a real external feed, but a point-in-time pull, so the
    pull date is always shown and the values are badged LIVE (not derived/faked). A
    customer with no bureau record now reads an honest 'No bureau record' — the feed
    exists, they simply aren't in it — rather than the old blanket 'not sourced', which
    is reserved for a genuine load failure. Display-only for now: this does NOT gate the
    recommendation engine yet (planned next step, per the agreed sequence)."""
    try:
        b = gateway.get_credit_bureau(cust_id)
    except Exception:
        # A real probe failure — honestly unsourced, not "no record".
        return None, to_source(note='Credit-bureau feed unavailable for this customer.').to_dict()
    if not b:
        return None, live('No bureau record', note='No TransUnion credit-bureau record matched this customer.').to_dict()
    if b.get('no_hit'):
        chip = live('No score · thin file',
                    note=f'On the credit bureau but with no scoreable history (as of {b.get("as_of")}).').to_dict()
        return b, chip
    grade = f' · {b["grade"]}' if b.get('grade') else ''
    pd = f' PD {b["pd"]}%.' if b.get('pd') is not None else ''
    chip = live(f'{b["score"]}{grade}',
                note=f'TransUnion bureau score, as of {b.get("as_of")}.{pd}').to_dict()
    return b, chip


def _build_crm(gateway: WarehouseGateway, cust_id: str) -> dict[str, Any] | None:
    """Subsidiary CRM panels: property-register leads (phone-matched) and the insurance
    CRM profile (HFBI, national-ID bridged). Returns {'property_leads':…, 'insurance':…}
    with either sub-key None when absent, or None overall when the customer has neither —
    so the frontend renders nothing rather than an empty shell. Never raises."""
    def _safe(fn):
        try:
            return fn(cust_id)
        except Exception:
            return None
    prop = _safe(gateway.get_property_leads)
    ins = _safe(gateway.get_insurance_crm)
    if not prop and not ins:
        return None
    return {'property_leads': prop, 'insurance': ins}


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

    # Credit bureau (TransUnion CRB) — a real external feed, matched by national ID.
    # Display-only: the panel + the header CRB chip are populated here; it does not yet
    # gate recommendations. Returns (panel, chip); panel is None when there's nothing to
    # show and the chip then reads 'No bureau record' / 'not sourced' honestly.
    bureau_panel, crb_metric = _credit_bureau(gateway, cust_id)

    # Subsidiary CRM — property-sales leads (phone-matched) + insurance CRM profile
    # (national-ID bridged). None when the customer has neither.
    crm_panel = _build_crm(gateway, cust_id)

    # Lending health — delinquency standing (NPL/watch + impairment) + collateral held.
    # None when neither applies. Display-only (does not feed the risk gate).
    try:
        lending_panel = gateway.get_lending_health(cust_id)
    except Exception:
        lending_panel = None

    # Silent-attrition early warning — DERIVED from the deposit-balance history
    # (c360/retention.py). Optional: None when the gateway has no history or the
    # trend can't be judged, in which case the UI simply omits the chip.
    try:
        retention = gateway.get_retention_signal(cust_id)
    except Exception:
        retention = None

    # RM provenance: 'allocation' = the current relationship manager (portfolio
    # allocation); 'onboarding' = the account-opening officer we fall back to when the
    # allocation source is unavailable. Label the fallback so a stale name is never
    # shown as the current RM (no rm_source, e.g. mock/preview, carries no caveat).
    rm_note = ('Account-opening officer. The current RM allocation is not available.'
               if c.get('rm_source') == 'onboarding' and c.get('rm_name') else None)

    # Previous RM (reassignment signal) from the allocation base, shown only when it
    # genuinely differs from the current RM. Optional; absent → simply not rendered.
    prev_rm = None
    try:
        prof = gateway.get_profitability(cust_id)
        pr = prof.get('prev_rm') if prof else None
        if pr and pr != c.get('rm_name'):
            prev_rm = pr
    except Exception:
        prev_rm = None

    return {
        'cust_id': c['cust_id'],
        # Present ONLY for an property client. The page keys off it to say, in
        # one line at the top, that this is not a bank customer - otherwise a screen
        # full of zeroes and 'not sourced' badges reads as a broken page rather than
        # an accurate one. Carries the bank id when the client also banks with us, so
        # the thin profile can hand over to the real one.
        **({'property_client': c['property_client']} if c.get('property_client') else {}),
        # Present ONLY for an insurance-register client, for the same reason: a page
        # of zeroes and 'not sourced' badges reads as broken unless it says why.
        **({'insurance_client': c['insurance']} if c.get('insurance') else {}),
        'retention': ({**retention, 'status': Provenance.DERIVED.value} if retention else None),
        'identity': {
            'name': live(c['name']).to_dict(),
            'segment': live(c['segment']).to_dict(),
            'branch': live(c['branch']).to_dict(),
            'rm_name': live(c.get('rm_name'), note=rm_note).to_dict(),
            'rm_previous': live(prev_rm).to_dict() if prev_rm else None,
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
        # Credit-bureau panel (TransUnion CRB), or None when the customer has no bureau
        # record / it couldn't be read. The frontend renders the full card from this.
        'credit_bureau': bureau_panel,
        # Subsidiary CRM (property leads + insurance CRM), or None when neither applies.
        'crm': crm_panel,
        # Lending health (delinquency + collateral), or None when neither applies.
        'lending': lending_panel,
        'risk': {
            'risk_class': risk_metric,
            'crb_status': crb_metric,
            'kyc_status': kyc_metric,
            # Sourced live from dim_customer.account_opening_date; unsourced in mock.
            'relationship_since': (
                live(c['relationship_since'], unit='date')
                if c.get('relationship_since')
                else to_source(unit='date', note='Relationship-since not yet sourced.')
            ).to_dict(),
        },
    }


def build_related_parties(gateway: WarehouseGateway, scope, cust_id: str) -> dict[str, Any] | None:
    """Related parties from the curated register, filtered to what THIS caller may see.

    Distinct from :func:`build_linked_parties`, which finds the SAME legal person
    under several customer numbers. This finds DIFFERENT parties and names the role
    between them — a company's directors and signatories, the companies a person
    sits on.

    Two filters, both deliberate:

    * The same scope + staff sieve as everywhere else, so a related party outside
      the caller's book, or an HF employee, never leaks through a side door. The
      register is a legitimate route to a record the caller is not entitled to see,
      so it gets the same treatment as search.
    * Family ties (spouse, sibling, next of kin) are dropped. They are personal data
      about third parties with no bearing on a banking decision, and putting a
      customer's relatives on an RM's screen is not something the register's
      existence justifies. `withheld` reports how many were removed so the number
      on screen is never silently short.

    Returns None when nothing visible remains — the panel then renders nothing.
    """
    from ..rbac.scoping import customer_visible, staff_hidden  # local: avoid import cycle

    try:
        related = gateway.get_related_parties(cust_id)
    except Exception:
        related = None
    if not related or not related.get('members'):
        return None

    corporate = [m for m in related['members'] if not m.get('personal')]
    withheld_personal = len(related['members']) - len(corporate)
    visible = [m for m in corporate
               if customer_visible(scope, m) and not staff_hidden(scope, m)]
    if not visible:
        return None
    return {
        'basis': related.get('basis', 'Related-party register'),
        'count': len(visible),
        'withheld_personal': withheld_personal,
        'members': [{
            'cust_id': m['cust_id'], 'name': m.get('name'), 'segment': m.get('segment'),
            'branch': m.get('branch'), 'value': m.get('value'),
            'products_held': m.get('products_held'),
            'roles': m.get('roles', []), 'role_labels': m.get('role_labels', []),
            'direction': m.get('direction'),
        } for m in visible],
    }


def build_linked_parties(gateway: WarehouseGateway, scope, cust_id: str) -> dict[str, Any] | None:
    """Same-person linked records (shared national ID), filtered to what THIS caller may
    see: members outside their book are dropped, and HF-staff records are hidden from
    everyone but the admin tier (the staff sieve). The combined value reflects only the
    visible members plus the primary, so it never leaks a total the caller couldn't
    otherwise reach. Returns None when nothing linked is visible."""
    from ..rbac.scoping import customer_visible, staff_hidden  # local: avoid import cycle

    try:
        linked = gateway.get_linked_parties(cust_id)
    except Exception:
        linked = None
    if not linked or not linked.get('members'):
        return None
    visible = [m for m in linked['members']
               if customer_visible(scope, m) and not staff_hidden(scope, m)]
    if not visible:
        return None
    combined = (linked.get('primary_value') or 0) + sum(m.get('value') or 0 for m in visible)
    return {
        'basis': linked.get('basis', 'National ID'),
        'count': len(visible),
        'combined_value': combined,
        'members': [{
            'cust_id': m['cust_id'], 'name': m['name'], 'segment': m['segment'],
            'branch': m.get('branch'), 'value': m.get('value'),
            'products_held': m.get('products_held'),
        } for m in visible],
    }


def build_value_summary(gateway: WarehouseGateway, cust_id: str) -> dict[str, Any]:
    """Cross-domain value summary. Core banking is LIVE; the other three domains
    are declared but PREVIEW/TO_SOURCE until their pipelines land, so the donut
    never shows a phantom slice as if it were real."""
    v = gateway.get_relationship_value(cust_id)
    # An property client has no bank relationship at all, and their entire
    # holding with the group is the property. Leaving the Properties row on the
    # generic 'pending the property register CRM integration' placeholder would report a known,
    # exact figure as unsourced - and leave the page reading as if we hold nothing
    # on someone with six units. Their number comes straight off the register.
    # Insurance value: the annual premium across the policies this customer holds.
    # The row used to be a flat 'not sourced' for everyone, so a customer with 28
    # policies and a real premium still read as nothing on the overview. That is the
    # zero people have been reporting. None (not 0) when they genuinely hold no
    # policy, which keeps the honest 'not sourced' state for that case.
    insurance_value = None
    try:
        banc = gateway.get_bancassurance(cust_id, None)
    except Exception:
        banc = None
    if banc and banc.get('policies'):
        insurance_value = round(sum(p.get('premium') or 0 for p in banc['policies']))

    hfdi_value = None
    client_id_int = prop_reg.parse_id(cust_id)
    if client_id_int is not None:
        try:
            client = gateway.get_property_client(client_id_int)
        except Exception:
            client = None
        if client:
            hfdi_value = client['property_client']['units_value']
    return {
        'headline': {
            'relationship_value': live(v['relationship_value'], unit='KES').to_dict(),
            'deposits': live(v['deposits'], unit='KES').to_dict(),
            'loans': live(v['loans'], unit='KES').to_dict(),
            'revenue': live(v['revenue'], unit='KES').to_dict(),
        },
        # value-by-domain: HFCB is real; others are placeholders flagged as such.
        'by_domain': [
            {'domain': brand.DOMAIN_LABELS['bank'], 'value': v['relationship_value'],
             'status': Provenance.LIVE.value,
             **({'note': f'No bank relationship. This client is on the '
                          f'{brand.PROPERTY} register only.'}
                if client_id_int is not None else {})},
            {'domain': brand.DOMAIN_LABELS['digital'], 'value': None, 'status': Provenance.TO_SOURCE.value,
             'note': 'Whizz value pending pipeline from the Kocela MySQL estate.'},
            ({'domain': brand.DOMAIN_LABELS['property'], 'value': hfdi_value,
              'status': Provenance.LIVE.value,
              'note': f'Total unit value from the {brand.PROPERTY} register.'}
             if hfdi_value is not None else
             {'domain': brand.DOMAIN_LABELS['property'], 'value': None, 'status': Provenance.TO_SOURCE.value,
              'note': f'Properties value pending the {brand.PROPERTY} CRM feed.'}),
            ({'domain': brand.DOMAIN_LABELS['insurance'], 'value': insurance_value,
              'status': Provenance.LIVE.value,
              'note': 'Annual premium across the policies held.'}
             if insurance_value is not None else
             {'domain': brand.DOMAIN_LABELS['insurance'], 'value': None,
              'status': Provenance.TO_SOURCE.value,
              'note': 'No insurance policy linked to this customer.'}),
        ],
    }
