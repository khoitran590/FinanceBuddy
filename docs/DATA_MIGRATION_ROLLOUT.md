# Remaining architecture improvements: rollout and verification

October 13, 2026. Implementation is local; no live Supabase migration has been run.

## Deployment sequence

1. Stop application writes, including other sessions and any older app version. Retain the deployed application revision and a complete database snapshot/schema export. Save the configured Plaid encryption key separately; portable JSON exports exclude credentials and cannot replace an infrastructure backup.
2. Confirm migrations `202610130001` and `202610130002` are installed. Export the current portable backup where size permits, and run `scripts/money_preflight.sql` as the database administrator. Save the report privately. Review expected rounding deltas for every owner/collection; resolve blocking values and explicitly non-USD accounts before proceeding.
3. Apply `202610130003_money_accounts_and_provenance.sql` in a transaction. It rounds each legacy value from its decimal text representation to cents using HALF_UP (ties away from zero). New writes reject fractional cents. The supported per-value range is ±999,999,999,999.99 USD. The migration stores original values and expected rounded values in the unexposed `financebuddy_private.money_migration_audit` table. Run `scripts/verify_money_migration.sql` immediately; every original row/value must reconcile to its expected value, and collection counts/totals must agree.
4. Apply `202610130004_recoverable_operations_and_backups.sql`, then `202610130005_transaction_queries.sql`, each in a transaction. Deploy the matching Python code and verify the flows below in a development Supabase project before enabling production writes.
5. Keep the pre-rollout database snapshot and encryption key through verification. If reconciliation or validation fails, keep writes stopped. Roll back an uncommitted migration; for a committed rollout, restore the complete pre-rollout database/schema snapshot and the previous app revision together. A portable export alone cannot undo rounding, restore encrypted credentials, or reconstruct operation receipts. Do not cast newly edited values back to floats or drop new tables while the app is running.

## Changed data contracts

- Money uses `Decimal` in domain records and calculations, serialized as decimal strings. PostgreSQL uses `numeric` with exact-cent and range constraints. Floats are confined to widgets/chart display and non-monetary timing calculations. CSV/PDF currency columns and foreign currency markers, provider currencies, balances, snapshots, and backups reject non-USD values. Currency-less statements are explicitly reviewed as USD; legacy dollar records and previously unspecified balance currencies migrate as USD.
- Transactions reference owner-scoped UUID accounts. Provider mappings and labels are separate attributes. Labels can repeat or change without changing ownership. Disconnect retains the account/provider association when keeping history; deletion and account replacement use the owned ID. Ambiguous legacy labels require a selected account rather than automatic matching. Current and historical bank accounts without transactions are also backfilled. Previously missing bank associations and ambiguous historical data remain subject to the existing restore review policy.
- Category precedence is manual override, user rule, provider suggestion, then built-in suggestion. The same application pipeline handles statements and bank sync, and the database applies current rules at commit. Rule keyword/version and category origin are stored. Existing categories with unknown provenance remain `legacy` and are preserved until deliberately edited. Provider-supplied pending IDs transfer overrides, allocations and import aliases to the posted source; changed totals retain allocations for review and count the current source once.
- Logical writes use an operation UUID and an atomic result receipt. A lost response causes result lookup and, if necessary, retry with the same ID. Pending IDs are kept only in the owner's session; result receipts remain in the private database schema. Import batches are visible after a fresh session and undo requires unchanged source revisions. Revisions are never reused after restore/reinsertion. New file claims, category edits and allocations invalidate older undo receipts. Exact file retries retain their revision. Receipts contain structured results/provenance, never raw statements or bank access tokens.
- Disconnect records its removal choice before provider revocation. Pending disconnects disable sync and expose a retry action. Local cleanup follows confirmed revocation or `ITEM_NOT_FOUND`, which Plaid documents as covering previously removed/depermissioned Items. Other provider failures leave the encrypted credential and recorded intent available for retry. [Plaid Item errors](https://plaid.com/docs/errors/item/).
- Version 3 backup export is one SQL statement/MVCC snapshot. It includes canonical sources, allocations, import mappings, accounts, budgets, goals, rules, settings and retained USD balance snapshots. Export/restore enforce 10 MB and 50,000 records per collection. Version 1/2 restore remains supported, with deliberate legacy cent rounding and complete split recovery. Provider cursors reset for full reconciliation, credentials stay outside the export, and old import receipts cannot undo newly restored rows.

## Read boundaries

The navigation catalog returns counts, date bounds, category choices and owned account summaries. Accounts, Compare, category rules and backup preparation no longer load transaction history merely to display the page. Transaction queries filter in PostgreSQL and use `(date, projected ID)` keyset pagination, with up to 200 displayed rows per page. CSV export is prepared only on request. Duplicate suggestions are explicitly scoped to the displayed page.

Overview defaults to three months; an explicit All time selection can still retrieve its selected history for detailed charts. Recurring/income/forecast and Plan history are limited to a year. Only the selected Overview view renders; unselected views do not fetch balance history or compute their insights. SQL category/monthly summaries and targeted import-candidate/ID queries avoid redundant full-history reads. Bounded balance queries include the last earlier snapshot per account so inactive accounts do not silently disappear at the window boundary.

The catalog cache lives only within the current repository instance/rerun and is invalidated after writes. There is no shared financial-data/JWT cache. HTTP clients close at each request. Read and write Protocols live in the domain layer; application services own categorization, import/undo, restore and category-plus-rule intent. The SQLite adapter remains for legacy export/tests; production storage is Supabase. No unattended sync infrastructure was added because the product currently requests manual sync.

## Verification

Run:

```sh
.venv/bin/python -m pytest -q
node scripts/test_database.mjs /path/to/@electric-sql/pglite/dist/index.js
# Optional synthetic query measurement:
node scripts/test_database.mjs /path/to/@electric-sql/pglite/dist/index.js --benchmark
```

The database runner applies every migration, compares money counts/exact values/totals, verifies legacy split migration, and runs eight PostgreSQL regression scripts with authenticated/anonymous roles and two owners. The new script covers exact splits/currency rejection, labels shared across banks, common category rules and manual overrides, pending posting, keyset allocation pages, operation retries/conflicts, edited/restored undo rejection, backup round trips, and disconnect recovery.

A 50,000-row synthetic PGlite run returned 200 rows in one page: 155,254 response bytes versus 38,789,437 bytes for the full projection (about 250 times smaller). Observed local WASM times were 130 ms for the page, 161 ms for its aggregate summary, and 1,151 ms for the full projection. These are subsystem measurements, not deployed Supabase/Streamlit latency or a memory/throughput guarantee. The Python transport test verifies a 50,000-count transaction page needs one request and only 200 validated records; failure-injection tests recover writes committed before the response was lost.

Before production rollout, verify actual Supabase grants/RLS, query plans, request/response sizes and application latency using representative owned histories. Exercise sign-in, import/retry/undo in another session, split/update/removal, old-backup reconciliation, pending posting, bank disconnect failure/retry, and version 3 download/restore. No live provider behavior or production migration has been claimed by the local checks.
