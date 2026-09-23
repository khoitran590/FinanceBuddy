begin;
do $$
declare v_id uuid;
begin
  select id into v_id from auth.users order by id limit 1;
  perform pg_catalog.set_config('request.jwt.claim.sub',v_id::text,true);
end $$;
set local role authenticated;
do $$
declare v_count integer;
begin
  insert into public.transactions(user_id,id,date,description,amount,category,account_name)
  values
    (auth.uid(),'__rule_test_percent',current_date,'50% off',-1,'Uncategorized','Test'),
    (auth.uid(),'__rule_test_plain',current_date,'coffee',-1,'Uncategorized','Test');
  v_count := public.fb_apply_category_rule('%','Shopping');
  if v_count <> 1 then raise exception 'Wildcard affected unexpected records'; end if;
  if (select category from public.transactions where id='__rule_test_percent') <> 'Shopping'
    or (select category from public.transactions where id='__rule_test_plain') <> 'Uncategorized' then
    raise exception 'Literal category rule did not match as expected';
  end if;
end $$;
rollback;
select 'Literal category rule passed; writes rolled back' as result;
