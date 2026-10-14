-- Disposable PostgreSQL/Supabase regression test; all changes roll back.
begin;
do $$
declare v_uid uuid;
begin
  select id into v_uid from auth.users order by id limit 1;
  if v_uid is null then raise exception 'A test user is required'; end if;
  perform set_config('request.jwt.claim.sub',v_uid::text,true);
end $$;
set local role authenticated;
do $$
declare
  a jsonb; b jsonb; refs jsonb; result jsonb; backup jsonb; count_before integer; imported jsonb; bank_rows jsonb;
begin
  a:=jsonb_build_array(
    jsonb_build_object('id','statement:__a1','date','2026-10-01','description','Coffee','amount',-4.5,
      'category','Dining','account_name','Import test','account_type','Checking','statement_file_id',repeat('a',64),'statement_row',1),
    jsonb_build_object('id','statement:__a2','date','2026-10-01','description','Coffee','amount',-4.5,
      'category','Dining','account_name','Import test','account_type','Checking','statement_file_id',repeat('a',64),'statement_row',2));
  result:=public.fb_append_statement(a);
  if result->>'inserted'<>'2' or jsonb_array_length(result->'inserted_rows')<>2 then
    raise exception 'Identical legitimate purchases collapsed'; end if;
  if (select sum(amount) from public.transactions where account_name='Import test')<>-9 then
    raise exception 'Repeated purchase totals incorrect'; end if;
  result:=public.fb_append_statement(a,true);
  if result->>'inserted'<>'0' or result->>'matched'<>'2' then raise exception 'Exact retry duplicated rows'; end if;
  b:=jsonb_set(jsonb_set(a,'{0,id}','"statement:__b1"'),'{0,statement_file_id}',to_jsonb(repeat('b',64)));
  b:=jsonb_set(jsonb_set(b,'{1,id}','"statement:__b2"'),'{1,statement_file_id}',to_jsonb(repeat('b',64)));
  b:=b || jsonb_build_array(jsonb_build_object('id','statement:__b3','date','2026-10-01','description','Coffee','amount',-4.5,
    'category','Dining','account_name','Import test','account_type','Checking','statement_file_id',repeat('b',64),'statement_row',3));
  result:=public.fb_append_statement(b);
  if result->>'inserted'<>'1' or result->>'matched'<>'2' then raise exception 'Occurrence reconciliation failed'; end if;
  if public.fb_undo_append(result->'inserted_rows')<>1 then raise exception 'Undo did not use actual inserted IDs'; end if;
  if (select count(*) from public.transactions where account_name='Import test')<>2 then raise exception 'Undo removed existing matches'; end if;
  result:=public.fb_append_statement(b,true);
  if result->>'inserted'<>'1' then raise exception 'Retry after undo did not recreate missing row'; end if;

  -- Different bank references are different purchases, even with equal content.
  refs:=jsonb_set(jsonb_set(a,'{0,id}','"statement:__ref1"'),'{0,statement_file_id}',to_jsonb(repeat('c',64)));
  refs:=jsonb_set(jsonb_set(refs,'{1,id}','"statement:__ref2"'),'{1,statement_file_id}',to_jsonb(repeat('c',64)));
  refs:=jsonb_set(refs,'{0,statement_reference}','"first"',true);
  refs:=jsonb_set(refs,'{1,statement_reference}','"second"',true);
  result:=public.fb_append_statement(refs,true);
  if result->>'inserted'<>'2' then raise exception 'Distinct references collapsed'; end if;
  refs:=jsonb_set(jsonb_set(refs,'{0,statement_file_id}',to_jsonb(repeat('d',64))),'{1,statement_file_id}',to_jsonb(repeat('d',64)));
  refs:=jsonb_set(jsonb_set(refs,'{0,id}','"statement:__refretry1"'),'{1,id}','"statement:__refretry2"');
  result:=public.fb_append_statement(refs,true);
  if result->>'inserted'<>'0' then raise exception 'Reference retry created new records'; end if;
  select count(*) into count_before from public.transactions;
  refs:=jsonb_set(jsonb_set(refs,'{0,statement_file_id}',to_jsonb(repeat('8',64))),'{1,statement_file_id}',to_jsonb(repeat('8',64)));
  refs:=jsonb_set(jsonb_set(refs,'{0,id}','"statement:__conflict1"'),'{1,id}','"statement:__conflict2"');
  refs:=jsonb_set(refs,'{0,statement_reference}','"brandnew"');
  refs:=jsonb_set(refs,'{1,amount}','-9');
  -- A partially attempted conflicting batch must roll back both rows and receipts.
  begin
    perform public.fb_append_statement(refs);
    raise exception 'Conflicting reference accepted';
  exception when raise_exception then
    if sqlerrm='Conflicting reference accepted' then raise; end if;
  end;
  if (select count(*) from public.transactions)<>count_before then raise exception 'Reference conflict partially committed'; end if;
  if exists(select 1 from public.statement_import_rows where file_id=repeat('8',64)) then
    raise exception 'Reference conflict committed retry receipts'; end if;

  -- Exact bank content matches preserve the provider record and its allocations.
  insert into public.transactions(user_id,id,date,description,amount,category,account_name,account_type)
    values(auth.uid(),'plaid:__import_bank','2026-10-01','Coffee',-4.5,'Travel','Bank import test','Checking');
  perform public.fb_split_transaction('plaid:__import_bank','Groceries',2,'Dining',2.5);
  a:=jsonb_set(jsonb_set(jsonb_build_array(a->0),'{0,id}','"statement:__bank"'),'{0,account_name}','"Bank import test"');
  a:=jsonb_set(a,'{0,statement_file_id}',to_jsonb(repeat('e',64)));
  result:=public.fb_append_statement(a);
  if result->>'inserted'<>'0' or (select category from public.transactions where id='plaid:__import_bank')<>'Travel'
    or (select count(*) from public.transaction_allocations where transaction_id='plaid:__import_bank')<>2 then
    raise exception 'Bank matching changed annotations or counted twice'; end if;
  update public.transactions set amount=-5 where id='plaid:__import_bank';
  if public.fb_append_statement(a)->>'inserted'<>'0' then raise exception 'Receipt retry duplicated changed provider row'; end if;

  -- Backups carry source identity and import aliases; original bank data stays current.
  select jsonb_build_object('version',2,
    'transactions',(select jsonb_agg(to_jsonb(t)) from public.transactions t),
    'budgets','[]'::jsonb,'goals','[]'::jsonb,'category_rules','[]'::jsonb,
    'statement_import_rows',(select jsonb_agg(to_jsonb(m)) from public.statement_import_rows m)) into backup;
  -- Only this test's bank row is unlinked; other unrelated test rows are portable.
  perform public.fb_restore_backup(backup);
  if public.fb_append_statement(a)->>'inserted'<>'0' then raise exception 'Backup lost retry aliases'; end if;
  if (select statement_file_id from public.transactions where id='statement:__a1')<>repeat('a',64) then
    raise exception 'Restore lost source file identity'; end if;

  -- Statement-first matching transfers categories, allocations and every alias.
  imported:=jsonb_build_array(jsonb_build_object('id','statement:__reverse0','date','2026-10-01',
    'description','Coffee','amount',-4.5,'category','Travel','account_name','Reverse test','account_type','Checking',
    'statement_file_id',repeat('9',64),'statement_row',1),
    jsonb_build_object('id','statement:__reverse1','date','2026-10-01','description','Coffee','amount',-4.5,
    'category','Dining','account_name','Reverse test','account_type','Checking','statement_file_id',repeat('9',64),'statement_row',2));
  perform public.fb_append_statement(imported);
  perform public.fb_split_transaction('statement:__reverse0','Groceries',2,'Dining',2.5);
  perform public.fb_save_plaid_item('__reverse','encrypted-test','Reverse bank');
  perform public.fb_replace_plaid_accounts('__reverse','[{"account_id":"__reverseacct","name":"Reverse test","type":"depository"}]');
  begin
    perform public.fb_replace_transactions('Reverse test',imported);
    raise exception 'Connected account statement replacement allowed';
  exception when raise_exception then
    if sqlerrm='Connected account statement replacement allowed' then raise; end if;
  end;
  bank_rows:=jsonb_build_array(jsonb_build_object('id','plaid:__reverse0','date','2026-10-01',
    'description','Coffee','amount',-4.5,'category','Dining','account_name','Reverse test','account_type','Checking'),
    jsonb_build_object('id','plaid:__reverse1','date','2026-10-01','description','Coffee','amount',-4.5,
    'category','Dining','account_name','Reverse test','account_type','Checking'));
  result:=public.fb_apply_plaid_sync('__reverse',null,'current',now(),bank_rows,'[]');
  if result->>'reconciled_imports'<>'2' or (select sum(amount) from public.transactions where account_name='Reverse test')<>-9 then
    raise exception 'Statement-first sync double counted purchases'; end if;
  if (select category from public.transactions where id='plaid:__reverse0')<>'Travel'
    or (select count(*) from public.transaction_allocations where transaction_id='plaid:__reverse0')<>2 then
    raise exception 'Statement-first sync lost category allocations'; end if;
  if public.fb_append_statement(imported)->>'inserted'<>'0' then raise exception 'Merged source lost retry aliases'; end if;
  -- Keeping identical rows separate is an explicit, durable decision.
  imported:=jsonb_set(jsonb_set(imported,'{0,id}','"statement:__separate0"'),'{1,id}','"statement:__separate1"');
  imported:=jsonb_set(jsonb_set(imported,'{0,statement_file_id}',to_jsonb(repeat('7',64))),'{1,statement_file_id}',to_jsonb(repeat('7',64)));
  perform public.fb_append_statement(imported,true);
  bank_rows:=jsonb_set(jsonb_set(bank_rows,'{0,id}','"plaid:__extra0"'),'{1,id}','"plaid:__extra1"');
  result:=public.fb_apply_plaid_sync('__reverse','current','extra',now(),bank_rows,'[]');
  if result->>'reconciled_imports'<>'0' or not exists(select 1 from public.transactions where id='statement:__separate0') then
    raise exception 'Sync merged explicitly separate purchases'; end if;
end $$;
reset role;
do $$
declare v_first uuid; v_other uuid;
begin
  select id into v_first from auth.users order by id limit 1;
  select id into v_other from auth.users where id<>v_first order by id limit 1;
  if v_other is null then raise exception 'Two test users are required'; end if;
  insert into public.transactions(user_id,id,date,description,amount,account_name)
    values(v_other,'__private_import',current_date,'Private',-1,'Private');
  insert into public.statement_import_rows values(v_other,repeat('f',64),1,'__private_import');
end $$;
set local role authenticated;
do $$
begin
  if exists(select 1 from public.statement_import_rows where transaction_id='__private_import') then
    raise exception 'Import receipt RLS leaked another user'; end if;
  begin
    insert into public.statement_import_rows values(auth.uid(),repeat('f',64),2,'__private_import');
    raise exception 'Cross-owner receipt parent accepted';
  exception when foreign_key_violation then null;
  end;
end $$;
set local role anon;
do $$
begin
  begin
    perform public.fb_append_statement('[]');
    raise exception 'Anonymous statement import allowed';
  exception when insufficient_privilege then null;
  end;
end $$;
rollback;
select 'Statement occurrence matching, references, retry, undo, restore, and RLS checks passed' as result;
