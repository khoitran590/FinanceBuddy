-- Statement identity is file + source row; content is only a matching signal.
-- The matching workspace is temporary and discarded at transaction end.
do $$
begin
  if not pg_catalog.has_database_privilege('authenticated',pg_catalog.current_database(),'TEMP') then
    execute pg_catalog.format('grant temporary on database %I to authenticated',pg_catalog.current_database());
  end if;
end $$;
alter table public.transactions
  add column statement_file_id text check(statement_file_id ~ '^[a-f0-9]{64}$'),
  add column statement_row integer check(statement_row between 1 and 50000),
  add column statement_reference text check(length(statement_reference) between 1 and 256),
  add column statement_match_bank boolean not null default false,
  add constraint statement_identity_pair check((statement_file_id is null)=(statement_row is null));
create function public.fb_import_normalize(p_text text)
returns text language sql immutable security invoker set search_path='' as $$
  select pg_catalog.translate(pg_catalog.btrim(p_text),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')
$$;
revoke all on function public.fb_import_normalize(text) from public,anon;
grant execute on function public.fb_import_normalize(text) to authenticated;
create unique index transactions_statement_row_idx on public.transactions(user_id,statement_file_id,statement_row)
  where statement_file_id is not null;
create unique index transactions_statement_reference_idx on public.transactions
  (user_id,public.fb_import_normalize(account_name),public.fb_import_normalize(account_type),statement_reference)
  where statement_reference is not null;

create function public.fb_import_match_key(p_date date,p_amount float8,p_description text,p_account text,p_type text)
returns text language sql immutable security invoker set search_path='' as $$
  select pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
    pg_catalog.jsonb_build_array(p_date,p_amount,public.fb_import_normalize(p_description),
      public.fb_import_normalize(p_account),public.fb_import_normalize(p_type))::text,'UTF8')),'hex')
$$;
revoke all on function public.fb_import_match_key(date,float8,text,text,text) from public,anon;
grant execute on function public.fb_import_match_key(date,float8,text,text,text) to authenticated;
alter table public.transactions add column import_match_key text generated always as
  (public.fb_import_match_key(date,amount,description,account_name,account_type)) stored;
create index transactions_import_match_idx on public.transactions(user_id,import_match_key,id);

create table public.statement_import_rows (
  user_id uuid not null references auth.users(id) on delete cascade,
  file_id text not null check(file_id ~ '^[a-f0-9]{64}$'),
  row_number integer not null check(row_number between 1 and 50000),
  transaction_id text not null,
  primary key(user_id,file_id,row_number),
  foreign key(user_id,transaction_id) references public.transactions(user_id,id) on delete cascade
);
create index statement_import_rows_transaction_idx on public.statement_import_rows(user_id,transaction_id);
alter table public.statement_import_rows enable row level security;
revoke all on public.statement_import_rows from anon;
grant select,insert,update,delete on public.statement_import_rows to authenticated;
create policy owner_all on public.statement_import_rows for all to authenticated
  using((select auth.uid())=user_id) with check((select auth.uid())=user_id);

create function public.fb_append_statement(p_rows jsonb,p_keep_matching boolean default false)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
  v_uid uuid := (select auth.uid()); r record; v_selected public.transactions%rowtype;
  v_match text; v_inserted integer := 0; v_matched integer := 0; v_result jsonb;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if pg_catalog.jsonb_typeof(p_rows) is distinct from 'array' or pg_catalog.jsonb_array_length(p_rows)>50000
    or p_keep_matching is null then raise exception 'Invalid statement'; end if;
  if exists(select 1 from pg_catalog.jsonb_to_recordset(p_rows) as x(
      id text,date date,description text,amount float8,category text,account_name text,account_type text,
      statement_file_id text,statement_row integer)
    where x.id is null or x.id not like 'statement:%' or x.date is null or x.amount is null
      or x.description is null or x.category is null or x.account_name is null or x.account_type is null
      or x.statement_file_id is null or x.statement_file_id !~ '^[a-f0-9]{64}$'
      or x.statement_row is null or x.statement_row not between 1 and 50000)
    or (select count(distinct value->>'statement_file_id') from pg_catalog.jsonb_array_elements(p_rows))>1
    or exists(select 1 from pg_catalog.jsonb_to_recordset(p_rows) as x(statement_row integer)
      group by x.statement_row having count(*)>1) then raise exception 'Invalid statement row identity'; end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text,0));
  -- Local indexed occurrence consumption avoids repeatedly copying large JSON
  -- arrays or testing an ever-growing list of IDs. It disappears at commit.
  create temporary table if not exists fb_statement_used(id text primary key,inserted boolean not null default false) on commit drop;
  truncate table pg_temp.fb_statement_used;
  -- Reserve all authoritative matches before assigning fuzzy occurrences.
  with incoming as materialized (
    select * from pg_catalog.jsonb_to_recordset(p_rows) as x(
      id text,statement_file_id text,statement_row integer,statement_reference text,account_name text,account_type text)
  )
  insert into pg_temp.fb_statement_used(id)
    select t.id from public.transactions t join incoming x on t.id=x.id where t.user_id=v_uid
    union select m.transaction_id from public.statement_import_rows m join incoming x
      on m.file_id=x.statement_file_id and m.row_number=x.statement_row where m.user_id=v_uid
    union select t.id from public.transactions t join incoming x
      on t.statement_file_id=x.statement_file_id and t.statement_row=x.statement_row where t.user_id=v_uid
    union select t.id from public.transactions t join incoming x
      on t.statement_reference=x.statement_reference
      and public.fb_import_normalize(t.account_name)=public.fb_import_normalize(x.account_name)
      and public.fb_import_normalize(t.account_type)=public.fb_import_normalize(x.account_type)
      where t.user_id=v_uid and x.statement_reference is not null
    on conflict do nothing;
  create temporary table if not exists fb_statement_pool(id text primary key,match_key text,reference text) on commit drop;
  create index if not exists fb_statement_pool_match_idx on pg_temp.fb_statement_pool(match_key,id);
  create index if not exists fb_statement_pool_unreferenced_idx on pg_temp.fb_statement_pool(match_key,id) where reference is null;
  truncate table pg_temp.fb_statement_pool;
  insert into pg_temp.fb_statement_pool(id,match_key,reference)
    select t.id,t.import_match_key,t.statement_reference from public.transactions t
    where t.user_id=v_uid and not exists(select 1 from pg_temp.fb_statement_used u where u.id=t.id)
      and t.import_match_key in (select public.fb_import_match_key(x.date,x.amount,x.description,x.account_name,x.account_type)
        from pg_catalog.jsonb_to_recordset(p_rows) as x(date date,amount float8,description text,account_name text,account_type text));
  for r in select * from pg_catalog.jsonb_to_recordset(p_rows) as x(
    id text,date date,description text,amount float8,category text,account_name text,account_type text,
    statement_file_id text,statement_row integer,statement_reference text)
  loop
    v_match := public.fb_import_match_key(r.date,r.amount,r.description,r.account_name,r.account_type);
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
    if not found and r.statement_reference is not null then
      select * into v_selected from public.transactions t where t.user_id=v_uid
        and t.statement_reference=r.statement_reference
        and public.fb_import_normalize(t.account_name)=public.fb_import_normalize(r.account_name)
        and public.fb_import_normalize(t.account_type)=public.fb_import_normalize(r.account_type);
      if found and v_selected.import_match_key<>v_match then
        raise exception 'A bank reference already exists with different transaction details';
      end if;
    end if;
    if v_selected.id is null and not p_keep_matching then
      if r.statement_reference is null then
        select t.* into v_selected from pg_temp.fb_statement_pool p join public.transactions t on t.user_id=v_uid and t.id=p.id
          where p.match_key=v_match order by p.id limit 1;
      else
        select t.* into v_selected from pg_temp.fb_statement_pool p join public.transactions t on t.user_id=v_uid and t.id=p.id
          where p.match_key=v_match and p.reference is null order by p.id limit 1;
      end if;
    end if;
    if v_selected.id is null then
      insert into public.transactions(user_id,id,date,description,amount,category,account_name,account_type,
        statement_file_id,statement_row,statement_reference,statement_match_bank)
      values(v_uid,r.id,r.date,r.description,r.amount,r.category,r.account_name,r.account_type,
        r.statement_file_id,r.statement_row,r.statement_reference,not p_keep_matching) returning * into v_selected;
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

-- Existing atomic replace/restore functions remain in charge of allocations and
-- bank reconciliation; add provenance without changing their transaction boundary.
create function public.fb_statement_account_is_connected(p_account text)
returns boolean language sql stable security invoker set search_path='' as $$
  select exists(select 1 from public.plaid_items i where i.user_id=(select auth.uid()) and (
    exists(select 1 from public.transactions t where t.user_id=i.user_id and t.plaid_item_id=i.item_id
      and public.fb_import_normalize(t.account_name)=public.fb_import_normalize(p_account))
    or exists(select 1 from public.plaid_accounts a where a.user_id=i.user_id and a.item_id=i.item_id
      and public.fb_import_normalize(coalesce(nullif(a.official_name,''),a.name)||case when a.mask is null or a.mask='' then '' else ' ••••'||a.mask end)=public.fb_import_normalize(p_account))))
$$;
revoke all on function public.fb_statement_account_is_connected(text) from public,anon;
grant execute on function public.fb_statement_account_is_connected(text) to authenticated;

alter function public.fb_replace_transactions(text,jsonb) rename to fb_replace_transactions_sources;
alter function public.fb_replace_transactions_sources(text,jsonb) set schema financebuddy_private;
create function public.fb_replace_transactions(p_account text,p_rows jsonb)
returns integer language plpgsql security invoker set search_path='' as $$
declare v_count integer; v_uid uuid := (select auth.uid());
begin
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text,0));
  if p_account is not null and public.fb_statement_account_is_connected(p_account)
    and exists(select 1 from pg_catalog.jsonb_to_recordset(p_rows) as r(id text) where r.id not like 'plaid:%') then
    raise exception 'Append statements for a connected bank, or disconnect before replacing history';
  end if;
  v_count:=financebuddy_private.fb_replace_transactions_sources(p_account,p_rows);
  update public.transactions t set statement_file_id=r.statement_file_id,
    statement_row=r.statement_row,statement_reference=r.statement_reference,statement_match_bank=coalesce(r.statement_match_bank,false)
  from pg_catalog.jsonb_to_recordset(p_rows) as r(id text,statement_file_id text,statement_row integer,statement_reference text,statement_match_bank boolean)
  where t.user_id=v_uid and t.id=r.id;
  return v_count;
end $$;
revoke all on function public.fb_replace_transactions(text,jsonb) from public,anon;
grant execute on function public.fb_replace_transactions(text,jsonb) to authenticated;

alter function public.fb_restore_backup(jsonb) rename to fb_restore_backup_sources;
alter function public.fb_restore_backup_sources(jsonb) set schema financebuddy_private;
create function public.fb_restore_backup(p_backup jsonb)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare v_result jsonb; v_uid uuid := (select auth.uid());
begin
  v_result:=financebuddy_private.fb_restore_backup_sources(p_backup);
  if p_backup ? 'statement_import_rows' then
    if jsonb_typeof(p_backup->'statement_import_rows') is distinct from 'array'
      or jsonb_array_length(p_backup->'statement_import_rows')>50000 then raise exception 'Invalid import history'; end if;
    insert into public.statement_import_rows(user_id,file_id,row_number,transaction_id)
    select v_uid,r.file_id,r.row_number,r.transaction_id
    from pg_catalog.jsonb_to_recordset(p_backup->'statement_import_rows') as r(file_id text,row_number integer,transaction_id text);
  end if;
  return v_result;
end $$;
revoke all on function public.fb_restore_backup(jsonb) from public,anon;
grant execute on function public.fb_restore_backup(jsonb) to authenticated;

-- Reconcile statement-first workflows when the provider later supplies the same
-- purchases. Explicitly retained separate purchases are excluded from matching.
alter function public.fb_apply_plaid_sync(text,text,text,timestamptz,jsonb,jsonb,bigint,boolean)
  rename to fb_apply_plaid_sync_sources;
alter function public.fb_apply_plaid_sync_sources(text,text,text,timestamptz,jsonb,jsonb,bigint,boolean)
  set schema financebuddy_private;
create function public.fb_apply_plaid_sync(
  p_item_id text,p_expected_cursor text,p_next_cursor text,p_synced_at timestamptz,
  p_upserts jsonb,p_removed_ids jsonb,p_expected_generation bigint default 0,p_reconcile boolean default false
) returns jsonb language plpgsql security invoker set search_path='' as $$
declare v_uid uuid := (select auth.uid()); v_result jsonb; v_count integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if jsonb_typeof(p_upserts) is distinct from 'array' or jsonb_typeof(p_removed_ids) is distinct from 'array'
    or jsonb_array_length(p_upserts)>50000 or jsonb_array_length(p_removed_ids)>50000 then
    raise exception 'Invalid sync batch'; end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(v_uid::text,0));
  create temporary table if not exists fb_bank_import_matches(source_id text primary key,bank_id text unique) on commit drop;
  truncate table pg_temp.fb_bank_import_matches;
  with bank_rows as (
    select r.id,public.fb_import_match_key(r.date,r.amount,r.description,r.account_name,r.account_type) match_key
    from pg_catalog.jsonb_to_recordset(p_upserts) as r(id text,date date,amount float8,description text,account_name text,account_type text)
    where not exists(select 1 from public.transactions t where t.user_id=v_uid and t.id=r.id)
      and not exists(select 1 from pg_catalog.jsonb_array_elements_text(p_removed_ids) x where x.value=r.id)
  ), bank_ranked as (
    select id,match_key,row_number() over(partition by match_key order by id) occurrence from bank_rows
  ), import_ranked as (
    select id,import_match_key,row_number() over(partition by import_match_key order by id) occurrence
    from public.transactions where user_id=v_uid and statement_match_bank and statement_file_id is not null
      and id not like 'plaid:%' and plaid_item_id is null and import_match_key in(select match_key from bank_rows)
  )
  insert into pg_temp.fb_bank_import_matches(source_id,bank_id)
    select i.id,b.id from import_ranked i join bank_ranked b
      on b.match_key=i.import_match_key and b.occurrence=i.occurrence;
  -- Cursor and generation validation, provider upserts, removals and pruning
  -- remain inside the existing atomic function. Any failure rolls all work back.
  v_result:=financebuddy_private.fb_apply_plaid_sync_sources(p_item_id,p_expected_cursor,p_next_cursor,p_synced_at,
    p_upserts,p_removed_ids,p_expected_generation,p_reconcile);
  -- Release unique reference/file-row claims before transferring provenance.
  create temporary table if not exists fb_bank_import_details(
    source_id text primary key,bank_id text,category text,file_id text,row_number integer,reference text
  ) on commit drop;
  truncate table pg_temp.fb_bank_import_details;
  insert into pg_temp.fb_bank_import_details
    select s.id,m.bank_id,s.category,s.statement_file_id,s.statement_row,s.statement_reference
    from public.transactions s join pg_temp.fb_bank_import_matches m on m.source_id=s.id
    where s.user_id=v_uid;
  update public.transactions s set statement_file_id=null,statement_row=null,statement_reference=null
    from pg_temp.fb_bank_import_details m where s.user_id=v_uid and s.id=m.source_id;
  update public.transactions t set
    category=case when m.category='Uncategorized' then t.category else m.category end,
    statement_file_id=m.file_id,statement_row=m.row_number,statement_reference=m.reference,statement_match_bank=true
    from pg_temp.fb_bank_import_details m where t.user_id=v_uid and t.id=m.bank_id;
  update public.transaction_allocations a set transaction_id=m.bank_id
    from pg_temp.fb_bank_import_details m where a.user_id=v_uid and a.transaction_id=m.source_id;
  update public.statement_import_rows r set transaction_id=m.bank_id
    from pg_temp.fb_bank_import_details m where r.user_id=v_uid and r.transaction_id=m.source_id;
  delete from public.transactions t using pg_temp.fb_bank_import_details m where t.user_id=v_uid and t.id=m.source_id;
  get diagnostics v_count=row_count;
  return v_result || pg_catalog.jsonb_build_object('reconciled_imports',v_count);
end $$;
revoke all on function public.fb_apply_plaid_sync(text,text,text,timestamptz,jsonb,jsonb,bigint,boolean) from public,anon;
grant execute on function public.fb_apply_plaid_sync(text,text,text,timestamptz,jsonb,jsonb,bigint,boolean) to authenticated;

-- Reference/provenance changes are edits too; undo must preserve them.
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
      account_name text,account_type text,merchant_name text,subcategory text,payment_channel text,location text,
      pending boolean,plaid_item_id text,plaid_account_id text,statement_file_id text,statement_row integer,statement_reference text,statement_match_bank boolean
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
      or v_actual.account_type is distinct from v_expected.account_type
      or v_actual.merchant_name is distinct from v_expected.merchant_name
      or v_actual.subcategory is distinct from v_expected.subcategory
      or v_actual.payment_channel is distinct from v_expected.payment_channel
      or v_actual.location is distinct from v_expected.location
      or v_actual.pending is distinct from coalesce(v_expected.pending,false)
      or v_actual.plaid_item_id is distinct from v_expected.plaid_item_id
      or v_actual.plaid_account_id is distinct from v_expected.plaid_account_id
      or v_actual.statement_file_id is distinct from v_expected.statement_file_id
      or v_actual.statement_row is distinct from v_expected.statement_row
      or v_actual.statement_reference is distinct from v_expected.statement_reference
      or v_actual.statement_match_bank is distinct from coalesce(v_expected.statement_match_bank,false) then
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

notify pgrst, 'reload schema';
