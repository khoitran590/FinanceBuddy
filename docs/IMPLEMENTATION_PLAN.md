# FinanceBuddy implementation plan for the 12 handoff items (UX-1 to UX-6, FEAT-1 to FEAT-6)

> Companion to [`FEATURE_AND_UX_HANDOFF.md`](FEATURE_AND_UX_HANDOFF.md). Generated 2026-10-29 by a Claude Code Plan subagent after checking the handoff doc against the code. Where the two disagree, this plan reflects the code. Line numbers are from commit `a2cd007`.

## Status (2026-10-29)

All 12 items and the Phase 0 groundwork are implemented locally and not yet deployed. `pytest` passes, and so do all `scripts/test_database.mjs` checks, including the two new verify scripts. The open questions were decided as follows:

1. FEAT-1 stores sample data in reserved "· demo" statement accounts (option A).
2. FEAT-2 makes triaged rows rule-based, using the `triage` operation.
3. UX-5 presets stay anchored to the latest transaction and are only relabeled.

The other questions use the defaults recommended below:

- Bulk triage follows the dashboard filters.
- Saved-history Compare covers all accounts.
- Budget suggestions match how budgets count spending: gross outflows, rounded up to whole dollars.
- A budget's category can't be renamed; only its limit can change.
- Manual entries default to a "Cash" account.
- The bills timeline is labeled, not hidden, when the data is stale.

## 0. Summary

The handoff doc's direction is right. Several of its "no schema, low effort" claims fall apart once you read the SQL that sits under `DataService` and the repositories. The five findings that should change the plan most:

1. **`correct_category(..., apply_similar=True)` is not safe for bulk triage as written.**
   - It updates *every* transaction whose description contains the keyword.
   - It overwrites manual overrides: it sets `category_override=null, category_origin='rule'`.
   - It sets every allocation of every matched split to the same category, which wipes out the split (`supabase/migrations/202610130004_recoverable_operations_and_backups.sql:199-203`; SQLite equivalent at `src/repositories/transaction_repo.py:836-838`).
   - This breaks guardrail #3 (manual beats rule).
2. **`clean_merchant()` output is often not a substring of the description**, so rules built from it silently match nothing. I verified this by running it:
   - `"AMZN Mktp US*2K3L91"` becomes `"Amazon"`, which is not a substring.
   - `"TST* JOE'S PIZZA 0423 NEW YORK NY"` becomes `"JOE'S PIZZA NEW YORK NY"`, which is not a substring.
   - Rules match with `strpos(lower(description), keyword)` (`0004:200`, `0003:458+`), so the keyword must be a literal substring.
3. **Undoing an import is not a reliable way to clear sample data.**
   - `undo_batch` refuses if any row's `revision` changed or the row has allocations (`0004:261-263`).
   - Every UPDATE bumps `revision` (`0004:20`, trigger at `0003:101`). So one category edit or split in demo mode makes "Clear sample data" fail.
4. **`duplicate_candidates` flags equal split halves as duplicates.** I reproduced this: a −$100 split into −$50/−$50 projects as two `allocation:` rows with the same account, date, amount and description (`src/services/analytics.py:1370-1380`). It also keys on `account_name`, not `account_id`.
5. **Deleting a duplicate brings it back.**
   - `statement_import_rows` has `on delete cascade` (`202610130002_statement_import_identity.sql:45`). Re-importing the same file then re-inserts the deleted row.
   - Plaid rows come back on the next "modified" upsert or reconcile (`0003:373`, `0003:389`).
   - No single-transaction delete operation exists in `fb_execute_operation`.

---

## 1. Where the doc doesn't match the code

| Doc claim | Reality | Impact |
|---|---|---|
| UX-1: the editor "only edits category and merchant rule" | It also has a **Transaction selectbox listing every shown row** (`app.py:1402-1410`, key `category_transaction`). Tests depend on it (`tests/test_ui_smoke.py:53,410-433`). | UX-1 should pin the dialog to one transaction. That removes the same O(N) picker the doc complains about for split. Tests need rewriting. |
| UX-2 / FEAT-2: use `merchant_name()` as the rule keyword | `merchant_name()` returns the provider `merchant_name` or `clean_merchant(description)` (`analytics.py:79-80`). Aliases and digit-token stripping produce non-substrings (verified above). For Plaid rows, `description` is already `merchant_name or name` (`plaid_service.py:335`). | You need a new "keyword guaranteed to be a substring" helper. Don't pre-fill raw `merchant_name()`. |
| FEAT-2: `correct_category(apply_similar=True)` is "already implemented and atomic" | It is atomic, but it matches across all history, overrides manual categories, and collapses allocations (`0004:199-203`). | Implement triage with `bulk_category` on explicit IDs plus a `rule` operation (both already exist), or a new targeted operation. |
| UX-3: users are asked upfront for sign/date/number format | The format options only render **after** upload (`app.py:2097-2098`). Date/number/reference/year/total are **already** in an expander (`app.py:402`). Checking files **silently default to `outflow_negative`** (`app.py:395`, `parser.py:222-225`). Credit-card files must choose. | The remaining work is detection, a summary and a flip button. Note the existing test `test_credit_card_amount_requires_a_convention_and_never_uses_keywords` (`tests/test_statement_imports.py:171`). |
| UX-5: `render_filter_bar` is at `app.py:2448` | The function is at `app.py:2378`; 2448 is the `st.expander("Filters")` line. The default preset is **"Last 3 months"** (`app.py:2387`), not "This month". Every preset is anchored to `max_date`, not just "This month" (`analytics.py:241-270`). | The label fix should cover all relative presets. |
| FEAT-1: an import batch plus `undo_import` cleanly removes demo data with no schema change | Undo breaks after any edit or split. Budgets and goals aren't in the batch. The `accounts` row survives (the catalog lists empty accounts, `0005:27-32`). `fb_import_batches` only shows the last 20 appends, and users can undo the demo batch from "Recent import receipts" (`app.py:2214-2228`). | Use a dedicated demo account cleared by account ID, or a fully isolated sandbox (see FEAT-1). |
| FEAT-3: "no significant cons" | `STATEMENT_COLORS` has 3 colors (`charts.py:19`), so 6 periods reuse colors. The delta chart only compares the last two periods and says "Upload at least two statements" (`charts.py:342-343`). Partial current months skew comparisons. | Small, but real. |
| FEAT-5: filter dismissed merchants in `recurring_expenses` or its caller | `_recurring_groups` (`analytics.py:802`) also feeds `cash_flow_forecast` (`analytics.py:941`) and `insights` (`analytics.py:1283`). `income_summary` returns only a cadence label, not `interval_days`, so you can't project paydays. | The exclusion parameter has to go into `_recurring_groups`. Income projection needs an extension. |
| UX-4: schema "Optional (settings)" | No delete-one-transaction operation exists. `repo.delete_many` is a raw table delete with no receipt and is never used by the app. A cascade-safe merge needs SQL. | It needs a migration. |
| FEAT-6: update `read_backup` version validation | Also `validate_backup` (`src/services/backups.py`), the `fb_restore_backup` version gate (`0004:102`) and the explicit column lists in `fb_restore_backup_portable`. `to_jsonb(t)` export would include new columns automatically while restore silently drops them. | Bigger than stated. |
| Guardrail 4: `clear_private_session` must clean up any new sensitive keys | It already deletes **every** key (`src/services/security.py:10-13`). | The real risk is state kept *outside* `session_state`: temp files, module globals, `st.cache_*`. |
| UI_AUDIT #3: empty quick views show all transactions | Already fixed (`app.py:1458-1470`, `1502-1511`). | Nothing to do. |
| Checklist: run `pytest -q` | SQL changes also need `node scripts/test_database.mjs …` plus a `scripts/verify_*.sql` (per `DATA_MIGRATION_ROLLOUT.md`). | Add this to every PR that touches SQL. |

---

## 2. Sequencing and phases

### Phase 0: shared foundations (one PR, about 1–2 days; blocks the rest)

| Foundation | Why it goes first | Used by |
|---|---|---|
| **F1. Substring-safe merchant keyword helper**, e.g. `rule_keyword_for(tx) -> str \| None` and `keyword_breadth(keyword, matches, total)` in a new `src/services/merchants.py` (or `analytics.py` next to `clean_merchant`). It returns the longest run of `clean_merchant` tokens that is a literal case-insensitive substring of `tx.description`, else `None`. It also holds a payment-processor denylist (`paypal`, `square`, `sq`, `venmo`, `zelle`, `ach`, `pos`, `debit`, …). | UX-2, FEAT-2 and FEAT-5 all need "a phrase the rule engine will actually match". | UX-2, FEAT-2, FEAT-5 |
| **F2. Single active-dialog state.** Replace the per-feature booleans (`transaction_editor_open`, `transaction_editor_requested`, `app.py:1395-1396,1521-1530`) with `st.session_state.active_dialog = {"name": ..., "id": ...}` plus `open_dialog()` / `close_dialog()` helpers in `app.py` or a new `src/ui/dialogs.py`. Close it on navigation (`navigate_to`, `app.py:580`, and the nav radio). | `@st.dialog` allows one open dialog per run. Several booleans can both be True and leak across pages. | UX-1, UX-6, FEAT-4, FEAT-6, maybe UX-4 |
| **F3. Bounded JSON settings helpers** in `DataService`: `get_json_setting(key, default, validate)` / `set_json_setting(key, value)`, generalizing `chart_preferences()` (`app.py:526-541`). Enforce the 16,384-character `settings_backup_bounds` limit (`0004:341`). | Dismissals, exclusions, manual balance and the demo marker all live in `app_settings` and must survive backup/restore v3. | UX-4, FEAT-1, FEAT-5 |
| **F4. Extensible operation dispatch (SQL).** A migration that does `create or replace function public.fb_execute_operation` with `else v_result := financebuddy_private.fb_execute_extension(v_uid, p_kind, p_payload);` instead of `raise exception 'Unsupported operation'` (`0004:317`). Ship a stub extension that raises. Mirror it in SQLite. | Without it, each later migration re-copies the ~80-line dispatcher, which invites drift. Receipts and fingerprinting stay in the one place they're written (`0004:319`). | UX-4, FEAT-6, optional FEAT-2 v2, FEAT-1 option A |
| **F5. Deterministic sample-data generator** `src/services/sample_data.py`: build 6 months relative to a supplied `today`, producing CSV bytes for checking and card plus budgets/goals. | A realistic fixture for every later test, and the core of FEAT-1. | Tests, FEAT-1 |

F1, F2, F3 and F5 are pure Python and can be written in parallel. F4 is SQL plus SQLite.

### Phase 1: UI-only features, in parallel lanes (use worktrees; all touch `app.py` but different functions)

| Lane | Items | Functions touched | Notes |
|---|---|---|---|
| A: Transactions | **UX-1 + UX-2** together, then **FEAT-2** | `render_transaction_editor`, `render_transactions` | Same functions, so keep it sequential in one lane. |
| B: Filter bar | **UX-5** | `render_filter_bar`, `DATE_PRESETS`, `reset_dashboard_filters` | Separate code region. |
| C: Compare | **FEAT-3** | `render_compare`, `charts.py` comparison functions | Isolated. |
| D: Plan | **UX-6**, then **FEAT-4** | `render_budgets`, `render_goals` | Do UX-6 first: FEAT-4's "Use 3-month average" goes inside UX-6's create-budget modal, and "+ Set budget" opens it. |

### Phase 2: behavior changes to import and analytics

- **UX-3** (`parser.py`, import flow).
- **FEAT-5** (`_recurring_groups` signature change; touches `render_overview_recurring` / `render_overview_balances` and the Summary forecast).

These two can run in parallel.

### Phase 3: data mutation and schema (one at a time, each with its own migration and verify script)

1. **UX-4**: merge/dismiss operation via F4.
2. **FEAT-1**: demo mode. It goes after the features it showcases, and its clear path relies on F4 (option A) or the sandbox (option B).
3. **FEAT-6**: annotations table, manual entry, backup v4. Last, because it changes the backup contract and the projection view.

What depends on what:
- F1 → UX-2, FEAT-2, FEAT-5
- F2 → UX-1, UX-6, FEAT-4
- UX-6 → FEAT-4
- F4 → UX-4, FEAT-6
- F5 → FEAT-1
- Everything else is independent.

---

## 3. Per-item plans

### UX-1: One transaction dialog for editing and splitting (effort S–M)

**Steps**
1. In `render_transaction_editor` (`app.py:1399-1451`):
   - Remove the "Transaction" selectbox. Take the transaction ID from F2's `active_dialog["id"]`, renaming `category_transaction` to a non-widget key such as `editor_transaction_id`.
   - Look the row up in `transactions_by_id`. If it's missing (a filter change or provider removal), show "This transaction is no longer in view" and close.
2. Add `st.segmented_control` / `view_switcher` inside the dialog: `Category & rule` | `Split`.
   - Show Split only if `amount < 0 or (amount > 0 and allocations)`, mirroring `app.py:1539-1540`, and `abs(source_amount) >= 0.02`.
3. Split target: `source_id = selected.source_transaction_id or selected.id` and `source_amount = selected.source_amount if not None else selected.amount`.
   - When `selected.id.startswith("allocation:")` or `selected.split_needs_review`, show a callout: "This purchase is already split. Saving replaces both parts of the $X purchase."
4. Move the split form body from `app.py:1557-1581`. Give every widget a **transaction-scoped key**, e.g. `f"split_amount_{source_id}"`.
5. Compute `amount_two = abs(source_amount) - money(amount_one)` in Decimal, as at `app.py:1567`. Call `repo.split_transaction(source_id, ...)`.
6. Delete the page expander at `app.py:1532-1583`.
7. Widen the dialog to `width="medium"`.

**Tests**
- Rewrite `tests/test_ui_smoke.py::test_transaction_ui_edits_allocations_and_resplits_the_source`. It currently asserts `selectbox(key="split_transaction")` and clicks "Split transaction" on the page; it should drive the dialog's Split tab.
- Update the `category_transaction` assertions at `test_ui_smoke.py:410-433`.
- Add a case: open the dialog on an `allocation:` row, re-split, and assert `len(repo.get_canonical_all()) == 1` and the sum equals the source.
- Keep the "failed save keeps choices" assertion (`test_ui_smoke.py:417-425`).

**Pay extra attention**
- **`number_input` max_value with a static key.** Today the split amount input has no key, so its identity includes `max_value`/`value` and resets per transaction. If you add a key that isn't scoped to the transaction, a value left over from a larger transaction exceeds the new `max_value` and Streamlit raises `StreamlitAPIException`. Scope keys per `source_id`.
- **Row-index drift reopening the dialog.**
  - Table selection is a row *index* (`components.py:423-427`) mapped through `shown_ids[picked_row]` (`app.py:1513-1521`).
  - After a split, one source row becomes two `allocation:` rows, so the remembered index can point at a different ID. `last_table_pick != picked_id` then auto-reopens the editor on a neighboring row.
  - Fix: clear the table selection after a successful save by bumping the `selection_key` suffix, or by storing `last_table_pick` as an ID-plus-data revision. Test for this explicitly.
- **Float to Decimal at the boundary.** `amount_one` is a float from `number_input`; `money()` tolerates only 1e-9 float noise (`money.py:18-20`). Never compute `amount_two` in float.
- **Rule creation on an allocation row.** With `apply_similar`, every allocation of matching transactions gets the same category (`0004:202`). This overlaps with UX-2; warn when the keyword matches split transactions.

### UX-2: Pre-fill the cleaned merchant name in category rules (effort S, after F1)

**Steps**
1. In the `Category & rule` tab, pre-fill the phrase with `rule_keyword_for(selected)` from F1. If it returns `None`, leave the field empty with the help text "We couldn't find a reliable merchant phrase".
2. The phrase input sits inside `st.form("category_correction")` (`app.py:1423`), so a live match count can't update per keystroke. Either:
   - move the keyword input and a "Check matches" button outside the form, or
   - compute the preview only for the pre-filled phrase (computed once per dialog open and stored in `session_state[f"rule_preview_{id}"]`).
3. Match preview: `repo.get_page({"search": kw}, limit=1)["count"]` gives an approximate count.
   - `fb_filtered_transactions` searches `description || merchant_name` (`0005:47`), while rules match `description` only (`0004:200`). Label the number "about N" or add a dedicated count function.
   - Divide by `catalog["count"]`. Warn above 25%, or when the phrase is on the processor denylist.
4. Show how many matching rows are already manually categorized or split, since `apply_similar` will override them (see FEAT-2).

**Tests**
- New `tests/test_merchants.py`:
  - `rule_keyword_for` always returns a substring (parametrize with the verified examples).
  - It returns `None` for pure processor strings.
  - Breadth warning thresholds.
- UI smoke: the pre-filled value appears in the text input.

**Pay extra attention**
- Don't pre-fill and pre-check "Also update saved transactions". Keep `apply_similar` opt-in (`app.py:1430`).
- The keyword is stored lowercased and trimmed (`0004:197`). Previews must use the same normalization.
- Rule precedence is alphabetical by keyword, first match wins (`categorization.py:14`, `0003:458+ order by keyword limit 1`), not longest match. A short broad keyword like `"coffee"` beats `"coffee bean co"`. Surface this when overlapping rules exist.

### UX-5: Visible filter chips and honest date preset labels (effort S–M)

**Steps**
1. In `render_filter_bar` (`app.py:2378`), create `chips_slot = st.container(key="active_filter_chips")` **before** the expander at `app.py:2448`. After the widgets inside the expander run, fill `chips_slot` with the chips row and the Reset button whenever `active_count > 0` (or the transfers toggle is on).
   - The chip values come from widgets inside the expander, so a placeholder filled later is the simplest approach.
   - Remove the in-expander chips (`app.py:2545-2552`) to avoid rendering them twice. Give Reset a single key.
2. Preset labels: `format_func=preset_label(value, max_date, today)`. When `(max_date.year, max_date.month) != (today.year, today.month)`, relabel:
   - "This month" → "Latest month (Sep 2026)"
   - "Last 30 days" → "30 days to Sep 20, 2026"
   - and similarly for the others.
   - Update the help text at `app.py:2417,2426`.
3. Because labels now change, apply `keep_choice("dashboard_date_preset", presets, format_func)` before the selectbox. The browser can send back a stale label (`app.py:498-512`).
4. Fix the Reset `disabled` condition at `app.py:2537`. It compares against "All time", but the default is "Last 3 months", so Reset is almost never disabled.

**Tests**
- `test_ui_smoke.py::test_overview_tabs_balances_presets_and_budget_pace`: it asserts the `Filters` expander label (`:328`) and preset values (`:324,365,399`).
- Add assertions:
  - The chip container renders outside the expander when search is set.
  - Labels show "Latest month (…)" when fixture dates are old.
- `tests/test_spending_insights.py:74` tests `preset_range("This month")`. Leave `preset_range` semantics unchanged unless product decides to anchor to today (open question 3).

**Pay extra attention**
- The date chip shows whenever the range isn't min..max (`app.py:2519`), which is almost always under the 3-month default. Decide whether it counts as an "active filter" for the outside row.
- On mobile the extra row adds height. Add `.st-key-active_filter_chips` to the `@container (max-width:700px)` rules (`components.py:180-208`) and keep 44px button targets.
- Chip text must stay `html.escape`d (`app.py:2549`): the merchant search is user input.

### FEAT-2: Bulk "uncategorized merchant" triage queue (effort M, after F1 and UX-2)

**Steps**
1. Add a pure analytics function:
   - `AnalyticsService.uncategorized_merchant_groups(transactions, limit=10) -> [{merchant, keyword, ids, count, total, sample_descriptions, unmatched_ids}]`
   - Group by `merchant_name(tx).lower()` (same key as `analytics.py:806`).
   - Compute `keyword` = longest common substring-safe phrase across group descriptions (via F1).
   - `ids` = transactions in the group that actually contain `keyword`. Members without it go to `unmatched_ids`, shown as "2 more won't match this phrase".
2. Data source: fetch all uncategorized rows, not just the current 200-row page:
   - `repo.get_filtered({**dashboard_filters, "view": "Needs a category"})`, which the server filters (`0005:48`).
   - Compute it only when `tx_view == "Needs a category"`.
3. UI at the top of that view: cards showing "Blue Bottle Coffee · 6 · $41.50". Each has:
   - a category select,
   - an editable phrase (pre-filled, with a breadth warning from F1),
   - a "Save rule for future imports" checkbox,
   - an **Apply** button.
4. Add `DataService.triage_merchant(ids, category, keyword, save_rule)`:
   - **v1 (no migration):** `repo.correct_categories(ids, category)`, using the existing `bulk_category` operation (`0004:288-295`), which targets only the listed IDs and handles `allocation:` IDs. Then, if `save_rule`, `repo.upsert_category_rule(CategoryRule(keyword, category))` (the `rule` operation, `0004:311-313`).
   - Each step has its own operation receipt. If the rule step fails after categorization, show "Categories saved; rule not saved — Retry". The retry reuses the pending operation ID via `OperationService` (`operations.py:15`).
   - **v2 (optional, after F4):** one atomic `triage` extension operation that inserts the rule and updates only the listed IDs with `category_origin='rule'`.
5. **Do not** use `correct_category(..., apply_similar=True)` here (see section 0, finding 1).

**Tests**
- `tests/test_analytics_features.py`: grouping, keyword-is-substring invariant, `unmatched_ids`, Decimal totals.
- `tests/test_repository_features.py`: `triage_merchant` leaves manually categorized and non-listed rows untouched, and handles `allocation:` IDs.
- `tests/test_ui_smoke.py`: apply a group and check `needs_review` decreases. Also fail the rule step and check the retry message.

**Pay extra attention**
- `bulk_category` raises `'Some transactions changed'` if any ID disappeared (`0004:294`), for example after a concurrent sync. Catch it, re-fetch, and re-render rather than showing a scary error.
- Rows from v1 get `category_origin='manual'`. A later rule edit won't move them. That's a product choice (open question 6).
- `needs_review` counts projected allocation rows (`0005:25`). Group counts should use the same projection so the badge and the queue agree.
- Large histories: thousands of uncategorized rows means several 1,000-row pages. Compute once per rerun, only in this view, with no cross-session cache.

### FEAT-3: Compare saved months or quarters (effort M)

**Steps**
1. `render_compare` (`app.py:1797`): add `view_switcher("Compare source", ["Saved history", "Uploaded files"], "compare_source")`. Move the existing body into `render_compare_uploads()`.
2. Saved history:
   - Choose a granularity (Month / Quarter / Custom) and 2–6 periods. Options come from `catalog["min_date"]..catalog["max_date"]`, without loading rows.
   - Run **one** `repo.get_filtered({"start": min_selected, "end": max_selected, "accounts": …})` and bucket by period in Python. That is better than N RPCs.
   - Build `[{label, transactions, start}]` → `AnalyticsService.compare_periods` (`analytics.py:1328`).
3. Mark partial periods. Use `complete_months` (`analytics.py:375`) or compare the period bounds to `max_date`, and add "(partial)" to the label.
4. Charts (`src/ui/charts.py`):
   - Extend `STATEMENT_COLORS` to 6 accessible colors, or use a sequential ramp.
   - Change the delta-chart empty-state copy, which says "Upload…" (`charts.py:343`).
   - Re-check label contrast (UI_AUDIT #4: teal `#2E9C95` and purple `#9B7BD8` fail 4.5:1 with white text).
5. Decide whether dashboard account filters apply (open question 8).

**Tests**
- `tests/test_analytics_features.py`: bucketing helper (put it in `AnalyticsService` as `periods_from_history(transactions, granularity, keys)`).
- `tests/test_charts.py`: 6 distinct colors.
- `tests/test_ui_smoke.py`: Saved-history mode with the F5 fixture, with no file uploader.

**Pay extra attention**
- `compare_periods` excludes transfers by default (`analytics.py:1336`). Keep the caption at `app.py:1854`.
- Credit-card-only periods switch status wording (`analytics.py:1359-1361`).
- Decimal values in DataFrames become object dtype. Convert to float in the chart and table code only.

### UX-6: Inline card actions and dialogs on the Plan page (effort M, after F2)

**Steps**
1. `render_budgets` (`app.py:1595`):
   - Header row with a primary "New budget" button that opens `active_dialog={"name":"budget","category":None}`.
   - Per budget, render the `render_progress` row plus a compact `Edit` / `Remove` pair in a keyed container `st.container(key=f"budget_card_{slug}")`.
   - Edit reuses the dialog with the category locked.
   - Remove uses a confirm step inside a dialog, replacing the checkbox expander at `app.py:1678-1688`.
2. `render_goals` (`app.py:1691`):
   - "New goal" dialog.
   - Per-card `+ Add funds` (dialog with an amount, computing `money(goal.current_amount + money(delta))`), `Edit`, and `Remove`.
   - Replace the dropdown expander at `app.py:1760-1785`.
3. Wrap every write in `try/except (ValueError, SupabaseError)` with the same "retry recovers the saved result" copy. Today `upsert_budget`, `delete_budget`, `save_goal` and `delete_goal` are unguarded (`app.py:1674,1687,1749,1776,1784`), so a network error shows a raw exception.

**Tests**
- Update `test_ui_smoke.py:439-444` (Plan assertions).
- Add:
  - create a budget via the dialog,
  - add funds updates `current_amount` exactly in Decimal,
  - remove asks for confirmation,
  - a simulated `SupabaseError` shows the friendly error and keeps the dialog open.

**Pay extra attention**
- Budgets are keyed by `category` (upsert at `0004:298-300`). Changing a budget's category is delete plus upsert, two operations and not atomic. Either disallow renaming or do upsert-new then delete-old.
- **Add funds race.** `save_goal` writes an absolute `current_amount` (`0004:306`). Two tabs adding $50 each lose one update. Acceptable for v1; a `goal_add` extension operation (F4) with `current_amount = current_amount + delta` fixes it.
- Per-card widget keys must use stable IDs: goal `id`, and a slug of the budget category.
- Dialog function arguments are frozen for fragment reruns. Re-read the goal by ID inside the dialog.
- Mobile: inline buttons go in columns. Add the card keys to the `@container (max-width:700px)` rules so they stack, and keep 44px targets (`components.py:174-179`).

### FEAT-4: Budget suggestions and an "Unbudgeted spending" card (effort M, after UX-6)

**Steps**
1. Analytics in `analytics.py`:
   - `category_spend_history(transactions, category) -> {last_month, avg_3m, months_used}` using `complete_months` (`:375`), Decimal throughout.
   - `unbudgeted_spending(budgets, transactions, month) -> [{category, spent}]` that excludes `TRANSFER_CATEGORIES`. Plan rows come from `get_filtered` without `include_transfers`, which defaults to **true** (`0005:49`), so transfers are present.
   - Decide whether "Uncategorized" is listed separately.
2. Budget dialog: show "Last month $X · 3-month average $Y" and a "Use 3-month average" button. It sets the dialog's `number_input` key **before** the widget renders (use an `on_click` callback).
   - Round up to whole dollars? Product question.
3. Below the budget list: "Unbudgeted spending $Z in N categories" with the top 3 and "+ Set budget" buttons that open the F2 dialog pre-filled.

**Tests**
- `tests/test_analytics_features.py`: averages ignore partial months, transfers excluded, empty history returns `None`.
- `tests/test_ui_smoke.py`: the unbudgeted card appears and "+ Set budget" opens a pre-filled dialog.

**Pay extra attention**
- Plan data is anchored to `catalog["max_date"]`, not today (`app.py:2675-2676`). "Last month" for stale data means the last month *in history*, so label it.
- `category_expenses` ignores refunds (positive amounts) (`analytics.py:398`). The averages will slightly overstate net spend. Document or net them.
- Setting a widget's session_state after it renders raises an exception. Use callbacks.

### UX-3: Sign-convention detection for statement imports (effort M)

**Steps**
1. Add a pure function `src/services/sign_detection.py::suggest_sign_convention(transactions) -> {suggested, confidence, evidence}`. It runs on the transactions **already parsed** with the provisional convention, so it works for CSV and PDF without touching `pdf_worker.py`. Signals:
   - share of positive vs negative rows,
   - income keywords (`CATEGORY_RULES["Salary/Income"]`) on positive vs negative rows,
   - card-payment keywords (`"payment thank you"`) for credit cards.
2. Never feed the suggestion into the parser automatically.
   - If the columns are Debit/Credit (`parser.py:221`), skip detection and show "Direction from Debit/Credit columns".
   - Otherwise show the summary "Detected 38 money-out ($1,420.50) and 2 money-in ($3,100.00)".
   - Add a prominent **Flip +/- signs** button that toggles `st.session_state[f"{key}_sign_{account_type}"]` via `on_click`, which re-parses.
   - If confidence is low, or the type is Credit Card, keep "Save statement" disabled until the user picks or confirms (extend the condition at `app.py:2177`).
3. Remove the silent Checking default: `index=1` at `app.py:395`. Pre-select only when confidence is high, and always show the choice.

**Tests**
- New `tests/test_sign_detection.py`: payroll positive → `outflow_negative`; payroll negative → `outflow_positive`; mixed → low confidence; Debit/Credit → not applicable.
- Keep `test_statement_imports.py:163-179` passing unchanged; the parser stays keyword-free.
- UI smoke: Flip changes the preview signs and the Save button's enabled state.

**Pay extra attention**
- **The sign convention is part of the file identity.** It's hashed into `_file_id` (`parser.py:95-99`). An import with wrong signs followed by a re-import with flipped signs gives new IDs *and* new `import_match_key` values (the amount is in the key), so nothing matches and every row is duplicated with the opposite sign. The fix for a wrong import is **Undo**, not a re-import. Say so in the UI next to the receipt.
- Credit-card single-column files must stay hard-required. That is the existing invariant from audit Finding #4.
- The parse cache (`app.py:381-389`) keys on options, so a flip re-parses correctly, but the 6-entry cache gets cleared often with flipping. That's fine.

### FEAT-5: 30-day bills timeline and recurring overrides (effort M–L, after F1/F3)

**Steps**
1. Analytics (`analytics.py`):
   - Add `excluded: frozenset[str] = frozenset()` to `_recurring_groups` (`:802`). Skip groups whose key (`merchant_name(item).lower()`) is excluded.
   - Pass it through `recurring_expenses` (`:861`), `cash_flow_forecast` (`:924`) and `insights` (`:1271`).
   - Extend `income_summary` (`:971`) to return `interval_days` (from `detect_cadence`).
   - Add `upcoming_paydays(income, start, end)` reusing `_advance`.
   - Add `cash_flow_timeline(recurring, income, start_balance: Decimal | None, today, days=30) -> [{date, label, amount, running}]`.
2. Settings via F3:
   - `recurring_exclusions` (list of merchant keys, capped at about 200).
   - `manual_cash_balance` stored as `{"amount": "1234.56", "as_of": "2026-10-29"}`, a Decimal string.
3. UI:
   - `render_overview_recurring` (`app.py:1179`): a timeline chart (`charts.py::make_cash_flow_timeline`; use the `dataviz` skill) plus a table alternative.
   - "Not a recurring bill" per row, which writes the exclusion and reruns.
   - An "Excluded merchants (N)" expander to restore them.
   - Manual balance input with an as-of date. Compute Safe to spend from the manual balance when `load_balances()` is empty (`app.py:661-673`, `1388-1392`).
4. Update all call sites to pass exclusions: `app.py:891` (insights), `:981` (forecast), `:1184`, `:1266`.

**Tests**
- `tests/test_spending_insights.py` / `test_analytics_features.py`:
  - an exclusion removes the merchant from recurring, forecast and the subscription insight;
  - the timeline running total is exact Decimal;
  - paydays project correctly for "Twice a month".
- UI smoke: excluding a merchant persists and survives rerun.

**Pay extra attention**
- **Stale data projects false bills.** `active` is computed relative to the latest transaction, not today (`analytics.py:809,853`), and `upcoming_charges` walks stale `next_date` forward (`:880-881`). With a statement that's 6 months old, every recurring charge looks active and gets projected into the coming month. Gate the timeline on freshness (latest transaction within about 35 days of today) or show a strong "based on history ending Sep 2026" label.
- Merchant keys drift: Plaid `merchant_name` can change between syncs, so exclusions may stop applying. Show unmatched exclusions.
- A manual balance goes stale. Show its as-of date and warn after 7 days.
- Restore v3 replaces settings (`0004:124-125`), so exclusions are restored with the backup. Good, but note it.

### UX-4: Act on possible duplicates (effort M–L, after F4)

**Steps**
1. Fix detection first (`analytics.py:1370`):
   - Key on `account_id or account_name`.
   - **Exclude groups whose members share `source_transaction_id`** (split halves).
   - Exclude `allocation:` rows entirely from removal actions.
   - Filter out dismissed pairs.
2. Dismiss ("Keep both"): store sorted ID pairs in setting `duplicate_dismissals` (F3). Prune IDs that no longer exist. Cap the size, because of the 16 KB setting limit (`0004:341`).
3. Merge ("Remove duplicate"): only for `statement:` / `manual:` rows that aren't split. Add a new extension operation `merge_duplicate {remove_id, keep_id}` (migration `2026110x_duplicate_merge.sql` via F4). Inside one transaction:
   - verify the two rows are in the same account with the same date, amount and description, and neither is `plaid:`;
   - `update statement_import_rows set transaction_id=keep_id where transaction_id=remove_id`, so a re-import maps to the kept row instead of re-inserting;
   - delete `remove_id`.
   - Mirror it in `transaction_repo.py`.
4. Plaid duplicates: offer only Dismiss, with copy explaining pending-to-posted reconciliation.
5. UI: replace the read-only expander (`app.py:1585-1592`) with pair cards. Each shows date, description, amount, source (Statement/Bank/Manual) and import date, plus the two actions. Button labels should include the description for screen readers.

**Tests**
- `tests/test_analytics_features.py`: split halves are not duplicates; different `account_id`s with the same label are not duplicates.
- `tests/test_statement_imports.py`: merge, then re-import the same file, and check the row is **not** re-created.
- `tests/test_atomic_cloud.py`: operation payload shape.
- New `scripts/verify_duplicate_merge.sql`, added to `scripts/test_database.mjs:55`: RLS with two owners, refusing `plaid:` rows, mapping re-pointed.

**Pay extra attention**
- **Resurrection**, shown above, via cascade (`202610130002:45`) and the Plaid upsert (`0003:373`).
- Merging invalidates import undo. The deleted ID fails `t.id is null` (`0004:261-262`), and re-pointing the mapping bumps the kept row's revision (`0004:328-337`). Tell the user "Import undo is no longer available for that statement".
- Any UPDATE re-runs `fb_category_policy` (`0003:458-475`) on rule/provider/builtin rows, so a re-pointed row may change category.
- Detection works on one 200-row page (`app.py:1500`), so pairs split across a page boundary are missed. Keep the caption.
- Row-index drift after a delete (see UX-1).

### FEAT-1: Sample data mode (effort L, after F5; F4 for option A)

There are two viable designs. It needs a product decision (open question 1).

**Option A: write to the user's database in a dedicated demo account (recommended if demo should "feel real")**

Steps:
1. "Explore with sample data" in `render_start_actions` (`app.py:590`). It calls `DataService.load_sample_data(today)`, which:
   - builds CSVs via F5;
   - runs them through `BankStatementParser.parse` with account names that can't collide, e.g. `"Sample Checking · demo"` / `"Sample Card · demo"`;
   - runs `import_statement` (valid `statement:` IDs and file IDs, as required by `0003:181-189`);
   - records `{"account_ids": [...], "budget_categories": [...], "goal_ids": [...], "rules_before": [...]}` in setting `sample_data` (F3).
2. Clear: a `clear_sample` extension operation (F4). Atomically:
   - delete transactions where `account_id in (demo ids)`, regardless of edits or splits;
   - delete those `accounts` rows (`fb_delete_owned_account` deliberately keeps the account, `0004:168-179`, and the catalog lists empty accounts, `0005:27-32`);
   - delete seeded budgets and goals still matching their seeded values;
   - delete category rules created while demo was active (rules not in `rules_before`); ask first;
   - delete the setting.
3. A persistent banner (in `app.py` after the sidebar) whenever the `sample_data` setting exists, with "Clear sample data & start fresh". Also hide the demo batch from "Recent import receipts" (`app.py:2214-2228`).

Pay extra attention:
- **Plaid adoption.** `fb_bank_account` adopts an existing statement account with the same normalized name and type (`0003:304`). Generic demo names like "Checking" could get merged into a real bank. The "· demo" suffix prevents this.
- **Rules leak into real data.** A rule like `starbucks → Groceries` made while exploring will categorize future real imports (`categorization.py:14`, `0003:458+`).
- Backups taken in demo mode include sample rows. v3 restores settings too, so the marker comes back consistently.
- If real data gets added while demo is active, analytics mix the two. The banner must stay, and Clear must only touch the demo account IDs.

**Option B: isolated sandbox (recommended if "never touches real data" is the priority)**

Steps:
- In demo mode, swap `repo`/`data_service` (`app.py:366-370`) for a `TransactionRepository` (the SQLite implementation that already backs the UI tests, `tests/test_ui_smoke.py:34-43`). Point it at a per-session temp file created with `tempfile.mkstemp` in the OS temp dir.
- Seed it from F5. Disable Plaid and backup restore in demo mode.

Pay extra attention:
- `clear_private_session` doesn't delete files. Add explicit file cleanup on logout, expiry and owner change (`security.py:16-22`, `app.py:333-336`).
- On Streamlit Cloud the temp disk is ephemeral and shared across sessions. Use random filenames with 0600 permissions.
- SQLite/Supabase behavior drift: for example, SQLite `correct_category` never bumps rule versions (`transaction_repo.py:835`).

**Tests (either option)**
- `tests/test_sample_data.py`: deterministic, relative to `today`, Decimal-exact, recurring and income are detected.
- `tests/test_ui_smoke.py`:
  - the button loads data and shows the banner;
  - after editing a category *and* splitting a sample row, Clear removes all of it and leaves a real imported transaction untouched.
- Option A: `scripts/verify_sample_clear.sql` with two owners.

### FEAT-6: Manual transactions and tags/notes (effort L, last)

**Steps**
1. **Store annotations in a separate table, not new columns on `transactions`**:
   ```sql
   transaction_annotations(user_id, transaction_id, notes text check(length<=2000),
     tags text[] check(cardinality<=20), primary key(user_id,transaction_id),
     foreign key(user_id,transaction_id) references transactions on delete cascade)
   ```
   with RLS `owner_all`, the grants pattern from `0004:31-34`, and a GIN index on `tags`. Reasons:
   - an UPDATE on `transactions` bumps `revision` (`0004:20`), which breaks import undo;
   - it re-runs `fb_category_policy` (`0003:458-475`), which can silently re-categorize a builtin/provider/rule row when the user only edited a note.
2. Migration `2026110x_annotations_and_manual.sql`:
   - Add `notes`/`tags` to `fb_transaction_projection` by `create or replace view`, appending columns at the end only. Verify that `fb_filtered_transactions` (`setof` the view) still compiles in the PGlite runner.
   - Add a `tags` filter in `fb_filtered_transactions` (`0005:39-49`).
   - Make `fb_apply_plaid_sync` move annotations on pending→posted the way it moves allocations (`0003:378-381`).
   - Extend `fb_export_backup` (version 4) and `fb_restore_backup` (accept 4, `0004:102`).
   - Extension operations: `annotate {id, notes, tags}`, `manual_add {row}` (ID `manual:<uuid4>` generated client-side, so it's idempotent), `manual_delete {id}` (only `manual:%`).
3. Python:
   - `Transaction` gets read-only `notes`/`tags` that are **excluded** from `OPTIONAL_TRANSACTION_FIELDS` (`supabase_repo.py:18`), so sync/import payloads never carry them.
   - `security.read_backup` and `backups.validate_backup` accept v4 with an annotations collection.
   - SQLite parity in `transaction_repo.py`.
4. UI:
   - "+ Add transaction" dialog via F2: date, description, positive amount plus a Money in/Money out toggle (`money()`), category, account (default "Cash"; open question 10). Sets `category_origin="manual"`.
   - Notes/tags fields in the UX-1 dialog.
   - A tag multiselect in `render_filter_bar` that adds to `query_filters`.

**Tests**
- `tests/test_data_architecture.py`: backup v4 round trip; v3 still restores.
- `tests/test_security.py`: `read_backup` accepts version 4.
- `tests/test_source_splits_and_reconciliation.py`: annotations survive pending→posted.
- `tests/test_statement_imports.py`: annotating does not break undo.
- `tests/test_ui_smoke.py`: add a manual transaction, then filter by tag.
- New `scripts/verify_annotations.sql`.

**Pay extra attention**
- **Silent loss on restore.** `fb_restore_backup_portable` inserts explicit columns. Any new column on `transactions` exported by `to_jsonb(t)` (`0004:79`) is dropped on restore without an error. That's another reason for a separate table and an explicit backup version.
- Manual rows get pooled by statement append matching (`0003:225-230`) and claimed by a later statement row. That's arguably the correct dedupe, but `statement_file_id` is then set on the manual row (`0003:282-285`).
- "Replace this account" deletes manual rows in that account (`0004:224`). Warn about it.
- Manual entries in a Plaid-connected account will duplicate the bank's own copy. Steer manual entries to a Cash account.
- Pydantic ignores unknown fields by default, so older app versions reading a v4 file would silently drop annotations. The version gate must reject unknown versions.

---

## 4. Cross-cutting risks

1. **Exact money.**
   - All new analytics must sum `Decimal` starting from `Decimal(0)`. Python `sum()` with its default start of `0` is fine for Decimal; mixing with float is not.
   - Widgets return floats; convert with `money()` immediately, e.g. `app.py:1567`, `1776`.
   - Watch existing mixed spots like `goal_plan`'s `/ 30.44` (`analytics.py:1149`). Don't copy that pattern.
2. **Split and allocation integrity.**
   - Every action keyed by row ID must handle `allocation:` IDs: categorize (supported), delete or merge (must refuse), dismiss (resolve to the source).
   - `apply_similar` collapses allocations (`0004:202`).
3. **Category provenance and silent re-categorization.**
   - Any UPDATE on `transactions` with origin in rule/provider/builtin re-applies rules (`0003:458-475`).
   - This includes the `revision=revision` updates fired by the allocation and import-row triggers (`0003:116-120`, `0004:328-337`).
   - New write paths (merge, annotations, demo) should avoid touching `transactions`, or accept that consequence explicitly.
4. **Recoverable operation IDs.**
   - The pending-ID key is `sha256(kind, payload)` (`operations.py:15`). Payloads must be deterministic: sort ID lists, use Decimal strings.
   - Two genuinely identical intents in a row are fine because the pending entry is removed on success.
   - Never put timestamps or random values in payloads, or retries can't recover the result.
5. **Plaid re-import of deleted rows.** Any delete of a `plaid:` row is undone by a later modified-upsert or reconcile (`0003:373,389`). Only allow Dismiss for bank rows.
6. **Atomic cloud writes.**
   - Multi-step UI flows (triage v1, budget rename, demo clear option A) must be either one operation or an explicitly recoverable sequence with retry copy.
   - Follow `fb_execute_operation`'s single-transaction pattern with `pg_advisory_xact_lock` per user (`0004:245`).
7. **Session isolation.**
   - Nothing uses `st.cache_*` today; keep it that way for user data.
   - Store anything private outside `session_state` (option B temp files) with cleanup hooks.
   - `bind_session_owner` (`security.py:16-22`) wipes state on user change but not files.
8. **Streamlit rerun and dialog pitfalls.**
   - One dialog per run (use F2).
   - Widget keys must be scoped to the entity they edit.
   - Don't assign `session_state[widget_key]` after the widget renders; use callbacks.
   - Index-based table selection drifts when rows are added or removed (`components.py:423-427`, `app.py:1513-1521`).
   - Labels that change need `keep_choice` (`app.py:498-512`).
9. **Performance.**
   - The Transactions page holds up to 200 rows per page (`app.py:2513`), but FEAT-2 and FEAT-3 need wider reads: use one bounded `get_filtered` per rerun, only when that view is selected.
   - The mobile card list already renders up to 50 button widgets per page (`components.py:405-415`). Don't add more per-card buttons there.
10. **Sample data leaking into real data or backups.** See FEAT-1: name collisions with Plaid adoption, rules applying to real data, the demo batch exposed in import receipts, and demo rows in backups.
11. **Broad merchant rules.** Literal substring matching (`0004:200`) with first-alphabetical precedence means short or processor phrases capture unrelated rows. Use F1's denylist plus a match-share warning.
12. **Sign auto-detection.** Suggestion only; never override Debit/Credit columns; credit cards still need a choice; the convention is part of `_file_id`.
13. **Light/dark and mobile.**
   - New HTML (chips row, banner, pair cards, timeline legend) must use `--fb-*` tokens (`components.py:19-28`) and be covered by the `forced-colors` rule (`components.py:85-88`).
   - Add new keyed containers to the `@container (max-width:700px)` rules.
   - Verify at 1280×720 and 390×844 in both themes.
14. **Accessibility.**
   - Action buttons in repeated cards need distinct names or `help` text, like `components.py:410-414`.
   - The timeline needs a table alternative.
   - New chart palettes must meet 4.5:1 for labels (UI_AUDIT #4).
   - New sections should use h2/h3 consistently (UI_AUDIT #10).
15. **Test harness parity.** UI tests swap in the SQLite `TransactionRepository` for the Supabase one (`tests/test_ui_smoke.py:42`). Every new repo method or operation must exist in both, plus SQL verification via `scripts/test_database.mjs`.

---

## 5. Open questions for you

1. **FEAT-1 storage:** demo data in your real Supabase account (option A: feels real, more cleanup risk) or an isolated in-session sandbox (option B: zero leakage, Plaid and backups disabled in demo)?
2. **FEAT-1 cleanup scope:** when clearing sample data, should budgets, goals and category rules created *during* demo also be removed, or only seeded ones?
3. **UX-5:** keep presets anchored to the latest transaction and only relabel them, or anchor to today when data is current and fall back to "Latest month (…)" only when it's stale? The latter changes `preset_range` and its tests.
4. **UX-5:** does the default date range count as an "active filter" chip shown outside the expander?
5. **UX-2 / FEAT-2:** what threshold makes a phrase "too broad"? The doc says 25% of all transactions. Should a denylisted processor phrase block saving, or only warn?
6. **FEAT-2:** should triaged rows be `manual` (v1, no migration; future rule edits won't move them) or `rule`-origin (v2, needs a migration)? Should the triage queue respect dashboard date/account filters or cover all history?
7. **UX-4:** is merge-and-remove allowed for statement rows only, or also manual rows? Is "Dismiss" per pair or per merchant/day?
8. **FEAT-3:** should Saved-history comparisons follow the dashboard account/category filters? Are quarters and custom ranges in scope for v1?
9. **FEAT-4:** round the 3-month average to whole dollars? Net out refunds? Show "Uncategorized" in unbudgeted spending?
10. **FEAT-6:** do manual entries need a new account type ("Cash"), which affects `_base_metrics` wording (`analytics.py:177-201`), or go into existing Checking accounts? Tag limits (count/length)? Should notes be searchable via the merchant search?
11. **FEAT-5:** should the timeline be hidden or just labeled when data is older than about 35 days? Should a manual balance be one number or per account?
12. **UX-6:** may a budget's category be changed (a non-atomic delete plus upsert), or only its limit?

### Critical files for implementation
- /Users/jennietran/Downloads/FinanceBuddy/app.py
- /Users/jennietran/Downloads/FinanceBuddy/src/services/analytics.py
- /Users/jennietran/Downloads/FinanceBuddy/src/repositories/supabase_repo.py (and its SQLite twin /Users/jennietran/Downloads/FinanceBuddy/src/repositories/transaction_repo.py)
- /Users/jennietran/Downloads/FinanceBuddy/supabase/migrations/202610130004_recoverable_operations_and_backups.sql (`fb_execute_operation`, `fb_correct_category`, undo, backup/restore)
- /Users/jennietran/Downloads/FinanceBuddy/tests/test_ui_smoke.py