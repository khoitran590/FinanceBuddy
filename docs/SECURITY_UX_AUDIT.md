# FinanceBuddy security and UX audit

> Historical audit from September 22, 2026. The security and data-integrity items
> implemented since this review, plus remaining rollout and operational work, are
> recorded in [SECURITY_IMPLEMENTATION.md](SECURITY_IMPLEMENTATION.md). The UI/UX
> findings below describe the original state; the current implementation status is
> recorded after the table.

Reviewed September 22, 2026. Changes are local; no production deployment or database migration was performed.

## Assessment

The application has a useful security foundation, but it should not be considered fully production-verified. The most urgent code issue was private Streamlit state surviving logout and account changes. This is fixed. The highest remaining data-integrity risk is cloud operations that delete and recreate data across separate requests.

A full frontend rewrite is not justified by this review. Prioritize navigation, import/recovery feedback, accessible controls, and predictable data scope. The existing typography, grouping, labeled inputs, import previews, and mobile transaction cards provide a workable foundation.

## Scope and evidence

- Read authentication/session handling, Supabase clients and SQL policies, Plaid encryption/sync, both repository implementations, import parsers, export formatting, deployment configuration, and UI code.
- Ran all 66 tests, including 19 new security and authenticated UI regression cases. All passed.
- Used mocked HTTP responses to test error redaction and malformed input; used a temporary SQLite database and mocked authentication for authenticated UI tests. No real financial records were modified.
- Inspected login, signup, and initial password-recovery UI in the browser. Checked login at desktop size and 390 × 844. Authenticated screens were assessed through source and Streamlit AppTest, not a signed-in browser session. Email delivery, real bank linking, production permissions, and full keyboard/screen-reader behavior remain unverified.
- Audited 59 installed Python packages with pip-audit. Initial scan reported the same pip advisory twice: PYSEC-2026-3721 / CVE-2026-13346, affecting installed pip 26.1.2. Updated project virtual-environment pip to 26.2.1 and required pip >=26.2 in Docker. Repeat scan: no known vulnerabilities. This describes the installed environment, not a fresh production image or OS-package scan.
- Bandit initially found 7 low-severity alerts and no medium/high alerts: three false-positive secret literals (an email-link type and environment-variable names), two intentional PDF extraction fallback handlers, and two startup process-launch warnings. The startup executable and arguments are application-controlled, with no shell. These do not establish exploitable injection, although a trusted runtime PATH is still required.
- `.env`, runtime secrets, and SQLite data are excluded from tracked files; no history entries were found for `.env` or `.streamlit/secrets.toml`. This was a targeted check, not an exhaustive secret scan of all historical content.

## Fixes implemented

| Priority | Finding and impact | Change |
| --- | --- | --- |
| High | Logout removed only the login token. Undo snapshots, uploaded statements, passwords in widget state, and Plaid connection state could survive and be reused by the next account in the same session. | Clear all application session keys on logout/authentication failure. Bind state to the verified user ID and discard old state when identity changes. Preserve only the new auth session during an identity change. |
| High | An incomplete backup such as `{"version":1}` was treated as empty collections and could wipe records. | Require every backup collection to be a list of objects before any write. Both repository paths share this check. Existing per-record validation still runs before deletion. |
| Medium | Statements/backups had no explicit application size budget; CSV reader errors could crash the flow. | 10 MiB upload limit at Streamlit and service boundaries; 50,000 CSV data rows, 100 PDF pages, 50,000 records per backup collection. Invalid/oversized CSV fields return a recoverable parsing message. |
| Medium | Non-finite monetary values and invalid budget/goal amounts could pass application validation. | Reject NaN/infinity in monetary models; require nonnegative budgets/current savings and positive savings targets. |
| Medium | Provider errors could expose request details or financial rows in the UI and persisted Plaid error messages. | Replace raw authentication, database, Plaid, and PDF errors with application-controlled messages. Hide framework exception details from the browser. Previously saved Plaid error text is not retroactively scrubbed. |
| Medium | Dashboard search/date/category filters changed monthly budget totals, potentially making overspending disappear. | Budget calculations now use all saved transactions for the selected month. Explain filter scope in both the sidebar and budgets screen. |
| Medium | “Files stay local” was inaccurate for hosted Streamlit; restore claimed to replace data it actually preserves. | Explain server-side upload processing and save confirmation. Clarify that backups are unencrypted and omit bank connections/settings; accurately label the four replaced collections. |
| Low | Login and onboarding copy described database internals and incorrectly implied a separate database per account. | Use plain-language instructions and account-isolation wording. |

Session cleanup removes application references; it is not a guarantee of immediate memory erasure from the server or browser. Upload limits reduce resource exposure but are not full sandboxing of a hostile PDF.

## Remaining security and data-integrity priorities

1. **High — make destructive cloud changes atomic.** `SupabaseTransactionRepository.restore_backup`, `replace_account`, `replace_all`, and `split_transaction` make multiple HTTP writes. A failure between deletion and insertion can lose records; a partial split can duplicate amounts. Implement PostgreSQL transactional RPCs using invoker permissions and `auth.uid()`, preserve RLS, and test rollback under an injected failure. Validate duplicate record IDs and schema constraints before writes. Test with two users and concurrent edits before migration rollout.
2. **High — verify deployed tenant isolation.** The checked-in migration enables RLS on all seven tables and checks `auth.uid() = user_id` on reads and writes. That does not prove deployed policies match. Run anonymous/user-A/user-B SELECT/INSERT/UPDATE/DELETE tests against a disposable Supabase project. Verify the configured key is publishable/anon, never service-role or secret. Add configuration rejection of privileged keys as defense in depth.
3. **Medium — enforce rules at the database boundary.** Authenticated users can access PostgREST directly. Python validation alone cannot enforce finite amounts, field lengths, record quotas, or business rules. Add matching database constraints after reviewing existing data; avoid floating-point storage for exact monetary accounting and plan a numeric/minor-unit migration.
4. **Medium — strengthen parsing isolation and request budgets.** A compact PDF can still expand or take excessive CPU during extraction, and page enumeration itself has cost. Run parsing in a worker process with CPU/memory/time limits, restrict aggregate uploads, and rate-limit expensive operations. All Streamlit tabs currently execute during reruns, increasing repeated reads/parsing and backup generation.
5. **Medium — scope undo to its operation.** “Undo last import” stores the entire history and later replaces all transactions. Later syncs/edits or another browser session can be overwritten. Use an import-batch identifier and an undo action that affects only that batch, with a clear expiry or revision check.
6. **Medium — validate production auth and operational controls.** Confirm Supabase auth throttling, email confirmation, redirect allowlists, session lifetime, leaked-password protection, and monitoring in deployment settings. Consider MFA and an inactivity timeout for sensitive financial sessions. Verify restoration from encrypted database backups and document key rotation. No live settings were changed.
7. **Medium — reduce bank-token exposure.** RLS restricts Plaid ciphertext to its owner, but the owner can read/write it through PostgREST. Consider moving token storage behind a narrowly scoped server operation/private schema. Retain tenant authorization for every operation and use managed key storage/rotation. Never replace the user's JWT with an unrestricted service key in ordinary repository requests.
8. **Medium — handle failure and partial state explicitly.** Several repository calls can still fail out of the UI flow. Add operation-specific retry/recovery states that preserve user input, safe diagnostic IDs, and no raw financial payloads in logs. Scrub previously persisted raw Plaid error messages through a reviewed maintenance operation if applicable. Surface remote logout failure without retaining local private state.

Supabase's [production checklist](https://supabase.com/docs/guides/deployment/going-into-prod) covers deployed RLS, auth rate limits, and operational checks. OWASP's [file upload guidance](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html) supports bounded uploads and layered validation.

## UX improvements to prioritize next

| Priority | Friction | Proposed flow and acceptance check |
| --- | --- | --- |
| High | New users land on an empty overview and must discover nested Import & data tabs. | Offer “Connect a bank” and “Upload a statement” as direct first-run actions. Reach the selected input in one action. Retain manual import as an equal option. |
| High | Five main tabs plus five data-management subtabs hide frequent actions alongside destructive maintenance. All tabs execute on rerun. | Use persistent navigation: Overview, Transactions, Plan, Accounts; place backup, restore, and rules in Settings, with Compare as a secondary analysis action. Render only the active destination. Preserve location after saving. |
| High | White text on the bright pink primary buttons appears weak; mobile transaction history stops at 50 cards. | Measure all interactive color pairs against WCAG AA, darken the button fill or use dark text, verify visible keyboard focus, and add mobile pagination/load-more. Ensure every transaction is reachable without desktop mode. |
| Medium | Preview, validation failures, save outcome, and next steps are spread through a long import screen. | Use a guided account → file → preview → save flow. Show skipped rows with reasons, explain duplicate matching, persist a receipt after rerun, and provide a next action to review categories. |
| Medium | Restore and undo can affect much more data than the currently visible account. | Show exact collection/count changes, offer a backup first, and require a clear confirmation. Coordinate this UI with transactional writes and scoped undo. |
| Medium | Sign-up/recovery use tabs and conditional forms; code-based recovery lacks password confirmation. | Keep the user on the active stage after rerun, show password requirements before submission, add confirmation consistently, and test expired-link/resend/recovery journeys. |
| Medium | Dashboard filters sit in the mobile sidebar and can silently reduce visible records. | Show active filters and the visible/total count near results, with a prominent reset action and a distinct no-results state. Keep monthly budgets independent, as fixed here. |
| Low | Login fills the desktop viewport and the app exposes development-oriented toolbar controls. | Use a constrained auth form width and production-appropriate toolbar settings; preserve the usable mobile form layout observed in this review. |

Suggested order: transactional safety and deployed RLS tests first; onboarding/navigation and import recovery second; accessibility/mobile history third. A new frontend framework should only be considered if measured Streamlit navigation, performance, or accessibility limitations block these goals.

## UX implementation update

The current app uses persistent Overview, Transactions, Plan, Accounts, Compare, and
Settings navigation. Only the selected page and its selected subsection render. Empty
Overview and Transactions pages offer direct bank and statement actions. Import now
guides account selection, file upload, preview, and save; it explains skipped rows and
duplicate matching, shows replacement impact and a backup download, and retains a save
receipt with a category-review action. Restore shows before/after collection counts.
Active filters and visible/total transaction counts appear in the main view with a reset
button. Mobile cards are paged so the full filtered history remains reachable. The auth
form is width constrained and recovery codes require password confirmation. The primary
button text and keyboard focus are more visible.

Automated UI smoke tests cover first-run routing, page persistence, budget independence,
and mobile paging. A deployed keyboard, screen-reader, and mobile-device review remains
necessary to confirm these improvements with real browser behavior and Plaid Link.
