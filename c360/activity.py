"""What a customer's transactions say they need - the activity-based cross-sell.

The rule engine (``recommendations/rules.py``) and the model reason from what a
customer HOLDS. This reasons from what they DO: salary landing every month, money
leaving by M-Pesa, cash over the counter, cheques and RTGS going out. Every
opportunity it raises carries the evidence in plain words ("salary credited 3 times
in the last 90 days, KES 412,000 in total"), so an RM can check it before calling.

Transactions are classified by their core-banking purpose (``justific_descrption``),
on every channel, counting only real movements (signed amount not zero). Channel is
NOT used to filter: M-Pesa deposits ride the integration bus and salaries can arrive
as a system journal credit (customer 1218821: a monthly JOURNAL CREDIT into her
MALIPO SALARY ACCOUNT). ``trn_type`` is NOT used for direction: verified 2026-09-30
it files M-Pesa money in and out under the same code. "DEPOSIT THROUGH TILL" is left
unclassified on purpose - nothing in the data says whether the till is an M-Pesa
till or a branch cashier's.

Deliberately absent: an "insurance premium" signal. ``INSURANCE DEBIT`` looks like
one, but KES 140.5B of it in Jul-Sep 2026 sat on five internal accounts, and the
median customer debit was about KES 3 - a levy, not a premium.

Thresholds were calibrated on the whole book (Jul-Sep 2026, 36,142 customers who
transacted): each rule flags between ~770 and ~8,300 customers, never most of them.

Pure: no SQL here beyond the shared classification expression.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from . import brand

WINDOW_DAYS = 90

# The purpose -> category classification, as a SQL CASE over an upper-cased, trimmed
# purpose column ``j`` and product column ``p``. Shared by the per-customer and
# whole-book queries so the two can never classify a transaction differently.
CATEGORY_SQL = """CASE
 WHEN j LIKE 'SALARY POSTINGS%' AND j NOT LIKE '%RETURN%' THEN 'salary'
 WHEN j = 'JOURNAL CREDIT' AND p LIKE '%SALARY%' THEN 'salary_acct'
 WHEN j = 'ACCOUNT TO MPESA(B2C)' THEN 'mpesa_out'
 WHEN j IN ('CR FROM MOBILE BANKING-MPESA TO ACC', 'HFCB MPESA TO ACCOUNT(B2C)',
            'MPESA CR WHIZZPAY') THEN 'mpesa_in'
 WHEN j IN ('ACCOUNT CREDIT KITS', 'ACCOUNT DEBIT FROM KITS') THEN 'pesalink'
 WHEN j LIKE 'PAY BILL%' OR j LIKE 'BUY GOOD%' OR j LIKE 'AIRTIME PURCHASE%'
      OR j LIKE 'UTILITY BILL PAYMENT%' THEN 'bills'
 WHEN j IN ('DEPOSIT CASH', 'CASH DEPOSIT MACHINE (CDM)') THEN 'cash_in'
 WHEN j IN ('CASH WITHDRAWAL', 'OTC CASH WITHDRAWAL') THEN 'cash_out'
 WHEN j LIKE 'V-DB%' THEN 'card'
 WHEN j LIKE 'CHEQUE DEPOSIT OF OTHER BANK%' OR j = 'ORDINARY CLEARING CHEQUE' THEN 'cheque_in'
 WHEN j IN ('CHEQUE PAYMENT FROM CARNET', 'RTGS -PAYMENT') OR j LIKE 'BANK DRAFT ISSUED%' THEN 'payments_out'
 WHEN j = 'PARTIAL TERM DEPOSIT WITHDRAWAL-JOURNAL' THEN 'td_withdrawal'
END"""

CATEGORY_LABELS = {
    'salary': 'Salary credits',
    'salary_acct': 'Credits into a salary account',
    'mpesa_out': 'Sent to M-Pesa',
    'mpesa_in': 'M-Pesa into account',
    'pesalink': 'PesaLink transfers',
    'bills': 'Pay Bill, Buy Goods, airtime',
    'cash_in': 'Cash deposits',
    'cash_out': 'Cash withdrawals',
    'card': 'Card (POS, ATM, online)',
    'cheque_in': 'Cheques received',
    'payments_out': 'Cheques, RTGS and drafts out',
    'td_withdrawal': 'Term-deposit part withdrawals',
}
CATEGORIES = tuple(CATEGORY_LABELS)

# Calibrated thresholds (see module docstring).
SALARY_MIN = 2            # two or more salary credits in 90 days = a salaried customer
# Journal credits into a salary-type account count as a salary pattern only when they
# arrive about monthly. A cap matters: one customer had 49 such credits in 90 days
# (KES 20.5M) - that is money being paid in, not a salary.
SALARY_ACCT_MAX = 6
MPESA_OUT_MIN = 10        # a regular M-Pesa user
CASH_MIN = 6              # cash-in + cash-out, with no digital use at all
IDLE_LIQUID_MIN = 1_000_000
BUSINESS_PAYMENTS_MIN = 6
CASH_OUT_NO_CARD_MIN = 4

# A customer who took a mobile loan within this many days is already a mobile borrower,
# even if today's balance is zero. Found on live data: customer 121669 was offered a
# "first" Whizz loan while their 50th, taken 11 days earlier, had just been repaid.
MOBILE_RECENT_DAYS = 365

BANK = brand.DOMAIN_LABELS['bank']
DIGITAL = brand.DOMAIN_LABELS['digital']

# Who each rule may be offered to. The first whole-book run pitched a fixed deposit and
# an overdraft to National Treasury programme accounts, "move to Whizz" to a cement
# company's scheme collections, and a personal Whizz loan to medium enterprises.
# Segments are dim_customer.customer_segment (upper-cased; 'Unsegmented' = UNKNOWN).
RETAIL_SEGMENTS = frozenset({'VIRTUAL', 'MASS', 'STANDARD', 'PRIVATE', 'UNKNOWN',
                             'UNSEGMENTED', 'SMALL ENTERPRISES'})
INSTITUTIONAL_SEGMENTS = frozenset({'INSTITUTIONAL BANKING', 'SCHEME', 'PROJECT FINANCE',
                                    'INTERNAL ACCOUNTS'})
# Personal products (salary loan, savings, debit card): individuals outside the large
# business and institutional books. Whizz products: individuals in retail / small
# business, where Mwanabiashara mobile loans are aimed.
_PERSONAL_EXCLUDED = INSTITUTIONAL_SEGMENTS | {'MEDIUM ENTERPRISES', 'LARGE ENTERPRISES'}


def _eligibility(individual: bool | None, segment: str | None) -> dict[str, bool]:
    seg = ' '.join(str(segment or '').split()).upper()
    personal = individual is True and seg not in _PERSONAL_EXCLUDED
    return {
        'personal': personal,
        'whizz': individual is True and seg in RETAIL_SEGMENTS,
        'deposit': seg not in INSTITUTIONAL_SEGMENTS,
        'business': individual is False and seg not in INSTITUTIONAL_SEGMENTS,
    }


def empty_profile() -> dict[str, dict[str, float]]:
    return {c: {'count': 0, 'value': 0} for c in CATEGORIES}


def profile_from_rows(rows: list[dict]) -> dict[str, dict[str, float]]:
    """{category: {count, value}} from rows of {cat, n, v}. Unknown categories dropped."""
    prof = empty_profile()
    for r in rows or []:
        cat = r.get('cat')
        if cat in prof:
            prof[cat]['count'] += int(r.get('n') or 0)
            prof[cat]['value'] += round(float(r.get('v') or 0))
    return prof


def _kes(v: float) -> str:
    return f'KES {v:,.0f}'


def _times(n: int) -> str:
    return 'once' if n == 1 else f'{n} times'


def opportunities(profile: dict[str, dict[str, float]], *, held: set[str],
                  liquid_balance: float = 0, has_active_card: bool | None = None,
                  individual: bool | None = None, segment: str | None = None,
                  ) -> list[dict[str, Any]]:
    """Activity-driven opportunities, strongest first.

    ``held`` is the set of product keys from ``c360.products`` (e.g. 'consumer',
    'term_deposit', 'mobile_loan'). ``liquid_balance`` is the customer's current +
    savings + notice balance (wallet excluded). ``has_active_card`` is None when the
    card register could not be read - the card rule then abstains rather than guess.
    ``individual`` (None = unknown) and ``segment`` decide which rules may apply; an
    unknown customer type gets no personal or business rule, only the deposit one.
    """
    ok = _eligibility(individual, segment)
    c = {k: int(v['count']) for k, v in profile.items()}
    v = {k: float(v['value']) for k, v in profile.items()}
    out: list[dict[str, Any]] = []

    def add(rule, product, name, domain, reason, short, score, evidence):
        out.append({'rule_id': rule, 'product': product, 'product_name': name,
                    'domain': domain, 'reason': reason, 'reason_short': short,
                    'base_score': score, 'evidence': evidence})

    by_posting = c['salary'] >= SALARY_MIN
    by_account = SALARY_MIN <= c['salary_acct'] <= SALARY_ACCT_MAX
    salaried = by_posting or by_account
    if by_posting:
        sal_ev = [f"Salary credited {_times(c['salary'])} in the last {WINDOW_DAYS} days, "
                  f"{_kes(v['salary'])} in total."]
        sal_how = f"Salary is paid into this account ({c['salary']} credits in {WINDOW_DAYS} days)"
    elif by_account:
        sal_ev = [f"{c['salary_acct']} credits into a salary account in the last {WINDOW_DAYS} "
                  f"days, {_kes(v['salary_acct'])} in total."]
        sal_how = (f"Regular credits land in their salary account ({c['salary_acct']} in "
                   f"{WINDOW_DAYS} days)")
    else:
        sal_ev, sal_how = [], ''

    if ok['personal'] and salaried and not held & {'consumer', 'vuna_hela'}:
        add('T1', 'unsecured', 'Salary-backed personal loan', BANK,
            f"{sal_how} and there is no personal loan with us. A salary-backed loan is the "
            f"natural offer.",
            'Salaried, no personal loan', 0.78, sal_ev)

    if ok['personal'] and salaried and not held & {'savings', 'notice', 'term_deposit'}:
        add('T2', 'savings', 'Savings account', BANK,
            f"{sal_how} but there is no savings product with us. A savings or target savings "
            f"account can take a fixed amount each payday.",
            'Salaried, no savings product', 0.70, sal_ev)

    if ok['deposit'] and liquid_balance >= IDLE_LIQUID_MIN and not held & {'term_deposit', 'call_deposit'}:
        add('T3', 'term_deposit', 'Fixed deposit', BANK,
            f"{_kes(liquid_balance)} is sitting in current and savings accounts with no fixed or "
            f"call deposit. Part of it can earn a term rate.",
            'Large idle balance, no fixed deposit', 0.74,
            [f"Current, savings and notice balances total {_kes(liquid_balance)} at the latest close."])

    biz = c['payments_out'] + c['cheque_in']
    if ok['business'] and biz >= BUSINESS_PAYMENTS_MIN and not held & {'overdraft', 'working_capital'}:
        add('T4', 'overdraft', 'Overdraft / working capital', BANK,
            f"{biz} cheque, RTGS or draft payments in {WINDOW_DAYS} days, and no overdraft or "
            f"working-capital line to smooth the cash flow between them.",
            'Business payments, no credit line', 0.68,
            [f"{c['payments_out']} cheques, RTGS or drafts out ({_kes(v['payments_out'])}); "
             f"{c['cheque_in']} cheques received ({_kes(v['cheque_in'])})."])

    if ok['whizz'] and c['mpesa_out'] >= MPESA_OUT_MIN and 'mobile_loan' not in held:
        add('T5', 'whizz_loan', 'Whizz mobile loan', DIGITAL,
            f"Sends money to M-Pesa regularly ({c['mpesa_out']} times in {WINDOW_DAYS} days) and "
            f"has not taken a mobile loan in the last 12 months. Whizz credit fits how they "
            f"already move money.",
            'Regular M-Pesa user, no recent mobile loan', 0.60,
            [f"{c['mpesa_out']} transfers to M-Pesa, {_kes(v['mpesa_out'])} in total."])

    digital = c['mpesa_out'] + c['mpesa_in'] + c['bills'] + c['pesalink']
    cash = c['cash_in'] + c['cash_out']
    if ok['whizz'] and cash >= CASH_MIN and digital == 0:
        add('T6', 'mobile', 'Whizz / mobile banking', BANK,
            f"Banks entirely in cash ({cash} counter or machine transactions in {WINDOW_DAYS} "
            f"days) and never digitally. Whizz saves them the branch visit.",
            'Cash only, no digital use', 0.62,
            [f"{c['cash_in']} cash deposits, {c['cash_out']} cash withdrawals; "
             f"no M-Pesa, PesaLink or bill payments in {WINDOW_DAYS} days."])

    if ok['personal'] and has_active_card is False and c['cash_out'] >= CASH_OUT_NO_CARD_MIN:
        add('T7', 'debit_card', 'Debit card', BANK,
            f"Withdraws cash over the counter ({c['cash_out']} times in {WINDOW_DAYS} days) and "
            f"holds no active card.",
            'Counter withdrawals, no card', 0.58,
            [f"{c['cash_out']} cash withdrawals, {_kes(v['cash_out'])} in total; no active card."])

    out.sort(key=lambda o: o['base_score'], reverse=True)
    return out


def watch_signals(profile: dict[str, dict[str, float]]) -> list[str]:
    """Things worth knowing that are not a sale - e.g. money leaving a term deposit."""
    notes = []
    td = profile['td_withdrawal']
    if td['count'] > 0:
        notes.append(f"Part-withdrew from a term deposit {_times(int(td['count']))} in the last "
                     f"{WINDOW_DAYS} days ({_kes(td['value'])}). Worth a retention call.")
    return notes


def recent_mobile_borrower(mobile: dict | None, as_of: date) -> bool:
    """True when the facilities block shows a mobile loan taken within
    MOBILE_RECENT_DAYS of ``as_of`` (``mobile`` is facilities.summarise()['mobile'])."""
    latest = (mobile or {}).get('latest_issued')
    if not latest:
        return False
    try:
        return (as_of - date.fromisoformat(str(latest)[:10])).days <= MOBILE_RECENT_DAYS
    except ValueError:
        return False
