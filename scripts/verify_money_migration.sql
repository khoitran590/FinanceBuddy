-- Run immediately AFTER 202610130003, BEFORE accepting writes or deploying.
-- No financial records are modified. Any unplanned row/value change fails.
begin;
create temporary table fb_money_reconciliation on commit drop as
with current_amounts as (
 select user_id,'transactions' collection,id record_id,amount from public.transactions
 union all select user_id,'allocations',id::text,amount from public.transaction_allocations
 union all select user_id,'budgets',category,monthly_limit from public.budgets
 union all select user_id,'goal_target',id::text,target_amount from public.savings_goals
 union all select user_id,'goal_current',id::text,current_amount from public.savings_goals
 union all select user_id,'account_current',account_id,current_balance from public.plaid_accounts where current_balance is not null
 union all select user_id,'account_available',account_id,available_balance from public.plaid_accounts where available_balance is not null
 union all select user_id,'account_limit',account_id,credit_limit from public.plaid_accounts where credit_limit is not null
 union all select user_id,'history_current',account_id||':'||as_of::text,current_balance from public.account_balances
 union all select user_id,'history_available',account_id||':'||as_of::text,available_balance from public.account_balances where available_balance is not null
 union all select user_id,'history_limit',account_id||':'||as_of::text,credit_limit from public.account_balances where credit_limit is not null
)
select coalesce(a.user_id,b.user_id) user_id,coalesce(a.collection,b.collection) collection,
 count(a.record_id) before_rows,count(b.record_id) after_rows,sum(a.original_amount::numeric) original_total,
 sum(a.rounded_amount) expected_total,sum(b.amount) actual_total,
 count(*) filter(where a.record_id is null or b.record_id is null or a.rounded_amount is distinct from b.amount) mismatches
from financebuddy_private.money_migration_audit a full join current_amounts b using(user_id,collection,record_id)
group by coalesce(a.user_id,b.user_id),coalesce(a.collection,b.collection);
select * from fb_money_reconciliation order by user_id,collection;
do $$ begin
 if exists(select 1 from fb_money_reconciliation where mismatches<>0 or before_rows<>after_rows or expected_total is distinct from actual_total) then
  raise exception 'Money migration reconciliation failed; keep the app stopped and investigate'; end if;
end $$;
rollback;
