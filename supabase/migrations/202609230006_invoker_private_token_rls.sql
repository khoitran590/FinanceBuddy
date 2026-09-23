-- The private schema is not exposed by the Data API. RLS makes token RPCs
-- run with caller privileges instead of relying on SECURITY DEFINER.
alter table financebuddy_private.plaid_tokens enable row level security;
drop policy if exists owner_all on financebuddy_private.plaid_tokens;
create policy owner_all on financebuddy_private.plaid_tokens
  for all to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);
grant usage on schema financebuddy_private to authenticated;
grant select, insert, update, delete on financebuddy_private.plaid_tokens to authenticated;

alter function public.fb_save_plaid_item(text,text,text) security invoker;
alter function public.fb_plaid_token(text) security invoker;
