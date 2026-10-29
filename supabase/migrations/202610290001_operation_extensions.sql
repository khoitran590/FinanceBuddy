-- New operation kinds live in one private extension so the dispatcher, its
-- receipts and its per-user lock stay in a single place.
create function financebuddy_private.fb_execute_extension(p_uid uuid,p_kind text,p_payload jsonb) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare v_count integer:=0; v_allocations integer:=0; v_keyword text; v_keep public.transactions%rowtype;
 v_remove public.transactions%rowtype; v_accounts uuid[];
begin
 case p_kind
 when 'triage' then
  -- Categorize the listed rows by a saved rule. The category policy trigger
  -- applies the first matching rule, as for imports; rows that another rule
  -- or no rule covers keep the chosen category as a manual choice.
  v_keyword:=lower(trim(coalesce(p_payload->>'keyword','')));
  if jsonb_typeof(p_payload->'ids') is distinct from 'array' or jsonb_array_length(p_payload->'ids') not between 1 and 50000
   or length(coalesce(p_payload->>'category','')) not between 1 and 128 or length(v_keyword) not between 1 and 256 then raise exception 'Invalid category batch'; end if;
  insert into public.category_rules(user_id,keyword,category) values(p_uid,v_keyword,p_payload->>'category')
   on conflict(user_id,keyword) do update set category=excluded.category,version=case when category_rules.category<>excluded.category then category_rules.version+1 else category_rules.version end;
  update public.transactions set category_origin='rule',category_override=null
   where user_id=p_uid and id in(select value from jsonb_array_elements_text(p_payload->'ids'));
  get diagnostics v_count=row_count;
  update public.transactions set category_override=p_payload->>'category',category_rule=null,category_rule_version=null
   where user_id=p_uid and id in(select value from jsonb_array_elements_text(p_payload->'ids')) and category<>p_payload->>'category';
  update public.transaction_allocations set category=p_payload->>'category'
   where user_id=p_uid and 'allocation:'||id::text in(select value from jsonb_array_elements_text(p_payload->'ids'));
  get diagnostics v_allocations=row_count;
  if v_count+v_allocations<>(select count(distinct value) from jsonb_array_elements_text(p_payload->'ids')) then raise exception 'Some transactions changed'; end if;
  return to_jsonb(v_count+v_allocations);
 when 'merge_duplicate' then
  -- Re-point import lineage before deleting, so re-importing the same file
  -- maps to the kept row instead of re-creating the removed one.
  select * into v_keep from public.transactions where user_id=p_uid and id=p_payload->>'keep_id';
  select * into v_remove from public.transactions where user_id=p_uid and id=p_payload->>'remove_id';
  if v_keep.id is null or v_remove.id is null or v_keep.id=v_remove.id then raise exception 'Transaction not found'; end if;
  if v_remove.id like 'plaid:%' or v_remove.plaid_item_id is not null then raise exception 'Bank transactions cannot be removed'; end if;
  if v_keep.account_id is distinct from v_remove.account_id or v_keep.date<>v_remove.date or v_keep.amount<>v_remove.amount
   or public.fb_import_normalize(v_keep.description)<>public.fb_import_normalize(v_remove.description) then raise exception 'Transactions are not duplicates'; end if;
  if exists(select 1 from public.transaction_allocations where user_id=p_uid and transaction_id=v_remove.id) then raise exception 'Split transactions cannot be removed'; end if;
  update public.statement_import_rows set transaction_id=v_keep.id where user_id=p_uid and transaction_id=v_remove.id;
  delete from public.transactions where user_id=p_uid and id=v_remove.id;
  return to_jsonb(1);
 when 'clear_sample' then
  -- Sample accounts are statement accounts with reserved names; bank accounts are never touched.
  if jsonb_typeof(p_payload->'account_names') is distinct from 'array' then raise exception 'Invalid sample data'; end if;
  select coalesce(array_agg(id),'{}') into v_accounts from public.accounts where user_id=p_uid and plaid_item_id is null
   and name in(select value from jsonb_array_elements_text(p_payload->'account_names'));
  delete from public.transactions where user_id=p_uid and account_id=any(v_accounts);
  get diagnostics v_count=row_count;
  delete from public.accounts where user_id=p_uid and id=any(v_accounts);
  -- Seeded plans are removed only while they still hold their seeded values.
  delete from public.budgets b using jsonb_to_recordset(coalesce(p_payload->'budgets','[]'::jsonb)) r(category text,monthly_limit numeric)
   where b.user_id=p_uid and b.category=r.category and b.monthly_limit=r.monthly_limit;
  delete from public.savings_goals g using jsonb_to_recordset(coalesce(p_payload->'goals','[]'::jsonb)) r(name text,target_amount numeric)
   where g.user_id=p_uid and g.name=r.name and g.target_amount=r.target_amount;
  delete from public.category_rules where user_id=p_uid and keyword in(select value from jsonb_array_elements_text(coalesce(p_payload->'rule_keywords','[]'::jsonb)));
  delete from public.app_settings where user_id=p_uid and key='sample_data';
  return to_jsonb(v_count);
 else raise exception 'Unsupported operation';
 end case;
end $$;
revoke all on function financebuddy_private.fb_execute_extension(uuid,text,jsonb) from public,anon;
grant execute on function financebuddy_private.fb_execute_extension(uuid,text,jsonb) to authenticated;

create or replace function public.fb_execute_operation(p_operation_id uuid,p_kind text,p_payload jsonb) returns jsonb
language plpgsql security invoker set search_path='' as $$
declare v_uid uuid:=(select auth.uid()); v_fingerprint text; v_old financebuddy_private.data_operations%rowtype;
 v_result jsonb; v_count integer; v_id bigint; v_account uuid; v_batch financebuddy_private.data_operations%rowtype;
begin
 if v_uid is null or p_operation_id is null then raise exception 'Authentication required'; end if;
 if p_payload is null or jsonb_typeof(p_payload)<>'object' then raise exception 'Invalid operation'; end if;
 perform pg_advisory_xact_lock(hashtextextended(v_uid::text,0));
 v_fingerprint:=encode(sha256(convert_to(jsonb_build_array(p_kind,p_payload)::text,'UTF8')),'hex');
 select * into v_old from financebuddy_private.data_operations where user_id=v_uid and id=p_operation_id;
 if found then
  if v_old.fingerprint<>v_fingerprint or v_old.kind<>p_kind then raise exception 'Operation ID reused with different intent'; end if;
  return v_old.result;
 end if;
 case p_kind
 when 'append' then
  v_result:=public.fb_append_statement(p_payload->'rows',coalesce((p_payload->>'keep_matching')::boolean,false));
  v_result:=v_result||jsonb_build_object('batch_id',p_operation_id);
 when 'undo_batch' then
  select * into v_batch from financebuddy_private.data_operations where user_id=v_uid and id=(p_payload->>'batch_id')::uuid and kind='append';
  if not found then raise exception 'Import batch not found'; end if;
  if v_batch.undone then v_result:='0'::jsonb;
  else
   if exists(select 1 from jsonb_array_elements(v_batch.result->'inserted_rows') r left join public.transactions t on t.user_id=v_uid and t.id=r.value->>'id'
    where t.id is null or t.revision<>(r.value->>'revision')::bigint
    or exists(select 1 from public.transaction_allocations a where a.user_id=v_uid and a.transaction_id=t.id)) then raise exception 'Imported rows have changed'; end if;
   delete from public.transactions where user_id=v_uid and id in(select value->>'id' from jsonb_array_elements(v_batch.result->'inserted_rows'));
   get diagnostics v_count=row_count;
   update financebuddy_private.data_operations set undone=true where user_id=v_uid and id=v_batch.id;
   v_result:=to_jsonb(v_count);
  end if;
 when 'split' then
  perform public.fb_split_transaction(p_payload->>'id',p_payload->>'first_category',(p_payload->>'first_amount')::numeric,p_payload->>'second_category',(p_payload->>'second_amount')::numeric);
  v_result:=jsonb_build_object('saved',true);
 when 'sync' then v_result:=public.fb_apply_plaid_sync(p_payload->>'p_item_id',p_payload->>'p_expected_cursor',p_payload->>'p_next_cursor',(p_payload->>'p_synced_at')::timestamptz,p_payload->'p_upserts',p_payload->'p_removed_ids',coalesce((p_payload->>'p_expected_generation')::bigint,0),coalesce((p_payload->>'p_reconcile')::boolean,false));
 when 'restore' then v_result:=public.fb_restore_backup(p_payload->'backup');
 when 'replace_account' then
  v_account:=(p_payload->>'account_id')::uuid;
  if v_account is null then
   if jsonb_array_length(p_payload->'rows')>0 then select id into v_account from public.fb_statement_account(p_payload->'rows'->0);
   else
    select min(id::text)::uuid into v_account from public.accounts where user_id=v_uid and name=p_payload->>'account_name';
    if(select count(*) from public.accounts where user_id=v_uid and name=p_payload->>'account_name')<>1 then raise exception 'Select an owned account ID'; end if;
   end if;
  end if;
  select coalesce(jsonb_agg(value||jsonb_build_object('account_id',v_account)),'[]'::jsonb) into v_result from jsonb_array_elements(p_payload->'rows');
  v_result:=to_jsonb(public.fb_replace_owned_account(v_account,v_result));
 when 'replace_all' then v_result:=to_jsonb(public.fb_replace_transactions(null,p_payload->'rows'));
 when 'delete_account' then v_result:=to_jsonb(public.fb_delete_owned_account((p_payload->>'account_id')::uuid));
 when 'category' then v_result:=public.fb_correct_category(p_payload->>'id',p_payload->>'category',coalesce(p_payload->>'keyword',''),coalesce((p_payload->>'apply_similar')::boolean,false));
 when 'bulk_category' then
  if jsonb_typeof(p_payload->'ids') is distinct from 'array' or jsonb_array_length(p_payload->'ids')>50000 then raise exception 'Invalid category batch'; end if;
  update public.transactions set category=p_payload->>'category',category_override=p_payload->>'category',category_origin='manual',category_rule=null,category_rule_version=null where user_id=v_uid and id in(select value from jsonb_array_elements_text(p_payload->'ids'));
  get diagnostics v_count=row_count;
  update public.transaction_allocations set category=p_payload->>'category' where user_id=v_uid and 'allocation:'||id::text in(select value from jsonb_array_elements_text(p_payload->'ids'));
  get diagnostics v_id=row_count;
  if v_count+v_id<>(select count(distinct value) from jsonb_array_elements_text(p_payload->'ids')) then raise exception 'Some transactions changed'; end if;
  v_result:=to_jsonb(v_count+v_id);
 when 'disconnect_begin' then v_result:=to_jsonb(public.fb_disconnect_begin(p_payload->>'item_id',(p_payload->>'remove_transactions')::boolean));
 when 'disconnect_finish' then v_result:=to_jsonb(public.fb_disconnect_finish(p_payload->>'item_id'));
 when 'budget' then
  insert into public.budgets(user_id,category,monthly_limit) values(v_uid,p_payload->>'category',(p_payload->>'monthly_limit')::numeric) on conflict(user_id,category) do update set monthly_limit=excluded.monthly_limit;
  v_result:='true'::jsonb;
 when 'delete_budget' then delete from public.budgets where user_id=v_uid and category=p_payload->>'category'; v_result:='true'::jsonb;
 when 'goal' then
  if p_payload->>'id' is null then
   insert into public.savings_goals(user_id,name,target_amount,current_amount,target_date) values(v_uid,p_payload->>'name',(p_payload->>'target_amount')::numeric,coalesce((p_payload->>'current_amount')::numeric,0),(p_payload->>'target_date')::date) returning id into v_id;
  else
   update public.savings_goals set name=p_payload->>'name',target_amount=(p_payload->>'target_amount')::numeric,current_amount=(p_payload->>'current_amount')::numeric,target_date=(p_payload->>'target_date')::date where user_id=v_uid and id=(p_payload->>'id')::bigint returning id into v_id;
   if not found then raise exception 'Goal not found'; end if;
  end if;
  v_result:=to_jsonb(v_id);
 when 'delete_goal' then delete from public.savings_goals where user_id=v_uid and id=(p_payload->>'id')::bigint; v_result:='true'::jsonb;
 when 'rule' then
  insert into public.category_rules(user_id,keyword,category) values(v_uid,lower(trim(p_payload->>'keyword')),p_payload->>'category') on conflict(user_id,keyword) do update set category=excluded.category,version=case when category_rules.category<>excluded.category then category_rules.version+1 else category_rules.version end;
  v_result:='true'::jsonb;
 when 'delete_rule' then delete from public.category_rules where user_id=v_uid and keyword=p_payload->>'keyword'; v_result:='true'::jsonb;
 when 'setting' then
  insert into public.app_settings(user_id,key,value) values(v_uid,p_payload->>'key',p_payload->>'value') on conflict(user_id,key) do update set value=excluded.value; v_result:='true'::jsonb;
 else v_result:=financebuddy_private.fb_execute_extension(v_uid,p_kind,p_payload);
 end case;
 insert into financebuddy_private.data_operations(user_id,id,kind,fingerprint,result) values(v_uid,p_operation_id,p_kind,v_fingerprint,v_result);
 return v_result;
end $$;
revoke all on function public.fb_execute_operation(uuid,text,jsonb) from public,anon;
grant execute on function public.fb_execute_operation(uuid,text,jsonb) to authenticated;
notify pgrst,'reload schema';
