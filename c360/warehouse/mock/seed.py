"""Seeded preview dataset.

Deterministic, hand-shaped so the recommendation engine has something real to
chew on: each customer is built to trip a *different* rule (product gap, behaviour
signal, peer benchmark). This is preview data — it stands in for the live queries
described in ``TrinoWarehouse`` and is always badged as such in the UI.

HFC context: Kenyan housing-finance bank, balances in KES, real branch and segment
names. Product keys match the ``hf_customer`` per-product holding flags named in
the architecture (deposit, current, savings, mobile, mortgage, asset finance,
overdraft, IPF, cash cover, trade, unsecured).
"""
from __future__ import annotations

# The canonical core-banking product set (hf_customer flags + product_map).
PRODUCT_LABELS = {
    'deposit': 'Deposit Account',
    'current': 'Current Account',
    'savings': 'Savings Account',
    'mobile': 'Mobile Banking',
    'mortgage': 'Mortgage',
    'asset_finance': 'Asset Finance',
    'overdraft': 'Overdraft',
    'ipf': 'Insurance Premium Finance',
    'cash_cover': 'Cash Cover',
    'trade': 'Trade Finance',
    'unsecured': 'Unsecured Loan',
}

# Typical products-held-per-segment (drives Rule C peer benchmark).
SEGMENT_BENCHMARK = {
    'Premier': 4.6,
    'Affluent': 4.1,
    'SME': 3.8,
    'Business': 3.4,
    'Retail': 2.7,
}

BRANCHES = ['Kenyatta Avenue', 'Mombasa', 'Kisumu', 'Thika', 'Nakuru', 'Karen', 'Westlands']

# sales_code ↔ RM, mirroring retail_allocated_portfolio.
RMS = {
    'SC-1042': 'Achieng Otieno',
    'SC-1077': 'Brian Kamau',
    'SC-1108': 'Faith Wanjiru',
    'SC-1155': 'Daniel Mwangi',
}

# ---------------------------------------------------------------------------
# Roster. `flags` are the live product-holding booleans. `value`, `deposits`,
# `loans`, `revenue` are the current snapshot (retail_allocated_portfolio join).
# `profile` is a shaping hint the mock gateway uses to synthesise plausible
# series and behaviour signals — it is not part of any real contract.
# ---------------------------------------------------------------------------
CUSTOMERS: list[dict] = [
    {
        'cust_id': 'HF-100238',
        'name': 'Naserian Holdings Ltd',
        'segment': 'SME',
        'branch': 'Westlands',
        'sales_code': 'SC-1042',
        'mobile': '+254 722 431 908',
        'email': 'finance@naserian.co.ke',
        'id_no': 'C.041992',
        'staff': False,
        'active': True,
        'flags': {
            'deposit': True, 'current': True, 'savings': False, 'mobile': True,
            'mortgage': True, 'asset_finance': False, 'overdraft': True, 'ipf': False,
            'cash_cover': False, 'trade': True, 'unsecured': False,
        },
        'value': 48_650_000, 'deposits': 12_400_000, 'loans': 36_250_000, 'revenue': 3_180_000,
        # Has a mortgage + trade, no IPF/asset finance → Rule A product-gap target.
        'profile': {'trend': 'up', 'txn_intensity': 'medium', 'disbursed': 41_000_000},
    },
    {
        'cust_id': 'HF-100571',
        'name': 'Peter Njoroge Kariuki',
        'segment': 'Retail',
        'branch': 'Thika',
        'sales_code': 'SC-1077',
        'mobile': '+254 711 220 145',
        'email': 'pnjoroge@gmail.com',
        'id_no': '22841097',
        'staff': False,
        'active': True,
        'flags': {
            'deposit': True, 'current': False, 'savings': True, 'mobile': True,
            'mortgage': False, 'asset_finance': False, 'overdraft': False, 'ipf': False,
            'cash_cover': False, 'trade': False, 'unsecured': False,
        },
        'value': 1_240_000, 'deposits': 180_000, 'loans': 0, 'revenue': 41_000,
        # Heavy mobile activity, thin deposit balance → Rule B behaviour signal.
        'profile': {'trend': 'flat', 'txn_intensity': 'high', 'disbursed': 0},
    },
    {
        'cust_id': 'HF-100904',
        'name': 'Aisha Abdallah Salim',
        'segment': 'Premier',
        'branch': 'Mombasa',
        'sales_code': 'SC-1108',
        'mobile': '+254 733 908 771',
        'email': 'aisha.salim@outlook.com',
        'id_no': '11920388',
        'staff': False,
        'active': True,
        'flags': {
            'deposit': True, 'current': True, 'savings': False, 'mobile': True,
            'mortgage': False, 'asset_finance': False, 'overdraft': False, 'ipf': False,
            'cash_cover': False, 'trade': False, 'unsecured': False,
        },
        'value': 9_820_000, 'deposits': 9_820_000, 'loans': 0, 'revenue': 210_000,
        # Premier segment holds ~4.6 products; she holds 3 → Rule C peer gap.
        'profile': {'trend': 'up', 'txn_intensity': 'low', 'disbursed': 0},
    },
    {
        'cust_id': 'HF-101120',
        'name': 'Kipchoge Farms Cooperative',
        'segment': 'Business',
        'branch': 'Nakuru',
        'sales_code': 'SC-1042',
        'mobile': '+254 720 553 219',
        'email': 'admin@kipchogefarms.co.ke',
        'id_no': 'C.088310',
        'staff': False,
        'active': True,
        'flags': {
            'deposit': True, 'current': True, 'savings': False, 'mobile': False,
            'mortgage': False, 'asset_finance': True, 'overdraft': True, 'ipf': True,
            'cash_cover': True, 'trade': True, 'unsecured': False,
        },
        'value': 27_300_000, 'deposits': 6_100_000, 'loans': 21_200_000, 'revenue': 1_940_000,
        # Asset finance without mobile banking → digital-adoption gap (Rule A).
        'profile': {'trend': 'down', 'txn_intensity': 'medium', 'disbursed': 24_500_000},
    },
    {
        'cust_id': 'HF-101488',
        'name': 'Grace Wambui Ndungu',
        'segment': 'Affluent',
        'branch': 'Karen',
        'sales_code': 'SC-1155',
        'mobile': '+254 707 118 440',
        'email': 'g.wambui@gmail.com',
        'id_no': '19833027',
        'staff': False,
        'active': True,
        'flags': {
            'deposit': True, 'current': True, 'savings': True, 'mobile': True,
            'mortgage': True, 'asset_finance': False, 'overdraft': False, 'ipf': False,
            'cash_cover': False, 'trade': False, 'unsecured': False,
        },
        'value': 63_400_000, 'deposits': 18_900_000, 'loans': 44_500_000, 'revenue': 4_020_000,
        # Mortgage + strong deposits, no IPF/asset finance → cross-sell rich.
        'profile': {'trend': 'up', 'txn_intensity': 'medium', 'disbursed': 52_000_000},
    },
    {
        'cust_id': 'HF-101755',
        'name': 'Samuel Otieno Odhiambo',
        'segment': 'Retail',
        'branch': 'Kisumu',
        'sales_code': 'SC-1077',
        'mobile': '+254 714 662 018',
        'email': 'samotieno@gmail.com',
        'id_no': '27110945',
        'staff': False,
        'active': True,
        'flags': {
            'deposit': True, 'current': False, 'savings': True, 'mobile': True,
            'mortgage': False, 'asset_finance': True, 'overdraft': False, 'ipf': False,
            'cash_cover': False, 'trade': False, 'unsecured': True,
        },
        'value': 3_780_000, 'deposits': 340_000, 'loans': 3_100_000, 'revenue': 288_000,
        'profile': {'trend': 'flat', 'txn_intensity': 'high', 'disbursed': 3_600_000},
    },
    {
        'cust_id': 'HF-102010',
        'name': 'Zawadi Enterprises Ltd',
        'segment': 'SME',
        'branch': 'Kenyatta Avenue',
        'sales_code': 'SC-1108',
        'mobile': '+254 733 401 662',
        'email': 'accounts@zawadi.co.ke',
        'id_no': 'C.102844',
        'staff': False,
        'active': True,
        'flags': {
            'deposit': True, 'current': True, 'savings': False, 'mobile': True,
            'mortgage': False, 'asset_finance': False, 'overdraft': True, 'ipf': False,
            'cash_cover': False, 'trade': False, 'unsecured': True,
        },
        'value': 15_600_000, 'deposits': 4_200_000, 'loans': 9_800_000, 'revenue': 940_000,
        'profile': {'trend': 'up', 'txn_intensity': 'high', 'disbursed': 11_000_000},
    },
    {
        'cust_id': 'HF-102377',
        'name': 'Mary Nyambura Gichuru',
        'segment': 'Premier',
        'branch': 'Karen',
        'sales_code': 'SC-1155',
        'mobile': '+254 722 900 314',
        'email': 'mary.gichuru@icloud.com',
        'id_no': '14577201',
        'staff': False,
        'active': True,
        'flags': {
            'deposit': True, 'current': True, 'savings': True, 'mobile': True,
            'mortgage': True, 'asset_finance': True, 'overdraft': False, 'ipf': True,
            'cash_cover': False, 'trade': False, 'unsecured': False,
        },
        'value': 71_900_000, 'deposits': 22_600_000, 'loans': 47_800_000, 'revenue': 5_310_000,
        # Well-covered flagship customer — few gaps, mostly benchmark-complete.
        'profile': {'trend': 'up', 'txn_intensity': 'medium', 'disbursed': 55_000_000},
    },
]

CUSTOMER_INDEX = {c['cust_id']: c for c in CUSTOMERS}
