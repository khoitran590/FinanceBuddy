-- Atomic financial writes. Functions run as the caller, so table grants and RLS apply.
-- Keep every table reference qualified and allow execution only to authenticated users.

alter table public.transactions
  add constraint transactions_amount_finite check (amount not in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)),
  add constraint transactions_field_lengths check (
    length(id) between 1 and 256 and length(description) between 1 and 2048
    and length(account_name) between 1 and 256 and length(category) between 1 and 128
    and length(account_type) between 1 and 64
  );
alter table public.budgets
  add constraint budgets_limit_finite check (monthly_limit not in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)),
  add constraint budgets_category_length check (length(category) between 1 and 128);
alter table public.savings_goals
  add constraint goals_amounts_finite check (
    target_amount not in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)
    and current_amount not in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)
  ),
  add constraint goals_name_length check (length(name) between 1 and 256);
alter table public.category_rules
  add constraint rules_field_lengths check (
    length(keyword) between 1 and 256 and length(category) between 1 and 128
  );

create or replace function public.fb_replace_transactions(p_account text, p_rows jsonb)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_count integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows) > 50000 then
    raise exception 'Invalid transaction list';
  end if;
  if p_account is not null and (length(p_account) < 1 or length(p_account) > 256) then
    raise exception 'Invalid account';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text, 0));
  -- Cast and validate the entire input before deleting any rows. A later SQL error
  -- also rolls back the complete function call.
  if exists (
    select 1 from pg_catalog.jsonb_to_recordset(p_rows) as r(
      id text, date date, description text, amount float8, category text,
      account_name text, account_type text
    ) where r.id is null or r.date is null or r.description is null or r.amount is null
      or r.category is null or r.account_name is null or r.account_type is null
      or (p_account is not null and r.account_name <> p_account)
  ) then raise exception 'Invalid transaction'; end if;
  if p_account is null then
    delete from public.transactions where user_id = v_uid;
  else
    delete from public.transactions where user_id = v_uid and account_name = p_account;
  end if;
  insert into public.transactions (user_id,id,date,description,amount,category,account_name,account_type)
  select v_uid,r.id,r.date,r.description,r.amount,r.category,r.account_name,r.account_type
  from pg_catalog.jsonb_to_recordset(p_rows) as r(
    id text, date date, description text, amount float8, category text,
    account_name text, account_type text
  );
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;
revoke all on function public.fb_replace_transactions(text,jsonb) from public, anon;
grant execute on function public.fb_replace_transactions(text,jsonb) to authenticated;

create or replace function public.fb_split_transaction(
  p_id text, p_first_category text, p_first_amount float8,
  p_second_category text, p_second_amount float8
)
returns void
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_original public.transactions%rowtype;
  v_sign integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if p_first_amount is null or p_second_amount is null
    or p_first_amount <= 0 or p_second_amount <= 0
    or p_first_amount in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)
    or p_second_amount in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)
    or length(p_first_category) not between 1 and 128
    or length(p_second_category) not between 1 and 128 then
    raise exception 'Invalid split';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text, 0));
  select * into v_original from public.transactions
    where user_id = v_uid and id = p_id for update;
  if not found then raise exception 'Transaction not found'; end if;
  if abs((p_first_amount + p_second_amount) - abs(v_original.amount)) > 0.005 then
    raise exception 'Split amounts do not equal transaction';
  end if;
  v_sign := case when v_original.amount < 0 then -1 else 1 end;
  insert into public.transactions (user_id,id,date,description,amount,category,account_name,account_type)
  values
    (v_uid,p_id || '-split-1',v_original.date,v_original.description || ' (split 1)',
      v_sign * p_first_amount,p_first_category,v_original.account_name,v_original.account_type),
    (v_uid,p_id || '-split-2',v_original.date,v_original.description || ' (split 2)',
      v_sign * p_second_amount,p_second_category,v_original.account_name,v_original.account_type);
  delete from public.transactions where user_id = v_uid and id = p_id;
end;
$$;
revoke all on function public.fb_split_transaction(text,text,float8,text,float8) from public, anon;
grant execute on function public.fb_split_transaction(text,text,float8,text,float8) to authenticated;

create or replace function public.fb_restore_backup(p_backup jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_transactions integer;
  v_budgets integer;
  v_goals integer;
  v_rules integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if jsonb_typeof(p_backup) is distinct from 'object' or p_backup->>'version' <> '1'
    or jsonb_typeof(p_backup->'transactions') is distinct from 'array'
    or jsonb_typeof(p_backup->'budgets') is distinct from 'array'
    or jsonb_typeof(p_backup->'goals') is distinct from 'array'
    or jsonb_typeof(p_backup->'category_rules') is distinct from 'array' then
    raise exception 'Invalid backup';
  end if;
  if jsonb_array_length(p_backup->'transactions') > 50000
    or jsonb_array_length(p_backup->'budgets') > 50000
    or jsonb_array_length(p_backup->'goals') > 50000
    or jsonb_array_length(p_backup->'category_rules') > 50000 then
    raise exception 'Backup too large';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text, 0));
  v_transactions := public.fb_replace_transactions(null, p_backup->'transactions');
  delete from public.budgets where user_id = v_uid;
  delete from public.savings_goals where user_id = v_uid;
  delete from public.category_rules where user_id = v_uid;
  insert into public.budgets (user_id,category,monthly_limit)
  select v_uid,r.category,r.monthly_limit
  from pg_catalog.jsonb_to_recordset(p_backup->'budgets') as r(category text, monthly_limit float8);
  get diagnostics v_budgets = row_count;
  insert into public.savings_goals (user_id,name,target_amount,current_amount,target_date)
  select v_uid,r.name,r.target_amount,r.current_amount,r.target_date
  from pg_catalog.jsonb_to_recordset(p_backup->'goals') as r(
    name text,target_amount float8,current_amount float8,target_date date
  );
  get diagnostics v_goals = row_count;
  insert into public.category_rules (user_id,keyword,category)
  select v_uid,r.keyword,r.category
  from pg_catalog.jsonb_to_recordset(p_backup->'category_rules') as r(keyword text,category text);
  get diagnostics v_rules = row_count;
  return pg_catalog.jsonb_build_object(
    'transactions',v_transactions,'budgets',v_budgets,'goals',v_goals,'category_rules',v_rules
  );
end;
$$;
revoke all on function public.fb_restore_backup(jsonb) from public, anon;
grant execute on function public.fb_restore_backup(jsonb) to authenticated;

create or replace function public.fb_replace_plaid_accounts(p_item_id text, p_rows jsonb)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_count integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows) > 100 then
    raise exception 'Invalid bank account list';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text, 0));
  if not exists (select 1 from public.plaid_items where user_id = v_uid and item_id = p_item_id) then
    raise exception 'Bank connection not found';
  end if;
  delete from public.plaid_accounts where user_id = v_uid and item_id = p_item_id;
  insert into public.plaid_accounts (
    user_id,account_id,item_id,name,official_name,type,subtype,mask
  )
  select v_uid,r.account_id,p_item_id,r.name,r.official_name,r.type,r.subtype,r.mask
  from pg_catalog.jsonb_to_recordset(p_rows) as r(
    account_id text,name text,official_name text,type text,subtype text,mask text
  );
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;
revoke all on function public.fb_replace_plaid_accounts(text,jsonb) from public, anon;
grant execute on function public.fb_replace_plaid_accounts(text,jsonb) to authenticated;

create or replace function public.fb_undo_append(p_rows jsonb)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_expected record;
  v_actual public.transactions%rowtype;
  v_count integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows) > 50000 then
    raise exception 'Invalid undo list';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text, 0));
  for v_expected in
    select * from pg_catalog.jsonb_to_recordset(p_rows) as r(
      id text,date date,description text,amount float8,category text,
      account_name text,account_type text
    )
  loop
    select * into v_actual from public.transactions
      where user_id = v_uid and id = v_expected.id for update;
    if not found or v_actual.date is distinct from v_expected.date
      or v_actual.description is distinct from v_expected.description
      or v_actual.amount is distinct from v_expected.amount
      or v_actual.category is distinct from v_expected.category
      or v_actual.account_name is distinct from v_expected.account_name
      or v_actual.account_type is distinct from v_expected.account_type then
      raise exception 'Imported transactions changed; cannot undo';
    end if;
  end loop;
  delete from public.transactions where user_id = v_uid and id in (
    select r.id from pg_catalog.jsonb_to_recordset(p_rows) as r(id text)
  );
  get diagnostics v_count = row_count;
  if v_count <> jsonb_array_length(p_rows) then
    raise exception 'Invalid undo list';
  end if;
  return v_count;
end;
$$;
revoke all on function public.fb_undo_append(jsonb) from public, anon;
grant execute on function public.fb_undo_append(jsonb) to authenticated;
