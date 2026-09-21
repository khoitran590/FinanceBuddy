-- FinanceBuddy schema. Run with the Supabase SQL editor or CLI.
-- Every exposed table is protected by authenticated-user row-level security.

create table if not exists public.transactions (
  user_id uuid not null references auth.users(id) on delete cascade,
  id text not null,
  date date not null,
  description text not null,
  amount double precision not null,
  category text not null default 'Uncategorized',
  account_name text not null,
  account_type text not null default 'Checking',
  primary key (user_id, id)
);

create index if not exists transactions_user_date_idx
  on public.transactions (user_id, date desc);
create index if not exists transactions_user_account_idx
  on public.transactions (user_id, account_name);

create table if not exists public.budgets (
  user_id uuid not null references auth.users(id) on delete cascade,
  category text not null,
  monthly_limit double precision not null check (monthly_limit >= 0),
  primary key (user_id, category)
);

create table if not exists public.savings_goals (
  id bigint generated always as identity,
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  target_amount double precision not null check (target_amount > 0),
  current_amount double precision not null default 0 check (current_amount >= 0),
  target_date date,
  primary key (user_id, id)
);

create table if not exists public.category_rules (
  user_id uuid not null references auth.users(id) on delete cascade,
  keyword text not null,
  category text not null,
  primary key (user_id, keyword)
);

create table if not exists public.app_settings (
  user_id uuid not null references auth.users(id) on delete cascade,
  key text not null,
  value text not null,
  primary key (user_id, key)
);

create table if not exists public.plaid_items (
  user_id uuid not null references auth.users(id) on delete cascade,
  item_id text not null,
  encrypted_access_token text not null,
  institution_name text not null default 'Connected institution',
  cursor text,
  status text not null default 'connected' check (status in ('connected', 'error')),
  last_synced_at timestamptz,
  error_message text,
  primary key (user_id, item_id)
);

create table if not exists public.plaid_accounts (
  user_id uuid not null references auth.users(id) on delete cascade,
  account_id text not null,
  item_id text not null,
  name text not null,
  official_name text,
  type text not null,
  subtype text,
  mask text,
  primary key (user_id, account_id),
  foreign key (user_id, item_id)
    references public.plaid_items(user_id, item_id) on delete cascade
);

alter table public.transactions enable row level security;
alter table public.budgets enable row level security;
alter table public.savings_goals enable row level security;
alter table public.category_rules enable row level security;
alter table public.app_settings enable row level security;
alter table public.plaid_items enable row level security;
alter table public.plaid_accounts enable row level security;

revoke all on public.transactions from anon;
revoke all on public.budgets from anon;
revoke all on public.savings_goals from anon;
revoke all on public.category_rules from anon;
revoke all on public.app_settings from anon;
revoke all on public.plaid_items from anon;
revoke all on public.plaid_accounts from anon;

grant select, insert, update, delete on public.transactions to authenticated;
grant select, insert, update, delete on public.budgets to authenticated;
grant select, insert, update, delete on public.savings_goals to authenticated;
grant select, insert, update, delete on public.category_rules to authenticated;
grant select, insert, update, delete on public.app_settings to authenticated;
grant select, insert, update, delete on public.plaid_items to authenticated;
grant select, insert, update, delete on public.plaid_accounts to authenticated;
grant usage, select on sequence public.savings_goals_id_seq to authenticated;

do $$
declare table_name text;
begin
  foreach table_name in array array[
    'transactions', 'budgets', 'savings_goals', 'category_rules',
    'app_settings', 'plaid_items', 'plaid_accounts'
  ]
  loop
    execute format('drop policy if exists "owner_all" on public.%I', table_name);
    execute format(
      'create policy "owner_all" on public.%I for all to authenticated '
      'using ((select auth.uid()) = user_id) '
      'with check ((select auth.uid()) = user_id)',
      table_name
    );
  end loop;
end $$;
