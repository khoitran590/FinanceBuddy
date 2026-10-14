-- Stop the app and export a recoverable backup before applying. See README.
-- Existing fractional cents round HALF_UP (away from zero at ties); retain audit.
create table financebuddy_private.money_migration_audit (
  user_id uuid not null, collection text not null, record_id text not null,
  original_amount text not null, rounded_amount numeric not null,
  primary key(user_id,collection,record_id)
);
alter table financebuddy_private.money_migration_audit enable row level security;
revoke all on financebuddy_private.money_migration_audit from public,anon,authenticated;
insert into financebuddy_private.money_migration_audit
 select user_id,'transactions',id,amount::text,round(amount::text::numeric,2) from public.transactions
 union all select user_id,'allocations',id::text,amount::text,round(amount::text::numeric,2) from public.transaction_allocations
 union all select user_id,'budgets',category,monthly_limit::text,round(monthly_limit::text::numeric,2) from public.budgets
 union all select user_id,'goal_target',id::text,target_amount::text,round(target_amount::text::numeric,2) from public.savings_goals
 union all select user_id,'goal_current',id::text,current_amount::text,round(current_amount::text::numeric,2) from public.savings_goals
 union all select user_id,'account_current',account_id,current_balance::text,round(current_balance::text::numeric,2) from public.plaid_accounts where current_balance is not null
 union all select user_id,'account_available',account_id,available_balance::text,round(available_balance::text::numeric,2) from public.plaid_accounts where available_balance is not null
 union all select user_id,'account_limit',account_id,credit_limit::text,round(credit_limit::text::numeric,2) from public.plaid_accounts where credit_limit is not null
 union all select user_id,'history_current',account_id||':'||as_of::text,current_balance::text,round(current_balance::text::numeric,2) from public.account_balances
 union all select user_id,'history_available',account_id||':'||as_of::text,available_balance::text,round(available_balance::text::numeric,2) from public.account_balances where available_balance is not null
 union all select user_id,'history_limit',account_id||':'||as_of::text,credit_limit::text,round(credit_limit::text::numeric,2) from public.account_balances where credit_limit is not null;
do $$ begin
 if exists(select 1 from public.plaid_accounts where iso_currency_code is not null and iso_currency_code<>'USD') then
  raise exception 'Non-USD bank balances require reconciliation before migrating'; end if;
end $$;
alter table public.transactions drop column import_match_key;
alter table public.transactions alter column amount type numeric using round(amount::text::numeric,2);
alter table public.transaction_allocations alter column amount type numeric using round(amount::text::numeric,2);
alter table public.budgets alter column monthly_limit type numeric using round(monthly_limit::text::numeric,2);
alter table public.savings_goals alter column target_amount type numeric using round(target_amount::text::numeric,2),
 alter column current_amount type numeric using round(current_amount::text::numeric,2);
alter table public.plaid_accounts alter column current_balance type numeric using round(current_balance::text::numeric,2),
 alter column available_balance type numeric using round(available_balance::text::numeric,2),
 alter column credit_limit type numeric using round(credit_limit::text::numeric,2);
alter table public.account_balances alter column current_balance type numeric using round(current_balance::text::numeric,2),
 alter column available_balance type numeric using round(available_balance::text::numeric,2),
 alter column credit_limit type numeric using round(credit_limit::text::numeric,2);
create function public.fb_money_valid(p_value numeric) returns boolean language sql immutable
 set search_path='' as $$ select p_value is null or (abs(p_value)<=999999999999.99 and p_value=round(p_value,2)) $$;
revoke all on function public.fb_money_valid(numeric) from public,anon;
grant execute on function public.fb_money_valid(numeric) to authenticated;
alter table public.transactions add constraint transaction_exact_cents check(public.fb_money_valid(amount));
alter table public.transaction_allocations add constraint allocation_exact_cents check(public.fb_money_valid(amount));
alter table public.budgets add constraint budget_exact_cents check(public.fb_money_valid(monthly_limit));
alter table public.savings_goals add constraint goal_exact_cents check(public.fb_money_valid(target_amount) and public.fb_money_valid(current_amount));
alter table public.plaid_accounts add constraint bank_exact_cents check(public.fb_money_valid(current_balance) and public.fb_money_valid(available_balance) and public.fb_money_valid(credit_limit));
alter table public.account_balances add constraint balance_exact_cents check(public.fb_money_valid(current_balance) and public.fb_money_valid(available_balance) and public.fb_money_valid(credit_limit));
alter table public.transactions add column currency text not null default 'USD' check(currency='USD');
alter table public.account_balances add column currency text not null default 'USD' check(currency='USD');
update public.plaid_accounts set iso_currency_code='USD' where iso_currency_code is null;
alter table public.plaid_accounts alter column iso_currency_code set default 'USD',
 alter column iso_currency_code set not null,
 add constraint bank_usd_only check(iso_currency_code='USD');

create table public.accounts (
 user_id uuid not null references auth.users(id) on delete cascade,
 id uuid not null default gen_random_uuid(), name text not null check(length(name) between 1 and 256),
 account_type text not null check(length(account_type) between 1 and 64), currency text not null default 'USD' check(currency='USD'),
 institution_name text, plaid_item_id text, plaid_account_id text, statement_key text,
 primary key(user_id,id), check((plaid_item_id is null)=(plaid_account_id is null))
);
create unique index accounts_provider_identity on public.accounts(user_id,plaid_item_id,plaid_account_id) where plaid_account_id is not null;
create unique index accounts_statement_identity on public.accounts(user_id,statement_key) where statement_key is not null;
alter table public.accounts enable row level security;
revoke all on public.accounts from public,anon;
grant select,insert,update,delete on public.accounts to authenticated;
create policy owner_all on public.accounts for all to authenticated using((select auth.uid())=user_id) with check((select auth.uid())=user_id);
alter table public.transactions add column account_id uuid,
 add column category_origin text not null default 'legacy' check(category_origin in ('legacy','manual','rule','provider','builtin')),
 add column builtin_category text check(length(builtin_category) between 1 and 128),
 add column provider_category text check(length(provider_category) between 1 and 128),
 add column category_override text check(length(category_override) between 1 and 128),
 add column category_rule text check(length(category_rule) between 1 and 256),
 add column category_rule_version bigint check(category_rule_version>=1),
 add column pending_transaction_id text check(length(pending_transaction_id) between 1 and 256),
 add column revision bigint not null default 1 check(revision>0),
 add constraint transaction_owned_account foreign key(user_id,account_id) references public.accounts(user_id,id);
alter table public.category_rules add column version bigint not null default 1;
create function public.fb_assign_account() returns trigger language plpgsql security invoker set search_path='' as $$
declare v_id uuid; v_key text;
begin
 if new.account_id is not null then
  if not exists(select 1 from public.accounts where user_id=new.user_id and id=new.account_id) then raise exception 'Account not owned'; end if;
 elsif new.plaid_item_id is not null and new.plaid_account_id is not null then
  insert into public.accounts(user_id,name,account_type,plaid_item_id,plaid_account_id)
   values(new.user_id,new.account_name,new.account_type,new.plaid_item_id,new.plaid_account_id)
   on conflict(user_id,plaid_item_id,plaid_account_id) where plaid_account_id is not null
   do update set name=excluded.name,account_type=excluded.account_type returning id into v_id;
  new.account_id:=v_id;
 else
  v_key:=public.fb_import_normalize(new.account_name)||chr(31)||public.fb_import_normalize(new.account_type);
  insert into public.accounts(user_id,name,account_type,statement_key) values(new.user_id,new.account_name,new.account_type,v_key)
   on conflict(user_id,statement_key) where statement_key is not null do update set name=excluded.name returning id into v_id;
  new.account_id:=v_id;
 end if;
 if tg_op='UPDATE' then new.revision:=old.revision+1; end if;
 return new;
end $$;
revoke all on function public.fb_assign_account() from public,anon;
grant execute on function public.fb_assign_account() to authenticated;
create trigger transaction_account_revision before insert or update on public.transactions for each row execute function public.fb_assign_account();
update public.transactions set account_name=account_name;
alter table public.transactions alter column account_id set not null;
create index transactions_owner_account_date_idx on public.transactions(user_id,account_id,date desc,id desc);
create index transactions_owner_date_id_idx on public.transactions(user_id,date desc,id desc);
-- Stable identity joins use this numeric key. Legacy five-argument calls remain compatible.
create or replace function public.fb_import_match_key(p_date date,p_amount float8,p_description text,p_account text,p_type text)
returns text language sql immutable security invoker set search_path='' as $$
 select encode(sha256(convert_to(jsonb_build_array(p_date,round(p_amount::numeric,2)::text,public.fb_import_normalize(p_description))::text,'UTF8')),'hex') $$;
alter table public.transactions add column import_match_key text generated always as(public.fb_import_match_key(date,amount::float8,description,account_name,account_type)) stored;
create index transactions_import_match_idx on public.transactions(user_id,account_id,import_match_key,id);
-- Allocations are edits to their source, including category-only edits.
create function public.fb_allocation_revision() returns trigger language plpgsql security invoker set search_path='' as $$
begin
 update public.transactions set revision=revision where user_id=coalesce(new.user_id,old.user_id) and id=coalesce(new.transaction_id,old.transaction_id);
 return coalesce(new,old);
end $$;
revoke all on function public.fb_allocation_revision() from public,anon;
grant execute on function public.fb_allocation_revision() to authenticated;
create trigger allocation_revision after insert or update or delete on public.transaction_allocations for each row execute function public.fb_allocation_revision();

-- No float parameters or tolerance for a split: both amounts must be exact cents.
drop function public.fb_split_transaction(text,text,float8,text,float8);
create function public.fb_split_transaction(p_id text,p_first_category text,p_first_amount numeric,p_second_category text,p_second_amount numeric)
returns void language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_original public.transactions%rowtype; v_sign integer;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 if p_first_amount is null or p_second_amount is null or p_first_amount<=0 or p_second_amount<=0
  or not public.fb_money_valid(p_first_amount) or not public.fb_money_valid(p_second_amount)
  or p_first_category is null or p_second_category is null or length(p_first_category) not between 1 and 128
  or length(p_second_category) not between 1 and 128 then raise exception 'Invalid split'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 select * into v_original from public.transactions where user_id=v_uid and id=p_id for update;
 if not found then raise exception 'Transaction not found'; end if;
 if p_first_amount+p_second_amount<>abs(v_original.amount) then raise exception 'Split amounts do not equal transaction'; end if;
 v_sign:=case when v_original.amount<0 then -1 else 1 end;
 delete from public.transaction_allocations where user_id=v_uid and transaction_id=p_id;
 insert into public.transaction_allocations(user_id,transaction_id,amount,category) values
  (v_uid,p_id,v_sign*p_first_amount,p_first_category),(v_uid,p_id,v_sign*p_second_amount,p_second_category);
end $$;
revoke all on function public.fb_split_transaction(text,text,numeric,text,numeric) from public,anon;
grant execute on function public.fb_split_transaction(text,text,numeric,text,numeric) to authenticated;

-- Name compatibility is limited to unambiguous labels. New UI sends owned IDs.
create function public.fb_statement_account(p_row jsonb) returns table(id uuid)
language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_id uuid; v_count integer; v_key text;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 if p_row->>'account_id' is not null then
  select a.id into v_id from public.accounts a where a.user_id=v_uid and a.id=(p_row->>'account_id')::uuid;
  if not found then raise exception 'Account not owned'; end if;
 else
  select count(*),min(a.id::text)::uuid into v_count,v_id from public.accounts a where a.user_id=v_uid
   and public.fb_import_normalize(a.name)=public.fb_import_normalize(p_row->>'account_name')
   and public.fb_import_normalize(a.account_type)=public.fb_import_normalize(p_row->>'account_type');
  if v_count>1 then raise exception 'Select an account ID; this label is ambiguous'; end if;
  if v_count=0 then
   v_key:=public.fb_import_normalize(p_row->>'account_name')||chr(31)||public.fb_import_normalize(p_row->>'account_type');
   insert into public.accounts(user_id,name,account_type,statement_key) values(v_uid,p_row->>'account_name',p_row->>'account_type',v_key)
    on conflict(user_id,statement_key) where statement_key is not null do update set name=excluded.name returning accounts.id into v_id;
  end if;
 end if;
 return query select v_id;
end $$;
revoke all on function public.fb_statement_account(jsonb) from public,anon;
grant execute on function public.fb_statement_account(jsonb) to authenticated;
drop index public.transactions_statement_reference_idx;
create unique index transactions_statement_reference_idx on public.transactions(user_id,account_id,statement_reference) where statement_reference is not null;
create or replace function public.fb_append_statement(p_rows jsonb,p_keep_matching boolean default false)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
  v_uid uuid := (select auth.uid()); r record; v_selected public.transactions%rowtype;
  v_match text; v_inserted integer := 0; v_matched integer := 0; v_result jsonb;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if pg_catalog.jsonb_typeof(p_rows) is distinct from 'array' or pg_catalog.jsonb_array_length(p_rows)>50000
    or p_keep_matching is null or exists(select 1 from jsonb_array_elements(p_rows) where coalesce(value->>'currency','USD')<>'USD' or not public.fb_money_valid((value->>'amount')::numeric)) then raise exception 'Invalid statement'; end if;
  if exists(select 1 from pg_catalog.jsonb_to_recordset(p_rows) as x(
      id text,date date,description text,amount numeric,category text,account_name text,account_type text,
      statement_file_id text,statement_row integer)
    where x.id is null or x.id not like 'statement:%' or x.date is null or x.amount is null
      or x.description is null or x.category is null or x.account_name is null or x.account_type is null
      or x.statement_file_id is null or x.statement_file_id !~ '^[a-f0-9]{64}$'
      or x.statement_row is null or x.statement_row not between 1 and 50000)
    or (select count(distinct value->>'statement_file_id') from pg_catalog.jsonb_array_elements(p_rows))>1
    or exists(select 1 from pg_catalog.jsonb_to_recordset(p_rows) as x(statement_row integer)
      group by x.statement_row having count(*)>1) then raise exception 'Invalid statement row identity'; end if;
  select coalesce(jsonb_agg(x.value || jsonb_build_object('account_id',a.id)),'[]'::jsonb) into p_rows
    from jsonb_array_elements(p_rows) x cross join lateral public.fb_statement_account(x.value) a;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text,0));
  -- Local indexed occurrence consumption avoids repeatedly copying large JSON
  -- arrays or testing an ever-growing list of IDs. It disappears at commit.
  create temporary table if not exists fb_statement_used(id text primary key,inserted boolean not null default false) on commit drop;
  truncate table pg_temp.fb_statement_used;
  -- Reserve all authoritative matches before assigning fuzzy occurrences.
  with incoming as materialized (
    select * from pg_catalog.jsonb_to_recordset(p_rows) as x(
      id text,statement_file_id text,statement_row integer,statement_reference text,account_name text,account_type text,account_id uuid)
  )
  insert into pg_temp.fb_statement_used(id)
    select t.id from public.transactions t join incoming x on t.id=x.id where t.user_id=v_uid
    union select m.transaction_id from public.statement_import_rows m join incoming x
      on m.file_id=x.statement_file_id and m.row_number=x.statement_row where m.user_id=v_uid
    union select t.id from public.transactions t join incoming x
      on t.statement_file_id=x.statement_file_id and t.statement_row=x.statement_row where t.user_id=v_uid
    union select t.id from public.transactions t join incoming x
      on t.account_id=x.account_id and t.statement_reference=x.statement_reference
      and public.fb_import_normalize(t.account_name)=public.fb_import_normalize(x.account_name)
      and public.fb_import_normalize(t.account_type)=public.fb_import_normalize(x.account_type)
      where t.user_id=v_uid and x.statement_reference is not null
    on conflict do nothing;
  create temporary table if not exists fb_statement_pool(id text primary key,match_key text,reference text,account_id uuid) on commit drop;
  create index if not exists fb_statement_pool_match_idx on pg_temp.fb_statement_pool(match_key,id);
  create index if not exists fb_statement_pool_unreferenced_idx on pg_temp.fb_statement_pool(match_key,id) where reference is null;
  truncate table pg_temp.fb_statement_pool;
  insert into pg_temp.fb_statement_pool(id,match_key,reference,account_id)
    select t.id,t.import_match_key,t.statement_reference,t.account_id from public.transactions t
    where t.user_id=v_uid and not exists(select 1 from pg_temp.fb_statement_used u where u.id=t.id)
      and t.import_match_key in (select public.fb_import_match_key(x.date,x.amount::float8,x.description,x.account_name,x.account_type)
        from pg_catalog.jsonb_to_recordset(p_rows) as x(date date,amount numeric,description text,account_name text,account_type text));
  for r in select * from pg_catalog.jsonb_to_recordset(p_rows) as x(
    id text,date date,description text,amount numeric,category text,account_name text,account_type text,
    statement_file_id text,statement_row integer,statement_reference text,account_id uuid,
    category_origin text,provider_category text,builtin_category text,category_rule text,category_rule_version bigint)
  loop
    v_match := public.fb_import_match_key(r.date,r.amount::float8,r.description,r.account_name,r.account_type);
    select t.* into v_selected from public.statement_import_rows m join public.transactions t
      on t.user_id=m.user_id and t.id=m.transaction_id
      where m.user_id=v_uid and m.file_id=r.statement_file_id and m.row_number=r.statement_row;
    if not found then
      select * into v_selected from public.transactions where user_id=v_uid and id=r.id;
    end if;
    if not found then
      select * into v_selected from public.transactions where user_id=v_uid
        and statement_file_id=r.statement_file_id and statement_row=r.statement_row;
    end if;
    if v_selected.id is not null and v_selected.account_id<>r.account_id then raise exception 'Statement belongs to a different account'; end if;
    if not found and r.statement_reference is not null then
      select * into v_selected from public.transactions t where t.user_id=v_uid
        and t.account_id=r.account_id and t.statement_reference=r.statement_reference
        and public.fb_import_normalize(t.account_name)=public.fb_import_normalize(r.account_name)
        and public.fb_import_normalize(t.account_type)=public.fb_import_normalize(r.account_type);
      if found and v_selected.import_match_key<>v_match then
        raise exception 'A bank reference already exists with different transaction details';
      end if;
    end if;
    if v_selected.id is null and not p_keep_matching then
      if r.statement_reference is null then
        select t.* into v_selected from pg_temp.fb_statement_pool p join public.transactions t on t.user_id=v_uid and t.id=p.id
          where p.account_id=r.account_id and p.match_key=v_match order by p.id limit 1;
      else
        select t.* into v_selected from pg_temp.fb_statement_pool p join public.transactions t on t.user_id=v_uid and t.id=p.id
          where p.account_id=r.account_id and p.match_key=v_match and p.reference is null order by p.id limit 1;
      end if;
    end if;
    if v_selected.id is null then
      insert into public.transactions(user_id,id,date,description,amount,category,account_name,account_type,
        statement_file_id,statement_row,statement_reference,statement_match_bank,account_id,category_origin,provider_category,builtin_category,category_rule,category_rule_version)
      values(v_uid,r.id,r.date,r.description,r.amount,r.category,r.account_name,r.account_type,
        r.statement_file_id,r.statement_row,r.statement_reference,not p_keep_matching,r.account_id,coalesce(r.category_origin,'legacy'),r.provider_category,r.builtin_category,r.category_rule,r.category_rule_version) returning * into v_selected;
      insert into pg_temp.fb_statement_used(id,inserted) values(v_selected.id,true);
      v_inserted:=v_inserted+1;
    else
      v_matched:=v_matched+1;
      insert into pg_temp.fb_statement_used(id) values(v_selected.id) on conflict do nothing;
      if r.statement_reference is not null and v_selected.statement_reference is null then
        update public.transactions set statement_reference=r.statement_reference
          where user_id=v_uid and id=v_selected.id;
      end if;
      if not p_keep_matching and v_selected.id not like 'plaid:%' and v_selected.statement_file_id is null then
        update public.transactions set statement_file_id=r.statement_file_id,statement_row=r.statement_row,statement_match_bank=true
          where user_id=v_uid and id=v_selected.id;
      end if;
    end if;
    delete from pg_temp.fb_statement_pool where id=v_selected.id;
    insert into public.statement_import_rows(user_id,file_id,row_number,transaction_id)
      values(v_uid,r.statement_file_id,r.statement_row,v_selected.id)
      on conflict(user_id,file_id,row_number) do update set transaction_id=excluded.transaction_id;
  end loop;
  select coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(t)),'[]'::jsonb) into v_result
    from public.transactions t join pg_temp.fb_statement_used u on u.id=t.id
    where t.user_id=v_uid and u.inserted;
  return pg_catalog.jsonb_build_object('inserted',v_inserted,'matched',v_matched,'inserted_rows',v_result);
end $$;
revoke all on function public.fb_append_statement(jsonb,boolean) from public,anon;
grant execute on function public.fb_append_statement(jsonb,boolean) to authenticated;


create function public.fb_bank_account(p_item text,p_provider text,p_name text,p_type text) returns uuid
language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_id uuid; v_count integer;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 if p_provider is null then
  select id into v_id from public.fb_statement_account(jsonb_build_object('account_name',p_name,'account_type',p_type));
  return v_id;
 end if;
 select id into v_id from public.accounts where user_id=v_uid and plaid_item_id=p_item and plaid_account_id=p_provider;
 if not found then
  select count(*),min(id::text)::uuid into v_count,v_id from public.accounts where user_id=v_uid
   and public.fb_import_normalize(name)=public.fb_import_normalize(p_name) and public.fb_import_normalize(account_type)=public.fb_import_normalize(p_type);
  if v_count=1 and exists(select 1 from public.accounts where user_id=v_uid and id=v_id and plaid_item_id is null) then
   update public.accounts set plaid_item_id=p_item,plaid_account_id=p_provider,statement_key=null where user_id=v_uid and id=v_id;
  else
   insert into public.accounts(user_id,name,account_type,plaid_item_id,plaid_account_id)
    values(v_uid,p_name,p_type,p_item,p_provider) returning id into v_id;
  end if;
 end if;
 update public.accounts set name=p_name,account_type=p_type,institution_name=coalesce((select institution_name from public.plaid_items where user_id=v_uid and item_id=p_item),institution_name) where user_id=v_uid and id=v_id;
 return v_id;
end $$;
revoke all on function public.fb_bank_account(text,text,text,text) from public,anon;
grant execute on function public.fb_bank_account(text,text,text,text) to authenticated;

create or replace function public.fb_apply_plaid_sync(
 p_item_id text,p_expected_cursor text,p_next_cursor text,p_synced_at timestamptz,
 p_upserts jsonb,p_removed_ids jsonb,p_expected_generation bigint default 0,p_reconcile boolean default false)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_item public.plaid_items%rowtype; r jsonb;
 v_account uuid; v_old public.transactions%rowtype; v_match public.transactions%rowtype;
 v_category text; v_origin text; v_rule public.category_rules%rowtype;
 v_removed integer:=0; v_pruned integer:=0; v_matched integer:=0; v_seen text[]:='{}'; v_source text;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 if jsonb_typeof(p_upserts) is distinct from 'array' or jsonb_typeof(p_removed_ids) is distinct from 'array'
  or jsonb_array_length(p_upserts)>50000 or jsonb_array_length(p_removed_ids)>50000 or p_next_cursor is null or length(p_next_cursor)>8192
  or exists(select 1 from jsonb_array_elements_text(p_removed_ids) where value not like 'plaid:%' or length(value)>256)
  or exists(select 1 from jsonb_array_elements(p_upserts) where value->>'id' not like 'plaid:%' or value->>'id' is null
   or coalesce(value->>'currency','USD')<>'USD' or not public.fb_money_valid((value->>'amount')::numeric)) then raise exception 'Invalid sync batch'; end if;
 if exists(select 1 from jsonb_array_elements(p_upserts) group by value->>'id' having count(*)>1) then raise exception 'Duplicate upserts' using errcode='21000'; end if;
 if exists(select 1 from jsonb_array_elements(p_upserts) where value->>'pending_transaction_id' is not null
  group by value->>'pending_transaction_id' having count(*)>1) then raise exception 'Ambiguous pending lineage'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 select * into v_item from public.plaid_items where user_id=v_uid and item_id=p_item_id for update;
 if not found or v_item.status='disconnecting' then raise exception 'Bank connection unavailable'; end if;
 if v_item.cursor is distinct from p_expected_cursor or v_item.sync_generation<>p_expected_generation or v_item.needs_reconciliation is distinct from p_reconcile then raise exception 'Bank state changed; retry sync'; end if;
 for r in select value from jsonb_array_elements(p_upserts) loop
  v_account:=public.fb_bank_account(p_item_id,r->>'plaid_account_id',r->>'account_name',r->>'account_type');
  select * into v_old from public.transactions where user_id=v_uid and id=r->>'id';
  v_source:=null;
  if v_old.id is not null and v_old.plaid_item_id is not null and v_old.plaid_item_id<>p_item_id then raise exception 'Transaction belongs to another bank'; end if;
  if v_old.id is null and r->>'pending_transaction_id' is not null then
   select * into v_old from public.transactions where user_id=v_uid and id=r->>'pending_transaction_id'
    and plaid_item_id=p_item_id and account_id=v_account and pending;
   if found then v_source:=v_old.id; end if;
  end if;
  if v_old.id is null then
   select * into v_match from public.transactions where user_id=v_uid and account_id=v_account
    and statement_match_bank and statement_file_id is not null and plaid_item_id is null
    and import_match_key=public.fb_import_match_key((r->>'date')::date,(r->>'amount')::float8,r->>'description',r->>'account_name',r->>'account_type')
    order by id limit 1;
   if found then v_old:=v_match; v_source:=v_old.id; v_matched:=v_matched+1; end if;
  end if;
  select * into v_rule from public.category_rules where user_id=v_uid and strpos(lower(r->>'description'),lower(keyword))>0 order by keyword limit 1;
  if v_old.category_override is not null then v_category:=v_old.category_override; v_origin:='manual';
  elsif v_old.category_origin='legacy' and v_old.category<>'Uncategorized' then v_category:=v_old.category; v_origin:='legacy';
  elsif v_rule.keyword is not null then v_category:=v_rule.category; v_origin:='rule';
  elsif r->>'provider_category' is not null then v_category:=r->>'provider_category'; v_origin:='provider';
  else v_category:=r->>'category'; v_origin:=coalesce(r->>'category_origin','legacy'); end if;
  if v_source is not null then
   update public.transactions set statement_file_id=null,statement_row=null,statement_reference=null where user_id=v_uid and id=v_source;
  end if;
  insert into public.transactions(user_id,id,date,description,amount,category,account_name,account_type,account_id,
   merchant_name,subcategory,payment_channel,location,pending,plaid_item_id,plaid_account_id,
   category_origin,provider_category,builtin_category,category_override,category_rule,category_rule_version,pending_transaction_id,
   statement_file_id,statement_row,statement_reference,statement_match_bank)
  values(v_uid,r->>'id',(r->>'date')::date,r->>'description',(r->>'amount')::numeric,v_category,r->>'account_name',r->>'account_type',v_account,
   r->>'merchant_name',r->>'subcategory',r->>'payment_channel',r->>'location',coalesce((r->>'pending')::boolean,false),p_item_id,r->>'plaid_account_id',
   v_origin,r->>'provider_category',r->>'builtin_category',v_old.category_override,case when v_origin='rule' then v_rule.keyword end,case when v_origin='rule' then v_rule.version end,r->>'pending_transaction_id',
   v_old.statement_file_id,v_old.statement_row,v_old.statement_reference,coalesce(v_old.statement_match_bank,false))
  on conflict(user_id,id) do update set date=excluded.date,description=excluded.description,amount=excluded.amount,category=excluded.category,
   account_name=excluded.account_name,account_type=excluded.account_type,account_id=excluded.account_id,
   merchant_name=excluded.merchant_name,subcategory=excluded.subcategory,payment_channel=excluded.payment_channel,location=excluded.location,
   pending=excluded.pending,plaid_item_id=excluded.plaid_item_id,plaid_account_id=excluded.plaid_account_id,
   category_origin=excluded.category_origin,provider_category=excluded.provider_category,builtin_category=excluded.builtin_category,category_override=excluded.category_override,
   category_rule=excluded.category_rule,category_rule_version=excluded.category_rule_version,pending_transaction_id=excluded.pending_transaction_id;
  if v_source is not null then
   update public.transaction_allocations set transaction_id=r->>'id' where user_id=v_uid and transaction_id=v_source;
   update public.statement_import_rows set transaction_id=r->>'id' where user_id=v_uid and transaction_id=v_source;
   delete from public.transactions where user_id=v_uid and id=v_source;
  end if;
  v_seen:=array_append(v_seen,r->>'id');
 end loop;
 delete from public.transactions where user_id=v_uid and (plaid_item_id=p_item_id or plaid_item_id is null)
  and id in(select value from jsonb_array_elements_text(p_removed_ids));
 get diagnostics v_removed=row_count;
 if p_reconcile then
  delete from public.transactions where user_id=v_uid and plaid_item_id=p_item_id and not(id=any(v_seen));
  get diagnostics v_pruned=row_count;
 end if;
 update public.plaid_items set cursor=p_next_cursor,last_synced_at=p_synced_at,status='connected',error_message=null,needs_reconciliation=false where user_id=v_uid and item_id=p_item_id;
 return jsonb_build_object('upserted',jsonb_array_length(p_upserts),'removed',v_removed+v_pruned,'reconciled_imports',v_matched);
end $$;
revoke all on function public.fb_apply_plaid_sync(text,text,text,timestamptz,jsonb,jsonb,bigint,boolean) from public,anon;
grant execute on function public.fb_apply_plaid_sync(text,text,text,timestamptz,jsonb,jsonb,bigint,boolean) to authenticated;

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
  if exists(select 1 from jsonb_array_elements(p_rows) where coalesce(value->>'iso_currency_code','USD')<>'USD') then raise exception 'Only USD supported'; end if;
  perform public.fb_bank_account(p_item_id,r.account_id,
    left(coalesce(nullif(r.official_name,''),r.name)||case when r.mask is null then '' else ' ••••'||r.mask end,256),
    case when r.type='credit' then 'Credit Card' else 'Checking' end)
    from jsonb_to_recordset(p_rows) r(account_id text,name text,official_name text,mask text,type text);
  delete from public.plaid_accounts where user_id = v_uid and item_id = p_item_id;
  insert into public.plaid_accounts (
    user_id,account_id,item_id,name,official_name,type,subtype,mask,
    current_balance,available_balance,credit_limit,iso_currency_code,balances_updated_at
  )
  select v_uid,r.account_id,p_item_id,r.name,r.official_name,r.type,r.subtype,r.mask,
    r.current_balance,r.available_balance,r.credit_limit,coalesce(r.iso_currency_code,'USD'),
    case when r.current_balance is null then null else pg_catalog.now() end
  from pg_catalog.jsonb_to_recordset(p_rows) as r(
    account_id text,name text,official_name text,type text,subtype text,mask text,
    current_balance numeric,available_balance numeric,credit_limit numeric,iso_currency_code text
  );
  get diagnostics v_count = row_count;
  insert into public.account_balances (
    user_id,account_id,item_id,as_of,type,current_balance,available_balance,credit_limit
  )
  select v_uid,r.account_id,p_item_id,(pg_catalog.now() at time zone 'utc')::date,r.type,
    r.current_balance,r.available_balance,r.credit_limit
  from pg_catalog.jsonb_to_recordset(p_rows) as r(
    account_id text,type text,current_balance numeric,available_balance numeric,credit_limit numeric
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


notify pgrst,'reload schema';

create function public.fb_category_policy() returns trigger language plpgsql security invoker set search_path='' as $$
declare v_rule public.category_rules%rowtype;
begin
 if new.category_override is not null then new.category:=new.category_override;new.category_origin:='manual';
 elsif new.category_origin in('rule','provider','builtin') then
  select * into v_rule from public.category_rules where user_id=new.user_id and strpos(lower(new.description),lower(keyword))>0 order by keyword limit 1;
  if found then new.category:=v_rule.category;new.category_origin:='rule';new.category_rule:=v_rule.keyword;new.category_rule_version:=v_rule.version;
  else
   new.category_rule:=null;new.category_rule_version:=null;
   if new.provider_category is not null then new.category:=new.provider_category;new.category_origin:='provider';
   elsif new.builtin_category is not null then new.category:=new.builtin_category;new.category_origin:='builtin'; end if;
  end if;
 end if;
 return new;
end $$;
revoke all on function public.fb_category_policy() from public,anon;
grant execute on function public.fb_category_policy() to authenticated;
create trigger transaction_category_policy before insert or update on public.transactions for each row execute function public.fb_category_policy();
notify pgrst,'reload schema';

insert into public.accounts(user_id,name,account_type,plaid_item_id,plaid_account_id)
 select a.user_id,left(coalesce(nullif(a.official_name,''),a.name)||case when a.mask is null then '' else ' ••••'||a.mask end,256),
  case when a.type='credit' then 'Credit Card' else 'Checking' end,a.item_id,a.account_id from public.plaid_accounts a
 on conflict(user_id,plaid_item_id,plaid_account_id) where plaid_account_id is not null do update set name=excluded.name,account_type=excluded.account_type;
insert into public.accounts(user_id,name,account_type,plaid_item_id,plaid_account_id)
 select distinct b.user_id,'Historical account '||left(b.account_id,230),case when b.type='credit' then 'Credit Card' else 'Checking' end,b.item_id,b.account_id
 from public.account_balances b where not exists(select 1 from public.accounts a where a.user_id=b.user_id and a.plaid_item_id=b.item_id and a.plaid_account_id=b.account_id)
 on conflict(user_id,plaid_item_id,plaid_account_id) where plaid_account_id is not null do nothing;
notify pgrst,'reload schema';

update public.accounts a set institution_name=i.institution_name from public.plaid_items i where a.user_id=i.user_id and a.plaid_item_id=i.item_id;
notify pgrst,'reload schema';
