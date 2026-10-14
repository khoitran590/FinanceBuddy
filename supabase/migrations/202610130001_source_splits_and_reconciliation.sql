-- Source-preserving splits and recovery-safe incremental bank synchronization.
-- Apply while the app is stopped, then deploy the matching app version.

alter table public.transactions
  add column plaid_item_id text,
  add column plaid_account_id text,
  add constraint transactions_plaid_identity_lengths check (
    (plaid_item_id is null or length(plaid_item_id) between 1 and 256)
    and (plaid_account_id is null or length(plaid_account_id) between 1 and 256)
  );
alter table public.plaid_items
  add column sync_generation bigint not null default 0,
  add column needs_reconciliation boolean not null default false;
create index transactions_user_plaid_item_idx on public.transactions(user_id,plaid_item_id);

create table public.transaction_allocations (
  user_id uuid not null references auth.users(id) on delete cascade,
  id uuid not null default gen_random_uuid(),
  transaction_id text not null,
  amount double precision not null check (
    amount <> 0 and amount not in ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)),
  category text not null check(length(category) between 1 and 128),
  primary key(user_id,id),
  foreign key(user_id,transaction_id) references public.transactions(user_id,id) on delete cascade
);
create index transaction_allocations_parent_idx on public.transaction_allocations(user_id,transaction_id);
alter table public.transaction_allocations enable row level security;
revoke all on public.transaction_allocations from anon;
grant select,insert,update,delete on public.transaction_allocations to authenticated;
create policy owner_all on public.transaction_allocations for all to authenticated
  using ((select auth.uid())=user_id) with check ((select auth.uid())=user_id);

-- Backfill only unambiguous account labels. Future syncs always store provider IDs.
with matches as (
  select t.user_id,t.id,min(a.item_id) item_id,min(a.account_id) account_id
  from public.transactions t join public.plaid_accounts a on a.user_id=t.user_id
    and t.account_name=coalesce(nullif(a.official_name,''),a.name)
      || case when a.mask is null or a.mask='' then '' else ' ••••'||a.mask end
  where t.id like 'plaid:%'
  group by t.user_id,t.id having count(*)=1
)
update public.transactions t set plaid_item_id=m.item_id,plaid_account_id=m.account_id
from matches m where t.user_id=m.user_id and t.id=m.id;

-- Recover complete legacy pairs, including repeated splits, deepest first.
-- Partial/edited pairs remain untouched; a source re-created by a prior sync wins.
do $$
declare r record; v_changed boolean;
begin
  loop
    v_changed := false;
    for r in
      select a.*,pg_catalog.left(a.id,pg_catalog.length(a.id)-8) parent_id,
        pg_catalog.left(a.description,pg_catalog.length(a.description)-10) parent_description,
        b.id second_id,b.amount second_amount,b.category second_category
      from public.transactions a join public.transactions b
        on b.user_id=a.user_id and b.id=pg_catalog.left(a.id,pg_catalog.length(a.id)-8)||'-split-2'
      where pg_catalog.right(a.id,8)='-split-1'
        and pg_catalog.right(a.description,10)=' (split 1)'
        and b.description=pg_catalog.left(a.description,pg_catalog.length(a.description)-10)||' (split 2)'
        and (a.date,a.account_name,a.account_type)=(b.date,b.account_name,b.account_type)
        and a.amount*b.amount>0
        and not exists (select 1 from public.transaction_allocations x
          where x.user_id=a.user_id and x.transaction_id=pg_catalog.left(a.id,pg_catalog.length(a.id)-8))
      order by pg_catalog.length(a.id) desc
    loop
      -- A deeper pair may have consumed this candidate in the same iteration.
      if not exists (select 1 from public.transactions where user_id=r.user_id and id=r.id) then continue; end if;
      insert into public.transactions(user_id,id,date,description,amount,category,account_name,account_type,
        merchant_name,subcategory,payment_channel,location,pending,plaid_item_id,plaid_account_id)
      values(r.user_id,r.parent_id,r.date,r.parent_description,r.amount+r.second_amount,r.category,
        r.account_name,r.account_type,r.merchant_name,r.subcategory,r.payment_channel,r.location,r.pending,
        r.plaid_item_id,r.plaid_account_id) on conflict(user_id,id) do nothing;
      insert into public.transaction_allocations(user_id,transaction_id,amount,category)
      select r.user_id,r.parent_id,t.amount,t.category from public.transactions t
        where t.user_id=r.user_id and t.id in (r.id,r.second_id)
        and not exists(select 1 from public.transaction_allocations x where x.user_id=t.user_id and x.transaction_id=t.id);
      update public.transaction_allocations set transaction_id=r.parent_id
        where user_id=r.user_id and transaction_id in (r.id,r.second_id);
      delete from public.transactions where user_id=r.user_id and id in(r.id,r.second_id);
      v_changed := true;
    end loop;
    exit when not v_changed;
  end loop;
end $$;

create function public.fb_invalidate_plaid_sync(p_account text)
returns void language plpgsql security invoker set search_path='' as $$
declare v_uid uuid := (select auth.uid());
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text,0));
  update public.plaid_items i set cursor=null,sync_generation=sync_generation+1,needs_reconciliation=true
  where i.user_id=v_uid and (p_account is null
    or exists(select 1 from public.transactions t where t.user_id=v_uid and t.plaid_item_id=i.item_id and t.account_name=p_account)
    or exists(select 1 from public.plaid_accounts a where a.user_id=v_uid and a.item_id=i.item_id
      and coalesce(nullif(a.official_name,''),a.name)||case when a.mask is null or a.mask='' then '' else ' ••••'||a.mask end=p_account));
end $$;
revoke all on function public.fb_invalidate_plaid_sync(text) from public,anon;
grant execute on function public.fb_invalidate_plaid_sync(text) to authenticated;

-- Enrich older portable backups by source ID or an unambiguous current account.
-- Reject ambiguous active-bank rows instead of silently leaving unreconciled history.
create function public.fb_prepare_transaction_rows(p_rows jsonb)
returns setof jsonb language plpgsql security invoker set search_path='' as $$
declare v_uid uuid := (select auth.uid()); v_row jsonb;
  v_item text; v_account text; v_matches integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  for v_row in select value from pg_catalog.jsonb_array_elements(p_rows) loop
    if v_row ? 'allocations' and (jsonb_typeof(v_row->'allocations') is distinct from 'array'
      or jsonb_array_length(v_row->'allocations')>100) then
      raise exception 'Invalid allocation list';
    end if;
    v_item := v_row->>'plaid_item_id'; v_account := v_row->>'plaid_account_id';
    if v_row->>'id' like 'plaid:%' and v_item is null then
      select plaid_item_id,plaid_account_id into v_item,v_account from public.transactions
        where user_id=v_uid and id=v_row->>'id';
      if v_item is null then
        select count(*),min(item_id),min(account_id) into v_matches,v_item,v_account
        from public.plaid_accounts where user_id=v_uid
          and coalesce(nullif(official_name,''),name)||case when mask is null or mask='' then '' else ' ••••'||mask end=v_row->>'account_name';
        if v_matches<>1 then v_item:=null; v_account:=null; end if;
      end if;
      if v_item is null and exists(select 1 from public.plaid_items where user_id=v_uid) then
        raise exception 'Bank history must be matched before restore; sync connections and retry';
      end if;
    end if;
    if v_row->>'id' not like 'plaid:%' and v_item is not null then
      raise exception 'Invalid bank source identity';
    end if;
    return next v_row || pg_catalog.jsonb_build_object(
      'plaid_item_id',v_item,'plaid_account_id',v_account);
  end loop;
  return;
end $$;
revoke all on function public.fb_prepare_transaction_rows(jsonb) from public,anon;
grant execute on function public.fb_prepare_transaction_rows(jsonb) to authenticated;

create or replace function public.fb_replace_transactions(p_account text, p_rows jsonb)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_count integer;
  v_rows jsonb;
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
  select coalesce(pg_catalog.jsonb_agg(r.value),'[]'::jsonb) into v_rows
    from public.fb_prepare_transaction_rows(p_rows) as r(value);
  perform public.fb_invalidate_plaid_sync(p_account);
  if p_account is null then
    delete from public.transactions where user_id = v_uid;
  else
    delete from public.transactions where user_id = v_uid and account_name = p_account;
  end if;
  insert into public.transactions (
    user_id,id,date,description,amount,category,account_name,account_type,
    merchant_name,subcategory,payment_channel,location,pending,plaid_item_id,plaid_account_id
  )
  select v_uid,r.id,r.date,r.description,r.amount,r.category,r.account_name,r.account_type,
    r.merchant_name,r.subcategory,r.payment_channel,r.location,coalesce(r.pending,false),r.plaid_item_id,r.plaid_account_id
  from pg_catalog.jsonb_to_recordset(v_rows) as r(
    id text, date date, description text, amount float8, category text,
    account_name text, account_type text, merchant_name text, subcategory text,
    payment_channel text, location text, pending boolean,plaid_item_id text,plaid_account_id text
  );
  get diagnostics v_count = row_count;
  insert into public.transaction_allocations(user_id,id,transaction_id,amount,category)
  select v_uid,(a.value->>'id')::uuid,r.value->>'id',
    (a.value->>'amount')::float8,a.value->>'category'
  from pg_catalog.jsonb_array_elements(v_rows) r,
    lateral pg_catalog.jsonb_array_elements(coalesce(r.value->'allocations','[]'::jsonb)) a;
  return v_count;
end;
$$;
revoke all on function public.fb_replace_transactions(text,jsonb) from public, anon;
grant execute on function public.fb_replace_transactions(text,jsonb) to authenticated;

create or replace function public.fb_split_transaction(
  p_id text,p_first_category text,p_first_amount float8,p_second_category text,p_second_amount float8
) returns void language plpgsql security invoker set search_path='' as $$
declare v_uid uuid := (select auth.uid()); v_original public.transactions%rowtype; v_sign integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if p_first_amount is null or p_second_amount is null or p_first_amount<=0 or p_second_amount<=0
    or p_first_amount in ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
    or p_second_amount in ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
    or p_first_category is null or p_second_category is null
    or length(p_first_category) not between 1 and 128 or length(p_second_category) not between 1 and 128 then
    raise exception 'Invalid split';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text,0));
  select * into v_original from public.transactions where user_id=v_uid and id=p_id for update;
  if not found then raise exception 'Transaction not found'; end if;
  if abs(p_first_amount+p_second_amount-abs(v_original.amount))>0.005 then
    raise exception 'Split amounts do not equal transaction';
  end if;
  v_sign := case when v_original.amount<0 then -1 else 1 end;
  delete from public.transaction_allocations where user_id=v_uid and transaction_id=p_id;
  insert into public.transaction_allocations(user_id,transaction_id,amount,category) values
    (v_uid,p_id,v_sign*p_first_amount,p_first_category),(v_uid,p_id,v_sign*p_second_amount,p_second_category);
end $$;
revoke all on function public.fb_split_transaction(text,text,float8,text,float8) from public,anon;
grant execute on function public.fb_split_transaction(text,text,float8,text,float8) to authenticated;

-- Preserve old collection restore validation while accepting canonical v2 backups.
alter function public.fb_restore_backup(jsonb) rename to fb_restore_backup_collections;
revoke all on function public.fb_restore_backup_collections(jsonb) from public,anon;
grant execute on function public.fb_restore_backup_collections(jsonb) to authenticated;
create function public.fb_restore_backup(p_backup jsonb)
returns jsonb language plpgsql security invoker set search_path='' as $$
begin
  if p_backup->>'version' is null or p_backup->>'version' not in ('1','2') then
    raise exception 'Unsupported backup version';
  end if;
  return public.fb_restore_backup_collections(p_backup || '{"version":1}'::jsonb);
end $$;
revoke all on function public.fb_restore_backup(jsonb) from public,anon;
grant execute on function public.fb_restore_backup(jsonb) to authenticated;

create function public.fb_delete_account_transactions(p_account text)
returns integer language plpgsql security invoker set search_path='' as $$
declare v_uid uuid := (select auth.uid()); v_count integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if p_account is null or length(p_account) not between 1 and 256 then raise exception 'Invalid account'; end if;
  perform public.fb_invalidate_plaid_sync(p_account);
  delete from public.transactions where user_id=v_uid and account_name=p_account;
  get diagnostics v_count=row_count;
  return v_count;
end $$;
revoke all on function public.fb_delete_account_transactions(text) from public,anon;
grant execute on function public.fb_delete_account_transactions(text) to authenticated;

create or replace function public.fb_apply_category_rule(p_keyword text,p_category text)
returns integer language plpgsql security invoker set search_path='' as $$
declare v_uid uuid := (select auth.uid()); v_count integer; v_allocations integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if p_keyword is null or p_category is null or length(pg_catalog.btrim(p_keyword)) not between 1 and 256
    or length(p_category) not between 1 and 128 then raise exception 'Invalid category rule'; end if;
  update public.transactions set category=p_category where user_id=v_uid
    and pg_catalog.strpos(pg_catalog.lower(description),pg_catalog.lower(pg_catalog.btrim(p_keyword)))>0;
  get diagnostics v_count=row_count;
  update public.transaction_allocations a set category=p_category
    from public.transactions t where t.user_id=v_uid and a.user_id=t.user_id and a.transaction_id=t.id
    and pg_catalog.strpos(pg_catalog.lower(t.description),pg_catalog.lower(pg_catalog.btrim(p_keyword)))>0;
  get diagnostics v_allocations=row_count;
  return v_count;
end $$;

-- Remove the obsolete overload so older clients cannot bypass generation checks.
drop function public.fb_apply_plaid_sync(text,text,text,timestamptz,jsonb,jsonb);
create or replace function public.fb_apply_plaid_sync(
  p_item_id text, p_expected_cursor text, p_next_cursor text,
  p_synced_at timestamptz, p_upserts jsonb, p_removed_ids jsonb,
  p_expected_generation bigint default 0, p_reconcile boolean default false
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_cursor text;
  v_generation bigint;
  v_reconcile boolean;
  v_upserted integer;
  v_removed integer;
  v_pruned integer;
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
  select cursor,sync_generation,needs_reconciliation into v_cursor,v_generation,v_reconcile from public.plaid_items
    where user_id=v_uid and item_id=p_item_id for update;
  if not found then raise exception 'Bank connection not found'; end if;
  if v_cursor is distinct from p_expected_cursor or v_generation is distinct from p_expected_generation then
    raise exception 'Bank cursor changed; retry sync';
  end if;
  if v_reconcile is distinct from p_reconcile then
    raise exception 'Bank reconciliation state changed; retry sync';
  end if;
  insert into public.transactions (
    user_id,id,date,description,amount,category,account_name,account_type,
    merchant_name,subcategory,payment_channel,location,pending,plaid_item_id,plaid_account_id
  )
  select v_uid,r.id,r.date,r.description,r.amount,r.category,r.account_name,r.account_type,
    r.merchant_name,r.subcategory,r.payment_channel,r.location,coalesce(r.pending,false),p_item_id,r.plaid_account_id
  from pg_catalog.jsonb_to_recordset(p_upserts) as r(
    id text,date date,description text,amount float8,category text,
    account_name text,account_type text,merchant_name text,subcategory text,
    payment_channel text,location text,pending boolean,plaid_account_id text
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
    pending=excluded.pending,
    plaid_item_id=excluded.plaid_item_id,plaid_account_id=excluded.plaid_account_id;
  get diagnostics v_upserted = row_count;
  delete from public.transactions
    where user_id=v_uid and (plaid_item_id=p_item_id or plaid_item_id is null) and id in (
      select value from pg_catalog.jsonb_array_elements_text(p_removed_ids) value
    );
  get diagnostics v_removed = row_count;
  if p_reconcile then
    delete from public.transactions t where t.user_id=v_uid and t.plaid_item_id=p_item_id
      and not exists (select 1 from pg_catalog.jsonb_to_recordset(p_upserts) as r(id text) where r.id=t.id);
    get diagnostics v_pruned = row_count;
    v_removed := v_removed + v_pruned;
  end if;
  update public.plaid_items set
    cursor=p_next_cursor,last_synced_at=p_synced_at,status='connected',error_message=null,needs_reconciliation=false
    where user_id=v_uid and item_id=p_item_id;
  return pg_catalog.jsonb_build_object('upserted',v_upserted,'removed',v_removed);
end;
$$;
revoke all on function public.fb_apply_plaid_sync(text,text,text,timestamptz,jsonb,jsonb,bigint,boolean)
  from public, anon;
grant execute on function public.fb_apply_plaid_sync(text,text,text,timestamptz,jsonb,jsonb,bigint,boolean)
  to authenticated;

-- Splitting is a user edit even when the source row itself has not changed.
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
    if exists(select 1 from public.transaction_allocations where user_id=v_uid and transaction_id=v_expected.id) then
      raise exception 'Imported transaction has category allocations; cannot undo';
    end if;
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
