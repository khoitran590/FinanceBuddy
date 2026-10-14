-- Run in a test Supabase project or through scripts/test_database.mjs.
-- All writes are rolled back; two Auth users are needed for the isolation check.
begin;
do $$
declare v_id uuid;
begin
  select id into v_id from auth.users order by id limit 1;
  if v_id is null then raise exception 'A test user is required'; end if;
  perform pg_catalog.set_config('request.jwt.claim.sub',v_id::text,true);
end $$;
set local role authenticated;
do $$
declare
  v_uid uuid := auth.uid(); v_rows jsonb; v_backup jsonb; v_generation bigint;
  v_amount float8; v_count integer; v_allocations jsonb; v_other uuid;
begin
  perform public.fb_save_plaid_item('__reconcile','encrypted-test','Test bank');
  perform public.fb_save_plaid_item('__other','encrypted-test','Other bank');
  v_rows := jsonb_build_array(jsonb_build_object(
    'id','plaid:__source','date','2026-09-01','description','Store','amount',-100,
    'category','Shopping','account_name','Card','account_type','Credit Card','plaid_account_id','__account'));
  perform public.fb_apply_plaid_sync('__reconcile',null,'october',now(),v_rows,'[]');
  perform public.fb_split_transaction('plaid:__source','Groceries',60,'Shopping',40);
  if (select count(*) from public.transactions where id='plaid:__source')<>1 then
    raise exception 'Split deleted source';
  end if;
  if (select count(*) from public.transaction_allocations where transaction_id='plaid:__source')<>2 then
    raise exception 'Split did not store allocations';
  end if;
  begin
    perform public.fb_undo_append(v_rows);
    raise exception 'Undo removed split annotations';
  exception when raise_exception then
    if sqlerrm='Undo removed split annotations' then raise; end if;
  end;
  -- Provider changes the amount: the source wins, while annotations remain.
  v_rows := jsonb_set(v_rows,'{0,amount}','-110');
  perform public.fb_apply_plaid_sync('__reconcile','october','newer',now(),v_rows,'[]');
  select amount into v_amount from public.transactions where id='plaid:__source';
  if v_amount<>-110 then raise exception 'Bank total not updated'; end if;
  select sum(amount) into v_amount from public.transaction_allocations where transaction_id='plaid:__source';
  if v_amount<>-100 then raise exception 'Provider destroyed category allocations'; end if;
  -- A failed re-split must preserve the previous allocations.
  begin
    perform public.fb_split_transaction('plaid:__source','Groceries',60,'Shopping',30);
    raise exception 'Invalid re-split accepted';
  exception when raise_exception then
    if sqlerrm='Invalid re-split accepted' then raise; end if;
  end;
  select jsonb_agg(jsonb_build_object('id',id,'amount',amount,'category',category)) into v_allocations
    from public.transaction_allocations where transaction_id='plaid:__source';
  -- Restore an old amount and a stale provider row while retaining bank connections.
  v_backup := jsonb_build_object('version',2,'transactions',jsonb_build_array(
    jsonb_build_object('id','plaid:__source','date','2026-09-01','description','Store','amount',-100,
      'category','Travel','account_name','Card','account_type','Credit Card',
      'plaid_item_id','__reconcile','plaid_account_id','__account','allocations',v_allocations),
    jsonb_build_object('id','plaid:__stale','date','2026-09-01','description','Stale','amount',-25,
      'category','Shopping','account_name','Card','account_type','Credit Card','plaid_item_id','__reconcile'),
    jsonb_build_object('id','plaid:__other','date','2026-09-01','description','Other','amount',-5,
      'category','Shopping','account_name','Other','account_type','Checking','plaid_item_id','__other'),
    jsonb_build_object('id','__statement','date','2026-09-01','description','Imported','amount',-10,
      'category','Shopping','account_name','Checking','account_type','Checking')),
    'budgets','[]'::jsonb,'goals','[]'::jsonb,'category_rules','[]'::jsonb);
  perform public.fb_restore_backup(v_backup);
  select sync_generation into v_generation from public.plaid_items where item_id='__reconcile';
  if not (select needs_reconciliation and cursor is null from public.plaid_items where item_id='__reconcile') then
    raise exception 'Restore did not invalidate cursor';
  end if;
  if (select count(*) from public.transaction_allocations where transaction_id='plaid:__source')<>2 then
    raise exception 'Restore lost allocations';
  end if;
  -- Two restores can share a NULL cursor; generation must still reject stale work.
  perform public.fb_restore_backup(v_backup);
  begin
    perform public.fb_apply_plaid_sync('__reconcile',null,'bad',now(),'[]','[]',v_generation,true);
    raise exception 'Stale generation accepted';
  exception when raise_exception then
    if sqlerrm='Stale generation accepted' then raise; end if;
  end;
  select sync_generation into v_generation from public.plaid_items where item_id='__reconcile';
  -- Incremental updates are forbidden until full reconciliation is requested.
  begin
    perform public.fb_apply_plaid_sync('__reconcile',null,'bad',now(),'[]','[]',v_generation,false);
    raise exception 'Incomplete reconciliation accepted';
  exception when raise_exception then
    if sqlerrm='Incomplete reconciliation accepted' then raise; end if;
  end;
  v_rows := jsonb_set(v_rows,'{0,amount}','-100') || jsonb_build_array(jsonb_build_object(
    'id','plaid:__new','date','2026-10-01','description','New','amount',-15,
    'category','Shopping','account_name','Card','account_type','Credit Card','plaid_account_id','__account'));
  perform public.fb_apply_plaid_sync('__reconcile',null,'latest',now(),v_rows,'[]',v_generation,true);
  if exists(select 1 from public.transactions where id='plaid:__stale') then raise exception 'Stale bank row retained'; end if;
  if not exists(select 1 from public.transactions where id='plaid:__new') then raise exception 'Missing bank row not restored'; end if;
  if (select category from public.transactions where id='plaid:__source')<>'Travel' then raise exception 'User category lost'; end if;
  if not exists(select 1 from public.transactions where id='plaid:__other')
    or not exists(select 1 from public.transactions where id='__statement') then
    raise exception 'Reconciliation affected another source';
  end if;
  -- A provider removal cascades annotations without leaving counted rows.
  perform public.fb_apply_plaid_sync('__reconcile','latest','removed',now(),'[]','["plaid:__source"]',v_generation,false);
  if exists(select 1 from public.transaction_allocations where transaction_id='plaid:__source') then
    raise exception 'Provider removal orphaned allocations';
  end if;
  -- Account replacement invalidates sync even when replacing with an empty list.
  perform public.fb_replace_transactions('Card','[]');
  if not (select needs_reconciliation and cursor is null from public.plaid_items where item_id='__reconcile') then
    raise exception 'Replacement did not invalidate sync';
  end if;
  -- Failed restore rolls back financial data AND cursor/generation changes.
  select sync_generation into v_generation from public.plaid_items where item_id='__reconcile';
  select count(*) into v_count from public.transactions;
  begin
    perform public.fb_restore_backup(v_backup || '{"budgets":[{"category":"Bad","monthly_limit":-1}]}'::jsonb);
    raise exception 'Invalid backup accepted';
  exception when check_violation then null;
  end;
  if (select sync_generation from public.plaid_items where item_id='__reconcile')<>v_generation
    or (select count(*) from public.transactions)<>v_count then raise exception 'Failed restore partially committed'; end if;
end $$;
reset role;
do $$
declare v_other uuid; v_first uuid;
begin
  select id into v_first from auth.users order by id limit 1;
  select id into v_other from auth.users where id<>v_first order by id limit 1;
  if v_other is null then raise exception 'Two test users are required'; end if;
  insert into public.transactions(user_id,id,date,description,amount,account_name)
    values(v_other,'__private_source',current_date,'Private',-100,'Private');
  insert into public.transaction_allocations(user_id,transaction_id,amount,category)
    values(v_other,'__private_source',-100,'Private');
end $$;
set local role authenticated;
do $$
begin
  if exists(select 1 from public.transaction_allocations where transaction_id='__private_source') then
    raise exception 'Allocation RLS leaked another user';
  end if;
  begin
    insert into public.transaction_allocations(user_id,transaction_id,amount,category)
      values(auth.uid(),'__private_source',-100,'Bad');
    raise exception 'Allocation cross-owner parent accepted';
  exception when foreign_key_violation then null;
  end;
end $$;
set local role anon;
do $$
begin
  begin
    perform public.fb_restore_backup('{}');
    raise exception 'Anonymous restore allowed';
  exception when insufficient_privilege then null;
  end;
end $$;
rollback;
select 'Source split, reconciliation, rollback, and RLS checks passed' as result;
