-- Apply only after deploying code that reads/writes Plaid tokens via
-- fb_plaid_token/fb_save_plaid_item. Back up the database first.
-- This removes the now-redundant ciphertext from the public Data API table.
begin;
do $$
begin
  if (select count(*) from public.plaid_items) <>
     (select count(*) from financebuddy_private.plaid_tokens) then
    raise exception 'Private token copy is incomplete';
  end if;
  if exists (
    select 1 from public.plaid_items p
    left join financebuddy_private.plaid_tokens t
      on t.user_id=p.user_id and t.item_id=p.item_id
    where t.item_id is null or t.encrypted_access_token is distinct from p.encrypted_access_token
  ) then
    raise exception 'Private token copy differs from public token';
  end if;
end $$;

drop trigger financebuddy_mirror_plaid_token on public.plaid_items;
drop function financebuddy_private.mirror_plaid_token();

create or replace function public.fb_save_plaid_item(
  p_item_id text,p_encrypted_access_token text,p_institution_name text
)
returns void
language plpgsql
security invoker
set search_path = ''
as $$
declare v_uid uuid := (select auth.uid());
begin
  if v_uid is null or length(p_item_id) not between 1 and 256
    or length(p_encrypted_access_token) not between 1 and 8192
    or length(p_institution_name) not between 1 and 256 then
    raise exception 'Invalid bank connection';
  end if;
  insert into public.plaid_items (user_id,item_id,institution_name,status,error_message)
  values (v_uid,p_item_id,p_institution_name,'connected',null)
  on conflict (user_id,item_id) do update set
    institution_name=excluded.institution_name,
    status='connected',error_message=null;
  insert into financebuddy_private.plaid_tokens (user_id,item_id,encrypted_access_token)
  values (v_uid,p_item_id,p_encrypted_access_token)
  on conflict (user_id,item_id) do update
    set encrypted_access_token=excluded.encrypted_access_token;
end;
$$;

alter table public.plaid_items drop column encrypted_access_token;
commit;
