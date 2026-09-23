-- Additive transition: old app can still use public.plaid_items, while new app
-- switches to narrowly scoped RPCs. Drop the old column after app rollout.
create schema if not exists financebuddy_private;
revoke all on schema financebuddy_private from public, anon, authenticated;

create table if not exists financebuddy_private.plaid_tokens (
  user_id uuid not null,
  item_id text not null,
  encrypted_access_token text not null,
  primary key (user_id,item_id),
  foreign key (user_id,item_id)
    references public.plaid_items(user_id,item_id) on delete cascade
);
revoke all on financebuddy_private.plaid_tokens from public, anon, authenticated;

insert into financebuddy_private.plaid_tokens (user_id,item_id,encrypted_access_token)
select user_id,item_id,encrypted_access_token from public.plaid_items
on conflict (user_id,item_id) do update
  set encrypted_access_token = excluded.encrypted_access_token;

create or replace function financebuddy_private.mirror_plaid_token()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into financebuddy_private.plaid_tokens (user_id,item_id,encrypted_access_token)
  values (new.user_id,new.item_id,new.encrypted_access_token)
  on conflict (user_id,item_id) do update
    set encrypted_access_token = excluded.encrypted_access_token;
  return new;
end;
$$;
revoke all on function financebuddy_private.mirror_plaid_token() from public, anon, authenticated;

drop trigger if exists financebuddy_mirror_plaid_token on public.plaid_items;
create trigger financebuddy_mirror_plaid_token
  after insert or update of encrypted_access_token on public.plaid_items
  for each row execute function financebuddy_private.mirror_plaid_token();

create or replace function public.fb_save_plaid_item(
  p_item_id text,p_encrypted_access_token text,p_institution_name text
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare v_uid uuid := (select auth.uid());
begin
  if v_uid is null or length(p_item_id) not between 1 and 256
    or length(p_encrypted_access_token) not between 1 and 8192
    or length(p_institution_name) not between 1 and 256 then
    raise exception 'Invalid bank connection';
  end if;
  insert into public.plaid_items (
    user_id,item_id,encrypted_access_token,institution_name,status,error_message
  ) values (v_uid,p_item_id,p_encrypted_access_token,p_institution_name,'connected',null)
  on conflict (user_id,item_id) do update set
    encrypted_access_token=excluded.encrypted_access_token,
    institution_name=excluded.institution_name,
    status='connected',error_message=null;
end;
$$;
revoke all on function public.fb_save_plaid_item(text,text,text) from public, anon;
grant execute on function public.fb_save_plaid_item(text,text,text) to authenticated;

create or replace function public.fb_plaid_token(p_item_id text)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare v_uid uuid := (select auth.uid());
        v_token text;
begin
  if v_uid is null then raise exception 'Authentication required'; end if;
  select encrypted_access_token into v_token
  from financebuddy_private.plaid_tokens
  where user_id=v_uid and item_id=p_item_id;
  return v_token;
end;
$$;
revoke all on function public.fb_plaid_token(text) from public, anon;
grant execute on function public.fb_plaid_token(text) to authenticated;
