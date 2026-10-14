-- Read projections retain sources and use allocations only when exact and valid.
-- SECURITY INVOKER is essential: underlying transaction/child RLS remains active.
create view public.fb_transaction_projection with(security_invoker=true) as
 select t.user_id,t.date,t.account_id,t.account_name,t.account_type,t.pending,t.description,t.merchant_name,
  case when a.id is null then t.id else 'allocation:'||a.id::text end id,coalesce(a.amount,t.amount) amount,coalesce(a.category,t.category) category,
  (to_jsonb(t)-'user_id'-'import_match_key')||jsonb_build_object('allocations',coalesce(s.rows,'[]'::jsonb),
   'id',case when a.id is null then t.id else 'allocation:'||a.id::text end,
   'amount',coalesce(a.amount,t.amount),'category',coalesce(a.category,t.category),
   'source_transaction_id',case when a.id is not null then t.id end,
   'source_amount',case when a.id is not null then t.amount end,
   'split_needs_review',coalesce(s.n,0)>0 and not coalesce(s.valid,false)) record
 from public.transactions t
 left join lateral(select count(*) n,sum(amount)=t.amount and bool_and(amount<>0 and (amount>0)=(t.amount>0)) valid,
  jsonb_agg(jsonb_build_object('id',id,'amount',amount,'category',category) order by id) rows
  from public.transaction_allocations where user_id=t.user_id and transaction_id=t.id) s on true
 left join public.transaction_allocations a on a.user_id=t.user_id and a.transaction_id=t.id and s.valid;
revoke all on public.fb_transaction_projection from public,anon;
grant select on public.fb_transaction_projection to authenticated;

create function public.fb_transaction_catalog() returns jsonb language sql stable security invoker set search_path='' as $$
 select jsonb_build_object('count',(select count(*) from public.transactions where user_id=(select auth.uid())),
  'display_count',(select count(*) from public.fb_transaction_projection where user_id=(select auth.uid())),
  'min_date',(select min(date) from public.transactions where user_id=(select auth.uid())),
  'max_date',(select max(date) from public.transactions where user_id=(select auth.uid())),
  'needs_review',(select count(*) from public.fb_transaction_projection where user_id=(select auth.uid()) and category='Uncategorized'),
  'categories',(select coalesce(jsonb_agg(category order by category),'[]'::jsonb) from(select distinct category from public.fb_transaction_projection where user_id=(select auth.uid())) c),
  'accounts',(select coalesce(jsonb_agg(to_jsonb(q) order by name,id),'[]'::jsonb) from(
   select a.id,a.name,a.account_type,a.institution_name,a.plaid_item_id,a.plaid_account_id,
    count(t.id) transactions,min(t.date) first_date,max(t.date) latest_date,
    exists(select 1 from public.plaid_items i where i.user_id=a.user_id and i.item_id=a.plaid_item_id) connected
   from public.accounts a left join public.transactions t on t.user_id=a.user_id and t.account_id=a.id
   where a.user_id=(select auth.uid()) group by a.user_id,a.id) q)) $$;
revoke all on function public.fb_transaction_catalog() from public,anon;
grant execute on function public.fb_transaction_catalog() to authenticated;

-- This invoker-only SQL helper intentionally has no SET clause so PostgreSQL
-- can inline filters/keyset bounds before building projection JSON. Every object
-- reference is qualified; it neither elevates privileges nor changes caller RLS.
create function public.fb_filtered_transactions(p_filters jsonb) returns setof public.fb_transaction_projection
language sql stable security invoker as $$
 select v.* from public.fb_transaction_projection v where v.user_id=(select auth.uid())
  and(v.date>=coalesce((p_filters->>'start')::date,'0001-01-01'::date)) and(v.date<=coalesce((p_filters->>'end')::date,'9999-12-31'::date))
  and(not(p_filters?'accounts') or v.account_id::text in(select pg_catalog.jsonb_array_elements_text(p_filters->'accounts')))
  and(not(p_filters?'categories') or v.category in(select pg_catalog.jsonb_array_elements_text(p_filters->'categories')))
  and(coalesce(p_filters->>'type','Both')='Both' or(p_filters->>'type'='Money in' and v.amount>0) or(p_filters->>'type'='Money out' and v.amount<0))
  and(pg_catalog.abs(v.amount)>=coalesce((p_filters->>'minimum')::numeric,0))
  and(coalesce(p_filters->>'search','')='' or pg_catalog.strpos(pg_catalog.lower(v.description||' '||coalesce(v.merchant_name,'')),pg_catalog.lower(p_filters->>'search'))>0)
  and(coalesce(p_filters->>'view','All') not in('Pending','Needs a category') or(p_filters->>'view'='Pending' and v.pending) or(p_filters->>'view'='Needs a category' and v.category='Uncategorized'))
  and(coalesce((p_filters->>'include_transfers')::boolean,true) or v.category not in('Transfers','Credit Card Payments')) $$;
revoke all on function public.fb_filtered_transactions(jsonb) from public,anon;
grant execute on function public.fb_filtered_transactions(jsonb) to authenticated;

create function public.fb_query_transactions(p_filters jsonb default '{}'::jsonb,p_limit integer default 200,p_after jsonb default null)
returns jsonb language plpgsql stable security invoker set search_path='' as $$
declare v_rows jsonb; v_last jsonb; v_count bigint;
begin
 if (select auth.uid()) is null then raise exception 'Authentication required'; end if;
 if p_limit is null or p_limit not between 1 and 1000 or jsonb_typeof(p_filters)<>'object' then raise exception 'Invalid query'; end if;
 select coalesce(jsonb_agg(record order by date desc,id desc),'[]'::jsonb) into v_rows from(
  select * from public.fb_filtered_transactions(p_filters)
  where p_after is null or(date,id)<((p_after->>'date')::date,p_after->>'id') order by date desc,id desc limit p_limit+1) q;
 if jsonb_array_length(v_rows)>p_limit then
  v_rows:=v_rows-p_limit;
  v_last:=jsonb_build_object('date',v_rows->(p_limit-1)->>'date','id',v_rows->(p_limit-1)->>'id');
 end if;
 if not coalesce((p_filters->>'skip_count')::boolean,false) then
  select count(*) into v_count from public.fb_filtered_transactions(p_filters);
 end if;
 return jsonb_build_object('rows',v_rows,'next_cursor',v_last,'count',v_count);
end $$;
revoke all on function public.fb_query_transactions(jsonb,integer,jsonb) from public,anon;
grant execute on function public.fb_query_transactions(jsonb,integer,jsonb) to authenticated;

create function public.fb_transaction_summary(p_filters jsonb default '{}'::jsonb) returns jsonb
language sql stable security invoker set search_path='' as $$
 with matching as materialized(select date,amount,category from public.fb_filtered_transactions(p_filters))
 select jsonb_build_object('count',count(*),'inflow',coalesce(sum(amount) filter(where amount>0),0),
  'outflow',coalesce(-sum(amount) filter(where amount<0),0),'net',coalesce(sum(amount),0),
  'categories',(select coalesce(jsonb_agg(to_jsonb(c) order by amount desc,category),'[]'::jsonb) from(
   select category,-sum(amount) amount,count(*) transactions,-avg(amount) average,
    case when sum(-sum(amount)) over()>0 then -sum(amount)/sum(-sum(amount)) over()*100 else 0 end share
   from matching where amount<0 group by category) c),
  'monthly',(select coalesce(jsonb_agg(to_jsonb(m) order by month),'[]'::jsonb) from(
   select to_char(date,'YYYY-MM') as month,coalesce(sum(amount) filter(where amount>0),0) inflow,coalesce(-sum(amount) filter(where amount<0),0) outflow from matching group by to_char(date,'YYYY-MM')) m)) from matching $$;
revoke all on function public.fb_transaction_summary(jsonb) from public,anon;
grant execute on function public.fb_transaction_summary(jsonb) to authenticated;

create function public.fb_existing_transaction_ids(p_ids jsonb) returns jsonb language sql stable security invoker set search_path='' as $$
 select coalesce(jsonb_agg(id),'[]'::jsonb) from public.transactions where user_id=(select auth.uid()) and id in(select jsonb_array_elements_text(p_ids)) $$;
revoke all on function public.fb_existing_transaction_ids(jsonb) from public,anon;
grant execute on function public.fb_existing_transaction_ids(jsonb) to authenticated;
notify pgrst,'reload schema';

-- Only rows capable of matching this file enter a preview, including references
-- and durable aliases whose bank dates/amounts may have changed since import.
create function public.fb_statement_candidates(p_rows jsonb) returns jsonb language plpgsql stable security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_rows jsonb; v_aliases jsonb;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 if jsonb_typeof(p_rows) is distinct from 'array' or jsonb_array_length(p_rows)>50000 then raise exception 'Invalid statement'; end if;
 with incoming as materialized(select * from jsonb_to_recordset(p_rows) r(id text,date date,account_id uuid,account_name text,account_type text,statement_file_id text,statement_reference text)),
 candidates as(select t.* from public.transactions t where t.user_id=v_uid and(
  (t.date between(select min(date) from incoming) and(select max(date) from incoming) and exists(select 1 from incoming i where
   (i.account_id is not null and t.account_id=i.account_id) or(i.account_id is null and public.fb_import_normalize(t.account_name)=public.fb_import_normalize(i.account_name) and public.fb_import_normalize(t.account_type)=public.fb_import_normalize(i.account_type))))
  or t.id in(select id from incoming)
  or t.id in(select m.transaction_id from public.statement_import_rows m where m.user_id=v_uid and m.file_id in(select statement_file_id from incoming))
  or exists(select 1 from incoming i where i.statement_reference=t.statement_reference and
   ((i.account_id is not null and i.account_id=t.account_id) or(i.account_id is null and public.fb_import_normalize(i.account_name)=public.fb_import_normalize(t.account_name))))))
 select coalesce(jsonb_agg(to_jsonb(t)-'user_id'-'import_match_key'||jsonb_build_object('allocations',(select coalesce(jsonb_agg(jsonb_build_object('id',a.id,'amount',a.amount,'category',a.category)),'[]'::jsonb) from public.transaction_allocations a where a.user_id=v_uid and a.transaction_id=t.id)) order by t.id),'[]'::jsonb) into v_rows from candidates t;
 select coalesce(jsonb_agg(to_jsonb(m)-'user_id'),'[]'::jsonb) into v_aliases from public.statement_import_rows m where m.user_id=v_uid and m.file_id in(select value->>'statement_file_id' from jsonb_array_elements(p_rows));
 return jsonb_build_object('transactions',v_rows,'mappings',v_aliases);
end $$;
revoke all on function public.fb_statement_candidates(jsonb) from public,anon;
grant execute on function public.fb_statement_candidates(jsonb) to authenticated;
notify pgrst,'reload schema';

-- Seed the window with each account's last earlier balance. Bounded history must
-- not silently omit a dormant account from every later net worth calculation.
create function public.fb_balance_history(p_since date) returns jsonb language sql stable security invoker set search_path='' as $$
 with earlier as(select distinct on(account_id) * from public.balance_snapshots where user_id=(select auth.uid()) and as_of<p_since order by account_id,as_of desc),
 rows as(select to_jsonb(b)-'user_id' record,b.as_of,b.account_id from public.balance_snapshots b where b.user_id=(select auth.uid()) and b.as_of>=p_since
  union all select to_jsonb(b)-'user_id'||jsonb_build_object('as_of',p_since,'carried_forward',true),p_since,b.account_id from earlier b
   where not exists(select 1 from public.balance_snapshots s where s.user_id=b.user_id and s.account_id=b.account_id and s.as_of=p_since))
 select coalesce(jsonb_agg(record order by as_of,account_id),'[]'::jsonb) from rows $$;
revoke all on function public.fb_balance_history(date) from public,anon;
grant execute on function public.fb_balance_history(date) to authenticated;
notify pgrst,'reload schema';
