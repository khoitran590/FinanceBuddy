-- Logical writes and their result receipts commit together, behind the user's RLS.
-- Private schema is not exposed by PostgREST. No credentials enter receipts/backups.
create sequence public.transaction_revision_seq;
revoke all on sequence public.transaction_revision_seq from public,anon;
grant usage on sequence public.transaction_revision_seq to authenticated;
create or replace function public.fb_assign_account() returns trigger language plpgsql security invoker set search_path='' as $$
declare v_id uuid; v_key text;
begin
 if new.account_id is not null then
  if not exists(select 1 from public.accounts where user_id=new.user_id and id=new.account_id) then raise exception 'Account not owned'; end if;
 elsif new.plaid_item_id is not null and new.plaid_account_id is not null then
  new.account_id:=public.fb_bank_account(new.plaid_item_id,new.plaid_account_id,new.account_name,new.account_type);
 else
  v_key:=public.fb_import_normalize(new.account_name)||chr(31)||public.fb_import_normalize(new.account_type);
  insert into public.accounts(user_id,name,account_type,statement_key) values(new.user_id,new.account_name,new.account_type,v_key)
   on conflict(user_id,statement_key) where statement_key is not null do update set name=excluded.name returning id into v_id;
  new.account_id:=v_id;
 end if;
 -- Never reuse a revision after restore, deletion or re-insertion of the same ID.
 new.revision:=nextval('public.transaction_revision_seq'::regclass);
 return new;
end $$;
update public.transactions set revision=revision;

create table financebuddy_private.data_operations (
 user_id uuid not null references auth.users(id) on delete cascade, id uuid not null,
 kind text not null, fingerprint text not null, result jsonb not null,
 created_at timestamptz not null default now(), undone boolean not null default false,
 primary key(user_id,id)
);
alter table financebuddy_private.data_operations enable row level security;
revoke all on financebuddy_private.data_operations from public,anon;
grant select,insert,update on financebuddy_private.data_operations to authenticated;
create policy owner_all on financebuddy_private.data_operations for all to authenticated using((select auth.uid())=user_id) with check((select auth.uid())=user_id);
create index data_operations_owner_recent on financebuddy_private.data_operations(user_id,created_at desc);
create function public.fb_operation_result(p_operation_id uuid) returns jsonb language sql stable security invoker set search_path='' as $$
 select jsonb_build_object('kind',kind,'result',result,'undone',undone) from financebuddy_private.data_operations where user_id=(select auth.uid()) and id=p_operation_id $$;
revoke all on function public.fb_operation_result(uuid) from public,anon;
grant execute on function public.fb_operation_result(uuid) to authenticated;
create function public.fb_import_batches() returns jsonb language sql stable security invoker set search_path='' as $$
 select coalesce(jsonb_agg(jsonb_build_object('id',id,'created_at',created_at,'inserted',result->'inserted','matched',result->'matched','undone',undone) order by created_at desc),'[]'::jsonb)
 from(select * from financebuddy_private.data_operations where user_id=(select auth.uid()) and kind='append' order by created_at desc limit 20) q $$;
revoke all on function public.fb_import_batches() from public,anon;
grant execute on function public.fb_import_batches() to authenticated;

create table public.balance_snapshots (
 user_id uuid not null references auth.users(id) on delete cascade,account_id uuid not null,
 as_of date not null,type text not null,current_balance numeric not null,available_balance numeric,credit_limit numeric,
 currency text not null default 'USD' check(currency='USD'),primary key(user_id,account_id,as_of),
 foreign key(user_id,account_id) references public.accounts(user_id,id),
 check(public.fb_money_valid(current_balance) and public.fb_money_valid(available_balance) and public.fb_money_valid(credit_limit))
);
alter table public.balance_snapshots enable row level security;
revoke all on public.balance_snapshots from public,anon;
grant select,insert,update,delete on public.balance_snapshots to authenticated;
create policy owner_all on public.balance_snapshots for all to authenticated using((select auth.uid())=user_id) with check((select auth.uid())=user_id);
create index balance_snapshots_owner_date on public.balance_snapshots(user_id,as_of desc,account_id);
insert into public.balance_snapshots(user_id,account_id,as_of,type,current_balance,available_balance,credit_limit)
 select b.user_id,a.id,b.as_of,b.type,b.current_balance,b.available_balance,b.credit_limit
 from public.account_balances b join public.accounts a on a.user_id=b.user_id and a.plaid_item_id=b.item_id and a.plaid_account_id=b.account_id on conflict do nothing;
create function public.fb_store_balance_snapshot() returns trigger language plpgsql security invoker set search_path='' as $$
begin
 insert into public.balance_snapshots(user_id,account_id,as_of,type,current_balance,available_balance,credit_limit,currency)
  select new.user_id,a.id,new.as_of,new.type,new.current_balance,new.available_balance,new.credit_limit,new.currency
  from public.accounts a where a.user_id=new.user_id and a.plaid_item_id=new.item_id and a.plaid_account_id=new.account_id
  on conflict(user_id,account_id,as_of) do update set type=excluded.type,current_balance=excluded.current_balance,available_balance=excluded.available_balance,credit_limit=excluded.credit_limit;
 return new;
end $$;
revoke all on function public.fb_store_balance_snapshot() from public,anon;
grant execute on function public.fb_store_balance_snapshot() to authenticated;
create trigger store_balance_snapshot after insert or update on public.account_balances for each row execute function public.fb_store_balance_snapshot();

create function public.fb_export_backup() returns jsonb language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_result jsonb; v_key text;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 -- One SQL statement = one MVCC snapshot across every exported collection.
 select jsonb_build_object('version',3,'currency','USD',
  'transactions',(select coalesce(jsonb_agg(to_jsonb(t)-'user_id'-'import_match_key'||jsonb_build_object('amount',t.amount::text,'allocations',(select coalesce(jsonb_agg(to_jsonb(a)-'user_id'-'transaction_id'||jsonb_build_object('amount',a.amount::text) order by a.id),'[]'::jsonb) from public.transaction_allocations a where a.user_id=t.user_id and a.transaction_id=t.id)) order by t.date,t.id),'[]'::jsonb) from public.transactions t where t.user_id=v_uid),
  'budgets',(select coalesce(jsonb_agg(to_jsonb(b)-'user_id'||jsonb_build_object('monthly_limit',b.monthly_limit::text) order by b.category),'[]'::jsonb) from public.budgets b where b.user_id=v_uid),
  'goals',(select coalesce(jsonb_agg(to_jsonb(g)-'user_id'||jsonb_build_object('target_amount',g.target_amount::text,'current_amount',g.current_amount::text) order by g.id),'[]'::jsonb) from public.savings_goals g where g.user_id=v_uid),
  'category_rules',(select coalesce(jsonb_agg(to_jsonb(r)-'user_id' order by r.keyword),'[]'::jsonb) from public.category_rules r where r.user_id=v_uid),
  'statement_import_rows',(select coalesce(jsonb_agg(to_jsonb(r)-'user_id' order by r.file_id,r.row_number),'[]'::jsonb) from public.statement_import_rows r where r.user_id=v_uid),
  'accounts',(select coalesce(jsonb_agg(to_jsonb(a)-'user_id' order by a.id),'[]'::jsonb) from public.accounts a where a.user_id=v_uid),
  'settings',(select coalesce(jsonb_agg(to_jsonb(s)-'user_id' order by s.key),'[]'::jsonb) from public.app_settings s where s.user_id=v_uid),
  'balance_snapshots',(select coalesce(jsonb_agg(to_jsonb(b)-'user_id'||jsonb_build_object('current_balance',b.current_balance::text,'available_balance',b.available_balance::text,'credit_limit',b.credit_limit::text) order by b.account_id,b.as_of),'[]'::jsonb) from public.balance_snapshots b where b.user_id=v_uid)) into v_result;
 foreach v_key in array array['transactions','budgets','goals','category_rules','statement_import_rows','accounts','settings','balance_snapshots'] loop
  if jsonb_array_length(v_result->v_key)>50000 then raise exception 'Backup exceeds restore record limits'; end if;
 end loop;
 if octet_length(convert_to(v_result::text,'UTF8'))>10485760 then raise exception 'Backup exceeds the 10 MB restore limit'; end if;
 return v_result;
end $$;
revoke all on function public.fb_export_backup() from public,anon;
grant execute on function public.fb_export_backup() to authenticated;

alter function public.fb_restore_backup(jsonb) rename to fb_restore_backup_portable;
alter function public.fb_restore_backup_portable(jsonb) set schema financebuddy_private;
create function public.fb_restore_backup(p_backup jsonb) returns jsonb language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_result jsonb; v_version integer:=(p_backup->>'version')::integer; v_key text;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 if v_version not in(1,2,3) or coalesce(p_backup->>'currency','USD')<>'USD' or octet_length(convert_to(p_backup::text,'UTF8'))>10485760 then raise exception 'Invalid backup'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 if v_version=3 then
  foreach v_key in array array['accounts','settings','balance_snapshots'] loop
   if jsonb_typeof(p_backup->v_key) is distinct from 'array' or jsonb_array_length(p_backup->v_key)>50000 then raise exception 'Invalid backup collection'; end if;
  end loop;
  insert into public.accounts(user_id,id,name,account_type,currency,institution_name,plaid_item_id,plaid_account_id,statement_key)
   select v_uid,id,name,account_type,currency,institution_name,plaid_item_id,plaid_account_id,statement_key from jsonb_to_recordset(p_backup->'accounts') r(id uuid,name text,account_type text,currency text,institution_name text,plaid_item_id text,plaid_account_id text,statement_key text)
   on conflict(user_id,id) do update set name=excluded.name,account_type=excluded.account_type,institution_name=coalesce(accounts.institution_name,excluded.institution_name);
 end if;
 v_result:=financebuddy_private.fb_restore_backup_portable(p_backup||jsonb_build_object('version',case when v_version=3 then 2 else v_version end));
 update public.category_rules r set version=coalesce(x.version,1)
  from jsonb_to_recordset(p_backup->'category_rules') x(keyword text,version bigint)
  where r.user_id=v_uid and r.keyword=lower(trim(x.keyword));
 -- Historical categories lacking provenance remain legacy. Explicit annotations
 -- and owned account IDs from v3 are restored after the older core routine.
 update public.transactions t set account_id=coalesce(r.account_id,t.account_id),currency=coalesce(r.currency,'USD'),
  category_origin=coalesce(r.category_origin,'legacy'),provider_category=r.provider_category,builtin_category=r.builtin_category,category_override=r.category_override,
  category_rule=r.category_rule,category_rule_version=r.category_rule_version,pending_transaction_id=r.pending_transaction_id
 from jsonb_to_recordset(p_backup->'transactions') r(id text,account_id uuid,currency text,category_origin text,provider_category text,builtin_category text,category_override text,category_rule text,category_rule_version bigint,pending_transaction_id text)
 where t.user_id=v_uid and t.id=r.id;
 if v_version=3 then
  delete from public.app_settings where user_id=v_uid;
  insert into public.app_settings(user_id,key,value) select v_uid,key,value from jsonb_to_recordset(p_backup->'settings') r(key text,value text);
  delete from public.balance_snapshots where user_id=v_uid;
  insert into public.balance_snapshots(user_id,account_id,as_of,type,current_balance,available_balance,credit_limit,currency)
   select v_uid,account_id,as_of,type,current_balance,available_balance,credit_limit,currency from jsonb_to_recordset(p_backup->'balance_snapshots') r(account_id uuid,as_of date,type text,current_balance numeric,available_balance numeric,credit_limit numeric,currency text);
 end if;
 return v_result;
end $$;
revoke all on function public.fb_restore_backup(jsonb) from public,anon;
grant execute on function public.fb_restore_backup(jsonb) to authenticated;

alter table public.plaid_items drop constraint plaid_items_status_check;
alter table public.plaid_items add constraint plaid_items_status_check check(status in('connected','error','disconnecting')),
 add column disconnect_remove_transactions boolean not null default false;
create function public.fb_disconnect_begin(p_item text,p_remove boolean) returns boolean language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_item public.plaid_items%rowtype;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 select * into v_item from public.plaid_items where user_id=v_uid and item_id=p_item for update;
 if not found then return false; end if;
 if v_item.status='disconnecting' and v_item.disconnect_remove_transactions is distinct from p_remove then raise exception 'Disconnect intent already recorded'; end if;
 update public.plaid_items set status='disconnecting',disconnect_remove_transactions=p_remove,sync_generation=sync_generation+1 where user_id=v_uid and item_id=p_item;
 return true;
end $$;
create function public.fb_disconnect_finish(p_item text) returns integer language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_item public.plaid_items%rowtype; v_count integer:=0;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 select * into v_item from public.plaid_items where user_id=v_uid and item_id=p_item for update;
 if not found then return 0; end if;
 if v_item.status<>'disconnecting' then raise exception 'Disconnect must be started first'; end if;
 if v_item.disconnect_remove_transactions then
  delete from public.transactions where user_id=v_uid and (plaid_item_id=p_item or account_id in(select id from public.accounts where user_id=v_uid and plaid_item_id=p_item));
  get diagnostics v_count=row_count;
  delete from public.balance_snapshots where user_id=v_uid and account_id in(select id from public.accounts where user_id=v_uid and plaid_item_id=p_item);
 end if;
 delete from public.plaid_items where user_id=v_uid and item_id=p_item;
 return v_count;
end $$;
revoke all on function public.fb_disconnect_begin(text,boolean),public.fb_disconnect_finish(text) from public,anon;
grant execute on function public.fb_disconnect_begin(text,boolean),public.fb_disconnect_finish(text) to authenticated;

create function public.fb_delete_owned_account(p_id uuid) returns integer language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_item text; v_count integer;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 select plaid_item_id into v_item from public.accounts where user_id=v_uid and id=p_id;
 if not found then raise exception 'Account not found'; end if;
 update public.plaid_items set cursor=null,sync_generation=sync_generation+1,needs_reconciliation=true where user_id=v_uid and item_id=v_item;
 delete from public.transactions where user_id=v_uid and account_id=p_id;
 get diagnostics v_count=row_count;
 return v_count;
end $$;
revoke all on function public.fb_delete_owned_account(uuid) from public,anon;
grant execute on function public.fb_delete_owned_account(uuid) to authenticated;

create function public.fb_correct_category(p_id text,p_category text,p_keyword text default '',p_apply boolean default false)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_count integer:=0;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 if p_category is null or length(p_category) not between 1 and 128 or length(p_keyword)>256 or(p_apply and length(trim(p_keyword))=0) then raise exception 'Invalid category change'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 if p_id like 'allocation:%' then
  update public.transaction_allocations set category=p_category where user_id=v_uid and id=substr(p_id,12)::uuid;
 else
  update public.transactions set category=p_category,category_override=p_category,category_origin='manual',category_rule=null,category_rule_version=null where user_id=v_uid and id=p_id;
 end if;
 if not found then raise exception 'Transaction not found'; end if;
 if length(trim(p_keyword))>0 then
  insert into public.category_rules(user_id,keyword,category) values(v_uid,lower(trim(p_keyword)),p_category)
   on conflict(user_id,keyword) do update set category=excluded.category,version=case when category_rules.category<>excluded.category then category_rules.version+1 else category_rules.version end;
  if p_apply then
   update public.transactions set category=p_category,category_origin='rule',category_override=null,category_rule=lower(trim(p_keyword)),category_rule_version=(select version from public.category_rules where user_id=v_uid and keyword=lower(trim(p_keyword))) where user_id=v_uid and strpos(lower(description),lower(trim(p_keyword)))>0;
   get diagnostics v_count=row_count;
   update public.transaction_allocations set category=p_category where user_id=v_uid and transaction_id in(select id from public.transactions where user_id=v_uid and strpos(lower(description),lower(trim(p_keyword)))>0);
  end if;
 end if;
 if p_id not like 'allocation:%' then
  update public.transactions set category=p_category,category_override=p_category,category_origin='manual',category_rule=null,category_rule_version=null where user_id=v_uid and id=p_id;
 end if;
 return jsonb_build_object('updated',v_count);
end $$;
revoke all on function public.fb_correct_category(text,text,text,boolean) from public,anon;
grant execute on function public.fb_correct_category(text,text,text,boolean) to authenticated;

create function public.fb_replace_owned_account(p_id uuid,p_rows jsonb) returns integer language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_account public.accounts%rowtype; v_count integer;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows)>50000 then raise exception 'Invalid transactions'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 select * into v_account from public.accounts where user_id=v_uid and id=p_id;
 if not found then raise exception 'Account not owned'; end if;
 if exists(select 1 from public.plaid_items where user_id=v_uid and item_id=v_account.plaid_item_id) and jsonb_array_length(p_rows)>0 then raise exception 'Append statements for a connected bank'; end if;
 if exists(select 1 from jsonb_array_elements(p_rows) where (value->>'account_id')::uuid is distinct from p_id) then raise exception 'Statement account mismatch'; end if;
 update public.plaid_items set cursor=null,sync_generation=sync_generation+1,needs_reconciliation=true where user_id=v_uid and item_id=v_account.plaid_item_id;
 delete from public.transactions where user_id=v_uid and account_id=p_id;
 insert into public.transactions(user_id,id,date,description,amount,category,account_name,account_type,account_id,
  currency,category_origin,provider_category,builtin_category,category_override,category_rule,category_rule_version,
  statement_file_id,statement_row,statement_reference,statement_match_bank)
 select v_uid,id,date,description,amount,category,v_account.name,v_account.account_type,p_id,
  coalesce(currency,'USD'),coalesce(category_origin,'legacy'),provider_category,builtin_category,category_override,category_rule,category_rule_version,
  statement_file_id,statement_row,statement_reference,false
 from jsonb_to_recordset(p_rows) r(id text,date date,description text,amount numeric,category text,currency text,category_origin text,provider_category text,builtin_category text,category_override text,category_rule text,category_rule_version bigint,statement_file_id text,statement_row integer,statement_reference text);
 get diagnostics v_count=row_count;
 return v_count;
end $$;
revoke all on function public.fb_replace_owned_account(uuid,jsonb) from public,anon;
grant execute on function public.fb_replace_owned_account(uuid,jsonb) to authenticated;

create function public.fb_execute_operation(p_operation_id uuid,p_kind text,p_payload jsonb) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_fingerprint text; v_old financebuddy_private.data_operations%rowtype;
 v_result jsonb; v_count integer; v_id bigint; v_account uuid; v_batch financebuddy_private.data_operations%rowtype;
begin
 if v_uid is null or p_operation_id is null then raise exception 'Authentication required'; end if;
 if p_payload is null or jsonb_typeof(p_payload)<>'object' then raise exception 'Invalid operation'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 v_fingerprint:=encode(sha256(convert_to(jsonb_build_array(p_kind,p_payload)::text,'UTF8')),'hex');
 select * into v_old from financebuddy_private.data_operations where user_id=v_uid and id=p_operation_id;
 if found then
  if v_old.fingerprint<>v_fingerprint or v_old.kind<>p_kind then raise exception 'Operation ID reused with different intent'; end if;
  return v_old.result;
 end if;
 case p_kind
 when 'append' then
  v_result:=public.fb_append_statement(p_payload->'rows',coalesce((p_payload->>'keep_matching')::boolean,false));
  v_result:=v_result||jsonb_build_object('batch_id',p_operation_id);
 when 'undo_batch' then
  select * into v_batch from financebuddy_private.data_operations where user_id=v_uid and id=(p_payload->>'batch_id')::uuid and kind='append';
  if not found then raise exception 'Import batch not found'; end if;
  if v_batch.undone then v_result:='0'::jsonb;
  else
   if exists(select 1 from jsonb_array_elements(v_batch.result->'inserted_rows') r left join public.transactions t on t.user_id=v_uid and t.id=r.value->>'id'
    where t.id is null or t.revision<>(r.value->>'revision')::bigint
    or exists(select 1 from public.transaction_allocations a where a.user_id=v_uid and a.transaction_id=t.id)) then raise exception 'Imported rows have changed'; end if;
   delete from public.transactions where user_id=v_uid and id in(select value->>'id' from jsonb_array_elements(v_batch.result->'inserted_rows'));
   get diagnostics v_count=row_count;
   update financebuddy_private.data_operations set undone=true where user_id=v_uid and id=v_batch.id;
   v_result:=to_jsonb(v_count);
  end if;
 when 'split' then
  perform public.fb_split_transaction(p_payload->>'id',p_payload->>'first_category',(p_payload->>'first_amount')::numeric,p_payload->>'second_category',(p_payload->>'second_amount')::numeric);
  v_result:=jsonb_build_object('saved',true);
 when 'sync' then v_result:=public.fb_apply_plaid_sync(p_payload->>'p_item_id',p_payload->>'p_expected_cursor',p_payload->>'p_next_cursor',(p_payload->>'p_synced_at')::timestamptz,p_payload->'p_upserts',p_payload->'p_removed_ids',coalesce((p_payload->>'p_expected_generation')::bigint,0),coalesce((p_payload->>'p_reconcile')::boolean,false));
 when 'restore' then v_result:=public.fb_restore_backup(p_payload->'backup');
 when 'replace_account' then
  v_account:=(p_payload->>'account_id')::uuid;
  if v_account is null then
   if jsonb_array_length(p_payload->'rows')>0 then select id into v_account from public.fb_statement_account(p_payload->'rows'->0);
   else
    select min(id::text)::uuid into v_account from public.accounts where user_id=v_uid and name=p_payload->>'account_name';
    if(select count(*) from public.accounts where user_id=v_uid and name=p_payload->>'account_name')<>1 then raise exception 'Select an owned account ID'; end if;
   end if;
  end if;
  select coalesce(jsonb_agg(value||jsonb_build_object('account_id',v_account)),'[]'::jsonb) into v_result from jsonb_array_elements(p_payload->'rows');
  v_result:=to_jsonb(public.fb_replace_owned_account(v_account,v_result));
 when 'replace_all' then v_result:=to_jsonb(public.fb_replace_transactions(null,p_payload->'rows'));
 when 'delete_account' then v_result:=to_jsonb(public.fb_delete_owned_account((p_payload->>'account_id')::uuid));
 when 'category' then v_result:=public.fb_correct_category(p_payload->>'id',p_payload->>'category',coalesce(p_payload->>'keyword',''),coalesce((p_payload->>'apply_similar')::boolean,false));
 when 'bulk_category' then
  if jsonb_typeof(p_payload->'ids') is distinct from 'array' or jsonb_array_length(p_payload->'ids')>50000 then raise exception 'Invalid category batch'; end if;
  update public.transactions set category=p_payload->>'category',category_override=p_payload->>'category',category_origin='manual',category_rule=null,category_rule_version=null where user_id=v_uid and id in(select value from jsonb_array_elements_text(p_payload->'ids'));
  get diagnostics v_count=row_count;
  update public.transaction_allocations set category=p_payload->>'category' where user_id=v_uid and 'allocation:'||id::text in(select value from jsonb_array_elements_text(p_payload->'ids'));
  get diagnostics v_id=row_count;
  if v_count+v_id<>(select count(distinct value) from jsonb_array_elements_text(p_payload->'ids')) then raise exception 'Some transactions changed'; end if;
  v_result:=to_jsonb(v_count+v_id);
 when 'disconnect_begin' then v_result:=to_jsonb(public.fb_disconnect_begin(p_payload->>'item_id',(p_payload->>'remove_transactions')::boolean));
 when 'disconnect_finish' then v_result:=to_jsonb(public.fb_disconnect_finish(p_payload->>'item_id'));
 when 'budget' then
  insert into public.budgets(user_id,category,monthly_limit) values(v_uid,p_payload->>'category',(p_payload->>'monthly_limit')::numeric) on conflict(user_id,category) do update set monthly_limit=excluded.monthly_limit;
  v_result:='true'::jsonb;
 when 'delete_budget' then delete from public.budgets where user_id=v_uid and category=p_payload->>'category'; v_result:='true'::jsonb;
 when 'goal' then
  if p_payload->>'id' is null then
   insert into public.savings_goals(user_id,name,target_amount,current_amount,target_date) values(v_uid,p_payload->>'name',(p_payload->>'target_amount')::numeric,coalesce((p_payload->>'current_amount')::numeric,0),(p_payload->>'target_date')::date) returning id into v_id;
  else
   update public.savings_goals set name=p_payload->>'name',target_amount=(p_payload->>'target_amount')::numeric,current_amount=(p_payload->>'current_amount')::numeric,target_date=(p_payload->>'target_date')::date where user_id=v_uid and id=(p_payload->>'id')::bigint returning id into v_id;
   if not found then raise exception 'Goal not found'; end if;
  end if;
  v_result:=to_jsonb(v_id);
 when 'delete_goal' then delete from public.savings_goals where user_id=v_uid and id=(p_payload->>'id')::bigint; v_result:='true'::jsonb;
 when 'rule' then
  insert into public.category_rules(user_id,keyword,category) values(v_uid,lower(trim(p_payload->>'keyword')),p_payload->>'category') on conflict(user_id,keyword) do update set category=excluded.category,version=case when category_rules.category<>excluded.category then category_rules.version+1 else category_rules.version end;
  v_result:='true'::jsonb;
 when 'delete_rule' then delete from public.category_rules where user_id=v_uid and keyword=p_payload->>'keyword'; v_result:='true'::jsonb;
 when 'setting' then
  insert into public.app_settings(user_id,key,value) values(v_uid,p_payload->>'key',p_payload->>'value') on conflict(user_id,key) do update set value=excluded.value; v_result:='true'::jsonb;
 else raise exception 'Unsupported operation';
 end case;
 insert into financebuddy_private.data_operations(user_id,id,kind,fingerprint,result) values(v_uid,p_operation_id,p_kind,v_fingerprint,v_result);
 return v_result;
end $$;
revoke all on function public.fb_execute_operation(uuid,text,jsonb) from public,anon;
grant execute on function public.fb_execute_operation(uuid,text,jsonb) to authenticated;
notify pgrst,'reload schema';

-- A new file's claim on a saved row is a provenance edit. Exact file retries
-- keep the revision; older batches cannot undo a row claimed by a newer import.
create function public.fb_import_row_revision() returns trigger language plpgsql security invoker set search_path='' as $$
begin
 if tg_op<>'UPDATE' or new.transaction_id is distinct from old.transaction_id then
  update public.transactions set revision=revision where user_id=coalesce(new.user_id,old.user_id) and id=coalesce(new.transaction_id,old.transaction_id);
 end if;
 return coalesce(new,old);
end $$;
revoke all on function public.fb_import_row_revision() from public,anon;
grant execute on function public.fb_import_row_revision() to authenticated;
create trigger import_row_revision after insert or update or delete on public.statement_import_rows for each row execute function public.fb_import_row_revision();
notify pgrst,'reload schema';

-- Every exported setting/snapshot/account must satisfy the restore models too.
alter table public.app_settings add constraint settings_backup_bounds check(length(key) between 1 and 256 and length(value)<=16384);
alter table public.accounts add constraint account_backup_bounds check(
 (institution_name is null or length(institution_name) between 1 and 256)
 and(plaid_item_id is null or length(plaid_item_id) between 1 and 256)
 and(plaid_account_id is null or length(plaid_account_id) between 1 and 256)
 and(statement_key is null or length(statement_key)<=512));
alter table public.balance_snapshots add constraint snapshot_type_bounds check(length(type) between 1 and 64);
alter table public.category_rules add constraint rule_version_positive check(version>=1);
notify pgrst,'reload schema';
