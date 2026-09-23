-- Run with: supabase db query --linked --file scripts/verify_private_plaid_tokens.sql
-- Verifies own-token retrieval, cross-user denial, anon denial, and rolls back writes.
begin;
do $$
declare v_ids uuid[];
begin
  select array_agg(id order by id) into v_ids
  from (select id from auth.users order by id limit 2) users;
  if pg_catalog.array_length(v_ids, 1) <> 2 then raise exception 'Two users required'; end if;
  perform pg_catalog.set_config('app.test_user_a',v_ids[1]::text,true);
  perform pg_catalog.set_config('app.test_user_b',v_ids[2]::text,true);
end $$;
set local role anon;
do $$
begin
  begin
    perform public.fb_plaid_token('__private_test');
    raise exception 'Anonymous token read was allowed';
  exception when insufficient_privilege then null;
  end;
end $$;
set local role authenticated;
do $$
begin
  perform pg_catalog.set_config('request.jwt.claim.sub',current_setting('app.test_user_a'),true);
  perform public.fb_save_plaid_item('__private_test','encrypted-test-token','Test bank');
  if public.fb_plaid_token('__private_test') <> 'encrypted-test-token' then
    raise exception 'Owner cannot read token';
  end if;
  if not exists (select 1 from public.plaid_items where item_id='__private_test') then
    raise exception 'Public metadata was not saved';
  end if;
  perform pg_catalog.set_config('request.jwt.claim.sub',current_setting('app.test_user_b'),true);
  if public.fb_plaid_token('__private_test') is not null then
    raise exception 'Cross-user token read was allowed';
  end if;
end $$;
rollback;
select 'Private token checks passed; writes rolled back' as result;
