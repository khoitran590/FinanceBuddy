-- Richer bank-connection detail for spending insights, account balances, and daily
-- balance snapshots for net worth. Apply before deploying the matching app version.

alter table public.transactions
  add column if not exists merchant_name text,
  add column if not exists subcategory text,
  add column if not exists payment_channel text,
  add column if not exists location text,
  add column if not exists pending boolean not null default false;
alter table public.transactions
  add constraint transactions_detail_lengths check (
    (merchant_name is null or length(merchant_name) between 1 and 256)
    and (subcategory is null or length(subcategory) between 1 and 128)
    and (payment_channel is null or length(payment_channel) between 1 and 32)
    and (location is null or length(location) between 1 and 256)
  );

alter table public.plaid_accounts
  add column if not exists current_balance double precision,
  add column if not exists available_balance double precision,
  add column if not exists credit_limit double precision,
  add column if not exists iso_currency_code text,
  add column if not exists balances_updated_at timestamptz;
alter table public.plaid_accounts
  add constraint plaid_accounts_balances_finite check (
    coalesce(current_balance, 0) not in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)
    and coalesce(available_balance, 0) not in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)
    and coalesce(credit_limit, 0) not in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)
  );

-- One row per account per day. History belongs to the bank connection, not to the
-- account metadata rows, which are replaced on every sync.
create table if not exists public.account_balances (
  user_id uuid not null references auth.users(id) on delete cascade,
  account_id text not null,
  item_id text not null,
  as_of date not null,
  type text not null,
  current_balance double precision not null,
  available_balance double precision,
  credit_limit double precision,
  primary key (user_id, account_id, as_of),
  foreign key (user_id, item_id)
    references public.plaid_items(user_id, item_id) on delete cascade,
  constraint account_balances_finite check (
    current_balance not in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)
    and coalesce(available_balance, 0) not in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)
    and coalesce(credit_limit, 0) not in ('Infinity'::float8, '-Infinity'::float8, 'NaN'::float8)
  )
);

alter table public.account_balances enable row level security;
revoke all on public.account_balances from anon;
grant select, insert, update, delete on public.account_balances to authenticated;
drop policy if exists "owner_all" on public.account_balances;
create policy "owner_all" on public.account_balances for all to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

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
    user_id,account_id,item_id,name,official_name,type,subtype,mask,
    current_balance,available_balance,credit_limit,iso_currency_code,balances_updated_at
  )
  select v_uid,r.account_id,p_item_id,r.name,r.official_name,r.type,r.subtype,r.mask,
    r.current_balance,r.available_balance,r.credit_limit,r.iso_currency_code,
    case when r.current_balance is null then null else pg_catalog.now() end
  from pg_catalog.jsonb_to_recordset(p_rows) as r(
    account_id text,name text,official_name text,type text,subtype text,mask text,
    current_balance float8,available_balance float8,credit_limit float8,iso_currency_code text
  );
  get diagnostics v_count = row_count;
  insert into public.account_balances (
    user_id,account_id,item_id,as_of,type,current_balance,available_balance,credit_limit
  )
  select v_uid,r.account_id,p_item_id,(pg_catalog.now() at time zone 'utc')::date,r.type,
    r.current_balance,r.available_balance,r.credit_limit
  from pg_catalog.jsonb_to_recordset(p_rows) as r(
    account_id text,type text,current_balance float8,available_balance float8,credit_limit float8
  )
  where r.current_balance is not null
  on conflict (user_id,account_id,as_of) do update set
    type=excluded.type,
    current_balance=excluded.current_balance,
    available_balance=excluded.available_balance,
    credit_limit=excluded.credit_limit;
  return v_count;
end;
$$;
revoke all on function public.fb_replace_plaid_accounts(text,jsonb) from public, anon;
grant execute on function public.fb_replace_plaid_accounts(text,jsonb) to authenticated;

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
  insert into public.transactions (
    user_id,id,date,description,amount,category,account_name,account_type,
    merchant_name,subcategory,payment_channel,location,pending
  )
  select v_uid,r.id,r.date,r.description,r.amount,r.category,r.account_name,r.account_type,
    r.merchant_name,r.subcategory,r.payment_channel,r.location,coalesce(r.pending,false)
  from pg_catalog.jsonb_to_recordset(p_rows) as r(
    id text, date date, description text, amount float8, category text,
    account_name text, account_type text, merchant_name text, subcategory text,
    payment_channel text, location text, pending boolean
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
  insert into public.transactions (
    user_id,id,date,description,amount,category,account_name,account_type,
    merchant_name,subcategory,payment_channel,location,pending
  )
  values
    (v_uid,p_id || '-split-1',v_original.date,v_original.description || ' (split 1)',
      v_sign * p_first_amount,p_first_category,v_original.account_name,v_original.account_type,
      v_original.merchant_name,v_original.subcategory,v_original.payment_channel,
      v_original.location,v_original.pending),
    (v_uid,p_id || '-split-2',v_original.date,v_original.description || ' (split 2)',
      v_sign * p_second_amount,p_second_category,v_original.account_name,v_original.account_type,
      v_original.merchant_name,v_original.subcategory,v_original.payment_channel,
      v_original.location,v_original.pending);
  delete from public.transactions where user_id = v_uid and id = p_id;
end;
$$;
revoke all on function public.fb_split_transaction(text,text,float8,text,float8) from public, anon;
grant execute on function public.fb_split_transaction(text,text,float8,text,float8) to authenticated;

-- Keep Plaid transaction changes and the Item cursor in one transaction.
create or replace function public.fb_apply_plaid_sync(
  p_item_id text, p_expected_cursor text, p_next_cursor text,
  p_synced_at timestamptz, p_upserts jsonb, p_removed_ids jsonb
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_cursor text;
  v_upserted integer;
  v_removed integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if jsonb_typeof(p_upserts) is distinct from 'array'
    or jsonb_typeof(p_removed_ids) is distinct from 'array'
    or jsonb_array_length(p_upserts) > 50000
    or jsonb_array_length(p_removed_ids) > 50000
    or p_next_cursor is null or length(p_next_cursor) > 8192 then
    raise exception 'Invalid sync batch';
  end if;
  if exists (
    select 1 from pg_catalog.jsonb_array_elements_text(p_removed_ids) id
    where id not like 'plaid:%' or length(id) > 256
  ) then raise exception 'Invalid removed transaction'; end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text, 0));
  select cursor into v_cursor from public.plaid_items
    where user_id=v_uid and item_id=p_item_id for update;
  if not found then raise exception 'Bank connection not found'; end if;
  if v_cursor is distinct from p_expected_cursor then
    raise exception 'Bank cursor changed; retry sync';
  end if;
  insert into public.transactions (
    user_id,id,date,description,amount,category,account_name,account_type,
    merchant_name,subcategory,payment_channel,location,pending
  )
  select v_uid,r.id,r.date,r.description,r.amount,r.category,r.account_name,r.account_type,
    r.merchant_name,r.subcategory,r.payment_channel,r.location,coalesce(r.pending,false)
  from pg_catalog.jsonb_to_recordset(p_upserts) as r(
    id text,date date,description text,amount float8,category text,
    account_name text,account_type text,merchant_name text,subcategory text,
    payment_channel text,location text,pending boolean
  )
  on conflict (user_id,id) do update set
    date=excluded.date,
    description=excluded.description,
    amount=excluded.amount,
    category=case when public.transactions.category='Uncategorized'
      then excluded.category else public.transactions.category end,
    account_name=excluded.account_name,
    account_type=excluded.account_type,
    merchant_name=excluded.merchant_name,
    subcategory=excluded.subcategory,
    payment_channel=excluded.payment_channel,
    location=excluded.location,
    pending=excluded.pending;
  get diagnostics v_upserted = row_count;
  delete from public.transactions
    where user_id=v_uid and id in (
      select value from pg_catalog.jsonb_array_elements_text(p_removed_ids) value
    );
  get diagnostics v_removed = row_count;
  update public.plaid_items set
    cursor=p_next_cursor,last_synced_at=p_synced_at,status='connected',error_message=null
    where user_id=v_uid and item_id=p_item_id;
  return pg_catalog.jsonb_build_object('upserted',v_upserted,'removed',v_removed);
end;
$$;
revoke all on function public.fb_apply_plaid_sync(text,text,text,timestamptz,jsonb,jsonb)
  from public, anon;
grant execute on function public.fb_apply_plaid_sync(text,text,text,timestamptz,jsonb,jsonb)
  to authenticated;
