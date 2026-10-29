-- Extension operations: rule-based triage, duplicate merge lineage, sample-data
-- clearing, and two-user isolation, all through the receipted dispatcher.
begin;
set local role authenticated;
select set_config('request.jwt.claim.sub','00000000-0000-0000-0000-000000000001',true);

do $$
declare uid uuid:=(select auth.uid()); row_e jsonb; result jsonb; n integer;
begin
 insert into public.transactions(user_id,id,date,description,amount,category,account_name,category_origin,builtin_category) values
  (uid,'__tri_1','2026-10-01','BLUE BOTTLE COFFEE OAKLAND',-5,'Uncategorized','Ext card','builtin','Uncategorized'),
  (uid,'__tri_2','2026-10-02','BLUE BOTTLE COFFEE SF',-6,'Uncategorized','Ext card','builtin','Uncategorized'),
  (uid,'__tri_3','2026-10-03','CORNER STORE',-7,'Uncategorized','Ext card','builtin','Uncategorized'),
  (uid,'__tri_4','2026-10-04','BLUE BOTTLE COFFEE NYC',-8,'Uncategorized','Ext card','builtin','Uncategorized');
 update public.transactions set category_override='Shopping' where user_id=uid and id='__tri_2';
 result:=public.fb_execute_operation(gen_random_uuid(),'triage',jsonb_build_object('ids',jsonb_build_array('__tri_1','__tri_2','__tri_3'),'category','Dining','keyword',' Blue Bottle Coffee '));
 if result<>'3'::jsonb then raise exception 'Triage count wrong: %',result; end if;
 if not exists(select 1 from public.category_rules where user_id=uid and keyword='blue bottle coffee' and category='Dining') then raise exception 'Triage rule missing'; end if;
 if(select count(*) from public.transactions where user_id=uid and id in('__tri_1','__tri_2') and category='Dining' and category_origin='rule' and category_rule='blue bottle coffee')<>2 then raise exception 'Triage rows are not rule-based'; end if;
 if(select category_origin from public.transactions where user_id=uid and id='__tri_3')<>'manual' or(select category from public.transactions where user_id=uid and id='__tri_3')<>'Dining' then raise exception 'Unmatched triage row lost the choice'; end if;
 if(select category from public.transactions where user_id=uid and id='__tri_4')<>'Uncategorized' then raise exception 'Triage changed an unlisted row'; end if;
 begin
  perform public.fb_execute_operation(gen_random_uuid(),'triage',jsonb_build_object('ids',jsonb_build_array('__tri_1','__missing'),'category','Dining','keyword','blue'));
  raise exception 'Stale triage accepted';
 exception when raise_exception then if sqlerrm<>'Some transactions changed' then raise; end if; end;
 if exists(select 1 from public.category_rules where user_id=uid and keyword='blue') then raise exception 'Failed triage kept its rule'; end if;

 -- The same purchase saved from two files; merging keeps one and re-imports map to it.
 row_e:=jsonb_build_object('id','statement:__dup_e','date','2026-10-05','description','Bakery','amount','-4.00','category','Dining',
  'account_name','Ext checking','account_type','Checking','statement_file_id',repeat('e',64),'statement_row',1);
 perform public.fb_append_statement(jsonb_build_array(row_e||jsonb_build_object('id','statement:__dup_d','statement_file_id',repeat('d',64))));
 perform public.fb_append_statement(jsonb_build_array(row_e),true);
 if(select count(*) from public.transactions where user_id=uid and id in('statement:__dup_d','statement:__dup_e'))<>2 then raise exception 'Duplicate fixture missing'; end if;
 begin
  perform public.fb_execute_operation(gen_random_uuid(),'merge_duplicate',jsonb_build_object('keep_id','statement:__dup_d','remove_id','__tri_1'));
  raise exception 'Different rows merged';
 exception when raise_exception then if sqlerrm<>'Transactions are not duplicates' then raise; end if; end;
 perform public.fb_execute_operation(gen_random_uuid(),'merge_duplicate',jsonb_build_object('keep_id','statement:__dup_d','remove_id','statement:__dup_e'));
 if exists(select 1 from public.transactions where user_id=uid and id='statement:__dup_e') then raise exception 'Duplicate not removed'; end if;
 if(select transaction_id from public.statement_import_rows where user_id=uid and file_id=repeat('e',64) and row_number=1) is distinct from 'statement:__dup_d' then raise exception 'Import lineage not re-pointed'; end if;
 result:=public.fb_append_statement(jsonb_build_array(row_e),true);
 if result->>'inserted'<>'0' or exists(select 1 from public.transactions where user_id=uid and id='statement:__dup_e') then raise exception 'Merged duplicate came back on re-import'; end if;

 -- Clearing sample data removes only reserved statement accounts and seeded plans.
 insert into public.transactions(user_id,id,date,description,amount,category,account_name) values
  (uid,'__sample_1','2026-10-01','Sample groceries',-20,'Groceries','Sample Checking · demo'),
  (uid,'__real_1','2026-10-01','Real groceries',-20,'Groceries','Ext checking');
 perform public.fb_split_transaction('__sample_1','Groceries',10,'Dining',10);
 insert into public.budgets(user_id,category,monthly_limit) values(uid,'Groceries',400),(uid,'Dining',250);
 insert into public.savings_goals(user_id,name,target_amount) values(uid,'Sample trip',1500);
 insert into public.category_rules(user_id,keyword,category) values(uid,'sample cafe','Dining');
 insert into public.app_settings(user_id,key,value) values(uid,'sample_data','{}');
 result:=public.fb_execute_operation(gen_random_uuid(),'clear_sample',jsonb_build_object('account_names',jsonb_build_array('Sample Checking · demo'),
  'budgets',jsonb_build_array(jsonb_build_object('category','Groceries','monthly_limit','400.00'),jsonb_build_object('category','Dining','monthly_limit','100.00')),
  'goals',jsonb_build_array(jsonb_build_object('name','Sample trip','target_amount','1500.00')),'rule_keywords',jsonb_build_array('sample cafe')));
 if result<>'1'::jsonb then raise exception 'Sample clear count wrong: %',result; end if;
 if exists(select 1 from public.accounts where user_id=uid and name='Sample Checking · demo') then raise exception 'Sample account kept'; end if;
 if not exists(select 1 from public.transactions where user_id=uid and id='__real_1') then raise exception 'Sample clear removed real data'; end if;
 if exists(select 1 from public.budgets where user_id=uid and category='Groceries') or not exists(select 1 from public.budgets where user_id=uid and category='Dining') then raise exception 'Sample clear removed the wrong budgets'; end if;
 if exists(select 1 from public.savings_goals where user_id=uid and name='Sample trip') or exists(select 1 from public.category_rules where user_id=uid and keyword='sample cafe')
  or exists(select 1 from public.app_settings where user_id=uid and key='sample_data') then raise exception 'Sample clear left seeded data'; end if;
end $$;

-- Another user cannot merge, triage or clear the first user's rows.
select set_config('request.jwt.claim.sub','00000000-0000-0000-0000-000000000002',true);
do $$
begin
 begin
  perform public.fb_execute_operation(gen_random_uuid(),'merge_duplicate',jsonb_build_object('keep_id','statement:__dup_d','remove_id','__real_1'));
  raise exception 'Cross-user merge accepted';
 exception when raise_exception then if sqlerrm<>'Transaction not found' then raise; end if; end;
 begin
  perform public.fb_execute_operation(gen_random_uuid(),'triage',jsonb_build_object('ids',jsonb_build_array('__tri_4'),'category','Dining','keyword','blue'));
  raise exception 'Cross-user triage accepted';
 exception when raise_exception then if sqlerrm<>'Some transactions changed' then raise; end if; end;
 perform public.fb_execute_operation(gen_random_uuid(),'clear_sample',jsonb_build_object('account_names',jsonb_build_array('Ext checking')));
end $$;
select set_config('request.jwt.claim.sub','00000000-0000-0000-0000-000000000001',true);
do $$ begin
 if not exists(select 1 from public.transactions where id='__real_1') or not exists(select 1 from public.category_rules where keyword='blue bottle coffee') then raise exception 'Another user changed owned data'; end if;
end $$;
rollback;
