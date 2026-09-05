# Generic-agent baseline — 20260904T234252Z

- n = 38  ·  status: {'OK': 38}
- accuracy by tier: {'tier1': 0.45, 'tier2': 0.8, 'tier3': 0.5}
- p95 latency: 56.4s  ·  avg latency: 33.7s
- avg input tokens/question: 131953

| id | tier | dataset | status | correct | latency(s) |
|----|------|---------|--------|---------|------------|
| t1_aapl_revenue_2024 | 1 | eval_set | OK | ✓ | 21.7 |
| t1_aapl_netincome_2024 | 1 | eval_set | OK | ✓ | 23.8 |
| t1_aapl_eps_diluted_2024 | 1 | eval_set | OK | ✓ | 25.3 |
| t1_swks_revenue_2024 | 1 | eval_set | OK | ✓ | 28.7 |
| t1_swks_netincome_2024 | 1 | eval_set | OK | ✓ | 27.5 |
| t1_qrvo_netincome_2024 | 1 | eval_set | OK | ✗ | 29.9 |
| t1_crus_revenue_2024 | 1 | eval_set | OK | ✓ | 29.0 |
| t1_glw_revenue_2024 | 1 | eval_set | OK | ✓ | 31.1 |
| t1_avgo_revenue_2024 | 1 | eval_set | OK | ✓ | 30.8 |
| t1_avgo_randd_2024 | 1 | eval_set | OK | ✓ | 26.7 |
| t2_aapl_grossprofit_pct_2024 | 2 | eval_set | OK | ✓ | 29.1 |
| t2_aapl_operatingincome_pct_2024 | 2 | eval_set | OK | ✓ | 28.9 |
| t2_aapl_netincome_pct_2024 | 2 | eval_set | OK | ✓ | 36.8 |
| t2_avgo_grossprofit_pct_2024 | 2 | eval_set | OK | ✓ | 48.3 |
| t2_qrvo_grossprofit_pct_2024 | 2 | eval_set | OK | ✓ | 30.6 |
| t2_aapl_revenue_yoy_2023_2024 | 2 | eval_set | OK | ✓ | 23.4 |
| t2_aapl_revenue_yoy_2022_2023 | 2 | eval_set | OK | ✗ | 33.6 |
| t2_swks_revenue_yoy_2023_2024 | 2 | eval_set | OK | ✗ | 39.9 |
| t2_avgo_revenue_yoy_2023_2024 | 2 | eval_set | OK | ✓ | 22.3 |
| t2_glw_grossprofit_pct_2024 | 2 | eval_set | OK | ✓ | 28.7 |
| unans_aapl_china_rev_2023 | 1 | eval_set | OK | ✗ | 31.1 |
| unans_tsmc_rev_2024 | 1 | eval_set | OK | ✗ | 28.5 |
| unans_aapl_div_yield_2024 | 1 | eval_set | OK | ✗ | 34.0 |
| ret_swks_apple_concentration_2024 | 1 | eval_set | OK | ✗ | 31.8 |
| ret_swks_markets_served_2024 | 1 | eval_set | OK | ✗ | 29.7 |
| ret_avgo_vmware_acquisition | 1 | eval_set | OK | ✗ | 36.1 |
| ret_aapl_product_categories | 1 | eval_set | OK | ✗ | 37.5 |
| ret_aapl_applecare_description | 1 | eval_set | OK | ✗ | 29.2 |
| ret_qrvo_customer_risk | 1 | eval_set | OK | ✗ | 36.8 |
| ret_glw_business_segments | 1 | eval_set | OK | ✗ | 34.4 |
| t3_aapl_suppliers_2024 | 3 | eval_set_tier3 | OK | ✗ | 46.2 |
| t3_qrvo_apple_pct_2024 | 3 | eval_set_tier3 | OK | ✓ | 25.7 |
| t3_crus_apple_trend | 3 | eval_set_tier3 | OK | ✓ | 63.4 |
| t3_most_exposed_supplier_2024 | 3 | eval_set_tier3 | OK | ✗ | 27.6 |
| t3_qrvo_dollar_impact_2024 | 3 | eval_set_tier3 | OK | ✓ | 31.7 |
| t3_crus_dollar_impact_2024 | 3 | eval_set_tier3 | OK | ✓ | 42.2 |
| t3_rank_apple_exposure_2024 | 3 | eval_set_tier3 | OK | ✗ | 56.4 |
| t3_unans_apple_supplier_share | 3 | eval_set_tier3 | OK | ✗ | 62.2 |
