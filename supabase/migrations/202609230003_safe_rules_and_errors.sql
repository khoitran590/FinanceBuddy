-- Literal merchant matching prevents wildcard characters from recategorizing
-- unrelated transactions. Old raw provider errors are replaced by safe copy.
create or replace function public.fb_apply_category_rule(p_keyword text,p_category text)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_count integer;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  if length(pg_catalog.btrim(p_keyword)) not between 1 and 256
    or length(p_category) not between 1 and 128 then
    raise exception 'Invalid category rule';
  end if;
  update public.transactions
    set category=p_category
    where user_id=v_uid
      and pg_catalog.strpos(pg_catalog.lower(description),pg_catalog.lower(pg_catalog.btrim(p_keyword))) > 0;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;
revoke all on function public.fb_apply_category_rule(text,text) from public, anon;
grant execute on function public.fb_apply_category_rule(text,text) to authenticated;

update public.plaid_items
set error_message='Reconnect this institution to continue syncing.'
where error_message is not null and error_message <> 'Reconnect this institution to continue syncing.';
