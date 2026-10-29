-- Notes/tags, manual entries, pending-to-posted lineage, backup v4 and isolation.
begin;
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-0000-0000-000000000001',true);

do $$
declare uid uuid:=(select auth.uid()); rev bigint; rows jsonb; backup jsonb; alloc uuid;
begin
 insert into public.transactions(user_id,id,date,description,amount,category,account_name) values(uid,'__ann_1','2026-10-01','Hotel',-200,'Travel','Ann card');
 select revision into rev from public.transactions where user_id=uid and id='__ann_1';
 perform public.fb_execute_operation(gen_random_uuid(),'annotate',jsonb_build_object('id','__ann_1','notes',' Booked for the conference ','tags',jsonb_build_array(' Trip ','trip','Work','')));
 if(select tags from public.transaction_annotations where user_id=uid and transaction_id='__ann_1')<>'{trip,work}'::text[] then raise exception 'Tags not normalized'; end if;
 if(select notes from public.transaction_annotations where user_id=uid and transaction_id='__ann_1')<>'Booked for the conference' then raise exception 'Notes not trimmed'; end if;
 if(select revision from public.transactions where user_id=uid and id='__ann_1')<>rev then raise exception 'Annotating changed the transaction revision'; end if;
 select jsonb_agg(record) into rows from public.fb_filtered_transactions('{"tags":["trip"]}');
 if jsonb_array_length(rows)<>1 or rows->0->>'notes'<>'Booked for the conference' or rows->0->'tags'<>'["trip","work"]'::jsonb then raise exception 'Tag filter or projection wrong: %',rows; end if;
 if exists(select 1 from public.fb_filtered_transactions('{"tags":["food"]}')) then raise exception 'Tag filter matched another tag'; end if;
 begin
  perform public.fb_execute_operation(gen_random_uuid(),'annotate',jsonb_build_object('id','__ann_1','tags',jsonb_build_array(repeat('x',41))));
  raise exception 'Long tag accepted';
 exception when check_violation then null; end;
 -- A note on one split part belongs to the purchase.
 perform public.fb_split_transaction('__ann_1','Travel',150,'Dining',50);
 select id into alloc from public.transaction_allocations where user_id=uid and transaction_id='__ann_1' limit 1;
 perform public.fb_execute_operation(gen_random_uuid(),'annotate',jsonb_build_object('id','allocation:'||alloc,'notes','Split note'));
 if(select notes from public.transaction_annotations where user_id=uid and transaction_id='__ann_1')<>'Split note' then raise exception 'Allocation note not on source'; end if;
 if(select count(*) from public.fb_filtered_transactions('{}') where record->>'notes'='Split note')<>2 then raise exception 'Split parts lost the note'; end if;
 perform public.fb_execute_operation(gen_random_uuid(),'annotate',jsonb_build_object('id','__ann_1','notes','','tags','[]'::jsonb));
 if exists(select 1 from public.transaction_annotations where user_id=uid and transaction_id='__ann_1') then raise exception 'Empty annotation kept'; end if;

 -- Manual entries are ordinary rows with manual: IDs and a manual category.
 perform public.fb_execute_operation(gen_random_uuid(),'manual_add',jsonb_build_object('row',jsonb_build_object('id','manual:__cash','date','2026-10-02',
  'description','Farmers market','amount','-12.50','category','Groceries','account_name','Cash','account_type','Checking'),'tags',jsonb_build_array('market')));
 if not exists(select 1 from public.transactions t join public.transaction_annotations n on n.user_id=t.user_id and n.transaction_id=t.id
  where t.user_id=uid and t.id='manual:__cash' and t.category='Groceries' and t.category_origin='manual' and t.account_id is not null and n.tags='{market}') then raise exception 'Manual entry not saved'; end if;
 begin
  perform public.fb_execute_operation(gen_random_uuid(),'manual_add',jsonb_build_object('row',jsonb_build_object('id','statement:__fake','date','2026-10-02',
   'description','X','amount','-1','category','Groceries','account_name','Cash','account_type','Checking')));
  raise exception 'Non-manual ID accepted';
 exception when raise_exception then if sqlerrm<>'Invalid manual transaction' then raise; end if; end;
 begin
  perform public.fb_execute_operation(gen_random_uuid(),'manual_delete',jsonb_build_object('id','__ann_1'));
  raise exception 'Deleted a non-manual row';
 exception when raise_exception then if sqlerrm<>'Transaction not found' then raise; end if; end;

 -- Pending bank rows hand their note to the posted row.
 perform public.fb_save_plaid_item('__ann_bank','encrypted','Ann Bank');
 perform public.fb_replace_plaid_accounts('__ann_bank','[{"account_id":"__ann_acct","name":"Ann checking","type":"depository","iso_currency_code":"USD","current_balance":"10.00"}]');
 perform public.fb_apply_plaid_sync('__ann_bank',null,'one',now(),jsonb_build_array(jsonb_build_object('id','plaid:__pending','date','2026-10-03','description','Taxi','amount','-20.00',
  'category','Transportation','account_name','Ann checking','account_type','Checking','plaid_account_id','__ann_acct','pending',true)),'[]');
 perform public.fb_execute_operation(gen_random_uuid(),'annotate',jsonb_build_object('id','plaid:__pending','notes','Airport ride'));
 perform public.fb_apply_plaid_sync('__ann_bank','one','two',now(),jsonb_build_array(jsonb_build_object('id','plaid:__posted','date','2026-10-04','description','Taxi','amount','-20.00',
  'category','Transportation','account_name','Ann checking','account_type','Checking','plaid_account_id','__ann_acct','pending_transaction_id','plaid:__pending')),'[]');
 if exists(select 1 from public.transactions where user_id=uid and id='plaid:__pending')
  or(select notes from public.transaction_annotations where user_id=uid and transaction_id='plaid:__posted') is distinct from 'Airport ride' then raise exception 'Note lost when the bank posted'; end if;

 -- Version 4 backups round-trip annotations; version 3 backups still restore.
 backup:=public.fb_export_backup();
 if backup->>'version'<>'4' or jsonb_array_length(backup->'transaction_annotations')<>2 then raise exception 'Backup missing annotations'; end if;
 perform public.fb_execute_operation(gen_random_uuid(),'restore',jsonb_build_object('backup',backup));
 if(select count(*) from public.transaction_annotations where user_id=uid)<>2 then raise exception 'Restore lost annotations'; end if;
 perform public.fb_execute_operation(gen_random_uuid(),'restore',jsonb_build_object('backup',(backup-'transaction_annotations')||'{"version":3}'));
 if exists(select 1 from public.transaction_annotations where user_id=uid) or not exists(select 1 from public.transactions where user_id=uid and id='manual:__cash') then raise exception 'Version 3 restore wrong'; end if;
 perform public.fb_execute_operation(gen_random_uuid(),'manual_delete',jsonb_build_object('id','manual:__cash'));
 if exists(select 1 from public.transactions where user_id=uid and id='manual:__cash') then raise exception 'Manual entry not deleted'; end if;
end $$;

select set_config('request.jwt.claim.sub','00000000-0000-0000-0000-000000000002',true);
do $$ begin
 begin
  perform public.fb_execute_operation(gen_random_uuid(),'annotate',jsonb_build_object('id','__ann_1','notes','Not mine'));
  raise exception 'Cross-user annotation accepted';
 exception when raise_exception then if sqlerrm<>'Transaction not found' then raise; end if; end;
 if exists(select 1 from public.transaction_annotations) then raise exception 'Another user can read annotations'; end if;
end $$;
rollback;
