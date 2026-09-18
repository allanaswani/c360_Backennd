"""In-process preview implementation of :class:`WarehouseGateway`.

Everything here is deterministic (seeded by ``cust_id``) so a given customer looks
the same on every reload — a designed preview, not noise. Values that correspond
to *live* queries are returned plainly; values that correspond to *must-build*
derivations (per-customer series) or *must-source* feeds (risk/KYC) are tagged by
the service layer, which knows each field's provenance. The gateway itself just
returns data + a coarse hint via method naming.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

from ... import credit_bureau as bureau_shape
from ... import crm as crm_shape
from ... import hfdi as hfdi_ns
from ... import lending as lending_shape
from ... import relationships as rel_shape
from ... import risk as risk_derive
from ...rbac.staff import is_staff_from_fields
from ..gateway import WarehouseGateway
from ..periods import ResolvedPeriod
from . import seed

# A stable synthetic "last closed business date" for preview mode.
_AS_OF = date(2026, 7, 22)

_INTENSITY = {'low': 40, 'medium': 140, 'high': 520}
_TREND_SLOPE = {'up': 0.55, 'flat': 0.0, 'down': -0.4}
_CHANNELS = ['Mobile', 'Branch', 'Agent', 'ATM', 'Internet']


def _rng(seed_str: str) -> 'FloatStream':
    return FloatStream(seed_str)


class FloatStream:
    """Tiny deterministic 0..1 stream — avoids importing random for reproducibility."""

    def __init__(self, seed_str: str):
        self._h = hashlib.sha256(seed_str.encode()).digest()
        self._i = 0

    def next(self) -> float:
        b = self._h[self._i % len(self._h)]
        self._i += 1
        if self._i % len(self._h) == 0:
            self._h = hashlib.sha256(self._h).digest()
        return b / 255.0


class MockWarehouse(WarehouseGateway):
    def as_of_date(self) -> date:
        return _AS_OF

    # --- identity & scope ------------------------------------------------
    def get_customer(self, cust_id: str) -> dict[str, Any] | None:
        # HFDI ids resolve against the property register, exactly as they do live.
        hfdi_client = hfdi_ns.parse_id(cust_id)
        if hfdi_client is not None:
            return self.get_property_client(hfdi_client)
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if not c:
            return None
        bio = self._bio(c)
        return {
            'cust_id': c['cust_id'],
            'name': c['name'],
            'segment': c['segment'],
            'branch': c['branch'],
            'sales_code': c['sales_code'],
            'rm_name': seed.RMS.get(c['sales_code']),
            'mobile': c['mobile'],
            'email': c['email'],
            'id_no': c['id_no'],
            'staff': c['staff'],
            'is_staff': self._is_staff(c, bio),
            'active': c['active'],
            # Must-source feeds: returned as None so the service tags them TO_SOURCE.
            'risk_class': None,
            'crb_status': None,
            'kyc_status': None,
            'relationship_since': None,
            'bio': bio,
        }

    def last_transaction_date(self, cust_id: str):
        """Preview parity for the live probe. ~70% of customers transacted in the last
        few weeks; the rest are 'active but stale' (last transaction 1.5-8 years back) so
        the header's staleness flag is exercised in preview exactly as against live data."""
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if not c:
            return None
        r = _rng(cust_id + 'lasttx')
        days_ago = int(r.next() * 25) if r.next() > 0.3 else int(500 + r.next() * 2500)
        return _AS_OF - timedelta(days=days_ago)

    @staticmethod
    def _is_staff(c: dict, bio: dict) -> bool:
        """Mock staff detection — the seed's explicit ``staff`` flag, plus the same
        employer/segment signals the live gateway uses (so previews behave identically)."""
        return is_staff_from_fields(
            explicit=c.get('staff'), segment=c.get('segment'), employer=bio.get('employer'))

    # Bio & identification (backlog item #1). Preview mode has no dim_customer, so a
    # deterministic bio is synthesised from the seed identity — organisations (company
    # IDs / entity names) carry a registration number and no personal fields, mirroring
    # the shape the live gateway returns.
    def _bio(self, c: dict) -> dict[str, Any]:
        idno = str(c.get('id_no') or '')
        name = c['name'].lower()
        is_org = idno[:1].upper() == 'C' or any(
            t in name for t in (' ltd', 'holdings', 'cooperative', 'enterprises', 'sacco', 'group', 'limited'))
        r = _rng(c['cust_id'] + 'bio')
        opened = (_AS_OF - timedelta(days=int(600 + r.next() * 3500))).isoformat()
        bio: dict[str, Any] = {
            'customer_type': 'Organisation' if is_org else 'Individual',
            'id_type': 'Company registration' if is_org else 'National ID',
            'id_no': c.get('id_no'),
            'issuing_authority': 'Registrar of Companies' if is_org else 'Government of Kenya',
            'kra_pin_status': 'Active',
            'employer': None if is_org else 'Self-employed',
            'address': f"P.O. Box {1000 + int(r.next() * 8000)}-00100, {c['branch']}",
            'alt_phone': None,
            'branch': c['branch'],
            'account_open_date': opened,
        }
        if is_org:
            bio.update({'date_of_birth': None, 'gender': None, 'city_of_birth': None})
        else:
            age = 24 + int(r.next() * 44)
            dob = date(_AS_OF.year - age, 1 + int(r.next() * 11), 1 + int(r.next() * 27))
            bio.update({'date_of_birth': dob.isoformat(),
                        'gender': 'Female' if r.next() > 0.5 else 'Male',
                        'city_of_birth': c['branch']})
        return bio

    def _visible(self, sales_codes: list[str] | None) -> list[dict]:
        if sales_codes is None:
            return seed.CUSTOMERS
        allowed = set(sales_codes)
        return [c for c in seed.CUSTOMERS if c['sales_code'] in allowed]

    def search_customers(self, query, *, sales_codes, limit=25, include_staff=True):
        q = (query or '').strip().lower()
        qid = ''.join(ch for ch in q if ch.isalnum())
        rows = self._visible(sales_codes)
        if q:
            def hit(c):
                if q in c['name'].lower() or q in c['cust_id'].lower():
                    return True
                # ID-document match (item #2): compare punctuation-stripped id numbers.
                return len(qid) >= 4 and qid in ''.join(ch for ch in str(c.get('id_no') or '').lower() if ch.isalnum())
            rows = [c for c in rows if hit(c)]
        summaries = [self._summary(c) for c in rows]
        if not include_staff:   # staff customers are admin-only
            summaries = [s for s in summaries if not s.get('is_staff')]
        return summaries[:limit]

    def list_customers(self, *, sales_codes, include_staff=True):
        summaries = [self._summary(c) for c in self._visible(sales_codes)]
        if not include_staff:
            summaries = [s for s in summaries if not s.get('is_staff')]
        return summaries

    def _summary(self, c: dict) -> dict[str, Any]:
        bio = self._bio(c)
        return {
            'cust_id': c['cust_id'], 'name': c['name'], 'segment': c['segment'],
            'branch': c['branch'], 'sales_code': c['sales_code'],
            'rm_name': seed.RMS.get(c['sales_code']),
            'value': c['value'], 'deposits': c['deposits'], 'loans': c['loans'],
            'products_held': sum(1 for v in c['flags'].values() if v),
            'id_no': c.get('id_no'), 'id_type': bio['id_type'], 'customer_type': bio['customer_type'],
            'is_staff': self._is_staff(c, bio),
        }

    # --- holdings & value ------------------------------------------------
    def get_product_holdings(self, cust_id):
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None:
            return {'flags': {}, 'product_map': {}}
        return {
            'flags': dict(c['flags']),
            'product_map': {k: seed.PRODUCT_LABELS[k] for k in c['flags']},
        }

    def get_relationship_value(self, cust_id):
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None:
            # Same answer the live gateway gives for an id it cannot resolve. An
            # HFDI property client reaches this: they hold no bank product, and the
            # page must say zero rather than borrow somebody else's balance.
            return {'relationship_value': 0, 'deposits': 0, 'loans': 0, 'revenue': 0}
        return {
            'relationship_value': c['value'],
            'deposits': c['deposits'],
            'loans': c['loans'],
            'revenue': c['revenue'],
        }

    def get_deposit_accounts(self, cust_id):
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None:
            return []
        r = _rng(cust_id + 'dep')
        accts = []
        products = [(k, v) for k, v in c['flags'].items()
                    if v and k in ('deposit', 'current', 'savings')]
        remaining = c['deposits']
        for i, (k, _) in enumerate(products):
            share = remaining if i == len(products) - 1 else int(remaining * (0.4 + 0.4 * r.next()))
            remaining -= share
            accts.append({
                'account_no': f'01{c["cust_id"][-6:]}{i}0',
                'product': seed.PRODUCT_LABELS[k],
                'product_key': k,
                'balance': max(share, 0),
                'currency': 'KES',
                'status': 'Active',
                'last_transaction_date': (_AS_OF - timedelta(days=int(r.next() * 20))).isoformat(),
            })
        return accts

    def get_loan_accounts(self, cust_id):
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None:
            return []
        r = _rng(cust_id + 'loan')
        accts = []
        loan_products = [(k, v) for k, v in c['flags'].items()
                         if v and k in ('mortgage', 'asset_finance', 'overdraft', 'unsecured', 'trade', 'ipf')]
        remaining = c['loans']
        for i, (k, _) in enumerate(loan_products):
            if remaining <= 0:
                break
            share = remaining if i == len(loan_products) - 1 else int(remaining * (0.3 + 0.5 * r.next()))
            remaining -= share
            accts.append({
                'account_no': f'05{c["cust_id"][-6:]}{i}0',
                'product': seed.PRODUCT_LABELS[k],
                'product_key': k,
                'outstanding_balance': max(share, 0),
                'currency': 'KES',
                'classification': 'Performing',
                'opened': (_AS_OF - timedelta(days=int(300 + r.next() * 1200))).isoformat(),
            })
        return accts

    # --- time series -----------------------------------------------------
    def _dates(self, period: ResolvedPeriod) -> list[date]:
        span = max(period.days, 1)
        step = 1 if span <= 31 else (7 if span <= 130 else 30)
        out, cur = [], period.start
        while cur <= period.end:
            out.append(cur)
            cur = cur + timedelta(days=step)
        if out[-1] != period.end:
            out.append(period.end)
        return out

    def _walk(self, base: float, slope: float, n: int, r: FloatStream, vol: float = 0.06) -> list[float]:
        vals, v = [], base * (1 - slope * 0.18)
        for i in range(n):
            drift = slope * base * (i / max(n - 1, 1)) * 0.36
            noise = (r.next() - 0.5) * 2 * vol * base
            vals.append(max(v + drift + noise, 0))
        return vals

    def deposit_loan_series(self, cust_id, period):
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None:
            return {'deposits': [], 'loans': []}
        slope = _TREND_SLOPE[c['profile']['trend']]
        ds = self._dates(period)
        dep = self._walk(c['deposits'] or 1, slope, len(ds), _rng(cust_id + 'ds-dep'))
        loan = self._walk(c['loans'] or 1, -slope * 0.4, len(ds), _rng(cust_id + 'ds-loan'), vol=0.03)
        return {
            'deposits': [{'period': d.isoformat(), 'balance': round(v)} for d, v in zip(ds, dep)],
            'loans': [{'period': d.isoformat(), 'balance': round(v)} for d, v in zip(ds, loan)] if c['loans'] else [],
        }

    def disbursement_vs_balance_series(self, cust_id, period):
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None or not c['loans']:
            return {'disbursed': [], 'balance': []}
        ds = self._dates(period)
        r = _rng(cust_id + 'disb')
        disbursed_total = c['profile'].get('disbursed', c['loans'])
        bal = self._walk(c['loans'], -0.3, len(ds), _rng(cust_id + 'lbal'), vol=0.02)
        # Disbursement is cumulative and always >= balance (paying down).
        cum, out = 0.0, []
        for i, d in enumerate(ds):
            cum = min(disbursed_total, cum + disbursed_total * (0.02 + r.next() * 0.05))
            out.append({'period': d.isoformat(), 'amount': round(max(cum, bal[i]))})
        return {
            'disbursed': out,
            'balance': [{'period': d.isoformat(), 'amount': round(v)} for d, v in zip(ds, bal)],
        }

    def transaction_series(self, cust_id, period):
        # Daily transaction *count* — matches the live fact_dep_trx_recording shape.
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None:
            return []
        base = _INTENSITY[c['profile']['txn_intensity']]
        slope = _TREND_SLOPE[c['profile']['trend']]
        ds = self._dates(period)
        vals = self._walk(base, slope, len(ds), _rng(cust_id + 'txn'), vol=0.22)
        return [{'period': d.isoformat(), 'count': int(round(v))} for d, v in zip(ds, vals)]

    def channel_usage(self, cust_id, period):
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None:
            return []
        r = _rng(cust_id + 'chan')
        weights = []
        for ch in _CHANNELS:
            base = r.next()
            if ch == 'Mobile' and c['flags'].get('mobile'):
                base += 1.4
            if ch == 'Branch' and not c['flags'].get('mobile'):
                base += 1.0
            weights.append(base)
        total = sum(weights) or 1
        return [{'channel': ch, 'share': round(w / total, 4)} for ch, w in zip(_CHANNELS, weights)]

    def recent_transactions(self, cust_id, *, period=None, limit=8, lookback_months=24):
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None:
            return []
        r = _rng(cust_id + 'recent')
        kinds = [('Mobile transfer', 'Mobile'), ('POS purchase', 'ATM'), ('Standing order', 'Branch'),
                 ('Salary credit', 'Branch'), ('Airtime', 'Mobile'), ('Loan repayment', 'Mobile'),
                 ('ATM withdrawal', 'ATM'), ('Bill payment', 'Internet')]
        out = []
        for i in range(limit):
            desc, chan = kinds[i % len(kinds)]
            sign = -1 if r.next() > 0.42 else 1
            amt = int((500 + r.next() * 90000))
            out.append({
                'date': (_AS_OF - timedelta(days=i, hours=int(r.next() * 12))).isoformat(),
                'description': desc,
                'channel': chan,
                'amount': sign * amt,
                'currency': 'KES',
            })
        return out

    # --- benchmark -------------------------------------------------------
    def portfolio_whole_book(self, period):
        # Mock has no real book to scan → return None so the portfolio service uses
        # its seeded roster (the mock IS the whole preview book anyway).
        return None

    def portfolio_trends(self, customers, period):
        # Mock has no real month-end history → return None so the portfolio service
        # falls back to its deterministic simulated (preview) trends.
        return None

    # A small related-party graph mirroring the live register's shape: directional
    # (origin -> related) edges carrying a role from the real vocabulary. Two
    # companies in the seed roster have officers; the officers point back. Lets the
    # panel be built and reviewed without the curated Postgres, which is on a LAN
    # segment the dev box cannot reach.
    #
    # Which side the register files an organisation under is NOT confirmed (see
    # c360.relationships), so nothing here or in the UI depends on it — these are
    # simply pairs, and both pages show the same role with the filing side stated.
    _RELATIONSHIPS = [
        ('HF-100571', 'HF-102010', 'DIRECTOR'),
        ('HF-100571', 'HF-102010', 'SIGNATORY'),
        ('HF-101488', 'HF-102010', 'DIRECTOR'),
        ('HF-102377', 'HF-102010', 'SIGNATORY'),
        ('HF-100904', 'HF-100238', 'PROPRIETOR'),
        ('HF-101755', 'HF-100238', 'GUARANTOR'),
        ('HF-101755', 'HF-101120', 'PARTNER'),
        # An id the register names that the customer master no longer holds — the
        # panel must keep the row and say it cannot name it, never invent a name.
        ('HF-109404', 'HF-102010', 'SIGNATORY'),
        # A family tie, so the 'personal' flag is exercised and can be seen to be
        # held back from the panel.
        ('HF-100571', 'HF-101488', 'HUSBAND_WIFE'),
    ]

    def get_related_parties(self, cust_id):
        """Preview related parties — same contract as the live register."""
        found: dict[str, dict] = {}
        for origin, related, rel in self._RELATIONSHIPS:
            if origin == cust_id:
                other, direction = related, 'outbound'
            elif related == cust_id:
                other, direction = origin, 'inbound'
            else:
                continue
            entry = found.setdefault(other, {'cust_id': other, 'roles': [], 'direction': direction})
            if rel not in entry['roles']:
                entry['roles'].append(rel)
        if not found:
            return None
        return rel_shape.shape([
            rel_shape.shape_member(entry, seed.CUSTOMER_INDEX.get(other))
            for other, entry in found.items()
        ])

    def get_risk_profile(self, cust_id):
        """Derived KYC + risk, same pure functions as live. Identity uses the real
        seed fields; the attributes the seed doesn't carry (KRA PIN / DOB / address)
        and loan performance are synthesised deterministically so the preview shows a
        realistic spread of Verified/Partial and Low/Medium/High."""
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if not c:
            return None
        r = _rng(cust_id + 'riskkyc')
        identity = {
            'id_no': c.get('id_no'),
            'mobile': c.get('mobile'),
            'email': c.get('email'),
            'kra_pin_status': '1' if r.next() > 0.22 else '0',
            'date_of_birth': '1980-01-01' if r.next() > 0.12 else None,
            'address': 'P O BOX 00100 NAIROBI' if r.next() > 0.18 else None,
        }
        statuses: list[str] = []
        if c['loans'] > 0:
            roll = r.next()
            if c['profile'].get('trend') == 'down' and roll > 0.55:
                statuses.append('Non-Performing' if roll > 0.82 else 'Watch')
            else:
                statuses.append('Performing')
        return risk_derive.derive_profile(
            identity, statuses, float(c['deposits']), float(c['loans']))

    def get_credit_bureau(self, cust_id):
        """Preview TransUnion (CRB) record, synthesised deterministically so the panel
        shows a realistic spread: most customers scored, a minority a 'no-hit' thin
        file, and a minority with no bureau record at all (returns None). Shaped through
        the same c360.credit_bureau logic as live, so the panel behaves identically."""
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if not c:
            return None
        r = _rng(cust_id + 'crb')
        roll = r.next()
        if roll < 0.18:                      # ~18% simply aren't on the bureau
            return None
        as_of = (_AS_OF - timedelta(days=int(120 + r.next() * 240))).isoformat()
        if roll < 0.30:                      # a thin-file no-hit (sentinel row)
            raw = {'score': 0, 'score_grade': 'YY', 'probability': 999.99,
                   'non_performing': 0, 'arrears_90_days': 0,
                   'number_of_enquiries': int(r.next() * 3), 'enquiries_90_days': 0}
            return bureau_shape.shape_bureau(raw, as_of=as_of)
        # A scored record. A down-trending / borrowing customer reads worse (higher PD,
        # some arrears) so the preview shows the full range.
        down = c['profile'].get('trend') == 'down' and c['loans'] > 0
        score = int(720 - r.next() * 300 - (140 if down else 0))
        score = max(250, min(830, score))
        pd = round(min(95.0, max(2.0, (830 - score) / 8.0 + r.next() * 6)), 2)
        grades = ['AA', 'BB', 'CC', 'DD', 'EE', 'FF', 'GG', 'HH', 'II', 'JJ']
        grade = grades[min(9, int((830 - score) / 60))]
        npl = 1 if (down and r.next() > 0.5) else 0
        raw = {
            'score': score, 'score_grade': grade, 'probability': pd,
            'non_performing': npl,
            'arrears_90_days': (1 if down and r.next() > 0.6 else 0),
            'max_arrears_last_6_months': (int(r.next() * 4) if down else 0),
            'number_of_enquiries': int(r.next() * 8),
            'enquiries_90_days': int(r.next() * 3),
        }
        return bureau_shape.shape_bureau(raw, as_of=as_of)

    def get_property_leads(self, cust_id):
        """Preview property-sales CRM — a minority of customers carry matched leads, so
        the panel appears occasionally (mirrors the ~13% live phone-match rate)."""
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if not c:
            return None
        r = _rng(cust_id + 'propcrm')
        if r.next() > 0.35:                 # most customers have no matched lead
            return None
        pool = ['SEALED', 'prospect', 'holding', 'potential', 'failed', 'booked']
        n = 1 + int(r.next() * 4)
        states = [pool[int(r.next() * len(pool))] for _ in range(n)]
        fups = int(r.next() * 12)
        return crm_shape.shape_property_leads(states, fups, int(fups * r.next()))

    def get_insurance_crm(self, cust_id):
        """Preview insurance CRM profile — appears for a subset of customers."""
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if not c:
            return None
        r = _rng(cust_id + 'inscrm')
        if r.next() > 0.4:
            return None
        agents = ['Eric Otieno', 'Faith Wanjiku', 'Peter Kariuki', 'Aisha Noor']
        occs = ['RETIRED BANKER', 'CIVIL SERVANT', 'BUSINESS OWNER', 'TEACHER', 'ENGINEER']
        branches = ['Buruburu Branch', 'Westlands Branch', 'Kisumu Branch', 'Nakuru Branch']
        return crm_shape.shape_insurance_crm({
            'risk_manager': 'robert.mugo@hfgroup.co.ke',
            'sales_person': agents[int(r.next() * len(agents))],
            'occupation': occs[int(r.next() * len(occs))],
            'branch': branches[int(r.next() * len(branches))],
            'location': None,
        })

    def get_lending_health(self, cust_id):
        """Preview credit standing + collateral. A down-trending borrower may be NPL or
        on watch; borrowers commonly carry collateral. Shaped through the same
        c360.lending logic as live."""
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if not c:
            return None
        r = _rng(cust_id + 'lend')
        delinquency = None
        if c['loans'] > 0:
            down = c['profile'].get('trend') == 'down'
            roll = r.next()
            if down and roll > 0.7:
                cls = ['SUBSTD', 'DOUBTFUL', 'LOSS'][min(2, int(r.next() * 3))]
                delinquency = lending_shape.shape_delinquency(
                    [{'classification': cls, 'impairment': round(200_000 + r.next() * 3_000_000)}],
                    False, 'May')
            elif roll > 0.72:
                delinquency = lending_shape.shape_delinquency([], True, 'May')
        collateral = None
        if c['loans'] > 0 and r.next() > 0.35:
            pool = [('PROPERTY', 1 + int(r.next() * 2)), ('VEHICLE', 1 + int(r.next() * 3)),
                    ('TERM DEPOSIT COLLATERAL', 1)]
            picks = [pool[i] for i in range(len(pool)) if r.next() > 0.5] or [pool[0]]
            collateral = lending_shape.shape_collateral([{'type': tp, 'count': n} for tp, n in picks])
        if not delinquency and not collateral:
            return None
        return {'delinquency': delinquency, 'collateral': collateral}

    def segment_product_benchmark(self, segment):
        # None, not a default, for a segment with no measured benchmark. Rule C then
        # abstains instead of comparing a customer against a number nobody computed -
        # the behaviour the live gateway already documents. A property client has no
        # peer group here at all, and used to be told they were 3.0 products behind it.
        return seed.SEGMENT_BENCHMARK.get(segment)

    # --- non-core domains (preview) -------------------------------------
    def get_whizz(self, cust_id, period):
        # Mirrors the live shape: Whizz activity sourced from KOCELA transactions.
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None or not c['flags'].get('mobile'):
            return None
        r = _rng(cust_id + 'whizz')
        base = _INTENSITY[c['profile']['txn_intensity']] * (0.5 + r.next())
        slope = _TREND_SLOPE[c['profile']['trend']]
        ds = self._dates(period)
        activity = self._walk(base, slope + 0.2, len(ds), _rng(cust_id + 'wact'), vol=0.24)
        counts = [int(round(v)) for v in activity]
        txn_count = sum(counts)
        cat_def = [('Send to M-Pesa', 0.42), ('Pay Bill', 0.24), ('Buy Goods', 0.18),
                   ('Airtime', 0.10), ('M-Pesa to account', 0.06)]
        total_val = round(base * 4_000 + r.next() * 60_000)
        # Spread the period's value across days in proportion to that day's txn count,
        # so the value line tracks the activity line without being identical.
        per_txn = (total_val / txn_count) if txn_count else 0
        activity_pts = [{'period': d.isoformat(), 'count': cnt, 'value': round(cnt * per_txn)}
                        for d, cnt in zip(ds, counts)]
        categories = [{'label': name, 'count': max(1, int(txn_count * share)),
                       'value': round(total_val * share)} for name, share in cat_def]
        recent = [{
            'date': (_AS_OF - timedelta(days=int(i + r.next() * 2))).isoformat(),
            'description': cat_def[i % len(cat_def)][0],
            'amount': round(500 + r.next() * 22_000), 'currency': 'KES',
        } for i in range(6)]
        return {
            'status': 'Active',
            'registered_since': (_AS_OF - timedelta(days=int(500 + r.next() * 1500))).isoformat(),
            'txn_count': txn_count,
            'txn_value': sum(x['value'] for x in categories),
            'services_used': len(categories),
            'activity': activity_pts,
            'categories': categories,
            'recent': recent,
        }

    # HFDI property clients - the group's property buyers, most of whom hold no bank
    # account and so have no dim_customer record at all. Live, 3,830 of 4,843
    # register clients are in that position; the preview carries the same two cases
    # so the page can be built and reviewed without the warehouse.
    #
    # (client_id, name, idno, phone, email, has_pin, units)
    # Each unit is (project, unit, value, paid_pct, mortgage).
    _HFDI_CLIENTS = [
        # Bridges to a seed customer: id_no matches HF-102010 (Zawadi Enterprises),
        # so the panel must offer their real profile rather than this thinner one.
        (415, 'Zawadi Enterprises Ltd', 'C.102844', '+254722000415',
         'accounts@zawadi.co.ke', True, [
             ('Ngong Town', '54', 8_000_000, 0.0, False),
             ('Clay City Undeveloped Land', '6', 9_000_000, 0.0, True),
             ('Ngong Town', '53', 8_000_000, 0.0, False),
         ]),
        # No bank record - the acquisition case this page exists for.
        (902, 'Halima Yusuf Abdi', '24551903', '+254733000902', None, False, [
            ('Komarock Heights', 'B12', 6_400_000, 0.62, False),
        ]),
        (1177, 'Tumaini Investments Limited', 'PVT/2014/88213', '+254720001177',
         'info@tumaini.co.ke', True, [
             ('Tatu City Villas', 'V7', 21_500_000, 0.35, True),
             ('Tatu City Villas', 'V8', 21_500_000, 0.35, True),
         ]),
        # Punctuated differently on each side ('C.088310' vs 'C/088310'): the bridge
        # normalises before comparing, so this still resolves to the bank record.
        (1503, 'Kipchoge Farms Cooperative', 'C/088310', '+254711001503', None, True, [
            ('Ngong View Estate', 'A3', 11_200_000, 0.88, False),
        ]),
        # On the register with no unit yet - still a client, and the count has to
        # reconcile, so the row is kept rather than filtered out of existence.
        (1688, 'Rehema Chepkoech Too', '30114872', '+254701001688', None, False, []),
    ]

    def _hfdi_row(self, entry):
        cid, name, idno, phone, email, pin, units = entry
        return {
            'client_id': float(cid), 'client_name': name, 'client_idno': idno,
            'client_phone': phone, 'client_email': email,
            'client_pin': 'A00' + str(cid) + 'X' if pin else None,
        }

    def _hfdi_shape(self, entry):
        cid, name, idno, phone, email, pin, units = entry
        summary = {
            'units': len(units),
            'units_value': sum(u[2] for u in units),
            'paid_pct': round(sum(u[3] for u in units) / len(units), 3) if units else None,
            'projects': sorted({u[0] for u in units}),
        }
        # The bank bridge, resolved the same way as live: normalised national id.
        bank = None
        key = hfdi_ns.normalise_idno(idno)
        for c in seed.CUSTOMER_INDEX.values():
            if key and hfdi_ns.normalise_idno(c.get('id_no')) == key:
                bio = self._bio(c)
                bank = {'cust_id': c['cust_id'], 'is_staff': self._is_staff(c, bio)}
                break
        return hfdi_ns.shape_client(self._hfdi_row(entry), units=summary, bank=bank)

    def search_property_clients(self, query, *, limit=50, unbanked_only=False):
        raw = (query or '').strip().lower()
        norm = hfdi_ns.normalise_idno(raw)
        out = []
        for entry in self._HFDI_CLIENTS:
            cid, name, idno, *_ = entry
            if raw and not (raw in name.lower() or raw == str(cid)
                            or (len(norm) >= 5 and norm in hfdi_ns.normalise_idno(idno))):
                continue
            out.append(self._hfdi_shape(entry))
        if unbanked_only:
            out = [c for c in out if not c['hfdi']['bank_cust_id']]
        out.sort(key=lambda c: (-(c['hfdi']['units_value'] or 0), c['name'] or ''))
        return out[:int(limit)]

    def get_property_client(self, client_id):
        for entry in self._HFDI_CLIENTS:
            if entry[0] == int(client_id):
                return self._hfdi_shape(entry)
        return None

    def property_client_coverage(self):
        total = len(self._HFDI_CLIENTS)
        banked = sum(1 for e in self._HFDI_CLIENTS if self._hfdi_shape(e)['hfdi']['bank_cust_id'])
        return {
            'total': total,
            'banked': banked,
            'unbanked': total - banked,
            'owners': sum(1 for e in self._HFDI_CLIENTS if e[6]),
            'units': sum(len(e[6]) for e in self._HFDI_CLIENTS),
            'note': hfdi_ns.coverage_note(total, banked),
        }

    def get_properties(self, cust_id):
        hfdi_client = hfdi_ns.parse_id(cust_id)
        if hfdi_client is not None:
            for entry in self._HFDI_CLIENTS:
                if entry[0] == hfdi_client:
                    units = [{'unit': u[1], 'project': u[0], 'value': u[2],
                              'paid_pct': u[3], 'mortgage': u[4]} for u in entry[6]]
                    if not units:
                        return None
                    units.sort(key=lambda p: p['value'], reverse=True)
                    return {'properties': units}
            return None
        # Mirrors the live HFDI shape: distinct units with value, % paid, mortgage.
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None or not c['flags'].get('mortgage'):
            return None
        r = _rng(cust_id + 'prop')
        n = 1 + int(r.next() * 2.4)
        projects = ['Komarock Heights', 'Kileleshwa Ridge', 'Nyali Gardens', 'Tatu City Villas', 'Ngong View Estate']
        props = []
        for i in range(n):
            value = round(6_000_000 + r.next() * 34_000_000)
            props.append({
                'unit': f'{chr(65 + i)}{int(4 + r.next() * 40)}',
                'project': projects[(hash(cust_id) + i) % len(projects)],
                'value': value,
                'paid_pct': round(0.1 + r.next() * 0.9, 3),
                'mortgage': r.next() > 0.4,
            })
        props.sort(key=lambda p: p['value'], reverse=True)
        return {'properties': props}

    def get_bancassurance(self, cust_id, period):
        c = seed.CUSTOMER_INDEX.get(cust_id)
        if c is None:
            return None
        insurable = c['flags'].get('ipf') or c['flags'].get('mortgage') or c['flags'].get('asset_finance')
        if not insurable:
            return None
        r = _rng(cust_id + 'banc')
        catalogue = [
            ('Home Insurance', 'home_cover'), ('Credit Life', 'life_cover'),
            ('Motor Insurance', 'asset_cover'), ('Fire & Perils', 'asset_cover'),
        ]
        k = 1 + int(r.next() * 3)
        policies = []
        for i in range(k):
            name, _ = catalogue[i % len(catalogue)]
            premium = round(18_000 + r.next() * 240_000)
            policies.append({
                'policy': f'POL-{c["cust_id"][-5:]}{i}',
                'product': name,
                'premium': premium,
                'sum_insured': round(premium * (12 + r.next() * 40)),
                'status': 'Active' if r.next() > 0.25 else 'Expired',
                'start': None,
                'end': None,
            })
        policies.sort(key=lambda p: p['premium'], reverse=True)
        return {'policies': policies}
