-- Run with: supabase db query --linked --file scripts/verify_atomic_plaid_sync.sql
begin;
do $$
declare v_id uuid;
begin
  select id into v_id from auth.users order by id limit 1;
  perform pg_catalog.set_config('request.jwt.claim.sub',v_id::text,true);
end $$;
set local role authenticated;
do $$
declare
  v_rows jsonb;
  v_count integer;
begin
  perform public.fb_save_plaid_item('__sync_test','encrypted-test-token','Test bank');
  v_rows := pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
    'id','plaid:__sync_test','date',current_date,'description','test',
    'amount',-1,'category','Uncategorized','account_name','Test','account_type','Checking'
  ));
  perform public.fb_apply_plaid_sync('__sync_test',null,'cursor-1',now(),v_rows,'[]'::jsonb);
  if (select cursor from public.plaid_items where item_id='__sync_test') <> 'cursor-1' then
    raise exception 'Sync cursor not saved';
  end if;
  begin
    perform public.fb_apply_plaid_sync('__sync_test',null,'cursor-2',now(),'[]'::jsonb,'[]'::jsonb);
    raise exception 'Stale cursor was accepted';
  exception when raise_exception then
    if sqlerrm = 'Stale cursor was accepted' then raise; end if;
  end;
  begin
    perform public.fb_apply_plaid_sync('__sync_test','cursor-1','cursor-2',now(),
      v_rows || v_rows,'[]'::jsonb);
    raise exception 'Duplicate upserts were accepted';
  exception when cardinality_violation then null;
  end;
  if (select cursor from public.plaid_items where item_id='__sync_test') <> 'cursor-1' then
    raise exception 'Failed sync advanced cursor';
  end if;
  select count(*) into v_count from public.transactions where id='plaid:__sync_test';
  if v_count <> 1 then raise exception 'Failed sync changed transactions'; end if;
end $$;
rollback;
select 'Atomic Plaid sync passed; writes rolled back' as result;
