"""Loan product_desc -> canonical holding-flag classification.

Guards the recommendation engine's most user-visible failure mode: a non-mortgage
loan being stamped as a 'mortgage', which makes the Next-Best-Product panel claim
"holds a mortgage" for a customer who has none (real report, 2026-09-01, cust 175767).

Tests the pure keyword mapping only (``TrinoWarehouse._apply_flags`` +
``_LOAN_KEYWORDS``); no warehouse connection is needed.
"""
from django.test import SimpleTestCase

from c360.warehouse.trino.trino_gateway import TrinoWarehouse, _LOAN_KEYWORDS


def _classify_loan(desc: str) -> set[str]:
    flags: dict[str, bool] = {}
    TrinoWarehouse._apply_flags(flags, desc, _LOAN_KEYWORDS, base=None)
    return {k for k, v in flags.items() if v}


class LoanClassificationTests(SimpleTestCase):
    # --- the regression: "purchase" must not mean "mortgage" -----------------
    def test_hire_purchase_is_asset_finance_not_mortgage(self):
        self.assertEqual(_classify_loan('HIRE PURCHASE'), {'asset_finance'})

    def test_motor_vehicle_purchase_is_asset_finance_not_mortgage(self):
        self.assertEqual(_classify_loan('MOTOR VEHICLE PURCHASE LOAN'), {'asset_finance'})

    def test_asset_purchase_is_asset_finance_not_mortgage(self):
        self.assertEqual(_classify_loan('ASSET PURCHASE'), {'asset_finance'})

    def test_construction_equipment_is_asset_finance_not_mortgage(self):
        # 'equipment' (asset finance) is matched before 'construction' (mortgage).
        self.assertEqual(_classify_loan('CONSTRUCTION EQUIPMENT FINANCE'), {'asset_finance'})

    # --- genuine mortgages still classify as mortgages -----------------------
    def test_plain_mortgage_still_mortgage(self):
        self.assertEqual(_classify_loan('MORTGAGE LOAN'), {'mortgage'})

    def test_owner_occupier_still_mortgage(self):
        self.assertEqual(_classify_loan('OWNER OCCUPIER PURCHASE'), {'mortgage'})

    def test_plot_loan_still_mortgage(self):
        self.assertEqual(_classify_loan('PLOT LOAN'), {'mortgage'})

    def test_housing_loan_still_mortgage(self):
        self.assertEqual(_classify_loan('HOUSING LOAN'), {'mortgage'})

    # --- other loan types unchanged ------------------------------------------
    def test_overdraft(self):
        self.assertEqual(_classify_loan('OVERDRAFT FACILITY'), {'overdraft'})

    def test_ipf(self):
        self.assertEqual(_classify_loan('INSURANCE PREMIUM FINANCE'), {'ipf'})

    def test_trade_lpo(self):
        self.assertEqual(_classify_loan('LPO FINANCING'), {'trade'})

    def test_personal_unsecured(self):
        self.assertEqual(_classify_loan('PERSONAL LOAN'), {'unsecured'})

    def test_salary_check_off_unsecured(self):
        self.assertEqual(_classify_loan('SALARY CHECK-OFF LOAN'), {'unsecured'})

    def test_unrecognised_desc_sets_no_flag(self):
        # No keyword hit -> the loan simply doesn't map to a canonical product.
        self.assertEqual(_classify_loan('SOME BESPOKE FACILITY'), set())
