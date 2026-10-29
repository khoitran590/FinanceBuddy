-- Notes and tags live beside transactions, not on them: editing a note must not
-- bump a transaction's revision (which blocks import undo) or re-run the
-- category policy trigger on rule/provider/built-in rows.
create function public.fb_tags_valid(p_tags text[]) returns boolean language sql immutable set search_path='' as $$
 select cardinality(p_tags)<=20 and coalesce(bool_and(length(t) between 1 and 40 and t=lower(trim(t))),true) from unnest(p_tags) t $$;
revoke all on function public.fb_tags_valid(text[]) from public,anon;
grant execute on function public.fb_tags_valid(text[]) to authenticated;

create table public.transaction_annotations(
 user_id uuid not null references auth.users(id) on delete cascade,
 transaction_id text not null,
 notes text check(notes is null or length(notes) between 1 and 2000),
 tags text[] not null default '{}' check(public.fb_tags_valid(tags)),
 primary key(user_id,transaction_id),
 foreign key(user_id,transaction_id) references public.transactions(user_id,id) on delete cascade
);
create index transaction_annotations_tags_idx on public.transaction_annotations using gin(tags);
alter table public.transaction_annotations enable row level security;
revoke all on public.transaction_annotations from public,anon;
grant select,insert,update,delete on public.transaction_annotations to authenticated;
create policy owner_all on public.transaction_annotations for all to authenticated using((select auth.uid())=user_id) with check((select auth.uid())=user_id);

-- A posted bank row replaces its pending row; carry the note across before the
-- pending row (and, by cascade, its annotation) is deleted.
create function public.fb_move_pending_annotation() returns trigger language plpgsql security invoker set search_path='' as $$
begin
 update public.transaction_annotations a set transaction_id=new.id
  where a.user_id=new.user_id and a.transaction_id=new.pending_transaction_id and new.id<>new.pending_transaction_id
   and not exists(select 1 from public.transaction_annotations b where b.user_id=new.user_id and b.transaction_id=new.id);
 return null;
end $$;
revoke all on function public.fb_move_pending_annotation() from public,anon;
grant execute on function public.fb_move_pending_annotation() to authenticated;
create trigger move_pending_annotation after insert or update of pending_transaction_id on public.transactions
 for each row when(new.pending_transaction_id is not null) execute function public.fb_move_pending_annotation();

create or replace view public.fb_transaction_projection with(security_invoker=true) as
 select t.user_id,t.date,t.account_id,t.account_name,t.account_type,t.pending,t.description,t.merchant_name,
  case when a.id is null then t.id else 'allocation:'||a.id::text end id,coalesce(a.amount,t.amount) amount,coalesce(a.category,t.category) category,
  (to_jsonb(t)-'user_id'-'import_match_key')||jsonb_build_object('allocations',coalesce(s.rows,'[]'::jsonb),
   'id',case when a.id is null then t.id else 'allocation:'||a.id::text end,
   'amount',coalesce(a.amount,t.amount),'category',coalesce(a.category,t.category),
   'source_transaction_id',case when a.id is not null then t.id end,
   'source_amount',case when a.id is not null then t.amount end,
   'split_needs_review',coalesce(s.n,0)>0 and not coalesce(s.valid,false),
   'notes',n.notes,'tags',coalesce(to_jsonb(n.tags),'[]'::jsonb)) record,
  coalesce(n.tags,'{}'::text[]) tags
 from public.transactions t
 left join lateral(select count(*) n,sum(amount)=t.amount and bool_and(amount<>0 and (amount>0)=(t.amount>0)) valid,
  jsonb_agg(jsonb_build_object('id',id,'amount',amount,'category',category) order by id) rows
  from public.transaction_allocations where user_id=t.user_id and transaction_id=t.id) s on true
 left join public.transaction_allocations a on a.user_id=t.user_id and a.transaction_id=t.id and s.valid
 left join public.transaction_annotations n on n.user_id=t.user_id and n.transaction_id=t.id;

create or replace function public.fb_filtered_transactions(p_filters jsonb) returns setof public.fb_transaction_projection
language sql stable security invoker as $$
 select v.* from public.fb_transaction_projection v where v.user_id=(select auth.uid())
  and(v.date>=coalesce((p_filters->>'start')::date,'0001-01-01'::date)) and(v.date<=coalesce((p_filters->>'end')::date,'9999-12-31'::date))
  and(not(p_filters?'accounts') or v.account_id::text in(select pg_catalog.jsonb_array_elements_text(p_filters->'accounts')))
  and(not(p_filters?'categories') or v.category in(select pg_catalog.jsonb_array_elements_text(p_filters->'categories')))
  and(coalesce(p_filters->>'type','Both')='Both' or(p_filters->>'type'='Money in' and v.amount>0) or(p_filters->>'type'='Money out' and v.amount<0))
  and(pg_catalog.abs(v.amount)>=coalesce((p_filters->>'minimum')::numeric,0))
  and(coalesce(p_filters->>'search','')='' or pg_catalog.strpos(pg_catalog.lower(v.description||' '||coalesce(v.merchant_name,'')),pg_catalog.lower(p_filters->>'search'))>0)
  and(coalesce(p_filters->>'view','All') not in('Pending','Needs a category') or(p_filters->>'view'='Pending' and v.pending) or(p_filters->>'view'='Needs a category' and v.category='Uncategorized'))
  and(coalesce((p_filters->>'include_transfers')::boolean,true) or v.category not in('Transfers','Credit Card Payments'))
  and(not(p_filters?'tags') or v.tags operator(pg_catalog.&&) array(select pg_catalog.jsonb_array_elements_text(p_filters->'tags'))) $$;

alter function financebuddy_private.fb_execute_extension(uuid,text,jsonb) rename to fb_execute_extension_base;
create function financebuddy_private.fb_execute_extension(p_uid uuid,p_kind text,p_payload jsonb) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare v_id text; v_notes text; v_tags text[];
begin
 case p_kind
 when 'annotate' then
  v_id:=p_payload->>'id';
  if v_id like 'allocation:%' then
   select transaction_id into v_id from public.transaction_allocations where user_id=p_uid and id::text=substr(v_id,12);
  end if;
  if v_id is null or not exists(select 1 from public.transactions where user_id=p_uid and id=v_id) then raise exception 'Transaction not found'; end if;
  v_notes:=nullif(trim(coalesce(p_payload->>'notes','')),'');
  select coalesce(array_agg(distinct lower(trim(value)) order by lower(trim(value))) filter(where trim(value)<>''),'{}') into v_tags
   from jsonb_array_elements_text(coalesce(p_payload->'tags','[]'::jsonb));
  if v_notes is null and cardinality(v_tags)=0 then
   delete from public.transaction_annotations where user_id=p_uid and transaction_id=v_id;
  else
   insert into public.transaction_annotations(user_id,transaction_id,notes,tags) values(p_uid,v_id,v_notes,v_tags)
    on conflict(user_id,transaction_id) do update set notes=excluded.notes,tags=excluded.tags;
  end if;
  return 'true'::jsonb;
 when 'manual_add' then
  -- Client-generated manual: IDs make retries idempotent through the receipt.
  if coalesce(p_payload->'row'->>'id','') not like 'manual:%' then raise exception 'Invalid manual transaction'; end if;
  if exists(select 1 from public.accounts where user_id=p_uid and id::text=p_payload->'row'->>'account_id' and plaid_item_id is not null) then
   raise exception 'Add manual entries to a cash or statement account'; end if;
  insert into public.transactions(user_id,id,date,description,amount,category,account_name,account_type,account_id,category_origin,category_override)
   select p_uid,r.id,r.date,r.description,r.amount,r.category,r.account_name,r.account_type,r.account_id,'manual',r.category
   from jsonb_to_record(p_payload->'row') r(id text,date date,description text,amount numeric,category text,account_name text,account_type text,account_id uuid);
  perform financebuddy_private.fb_execute_extension(p_uid,'annotate',jsonb_build_object('id',p_payload->'row'->>'id','notes',p_payload->>'notes','tags',coalesce(p_payload->'tags','[]'::jsonb)));
  return to_jsonb(1);
 when 'manual_delete' then
  delete from public.transactions where user_id=p_uid and id=p_payload->>'id' and id like 'manual:%';
  if not found then raise exception 'Transaction not found'; end if;
  return to_jsonb(1);
 else return financebuddy_private.fb_execute_extension_base(p_uid,p_kind,p_payload);
 end case;
end $$;
revoke all on function financebuddy_private.fb_execute_extension(uuid,text,jsonb) from public,anon;
grant execute on function financebuddy_private.fb_execute_extension(uuid,text,jsonb) to authenticated;

-- Backup version 4 = version 3 plus transaction annotations.
create or replace function public.fb_export_backup() returns jsonb language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_result jsonb; v_key text;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 -- One SQL statement = one MVCC snapshot across every exported collection.
 select jsonb_build_object('version',4,'currency','USD',
  'transactions',(select coalesce(jsonb_agg(to_jsonb(t)-'user_id'-'import_match_key'||jsonb_build_object('amount',t.amount::text,'allocations',(select coalesce(jsonb_agg(to_jsonb(a)-'user_id'-'transaction_id'||jsonb_build_object('amount',a.amount::text) order by a.id),'[]'::jsonb) from public.transaction_allocations a where a.user_id=t.user_id and a.transaction_id=t.id)) order by t.date,t.id),'[]'::jsonb) from public.transactions t where t.user_id=v_uid),
  'budgets',(select coalesce(jsonb_agg(to_jsonb(b)-'user_id'||jsonb_build_object('monthly_limit',b.monthly_limit::text) order by b.category),'[]'::jsonb) from public.budgets b where b.user_id=v_uid),
  'goals',(select coalesce(jsonb_agg(to_jsonb(g)-'user_id'||jsonb_build_object('target_amount',g.target_amount::text,'current_amount',g.current_amount::text) order by g.id),'[]'::jsonb) from public.savings_goals g where g.user_id=v_uid),
  'category_rules',(select coalesce(jsonb_agg(to_jsonb(r)-'user_id' order by r.keyword),'[]'::jsonb) from public.category_rules r where r.user_id=v_uid),
  'statement_import_rows',(select coalesce(jsonb_agg(to_jsonb(r)-'user_id' order by r.file_id,r.row_number),'[]'::jsonb) from public.statement_import_rows r where r.user_id=v_uid),
  'accounts',(select coalesce(jsonb_agg(to_jsonb(a)-'user_id' order by a.id),'[]'::jsonb) from public.accounts a where a.user_id=v_uid),
  'settings',(select coalesce(jsonb_agg(to_jsonb(s)-'user_id' order by s.key),'[]'::jsonb) from public.app_settings s where s.user_id=v_uid),
  'balance_snapshots',(select coalesce(jsonb_agg(to_jsonb(b)-'user_id'||jsonb_build_object('current_balance',b.current_balance::text,'available_balance',b.available_balance::text,'credit_limit',b.credit_limit::text) order by b.account_id,b.as_of),'[]'::jsonb) from public.balance_snapshots b where b.user_id=v_uid),
  'transaction_annotations',(select coalesce(jsonb_agg(to_jsonb(n)-'user_id' order by n.transaction_id),'[]'::jsonb) from public.transaction_annotations n where n.user_id=v_uid)) into v_result;
 foreach v_key in array array['transactions','budgets','goals','category_rules','statement_import_rows','accounts','settings','balance_snapshots','transaction_annotations'] loop
  if jsonb_array_length(v_result->v_key)>50000 then raise exception 'Backup exceeds restore record limits'; end if;
 end loop;
 if octet_length(convert_to(v_result::text,'UTF8'))>10485760 then raise exception 'Backup exceeds the 10 MB restore limit'; end if;
 return v_result;
end $$;
revoke all on function public.fb_export_backup() from public,anon;
grant execute on function public.fb_export_backup() to authenticated;

create or replace function public.fb_restore_backup(p_backup jsonb) returns jsonb language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_result jsonb; v_version integer:=(p_backup->>'version')::integer; v_key text;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 if v_version not in(1,2,3,4) or coalesce(p_backup->>'currency','USD')<>'USD' or octet_length(convert_to(p_backup::text,'UTF8'))>10485760 then raise exception 'Invalid backup'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 if v_version>=3 then
  foreach v_key in array array['accounts','settings','balance_snapshots'] loop
   if jsonb_typeof(p_backup->v_key) is distinct from 'array' or jsonb_array_length(p_backup->v_key)>50000 then raise exception 'Invalid backup collection'; end if;
  end loop;
  insert into public.accounts(user_id,id,name,account_type,currency,institution_name,plaid_item_id,plaid_account_id,statement_key)
   select v_uid,id,name,account_type,currency,institution_name,plaid_item_id,plaid_account_id,statement_key from jsonb_to_recordset(p_backup->'accounts') r(id uuid,name text,account_type text,currency text,institution_name text,plaid_item_id text,plaid_account_id text,statement_key text)
   on conflict(user_id,id) do update set name=excluded.name,account_type=excluded.account_type,institution_name=coalesce(accounts.institution_name,excluded.institution_name);
 end if;
 v_result:=financebuddy_private.fb_restore_backup_portable(p_backup||jsonb_build_object('version',case when v_version>=3 then 2 else v_version end));
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
 if v_version>=3 then
  delete from public.app_settings where user_id=v_uid;
  insert into public.app_settings(user_id,key,value) select v_uid,key,value from jsonb_to_recordset(p_backup->'settings') r(key text,value text);
  delete from public.balance_snapshots where user_id=v_uid;
  insert into public.balance_snapshots(user_id,account_id,as_of,type,current_balance,available_balance,credit_limit,currency)
   select v_uid,account_id,as_of,type,current_balance,available_balance,credit_limit,currency from jsonb_to_recordset(p_backup->'balance_snapshots') r(account_id uuid,as_of date,type text,current_balance numeric,available_balance numeric,credit_limit numeric,currency text);
 end if;
 if v_version=4 then
  if jsonb_typeof(p_backup->'transaction_annotations') is distinct from 'array' or jsonb_array_length(p_backup->'transaction_annotations')>50000 then raise exception 'Invalid backup collection'; end if;
  insert into public.transaction_annotations(user_id,transaction_id,notes,tags)
   select v_uid,r.transaction_id,r.notes,coalesce(r.tags,'{}') from jsonb_to_recordset(p_backup->'transaction_annotations') r(transaction_id text,notes text,tags text[]);
 end if;
 return v_result;
end $$;
revoke all on function public.fb_restore_backup(jsonb) from public,anon;
grant execute on function public.fb_restore_backup(jsonb) to authenticated;
notify pgrst,'reload schema';
