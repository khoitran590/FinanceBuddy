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
  insert into public.transactions (user_id,id,date,description,amount,category,account_name,account_type)
  select v_uid,r.id,r.date,r.description,r.amount,r.category,r.account_name,r.account_type
  from pg_catalog.jsonb_to_recordset(p_upserts) as r(
    id text,date date,description text,amount float8,category text,
    account_name text,account_type text
  )
  on conflict (user_id,id) do update set
    date=excluded.date,
    description=excluded.description,
    amount=excluded.amount,
    category=case when public.transactions.category='Uncategorized'
      then excluded.category else public.transactions.category end,
    account_name=excluded.account_name,
    account_type=excluded.account_type;
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
