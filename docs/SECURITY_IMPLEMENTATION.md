# Security and data-integrity implementation

Updated September 22, 2026. The FinanceBuddy Supabase project linked to this checkout
has the six `20260923` migrations applied. The first migration was already present in
the remote schema and was marked applied in migration history after its tables, columns,
RLS, and ownership policies were checked.

## Implemented

- Destructive cloud operations now use one PostgreSQL transaction per operation:
  account/all-transaction replacement, backup restore, split, Plaid account replacement,
  Plaid incremental sync with cursor update, and append undo. A failed call rolls back
  every write. Plaid sync rejects stale cursors so concurrent attempts cannot silently
  advance a cursor without applying the matching transactions.
- Database constraints reject non-finite amounts and overlong fields. Python models
  apply matching length and finite-number checks before sending rows.
- Category rule matching is literal, including `%` and `_`, and is applied in one
  database operation. Existing Plaid error text was scrubbed from remote rows.
- The app rejects privileged Supabase secret/service-role keys in the publishable-key
  slot. Anonymous and cross-user reads/writes were tested against the linked project's
  seven public tables. The database's automatic-RLS helper no longer grants direct
  execution to API roles.
- Plaid ciphertext was copied to an RLS-protected table in a private, unexposed schema.
  The new app retrieves it through an authenticated, caller-permission function. The
  public ciphertext column remains temporarily for the older deployed app version.
- PDF extraction runs in a separate process with a wall-clock timeout and bounded
  result; Linux deployments also set memory and CPU limits. Session data expires after
  30 minutes of inactivity on the next app rerun, and logout/account changes clear
  private state. Import undo checks that each appended row is still unchanged.

## Verification

The repository has SQL rollback/isolation checks under `scripts/verify_*.sql` and
Python regression tests. The linked database passed anonymous/user-A/user-B isolation,
cross-user token denial, atomic failure rollback, stale Plaid cursor, and literal-rule
checks. All test rows were created inside transactions that rolled back. The latest
Supabase security advisor reported only leaked-password protection disabled.

## Remaining rollout and operational work

1. Deploy the current application code, then test login, Plaid connection and sync,
   statement import, restore, and append undo against the deployed HTTPS URL. Only after
   that rollout, run `supabase/staged/drop_public_plaid_token_after_app_rollout.sql`.
   This checked SQL validates the private copy before removing the public compatibility
   column. Applying it before the app rollout would break the older deployment.
2. The linked project currently reports no managed backups or point-in-time recovery.
   Set up a tested, encrypted offsite database backup and restoration process, or move
   to a plan with suitable managed backups before relying on it for important financial
   history. The app's JSON export covers its main financial collections, but omits bank
   connection credentials and settings and is not encrypted by the app.
3. Supabase leaked-password protection is disabled. Its [password security
   documentation](https://supabase.com/docs/guides/auth/password-security) places this
   feature on Pro and above; enabling it requires a plan decision. Review email
   confirmation, redirect allowlists, auth rate limits, CAPTCHA/MFA, and monitoring in
   the project dashboard during production release.
4. Money columns still use `double precision` and Python `float`. The new constraints
   prevent NaN/infinity, but exact accounting requires a reviewed migration to decimal
   or integer minor units across storage, models, import, and analytics. Direct API
   clients also bypass the app's aggregate import quotas; add database-side per-user
   limits if that abuse case matters for public sign-up.
5. The navigation and import overhaul described in
   [SECURITY_UX_AUDIT.md](SECURITY_UX_AUDIT.md) is implemented locally. It still needs
   a deployed mobile and keyboard review with real authentication, Plaid Link, and
   uploaded statements before its usability can be confirmed in production.
