# Data architecture audit

Reviewed October 13, 2026.

Implementation follow-up: findings 1 and 2 are addressed in the working tree by
`202610130001_source_splits_and_reconciliation.sql` and the matching app changes.
See the README section “Source transactions, splits, and restore” for rollout and
legacy-data handling. The findings below record the architecture at audit time.

## Assessment

Keep the existing Python/Streamlit + Supabase architecture. The main improvements are to transaction identity, ingestion correctness, recovery semantics, and query boundaries. A framework rewrite or another database would not address these problems.

The project has useful separation between parsing, domain validation, persistence, analytics, and presentation. Its strongest storage decisions are per-user primary keys and row-level security, user JWTs rather than a service-role key for normal data access, atomic RPCs for destructive operations, and committing Plaid transaction changes with their sync cursor. Upload limits and bounded PDF processing are also valuable.

However, the transaction table currently serves as both a bank/source record and a user-edited analysis record. This is the root of several correctness problems. Account labels also serve as identities, and the UI orchestrates important multi-step data operations directly.

## Scope and verification

Reviewed `app.py`, domain models, parser/categorizer, analytics, Supabase client/repositories, legacy SQLite repositories, all checked-in migration definitions relevant to data processing, and existing tests and SQL verification scripts.

- Ran `.venv/bin/python -m pytest -q`: **118 passed in 7.63 seconds**.
- Ran isolated synthetic checks using temporary SQLite storage and the actual parser/provider normalization functions. Results are included below.
- PostgreSQL findings are based on the checked-in SQL, including the latest function redefinitions in `202609240001_spending_insights.sql`. The deployed schema, production data, live RLS behavior, database plans, and live Plaid API were not inspected.
- No application code, migration, or live data was changed for this audit.

Passing Python tests does not establish the behavior of the deployed SQL functions: several cloud tests check mocked RPC calls or SQL text. The SQL verification scripts provide a starting point for database integration coverage.

## Findings, ordered by impact

### 1. High — Splitting destroys the source transaction identity

Evidence: [split RPC](/Users/jennietran/Downloads/FinanceBuddy/supabase/migrations/202609240001_spending_insights.sql:162), [sync RPC](/Users/jennietran/Downloads/FinanceBuddy/supabase/migrations/202609240001_spending_insights.sql:247).

Splitting creates `original-id-split-1` and `original-id-split-2`, then deletes the original. A later Plaid modification upserts the original provider ID, recreating the full transaction alongside both splits. A provider removal deletes only the original ID and leaves the split rows. Re-importing a statement can similarly recreate its original row.

Synthetic reproduction with the legacy repository, whose split/upsert/delete behavior has the same identity issue: split a $100 expense into $60 and $40, then upsert the provider's $100 row. Spending becomes **$200 across three rows**. Removing the original leaves **$100 of orphaned splits**. This was not a live PostgreSQL execution.

Recommended design: retain a canonical transaction and store category allocations in a child table. Analytics should use allocations when present and the canonical amount otherwise. Provider removal should hide/delete the canonical row and its allocations together. If a provider changes the total after a split, mark allocations for review rather than silently keeping an invalid sum.

### 2. High — Restore and account replacement can invalidate incremental sync

Evidence: [restore RPC](/Users/jennietran/Downloads/FinanceBuddy/supabase/migrations/202609230001_atomic_data_operations.sql:114), [backup contents](/Users/jennietran/Downloads/FinanceBuddy/src/repositories/supabase_repo.py:231), [sync starting cursor](/Users/jennietran/Downloads/FinanceBuddy/src/services/plaid_service.py:200).

Restoring replaces all transactions but retains bank connections and their current cursors. For example, an October cursor remains after restoring a September backup. The next sync requests changes after that October cursor; transactions absent from the older backup that remain unchanged at the provider are not reconstructed by that incremental operation. Account replacement and deleting connected-account transactions have the same mismatch.

Recommended design: explicitly distinguish portable statement data from provider-managed data. Either preserve bank rows when restoring portable data, or atomically flag affected bank items for a full reconciliation from a reset cursor. Store transaction-to-item/account associations so reconciliation can target the correct rows and remove stale data. Preserve user annotations separately throughout reconciliation. A reset cursor alone is insufficient to remove old restored rows that are no longer in the provider's current history.

### 3. High — Content hashes silently collapse legitimate repeated purchases

Evidence: [ID generation](/Users/jennietran/Downloads/FinanceBuddy/src/services/categorizer.py:134), [parser IDs](/Users/jennietran/Downloads/FinanceBuddy/src/services/parser.py:225), [duplicate-skipping insert](/Users/jennietran/Downloads/FinanceBuddy/src/repositories/supabase_repo.py:79).

The hash includes account type/name, date, amount rounded to two decimals, and description. Two genuine purchases with identical values receive the same ID. The append operation ignores the second row. The preview counts existing IDs but does not resolve duplicates within a new file.

Synthetic reproduction: two Coffee purchases on the same date for $4.50 each produce **two parsed rows, one unique ID, and one inserted row**. Spending is $4.50 instead of $9.00. Account name is already included in the hash; the issue is not simply missing account scope.

Recommended design: prefer a bank reference when available. Otherwise give rows stable internal IDs, record import-batch and row identity, and use the content fingerprint as a matching signal rather than unconditional uniqueness. A file hash can make retrying the same file idempotent; overlapping statements need multiplicity-aware reconciliation or review so identical genuine occurrences survive. Plaid and statement rows also have different ID schemes, so imports of connected accounts need a cross-source reconciliation policy.

### 4. High — Credit-card heuristics override explicit debit/credit direction

Evidence: [sign heuristic](/Users/jennietran/Downloads/FinanceBuddy/src/services/parser.py:71), [debit/credit parsing followed by normalization](/Users/jennietran/Downloads/FinanceBuddy/src/services/parser.py:217).

The parser first derives direction from Debit/Credit columns, then overrides it for credit-card accounts using words such as `payment`, `credit`, and `return`. The 80% gate measures whether rows parse, not whether their financial meaning is correct.

Synthetic reproduction: an explicit **Debit $25** named `PAYMENT TO STORE` becomes **+$25**; an explicit **Credit $10** named `PROMOTIONAL BONUS` becomes **-$10**. Both rows pass with 100% parsing validity.

Recommended design: honor explicit debit/credit columns. For a single Amount column, use a bank format profile or an explicit sign-convention choice; description heuristics should flag ambiguous rows for review. Add date/number format profiles too: `01/02/2026` currently resolves to month/day first, and comma-decimal numbers are altered by unconditional comma removal. Add statement-total reconciliation when totals are available.

### 5. Medium — Money and currency need an explicit domain representation

Evidence: [money models](/Users/jennietran/Downloads/FinanceBuddy/src/domain/models.py:19), [database money types](/Users/jennietran/Downloads/FinanceBuddy/supabase/migrations/202609200001_financebuddy.sql:9), [provider normalization](/Users/jennietran/Downloads/FinanceBuddy/src/services/plaid_service.py:322), [balance aggregation](/Users/jennietran/Downloads/FinanceBuddy/src/services/analytics.py:1174).

Amounts, budgets, goals, balances, and split parameters use floats/double precision. Split validation accepts a half-cent tolerance rather than requiring an exact monetary sum. Transactions omit currency; current account metadata includes currency, but balance-history rows omit it, and analytics adds balances without grouping by currency. Mixed-currency data would therefore produce misleading totals. The audit did not establish that current users have mixed-currency data.

Recommended design: define a Money value using Decimal in Python and a deliberate `numeric` precision/scale in PostgreSQL, or integer minor units plus currency metadata. Preserve currency on transactions and historical snapshots. Aggregate per currency unless there is an explicit conversion policy. If the product is USD-only, validate that assumption at ingestion. Convert to floats only at chart boundaries where appropriate.

### 6. Medium — Every rerun retrieves the entire transaction history

Evidence: [unconditional load](/Users/jennietran/Downloads/FinanceBuddy/app.py:2449), [repository full reads](/Users/jennietran/Downloads/FinanceBuddy/src/repositories/supabase_repo.py:70), [client pagination](/Users/jennietran/Downloads/FinanceBuddy/src/services/supabase.py:218), [overview rendering](/Users/jennietran/Downloads/FinanceBuddy/app.py:1298).

Every page loads every transaction before navigation/filtering. The client paginates in 1,000-row requests, but returns one complete in-memory list. Duplicate checks fetch and validate the entire history again; replacement preview and backup can repeat these reads. At 50,000 records, one full read uses 51 requests with the current termination logic. Mobile card pagination only limits rendering after the full history is loaded.

The overview also invokes every tab renderer on each full script run. Balance history is fetched in full, and `get_item()` fetches all items plus one token RPC per item rather than querying a single item. Auth/data HTTP clients are recreated on each script run without explicit close handling.

Recommended design: introduce query-specific repository methods: filtered transaction pages, counts/date bounds/account lists, ID existence checks, bounded analysis windows, and monthly/category aggregates. Use keyset pagination with `(date, id)` and a matching `(user_id, date desc, id desc)` index where measured query plans justify it. Keep full exports as an explicit operation. Compute expensive insight groups once per data revision and render the selected view lazily. Any financial-data cache must be scoped to the authenticated owner, have bounded freshness, and invalidate after writes and account changes; never share JWT-bearing clients between users.

### 7. Medium — Import undo has no authoritative import ownership

Evidence: [UI's before/after inference](/Users/jennietran/Downloads/FinanceBuddy/app.py:2078), [insert returns only a count](/Users/jennietran/Downloads/FinanceBuddy/src/repositories/supabase_repo.py:79), [undo RPC](/Users/jennietran/Downloads/FinanceBuddy/supabase/migrations/202609230001_atomic_data_operations.sql:201).

The UI reads existing IDs, inserts with duplicate skipping, then infers undo IDs from the earlier read. Another session can insert matching rows between those steps. The current insert skips those rows, yet the inferred undo list can include them. Undo compares row values, which cannot prove that this import created the row. The comparison also predates the optional provider-detail fields.

Recommended design: an atomic import RPC should create an `import_batch`, return the exact inserted IDs/count, and attach provenance plus row revision information. Undo should operate on that batch and require unchanged revisions. This also provides a durable receipt if the UI loses its session.

### 8. Medium — Category provenance is lost and processing differs by source

Evidence: [statement custom rules](/Users/jennietran/Downloads/FinanceBuddy/app.py:368), [Plaid category selection](/Users/jennietran/Downloads/FinanceBuddy/src/services/plaid_service.py:332), [sync category preservation](/Users/jennietran/Downloads/FinanceBuddy/supabase/migrations/202609240001_spending_insights.sql:262).

Statement imports apply user rules; bank sync uses the provider mapping or built-in rules, without the user's saved rules. On subsequent syncs every existing category other than `Uncategorized` is treated as a protected user choice, including categories assigned automatically. The schema cannot distinguish manual decisions from provider suggestions. There is also no pending-to-posted transaction association for carrying annotations across a provider ID replacement.

Synthetic normalization check: a custom Coffee → Groceries rule yields **Groceries for statement processing and Dining for bank normalization**.

Recommended design: share a categorization pipeline across ingestion sources. Store provider category separately from user category override, with rule ID/version and category origin. Define precedence explicitly: manual override, applicable user rule, provider category, built-in rule. Preserve pending/posting lineage when supplied by the provider so manual edits and allocations can be transferred deliberately.

### 9. Medium — Display names are used for account ownership and deletion

Evidence: [transaction model](/Users/jennietran/Downloads/FinanceBuddy/src/domain/models.py:7), [account deletion](/Users/jennietran/Downloads/FinanceBuddy/src/repositories/supabase_repo.py:213), [disconnect cleanup](/Users/jennietran/Downloads/FinanceBuddy/src/services/plaid_service.py:273).

Transactions retain `account_name`/`account_type`, but omit the Plaid account and item IDs. Disconnect-with-removal deletes transactions using formatted account names. Two institutions or an imported account can share a label/mask; cleanup can then delete unrelated transactions. Provider label changes also fragment account filtering and prevent reliable cleanup of older labels.

Recommended design: create stable internal accounts and provider-account mappings. Transactions should reference an owner-scoped account ID; names should be editable attributes. Keep the provider connection association even if users retain history after disconnect. Delete/archive by internal account or item identity, never by display text. This foundation supports findings 2, 3, and 8.

### 10. Medium — Recovery and write outcomes need durable operation records

Evidence: [backup export](/Users/jennietran/Downloads/FinanceBuddy/src/repositories/supabase_repo.py:231), [backup limits](/Users/jennietran/Downloads/FinanceBuddy/src/services/security.py:26), [request error handling](/Users/jennietran/Downloads/FinanceBuddy/src/services/supabase.py:201), [import error message](/Users/jennietran/Downloads/FinanceBuddy/app.py:2093).

Backup export performs separate collection reads rather than one consistent database snapshot. It excludes settings, balance history, and source/sync metadata. Those exclusions are disclosed in the UI, but make it a portable user export rather than complete system recovery. Export has no matching 10 MB/50,000-record guard, so the app can produce a backup its restore rejects.

The data client turns network failures and database errors into generic messages. A timeout can occur after a write commits; the UI nevertheless says existing history is unchanged and suggests retrying. Retrying a non-idempotent operation such as a split cannot safely infer its outcome from that error. Some multi-step operations, including disconnect cleanup and category-plus-rule saving, also span separate requests.

Recommended design: give logical writes a client-generated operation ID and store durable results in the same transaction as the mutation. After an uncertain response, query that operation's result before retrying. Preserve safe error codes/correlation IDs for diagnostics while excluding credentials and financial payloads from logs. Export a versioned, consistent snapshot with matching restore limits; treat infrastructure backups and encryption-key recovery as a separate operational capability. Use a recoverable disconnect state machine because provider revocation and local deletion cannot share one database transaction.

## Recommended architecture

Retain the current domain/services/repositories/UI layers, but make their responsibilities explicit:

| Boundary | Responsibility |
| --- | --- |
| Source adapters | CSV/PDF/Plaid parsing, source identity, sign/date/number conventions, currency, optional pending lineage |
| Application services | Preview/commit import, categorize, reconcile bank sync, split allocations, restore, disconnect; operation IDs and durable receipts |
| Domain | Money, canonical transaction, account identity, user annotation, allocation invariants |
| Repositories + atomic SQL | Owner-scoped queries and writes; enforce constraints and complete logical database transactions |
| Read/analytics services | Filtered pages and bounded summaries; deterministic calculations over the requested data |
| Streamlit UI | Collect intent, show previews/results, call application services |

Suggested schema additions are `accounts`, `provider_accounts`, `import_batches`, `import_rows`, `transaction_allocations`, user-annotation fields/table, and `data_operations`. Canonical transactions need stable internal identity, account ID, source identity, currency, and revision timestamps. This can be introduced incrementally; it does not require an event-sourcing system or retaining every uploaded statement indefinitely.

Add repository Protocols/interfaces in a framework-independent location. `PlaidService` currently imports and type-annotates concrete SQLite repositories even though production supplies Supabase implementations, then uses `hasattr` to select atomic behavior. Declare capabilities explicitly. Move import/undo, restore, and category-rule orchestration out of `app.py` so the same invariants apply to a future mobile client.

Raw-file retention is optional. File hash, batch metadata, parser version, source references, and row provenance provide useful traceability without indefinitely storing original financial statements. Define any raw-data retention policy deliberately.

## Implementation order

1. **Correctness first:** honor explicit debit/credit direction; add regression tests for all reproduced cases; retain canonical transaction identity and move splits to allocations. Introduce stable account/source associations before changing restore/reconciliation behavior. As a temporary measure, restrict operations that break active bank sync until that model is available.
2. **Reliable ingestion and recovery:** commit imports with authoritative batch receipts; define occurrence-aware deduplication and cross-source reconciliation; separate category suggestions/overrides; implement restore reconciliation and operation-result recovery. Migrate money/currency using an explicit rounding policy and reconciliation of existing totals.
3. **Query and layering improvements:** filtered pages and summary queries; bounded insight windows; lazy view computation; owner-scoped revision-aware caches only where measured; repository Protocols and application services. Add unattended sync/webhook workers only when background freshness is a product requirement—the current manual sync model alone does not justify more infrastructure.

Before a destructive schema migration, export recoverable data, compare row counts and monetary totals before/after, and keep a reviewed rollback path. Do not rewrite existing source IDs or recalculate old ambiguous imports automatically without a reconciliation plan.

## Verification to add

- Run migrations against a disposable PostgreSQL/Supabase environment; verify isolation with two authenticated users and verify rollback for invalid batches.
- Exercise split → provider modification/removal and split → statement re-import; verify totals and annotations remain correct.
- Restore an older backup while bank connections retain newer state; verify complete reconciliation, including deletions.
- Import two identical genuine purchases, retry the same file, and import overlapping statements; verify occurrence counts.
- Race two imports and perform undo; verify one batch cannot remove another batch's rows.
- Preserve user categories and splits across pending-to-posted identity changes and provider category updates.
- Test explicit debit/credit signs, locale ambiguity, exact monetary allocation sums, currency restrictions, backup round trips and limits, and uncertain write outcomes.
- Measure request count, transferred bytes, memory, and page latency at representative histories before/after query changes. Existing picker microbenchmarks do not measure full database-to-dashboard performance.
