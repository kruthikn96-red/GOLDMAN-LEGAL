-- sample1_eligibility

-- 5.1.a
SELECT * FROM policies WHERE applicant__credit_score >= 680;

-- 5.1.b
SELECT * FROM policies WHERE policy__coverage_amount_usd <= 2000000;

-- 5.1.c.1
SELECT * FROM policies WHERE applicant__credit_score >= 680;

-- 5.1.c.2
SELECT 1 FROM policies HAVING SUM(CASE WHEN applicant__credit_score BETWEEN 680 AND 700 THEN total_portfolio_value_usd ELSE 0 END) / NULLIF(SUM(total_portfolio_value_usd), 0) <= 0.15;

-- 5.1.d
SELECT * FROM policies WHERE (applicant__debt_to_income_ratio_pct <= 40 OR (applicant__cosigner__credit_score > 750 AND applicant__debt_to_income_ratio_pct <= 45));

-- 5.1.e
SELECT * FROM policies WHERE applicant__payment_overdue_days_max <= 30;

-- 5.1.f
SELECT * FROM policies WHERE applicant__country_of_residence IN ('US', 'US_TERRITORY');

-- 5.1.g
SELECT * FROM policies WHERE (applicant__annual_income_usd >= 35000 OR (applicant__enrolled_in_approved_assistance_program = TRUE AND applicant__annual_income_usd >= 25000));

-- sample2_concentration

-- 7.3.i
SELECT policy__origin_state, SUM(total_portfolio_value_usd) / NULLIF(SUM(total_portfolio_value_usd), 0) AS share
FROM policies
GROUP BY policy__origin_state
HAVING share <= 0.25;

-- 7.3.ii
SELECT 1 FROM policies HAVING SUM(CASE WHEN policy__coverage_amount_usd > 1500000 THEN total_portfolio_value_usd ELSE 0 END) / NULLIF(SUM(total_portfolio_value_usd), 0) <= 0.1;

-- 7.3.iii
SELECT 1 FROM policies HAVING SUM(applicant__credit_score * policy__coverage_amount_usd) / NULLIF(SUM(policy__coverage_amount_usd), 0) >= 720.0;

-- 7.3.iv
SELECT 1 FROM policies HAVING SUM(CASE WHEN (policy__applicant__is_primary = TRUE AND applicant__age_years < 25) THEN total_portfolio_value_usd ELSE 0 END) / NULLIF(SUM(total_portfolio_value_usd), 0) <= 0.05;

-- 7.3.v
SELECT 1 FROM policies HAVING SUM(applicant__debt_to_income_ratio_pct * policy__coverage_amount_usd) / NULLIF(SUM(policy__coverage_amount_usd), 0) <= 35.0;

-- sample3_fees

-- 12.2.a
-- Fee rule `Processing Fee` compiles to an event-gated fee function.

-- 12.2.b
-- Fee rule `Annual Service Fee` compiles to an event-gated fee function.

-- 12.2.c
-- Fee rule `Late Payment Fee` compiles to an event-gated fee function.

-- 12.2.d
-- Fee rule `Early Termination Fee` compiles to an event-gated fee function.

-- 12.2.e
-- Fee rule `Reinstatement Fee` compiles to an event-gated fee function.
