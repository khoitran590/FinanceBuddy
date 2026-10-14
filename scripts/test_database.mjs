// Optional development check using PostgreSQL compiled to WASM. No live project access.
// npm install --prefix /tmp/financebuddy-sql --no-audit --no-fund @electric-sql/pglite@0.5.9
// node scripts/test_database.mjs /tmp/financebuddy-sql/node_modules/@electric-sql/pglite/dist/index.js
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const { PGlite } = await import(process.argv[2] ? pathToFileURL(process.argv[2]).href : '@electric-sql/pglite');
const db = new PGlite();
try {
  // Minimal Supabase Auth scaffolding; policies/functions are the actual migrations.
  await db.exec(`create role anon; create role authenticated; create role service_role;
    create schema auth; create table auth.users(id uuid primary key);
    insert into auth.users values ('00000000-0000-0000-0000-000000000001'),('00000000-0000-0000-0000-000000000002');
    create function auth.uid() returns uuid language sql stable as $$
      select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
    grant usage on schema auth to anon,authenticated;
    grant execute on function auth.uid() to anon,authenticated;`);
  for (const file of readdirSync(`${root}/supabase/migrations`).sort()) {
    if (file === '202610130001_source_splits_and_reconciliation.sql') {
      // Migration must recover legacy pairs without overriding a source previously
      // re-created by sync, and must preserve partial pairs instead of guessing.
      await db.exec(`insert into public.transactions(user_id,id,date,description,amount,account_name) values
        ('00000000-0000-0000-0000-000000000001','legacy-split-1',current_date,'Store (split 1)',-60,'Card'),
        ('00000000-0000-0000-0000-000000000001','legacy-split-2',current_date,'Store (split 2)',-40,'Card'),
        ('00000000-0000-0000-0000-000000000001','updated',current_date,'Store',-110,'Card'),
        ('00000000-0000-0000-0000-000000000001','updated-split-1',current_date,'Store (split 1)',-60,'Card'),
        ('00000000-0000-0000-0000-000000000001','updated-split-2',current_date,'Store (split 2)',-40,'Card'),
        ('00000000-0000-0000-0000-000000000001','partial-split-1',current_date,'Store (split 1)',-60,'Card'),
        ('00000000-0000-0000-0000-000000000001','nested-split-1-split-1',current_date,'Store (split 1) (split 1)',-20,'Card'),
        ('00000000-0000-0000-0000-000000000001','nested-split-1-split-2',current_date,'Store (split 1) (split 2)',-40,'Card'),
        ('00000000-0000-0000-0000-000000000001','nested-split-2',current_date,'Store (split 2)',-40,'Card');`);
    }
    await db.exec(readFileSync(`${root}/supabase/migrations/${file}`, 'utf8'));
  }
  await db.exec(`do $$ begin
    if (select amount from public.transactions where id='legacy')<>-100
      or (select amount from public.transactions where id='updated')<>-110
      or (select count(*) from public.transaction_allocations where transaction_id in ('legacy','updated'))<>4
      or (select count(*) from public.transaction_allocations where transaction_id='nested')<>3
      or (select amount from public.transactions where id='nested')<>-100
      or not exists(select 1 from public.transactions where id='partial-split-1') then
      raise exception 'Legacy split migration failed'; end if;
    end $$;`);
  console.log('Passed all migrations and legacy split recovery.');
  for (const file of ['verify_supabase_isolation.sql', 'verify_atomic_operations.sql', 'verify_atomic_plaid_sync.sql',
                       'verify_literal_category_rule.sql', 'verify_private_plaid_tokens.sql',
                       'verify_source_splits_and_reconciliation.sql', 'verify_statement_imports.sql']) {
    await db.exec(readFileSync(`${root}/scripts/${file}`, 'utf8'));
    console.log(`Passed ${file}`);
  }
} catch (error) {
  console.error('Database verification failed:', error.message);
  if (error.where) console.error(error.where);
  process.exitCode = 1;
} finally {
  await db.close();
}
