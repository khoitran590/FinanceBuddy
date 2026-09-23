-- Run with: supabase db query --linked --file scripts/verify_atomic_operations.sql
-- Uses a real authenticated role but rolls every test write back.
begin;
do $$
declare v_id uuid;
begin
  select id into v_id from auth.users order by id limit 1;
  if v_id is null then raise exception 'A test user is required'; end if;
  perform pg_catalog.set_config('request.jwt.claim.sub',v_id::text,true);
end $$;
set local role anon;
do $$
begin
  begin
    perform public.fb_replace_transactions(null,'[]'::jsonb);
    raise exception 'Anonymous RPC execution was allowed';
  exception when insufficient_privilege then null;
  end;
end $$;
set local role authenticated;
do $$
declare
  v_before integer;
  v_after integer;
  v_test jsonb;
begin
  select count(*) into v_before from public.transactions;
  v_test := pg_catalog.jsonb_build_array(
    pg_catalog.jsonb_build_object('id','__atomic_test','date',current_date,'description','test',
      'amount',-1,'category','Test','account_name','Test','account_type','Checking'),
    pg_catalog.jsonb_build_object('id','__atomic_test','date',current_date,'description','test',
      'amount',-1,'category','Test','account_name','Test','account_type','Checking')
  );
  begin
    perform public.fb_replace_transactions(null,v_test);
    raise exception 'Duplicate IDs did not fail';
  exception when unique_violation then null;
  end;
  select count(*) into v_after from public.transactions;
  if v_after <> v_before then raise exception 'Failed replacement changed transactions'; end if;

  begin
    perform public.fb_restore_backup(pg_catalog.jsonb_build_object(
      'version',1,'transactions','[]'::jsonb,
      'budgets','[{"category":"Test","monthly_limit":-1}]'::jsonb,
      'goals','[]'::jsonb,'category_rules','[]'::jsonb
    ));
    raise exception 'Invalid backup did not fail';
  exception when check_violation then null;
  end;
  select count(*) into v_after from public.transactions;
  if v_after <> v_before then raise exception 'Failed backup changed transactions'; end if;

  insert into public.transactions(user_id,id,date,description,amount,category,account_name)
  values (auth.uid(),'__atomic_split',current_date,'test',-3,'Test','Test'),
         (auth.uid(),'__atomic_split-split-1',current_date,'test',-1,'Test','Test');
  begin
    perform public.fb_split_transaction('__atomic_split','Test',1,'Test',2);
    raise exception 'Split collision did not fail';
  exception when unique_violation then null;
  end;
  if not exists (select 1 from public.transactions where id='__atomic_split') then
    raise exception 'Failed split lost original';
  end if;
  begin
    perform public.fb_undo_append('[{"id":"__atomic_split","date":"2026-01-01","description":"changed","amount":-3,"category":"Test","account_name":"Test","account_type":"Checking"}]'::jsonb);
    raise exception 'Changed import was removed';
  exception when raise_exception then
    if sqlerrm = 'Changed import was removed' then raise; end if;
  end;
  if not exists (select 1 from public.transactions where id='__atomic_split') then
    raise exception 'Failed undo lost transaction';
  end if;
end $$;
rollback;
select 'Atomic rollback and RPC access checks passed' as result;
