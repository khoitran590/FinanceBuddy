-- Real PostgreSQL semantics: money, account identity, provenance, receipts,
-- pending lineage, consistent backups, keyset projection and two-user isolation.
begin;
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-0000-0000-000000000001',true);

do $$
declare uid uuid:=(select auth.uid()); acct_a uuid; acct_b uuid; batch uuid:=gen_random_uuid(); clean_batch uuid:=gen_random_uuid(); receipt jsonb; saved jsonb; source jsonb;
 page jsonb; cursor jsonb; seen text[]:='{}'; row jsonb; n integer; rev bigint; pending_alloc uuid; snapshot jsonb;
begin
 perform public.fb_save_plaid_item('__arch_a','encrypted-a','Bank A');
 perform public.fb_save_plaid_item('__arch_b','encrypted-b','Bank B');
 perform public.fb_replace_plaid_accounts('__arch_a','[{"account_id":"__acct_a","name":"Same label","type":"depository","iso_currency_code":"USD","current_balance":"100.10"}]');
 perform public.fb_replace_plaid_accounts('__arch_b','[{"account_id":"__acct_b","name":"Same label","type":"depository","iso_currency_code":"USD","current_balance":"200.20"}]');
 select id into acct_a from public.accounts where user_id=uid and plaid_account_id='__acct_a';
 select id into acct_b from public.accounts where user_id=uid and plaid_account_id='__acct_b';
 if acct_a=acct_b or acct_a is null or acct_b is null then raise exception 'Labels collapsed owned accounts'; end if;
 source:=jsonb_build_object('id','statement:arch','date','2026-10-01','description','Coffee','amount','-0.30','category','Dining','category_origin','builtin','builtin_category','Dining',
  'account_name','Same label','account_type','Checking','account_id',acct_a,'currency','USD','statement_file_id',repeat('a',64),'statement_row',1,'statement_reference','ref-1');
 perform set_config('financebuddy.test_operation',batch::text,true);
 receipt:=public.fb_execute_operation(batch,'append',jsonb_build_object('rows',jsonb_build_array(source)));
 if receipt->>'inserted'<>'1' or receipt->>'batch_id'<>batch::text then raise exception 'Missing authoritative receipt'; end if;
 if public.fb_execute_operation(batch,'append',jsonb_build_object('rows',jsonb_build_array(source)))<>receipt then raise exception 'Retry changed original result'; end if;
 begin
  perform public.fb_execute_operation(batch,'append',jsonb_build_object('rows',jsonb_build_array(source),'keep_matching',true));
  raise exception 'Reused operation ID accepted';
 exception when raise_exception then if sqlerrm='Reused operation ID accepted' then raise; end if; end;
 begin
  perform public.fb_append_statement(jsonb_build_array(source||jsonb_build_object('id','statement:ambiguous','statement_file_id',repeat('b',64),'statement_reference',null,'account_id',null)));
  raise exception 'Ambiguous account label accepted';
 exception when raise_exception then if sqlerrm='Ambiguous account label accepted' then raise; end if; end;
 -- The same bank reference is valid in another explicitly selected account.
 perform public.fb_append_statement(jsonb_build_array(source||jsonb_build_object('id','statement:other','account_id',acct_b,'statement_file_id',repeat('b',64))));
 begin
  perform public.fb_append_statement(jsonb_build_array(source||jsonb_build_object('id','statement:eur','statement_file_id',repeat('c',64),'statement_reference',null,'currency','EUR')));
  raise exception 'Foreign currency accepted';
 exception when raise_exception then if sqlerrm='Foreign currency accepted' then raise; end if; end;
 begin
  insert into public.transactions(user_id,id,date,description,amount,account_name) values(uid,'__fraction',current_date,'Store',-0.001,'Fraction');
  raise exception 'Fractional cent accepted';
 exception when check_violation then null; end;
 perform public.fb_split_transaction('statement:arch','Dining',0.10,'Shopping',0.20);
 if(select sum(amount) from public.transaction_allocations where user_id=uid and transaction_id='statement:arch')<>-0.30 then raise exception 'Split lost cents'; end if;
 begin
  perform public.fb_split_transaction('statement:arch','Dining',0.101,'Shopping',0.199);
  raise exception 'Fractional allocation accepted';
 exception when raise_exception then if sqlerrm='Fractional allocation accepted' then raise; end if; end;
 begin
  perform public.fb_execute_operation(gen_random_uuid(),'undo_batch',jsonb_build_object('batch_id',batch));
  raise exception 'Edited import was undone';
 exception when raise_exception then if sqlerrm='Edited import was undone' then raise; end if; end;

 -- Common policy: rule > provider > built-in. Manual changes remain authoritative.
 insert into public.category_rules(user_id,keyword,category) values(uid,'coffee','Groceries');
 saved:=jsonb_build_object('id','plaid:arch-pending','date','2026-10-01','description','Coffee','amount','-0.30','category','Dining','category_origin','provider',
  'provider_category','Dining','builtin_category','Dining','account_name','Same label','account_type','Checking','plaid_account_id','__acct_a','pending',true,'currency','USD');
 perform public.fb_apply_plaid_sync('__arch_a',null,'one',now(),jsonb_build_array(saved),'[]');
 if(select category from public.transactions where user_id=uid and id='plaid:arch-pending')<>'Groceries' then raise exception 'Bank ignored custom rule'; end if;
 if not exists(select 1 from public.statement_import_rows where user_id=uid and transaction_id='plaid:arch-pending' and file_id=repeat('a',64)) then raise exception 'Matching lost import lineage'; end if;
 -- Statement source was legacy automatic (builtin), so its suggestion follows the rule.
 perform public.fb_correct_category('plaid:arch-pending','Entertainment');
 select id into pending_alloc from public.transaction_allocations where user_id=uid and transaction_id='plaid:arch-pending' limit 1;
 saved:=saved||jsonb_build_object('id','plaid:arch-posted','pending',false,'pending_transaction_id','plaid:arch-pending','amount','-0.31','provider_category','Shopping');
 perform public.fb_apply_plaid_sync('__arch_a','one','two',now(),jsonb_build_array(saved),'["plaid:arch-pending"]');
 if exists(select 1 from public.transactions where user_id=uid and id='plaid:arch-pending')
  or(select category from public.transactions where user_id=uid and id='plaid:arch-posted')<>'Entertainment'
  or not exists(select 1 from public.transaction_allocations where user_id=uid and id=pending_alloc and transaction_id='plaid:arch-posted')
  or not exists(select 1 from public.statement_import_rows where user_id=uid and transaction_id='plaid:arch-posted' and file_id=repeat('a',64)) then raise exception 'Posting lost annotations or lineage'; end if;
 page:=public.fb_query_transactions(jsonb_build_object('accounts',jsonb_build_array(acct_a)),1,null);
 if(page->'rows'->0->>'amount')::numeric<>-0.31 or(page->'rows'->0->>'split_needs_review')::boolean is not true then raise exception 'Invalid allocations double counted changed total'; end if;

 -- Provider updates refresh automatic choices instead of freezing first suggestion.
 saved:=saved||jsonb_build_object('id','plaid:arch-auto','pending_transaction_id',null,'description','Store','category','Dining','provider_category','Dining','amount','-1.00');
 perform public.fb_apply_plaid_sync('__arch_a','two','three',now(),jsonb_build_array(saved),'[]');
 saved:=saved||jsonb_build_object('provider_category','Shopping','category','Shopping');
 perform public.fb_apply_plaid_sync('__arch_a','three','four',now(),jsonb_build_array(saved),'[]');
 if(select category from public.transactions where user_id=uid and id='plaid:arch-auto')<>'Shopping' then raise exception 'Automatic provider category was frozen'; end if;
 begin
  perform public.fb_correct_category('missing','Dining','__rollback_rule',true);
  raise exception 'Missing category edit accepted';
 exception when raise_exception then if sqlerrm='Missing category edit accepted' then raise; end if; end;
 if exists(select 1 from public.category_rules where user_id=uid and keyword='__rollback_rule') then raise exception 'Category failure left partial rule'; end if;

 -- Keyset pages include projected allocation IDs once and exact summary amounts.
 perform public.fb_split_transaction('plaid:arch-auto','Dining',0.10,'Shopping',0.90);
 cursor:=null;
 loop
  page:=public.fb_query_transactions(jsonb_build_object('accounts',jsonb_build_array(acct_a,acct_b)),1,cursor);
  for row in select value from jsonb_array_elements(page->'rows') loop
   if(row->>'id')=any(seen) then raise exception 'Keyset repeated a row'; end if;
   seen:=array_append(seen,row->>'id');
  end loop;
  cursor:=page->'next_cursor';
  exit when cursor='null'::jsonb;
 end loop;
 if cardinality(seen)<>4 then raise exception 'Keyset omitted projections: %',cardinality(seen); end if;
 if(public.fb_transaction_summary(jsonb_build_object('accounts',jsonb_build_array(acct_a,acct_b)))->>'outflow')::numeric<>1.61 then raise exception 'Summary lost exact cents'; end if;
 if(public.fb_transaction_catalog()->>'count')::integer<3 then raise exception 'Catalog missing saved records'; end if;

 -- Snapshot includes portable metadata, settings and balances, excludes credentials.
 insert into public.app_settings(user_id,key,value) values(uid,'__arch_setting','true');
 perform public.fb_execute_operation(clean_batch,'append',jsonb_build_object('rows',jsonb_build_array(source||jsonb_build_object('id','statement:clean','description','Clean','account_id',acct_b,'statement_reference',null,'statement_file_id',repeat('d',64)))));
 snapshot:=public.fb_export_backup();
 if snapshot->>'version'<>'3' or not exists(select 1 from jsonb_array_elements(snapshot->'settings') where value->>'key'='__arch_setting')
  or jsonb_array_length(snapshot->'balance_snapshots')<2 or snapshot::text like '%encrypted-a%' then raise exception 'Snapshot incomplete or leaked credential'; end if;
 perform public.fb_execute_operation(gen_random_uuid(),'restore',jsonb_build_object('backup',snapshot));
 if not exists(select 1 from public.transactions where user_id=uid and id='plaid:arch-posted' and account_id=acct_a and category_override='Entertainment')
  or not exists(select 1 from public.plaid_items i where i.user_id=uid and i.item_id='__arch_a' and i.needs_reconciliation and i.cursor is null) then raise exception 'Restore lost annotations or bank reconciliation'; end if;
 begin
  perform public.fb_execute_operation(gen_random_uuid(),'undo_batch',jsonb_build_object('batch_id',clean_batch));
  raise exception 'Restored import was eligible for old undo';
 exception when raise_exception then if sqlerrm='Restored import was eligible for old undo' then raise; end if; end;

 -- Recoverable disconnect targets identity; same labels on the other bank survive.
 perform public.fb_disconnect_begin('__arch_a',true);
 begin
  perform public.fb_apply_plaid_sync('__arch_a',null,'five',now(),'[]','[]',2,true);
  raise exception 'Disconnecting bank synced';
 exception when raise_exception then if sqlerrm='Disconnecting bank synced' then raise; end if; end;
 n:=public.fb_disconnect_finish('__arch_a');
 if n<>2 or not exists(select 1 from public.transactions where user_id=uid and id='statement:other' and account_id=acct_b)
  or not exists(select 1 from public.accounts where user_id=uid and id=acct_a and plaid_item_id='__arch_a') then raise exception 'Disconnect deleted unrelated history or stable account association'; end if;
 if public.fb_disconnect_finish('__arch_a')<>0 then raise exception 'Disconnect retry was not safe'; end if;
end $$;

-- Owner cannot read another user's operation, accounts, projection, snapshots or backup.
select set_config('request.jwt.claim.sub','00000000-0000-0000-0000-000000000002',true);
do $$ begin
 if exists(select 1 from public.accounts where name='Same label') or exists(select 1 from public.fb_transaction_projection where description='Coffee')
  or exists(select 1 from public.balance_snapshots where current_balance=200.20)
  or jsonb_array_length(public.fb_import_batches())<>0
  or public.fb_operation_result(current_setting('financebuddy.test_operation')::uuid) is not null
  or public.fb_export_backup()::text like '%Same label%' then raise exception 'Cross-owner data was exposed'; end if;
end $$;
set local role anon;
do $$ begin
 begin perform public.fb_query_transactions(); raise exception 'Anonymous query allowed'; exception when insufficient_privilege then null; end;
 begin perform public.fb_export_backup(); raise exception 'Anonymous export allowed'; exception when insufficient_privilege then null; end;
 begin perform public.fb_execute_operation(gen_random_uuid(),'split','{}'); raise exception 'Anonymous write allowed'; exception when insufficient_privilege then null; end;
end $$;
rollback;
