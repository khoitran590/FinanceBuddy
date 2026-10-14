-- Read-only operator report, AFTER 202610130002 and BEFORE 202610130003.
-- Run as the database administrator. Save privately with the pre-rollout snapshot.
with amounts as (
 select user_id,'transactions' collection,amount::text::numeric amount from public.transactions
 union all select user_id,'allocations',amount::text::numeric from public.transaction_allocations
 union all select user_id,'budgets',monthly_limit::text::numeric from public.budgets
 union all select user_id,'goal_target',target_amount::text::numeric from public.savings_goals
 union all select user_id,'goal_current',current_amount::text::numeric from public.savings_goals
 union all select user_id,'account_current',current_balance::text::numeric from public.plaid_accounts where current_balance is not null
 union all select user_id,'account_available',available_balance::text::numeric from public.plaid_accounts where available_balance is not null
 union all select user_id,'account_limit',credit_limit::text::numeric from public.plaid_accounts where credit_limit is not null
 union all select user_id,'history_current',current_balance::text::numeric from public.account_balances
 union all select user_id,'history_available',available_balance::text::numeric from public.account_balances where available_balance is not null
 union all select user_id,'history_limit',credit_limit::text::numeric from public.account_balances where credit_limit is not null
)
select user_id,collection,count(*) rows,sum(amount) original_total,sum(round(amount,2)) rounded_total,
 sum(round(amount,2))-sum(amount) expected_rounding_delta,
 count(*) filter(where amount<>round(amount,2)) values_rounded,
 count(*) filter(where abs(round(amount,2))>999999999999.99 or(collection in('allocations','goal_target') and round(amount,2)=0)) blocking_values
from amounts group by user_id,collection order by user_id,collection;
select user_id,count(*) non_usd_accounts from public.plaid_accounts where iso_currency_code is not null and iso_currency_code<>'USD' group by user_id;

-- These portable metadata bounds are also enforced by the next migration.
select user_id,count(*) invalid_settings from public.app_settings where length(key) not between 1 and 256 or length(value)>16384 group by user_id;
