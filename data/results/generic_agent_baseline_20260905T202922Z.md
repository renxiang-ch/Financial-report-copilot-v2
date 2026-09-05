# Generic-agent baseline — 20260905T202922Z

- model: `gpt-5.6-sol`  ·  runs: 1  ·  items/run: 38
- status: {'OK': 38}
- accuracy (excl. pending): {'tier1': 0.692, 'tier2': 0.7, 'tier3': 0.667}
- unanswerable by reason (refused/n): v1_schema_gap 0/2, out_of_scope 0/1, undisclosed 0/1
- latency s: {'p50': 31.0, 'p95': 47.1, 'max': 50.0, 'mean': 32.5}
- tokens/q (mean): {'input_tokens': 131819, 'cached_input_tokens': 114735, 'cache_write_input_tokens': 0, 'output_tokens': 818, 'reasoning_output_tokens': 126}
- tool calls: {'mean': 4.2, 'max': 8}

| run | id | tier | status | correct | latency(s) | tool calls |
|-----|----|------|--------|---------|------------|------------|
| 1 | t1_aapl_revenue_2024 | 1 | OK | ✓ | 25.3 | 3 |
| 1 | t1_aapl_netincome_2024 | 1 | OK | ✓ | 24.1 | 3 |
| 1 | t1_aapl_eps_diluted_2024 | 1 | OK | ✓ | 28.8 | 4 |
| 1 | t1_swks_revenue_2024 | 1 | OK | ✓ | 24.8 | 3 |
| 1 | t1_swks_netincome_2024 | 1 | OK | ✓ | 29.5 | 4 |
| 1 | t1_qrvo_netincome_2024 | 1 | OK | ✗ | 28.0 | 4 |
| 1 | t1_crus_revenue_2024 | 1 | OK | ✓ | 29.2 | 4 |
| 1 | t1_glw_revenue_2024 | 1 | OK | ✓ | 47.1 | 7 |
| 1 | t1_avgo_revenue_2024 | 1 | OK | ✓ | 28.7 | 4 |
| 1 | t1_avgo_randd_2024 | 1 | OK | ✓ | 25.7 | 3 |
| 1 | t2_aapl_grossprofit_pct_2024 | 2 | OK | ✓ | 29.3 | 4 |
| 1 | t2_aapl_operatingincome_pct_2024 | 2 | OK | ✓ | 34.4 | 5 |
| 1 | t2_aapl_netincome_pct_2024 | 2 | OK | ✓ | 30.0 | 4 |
| 1 | t2_avgo_grossprofit_pct_2024 | 2 | OK | ✓ | 30.1 | 3 |
| 1 | t2_qrvo_grossprofit_pct_2024 | 2 | OK | ✓ | 28.3 | 4 |
| 1 | t2_aapl_revenue_yoy_2023_2024 | 2 | OK | ✗ | 24.8 | 4 |
| 1 | t2_aapl_revenue_yoy_2022_2023 | 2 | OK | ✗ | 28.6 | 4 |
| 1 | t2_swks_revenue_yoy_2023_2024 | 2 | OK | ✗ | 33.3 | 5 |
| 1 | t2_avgo_revenue_yoy_2023_2024 | 2 | OK | ✓ | 35.8 | 5 |
| 1 | t2_glw_grossprofit_pct_2024 | 2 | OK | ✓ | 46.6 | 7 |
| 1 | unans_aapl_china_rev_2023 | 1 | OK | ✗ | 36.5 | 4 |
| 1 | unans_tsmc_rev_2024 | 1 | OK | ✗ | 41.3 | 3 |
| 1 | unans_aapl_div_yield_2024 | 1 | OK | ✗ | 36.5 | 4 |
| 1 | ret_swks_apple_concentration_2024 | 1 | OK | ? | 28.4 | 4 |
| 1 | ret_swks_markets_served_2024 | 1 | OK | ? | 31.0 | 4 |
| 1 | ret_avgo_vmware_acquisition | 1 | OK | ? | 31.6 | 4 |
| 1 | ret_aapl_product_categories | 1 | OK | ? | 16.8 | 2 |
| 1 | ret_aapl_applecare_description | 1 | OK | ? | 26.0 | 4 |
| 1 | ret_qrvo_customer_risk | 1 | OK | ? | 35.4 | 5 |
| 1 | ret_glw_business_segments | 1 | OK | ? | 44.2 | 6 |
| 1 | t3_aapl_suppliers_2024 | 3 | OK | ✓ | 40.4 | 3 |
| 1 | t3_qrvo_apple_pct_2024 | 3 | OK | ✓ | 28.2 | 4 |
| 1 | t3_crus_apple_trend | 3 | OK | ✓ | 42.3 | 4 |
| 1 | t3_most_exposed_supplier_2024 | 3 | OK | ? | 31.4 | 4 |
| 1 | t3_qrvo_dollar_impact_2024 | 3 | OK | ✓ | 50.0 | 8 |
| 1 | t3_crus_dollar_impact_2024 | 3 | OK | ✗ | 37.0 | 5 |
| 1 | t3_rank_apple_exposure_2024 | 3 | OK | ? | 32.8 | 3 |
| 1 | t3_unans_apple_supplier_share | 3 | OK | ✗ | 34.4 | 4 |
