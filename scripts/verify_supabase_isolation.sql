-- Run with: supabase db query --linked --file scripts/verify_supabase_isolation.sql
-- All test writes are inside an explicit transaction and rolled back.
begin;
do $$
declare v_ids uuid[];
begin
  select array_agg(id order by id) into v_ids
  from (select id from auth.users order by id limit 2) users;
  if pg_catalog.array_length(v_ids, 1) <> 2 then
    raise exception 'Two test users are required';
  end if;
  perform pg_catalog.set_config('app.test_user_a',v_ids[1]::text,true);
  perform pg_catalog.set_config('app.test_user_b',v_ids[2]::text,true);
end $$;

set local role anon;
do $$
begin
  begin
    perform 1 from public.transactions limit 1;
    raise exception 'Anonymous read was allowed';
  exception when insufficient_privilege then null;
  end;
  begin
    insert into public.transactions(user_id,id,date,description,amount,category,account_name)
    values (null,'__security_test_anon',current_date,'test',-1,'Test','Test');
    raise exception 'Anonymous write was allowed';
  exception when insufficient_privilege then null;
  end;
end $$;

set local role authenticated;
do $$
begin
  perform pg_catalog.set_config('request.jwt.claim.sub',current_setting('app.test_user_b'),true);
  insert into public.transactions(user_id,id,date,description,amount,category,account_name)
  values (current_setting('app.test_user_b')::uuid,'__security_test_b',current_date,'test',-1,'Test','Test');
end $$;

do $$
declare v_count integer;
begin
  perform pg_catalog.set_config('request.jwt.claim.sub',current_setting('app.test_user_a'),true);
  if exists (select 1 from public.transactions where id='__security_test_b') then
    raise exception 'User A read User B transaction';
  end if;
  insert into public.transactions(user_id,id,date,description,amount,category,account_name)
  values (current_setting('app.test_user_a')::uuid,'__security_test_a',current_date,'test',-1,'Test','Test');
  if not exists (select 1 from public.transactions where id='__security_test_a') then
    raise exception 'User A cannot read own transaction';
  end if;
  begin
    insert into public.transactions(user_id,id,date,description,amount,category,account_name)
    values (current_setting('app.test_user_b')::uuid,'__security_test_cross',current_date,'test',-1,'Test','Test');
    raise exception 'Cross-user insert was allowed';
  exception when insufficient_privilege then null;
  end;
  update public.transactions set description='unexpected' where id='__security_test_b';
  get diagnostics v_count = row_count;
  if v_count <> 0 then raise exception 'Cross-user update was allowed'; end if;
  delete from public.transactions where id='__security_test_b';
  get diagnostics v_count = row_count;
  if v_count <> 0 then raise exception 'Cross-user delete was allowed'; end if;
end $$;
rollback;
select 'RLS isolation checks passed; all test writes rolled back' as result;
