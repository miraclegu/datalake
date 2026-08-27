-- lake.db 视图与宏 DDL（含 block 品种，2026-08-25 更新）
-- 视图 30，表宏 4

-- ===== VIEW adjust_factor =====
CREATE VIEW adjust_factor AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/tdx/adjust_factor.parquet');

-- ===== VIEW basic_daily =====
CREATE VIEW basic_daily AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/tdx/basic/*.parquet');

-- ===== VIEW code_map =====
CREATE VIEW code_map AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/std/code_map.parquet');

-- ===== VIEW dividend =====
CREATE VIEW dividend AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/std/dividend.parquet');

-- ===== VIEW dividend_forecast =====
CREATE VIEW dividend_forecast AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/stk_fin_forcast.parquet');

-- ===== VIEW fin_balance =====
CREATE VIEW fin_balance AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/financials/balance.parquet');

-- ===== VIEW fin_cashflow =====
CREATE VIEW fin_cashflow AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/financials/cashflow.parquet');

-- ===== VIEW fin_core =====
CREATE VIEW fin_core AS SELECT i.code, i.report_date, i.pub_date, (i.pub_date <= i.report_date) AS pub_date_is_placeholder, i.total_operating_revenue AS revenue, i.operating_profit AS operating_profit, i.total_profit AS profit_before_tax, i.net_profit AS net_profit_total, i.np_parent_company_owners AS net_profit_parent, i.minority_profit AS net_profit_minority, i.basic_eps AS eps_basic, i.rd_expenses AS rd_expense FROM fin_income AS i;

-- ===== VIEW fin_income =====
CREATE VIEW fin_income AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/financials/income.parquet');

-- ===== VIEW fin_indicator =====
CREATE VIEW fin_indicator AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/financials/indicator.parquet');

-- ===== VIEW fin_ratio =====
CREATE VIEW fin_ratio AS SELECT code, report_date, pub_date, (pub_date <= report_date) AS pub_date_is_placeholder, net_profit_this_year AS net_profit_parent, main_income_this_year AS main_revenue, total_assets_this_year AS total_assets, equities_this_year AS equities, roe_this_year AS roe_parent, roe_weighted_this_year AS roe_parent_weighted, eps_this_year AS eps_parent, bps_this_year AS bps, nocf_per_share_this_year AS nocf_per_share, nocf_this_year AS nocf, rd_expense, rd_expense_ratio FROM fin_indicator;

-- ===== VIEW fin_snapshots =====
CREATE VIEW fin_snapshots AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/fin_snapshots.parquet');

-- ===== VIEW fund_universe =====
CREATE VIEW fund_universe AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/std/security_universe_fund.parquet');

-- ===== VIEW index_member =====
CREATE VIEW index_member AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/std/index_member.parquet');

-- ===== VIEW index_member_asof =====
CREATE VIEW index_member_asof AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/std/index_member_asof.parquet');

-- ===== VIEW kline_bfq =====
CREATE VIEW kline_bfq AS SELECT k.symbol, m.jq_code, k.date, k.open, k.high, k.low, k."close", k.volume, k.amount, COALESCE(f.hfq_factor, 1) AS hfq_factor FROM kline_raw AS k LEFT JOIN adjust_factor AS f ON (((f.symbol = k.symbol) AND (f.date = k.date))) LEFT JOIN code_map AS m ON ((m.tdx_symbol = k.symbol));

-- ===== VIEW kline_hfq =====
CREATE VIEW kline_hfq AS SELECT k.symbol, m.jq_code, k.date, round((k.open * COALESCE(f.hfq_factor, 1)), 2) AS open, round((k.high * COALESCE(f.hfq_factor, 1)), 2) AS high, round((k.low * COALESCE(f.hfq_factor, 1)), 2) AS low, round((k."close" * COALESCE(f.hfq_factor, 1)), 2) AS "close", k.volume, k.amount, COALESCE(f.hfq_factor, 1) AS hfq_factor FROM kline_raw AS k LEFT JOIN adjust_factor AS f ON (((f.symbol = k.symbol) AND (f.date = k.date))) LEFT JOIN code_map AS m ON ((m.tdx_symbol = k.symbol));

-- ===== VIEW kline_qfq =====
CREATE VIEW kline_qfq AS WITH latest AS (SELECT symbol, argmax(hfq_factor, date) AS lf FROM adjust_factor GROUP BY symbol)SELECT k.symbol, m.jq_code, k.date, round(((k.open * COALESCE(f.hfq_factor, 1)) / COALESCE(l.lf, 1)), 2) AS open, round(((k.high * COALESCE(f.hfq_factor, 1)) / COALESCE(l.lf, 1)), 2) AS high, round(((k.low * COALESCE(f.hfq_factor, 1)) / COALESCE(l.lf, 1)), 2) AS low, round(((k."close" * COALESCE(f.hfq_factor, 1)) / COALESCE(l.lf, 1)), 2) AS "close", k.volume, k.amount FROM kline_raw AS k LEFT JOIN adjust_factor AS f ON (((f.symbol = k.symbol) AND (f.date = k.date))) LEFT JOIN latest AS l ON ((l.symbol = k.symbol)) LEFT JOIN code_map AS m ON ((m.tdx_symbol = k.symbol));

-- ===== VIEW kline_raw =====
CREATE VIEW kline_raw AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/tdx/kline/*.parquet');

-- ===== VIEW l0_dim_industry_asof =====
CREATE VIEW l0_dim_industry_asof AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/dim_industry_asof.parquet');

-- ===== VIEW l0_dim_name_history =====
CREATE VIEW l0_dim_name_history AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/dim_name_history.parquet');

-- ===== VIEW l0_dim_security =====
CREATE VIEW l0_dim_security AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/dim_security.parquet');

-- ===== VIEW l0_dim_security_asof =====
CREATE VIEW l0_dim_security_asof AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/dim_security_asof.parquet');

-- ===== VIEW l0_dim_status_change =====
CREATE VIEW l0_dim_status_change AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/dim_status_change.parquet');

-- ===== VIEW l0_idx_weight_month =====
CREATE VIEW l0_idx_weight_month AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/raw/jq/idx_weight_month.parquet');

-- ===== VIEW security_industry =====
CREATE VIEW security_industry AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/std/security_industry.parquet');

-- ===== VIEW security_name =====
CREATE VIEW security_name AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/std/security_name.parquet');

-- ===== VIEW security_status =====
CREATE VIEW security_status AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/std/security_status.parquet');

-- ===== VIEW security_universe =====
CREATE VIEW security_universe AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/std/security_universe.parquet');

-- ===== VIEW trading_calendar =====
CREATE VIEW trading_calendar AS SELECT * FROM read_parquet('/Users/guhao/finacial/datalake/std/trading_calendar.parquet');

-- ===== MACRO dividend_ttm_at =====
CREATE OR REPLACE MACRO dividend_ttm_at(d) AS TABLE
SELECT code, sum(bonus_amount_rmb) AS bonus_amount_wan_rmb, count_star() AS n_plan FROM dividend_visible_at(d) WHERE ((bonus_amount_rmb > 0) AND (visible_date > (d - to_days(CAST(trunc(CAST(365 AS DOUBLE)) AS INTEGER))))) GROUP BY code;

-- ===== MACRO dividend_visible_at =====
CREATE OR REPLACE MACRO dividend_visible_at(d) AS TABLE
SELECT * EXCLUDE (rn) FROM (SELECT v.*, row_number() OVER (PARTITION BY v.code, v.report_date, v.bonus_type ORDER BY v.visible_date DESC) AS rn FROM dividend AS v WHERE ((v.visible_date IS NOT NULL) AND (v.visible_date <= d) AND ((v.bonus_cancel_pub_date IS NULL) OR (v.bonus_cancel_pub_date > d)))) WHERE (rn = 1);

-- ===== MACRO fin_visible_at =====
CREATE OR REPLACE MACRO fin_visible_at(d) AS TABLE
SELECT * FROM (SELECT c.*, row_number() OVER (PARTITION BY c.code ORDER BY c.report_date DESC) AS rn FROM fin_core AS c WHERE ((c.report_date <= d) AND (c.pub_date <= d) AND (c.pub_date > c.report_date))) WHERE (rn = 1);

-- ===== MACRO index_members_at =====
CREATE OR REPLACE MACRO index_members_at(idx, d) AS TABLE
SELECT index_code, stock_code, valid_from, last_seen FROM index_member WHERE ((index_code = idx) AND (valid_from <= d) AND ((valid_to IS NULL) OR (valid_to >= d)));

