-- The platform-created RLS event-trigger helper does not need Data API grants.
-- Keep the event trigger itself installed; only remove direct RPC access.
do $$
begin
  if to_regprocedure('public.rls_auto_enable()') is not null then
    revoke execute on function public.rls_auto_enable()
      from public, anon, authenticated, service_role;
  end if;
end $$;
