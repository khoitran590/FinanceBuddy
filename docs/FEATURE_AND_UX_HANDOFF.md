# FinanceBuddy Roadmap: UX Polish & Feature Handoff Guide

**Updated:** October 29, 2026  
**Target Audience:** Coding Agents & Contributors  
**Implementation Plan:** [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md) has phased steps, risks, and corrections to this doc's effort/schema estimates. Read it before starting any item.  
**Related Docs:** [`UI_AUDIT.md`](./UI_AUDIT.md) · [`DATA_ARCHITECTURE_AUDIT.md`](./DATA_ARCHITECTURE_AUDIT.md) · [`DATA_MIGRATION_ROLLOUT.md`](./DATA_MIGRATION_ROLLOUT.md) · [`SECURITY_IMPLEMENTATION.md`](./SECURITY_IMPLEMENTATION.md)

---

## 1. Architectural Guardrails for Agents (Read First)

Before modifying any UI or service code, keep these enforced project invariants in mind:

1. **Exact Money Representation (`Decimal` / `Money`):**
   - Domain models (`src/domain/models.py`) and analytics (`src/services/analytics.py`) use `Decimal` via `src.domain.money.money`.
   - Convert to `float` **only** at widget/chart boundaries (e.g., `st.number_input`, Plotly traces).
2. **Canonical Source Transactions vs. Allocations:**
   - Splitting a transaction does **not** delete or rename the parent transaction ID; it attaches `TransactionAllocation` child records to the source transaction (`source_transaction_id`, `source_amount`).
   - Any UI change to splitting must preserve `repo.split_transaction(...)` against the canonical source ID.
3. **Category Provenance:**
   - Transactions track `category_origin` (`"manual"`, `"rule"`, `"provider"`, `"builtin"`, `"legacy"`).
   - Categorization mutations should flow through `DataService` (`src/services/data_service.py`) or atomic repository methods so provenance and rule versions stay consistent.
4. **Recoverable Operations & Tenant Isolation:**
   - Cloud writes use `pending_operation_ids` for idempotent retry recovery.
   - Never cache user financial data globally across sessions without scoping by `user_id` and invalidating on write; `clear_private_session` in `src/services/security.py` must clean up any sensitive `st.session_state` keys on logout.
5. **Visual & Accessibility Standards:**
   - Preserve the warm light/dark CSS token palette (`--fb-surface`, `--fb-ink`, `--fb-income`, `--fb-spend` in `src/ui/components.py`), `Public Sans` + `Newsreader` typography, 44px minimum touch targets on mobile/coarse pointers, and explicit `html.escape()` on all user-supplied strings rendered in HTML.

---

## 2. Recommended Agent Skills, Workflows & Slash Commands

When picking up items from this document, agents and users can leverage the following skills and workflows:

### Design & UI Polish Skills (Claude Code `impeccable` plugin, referenced in `docs/UI_AUDIT.md`)
With the `impeccable` plugin installed, invoke `/impeccable <command> [target]` (e.g. `/impeccable distill app.py`):
- **`/impeccable distill`** — Simplifying complex views, merging redundant forms/expanders into unified dialogs, and tightening information hierarchy.
- **`/impeccable adapt`** — Responsive container queries (`@container (max-width: 700px)`), mobile card actions, and filter bar wrapping.
- **`/impeccable clarify`** — Improving copy, auto-detection summaries, and date-range disambiguation.
- **`/impeccable harden`** — Edge-case handling, WCAG AA contrast checks, keyboard focus states, and idempotent retry states.
- **`/impeccable quieter`** — Reducing visual clutter and secondary control noise.
- **`/impeccable onboard`** — Useful for FEAT-1 (sample data mode) and the empty-state flow in `render_start_actions`.

### Claude Code Commands, Skills & Subagents
- **Plan mode (`Shift+Tab` to toggle) or the `Plan` subagent** — Use before starting items that touch both `src/repositories/` (SQLite + Supabase RPCs) and `app.py` (such as Manual Transactions, Tags/Notes, or Duplicate Resolution).
- **`/run` skill + `claude-in-chrome`** — Launch the Streamlit app and visually verify responsive layouts at desktop (`1280×720`) and mobile (`390×844`) viewports in both light and dark themes (use `resize_window` and screenshots; `gif_creator` to record multi-step flows for PR review).
- **`Agent` tool subagents (`general-purpose`, `Explore`, or `fork`, optionally with `isolation: "worktree"`)** — Useful for parallelizing backend repository/migration tests (`tests/test_repository_features.py`, `tests/test_supabase.py`) while another agent updates `app.py` and `tests/test_ui_smoke.py`. Worktree isolation keeps parallel edits from colliding.
- **`/code-review` and `/security-review`** — Run before opening each PR; `/security-review` matters most for items touching auth, Plaid sync, or Supabase RPCs (UX-4, FEAT-6).
- **`/init` or `claude-md-management:claude-md-improver`** — Use if you want to codify the architectural guardrails above into a persistent `CLAUDE.md` so every Claude Code session loads them automatically.
- **`dataviz` skill** — Use for FEAT-3 (saved-period comparison charts), FEAT-5 (cash-flow timeline), and any new chart in `src/ui/charts.py`.

---

## 3. Part I: UX Polish (More Approachable Interface)

### UX-1: Unified Transaction Action Modal (Merge Edit & Split)
- **Current State:** Clicking a transaction row (or tapping *Edit transaction* on mobile) opens `@st.dialog("Edit a transaction")` (`render_transaction_editor` in `app.py:1399`), which only edits the category and merchant rule. Splitting a transaction is buried at the bottom of the `Transactions` page inside `with st.expander("Split a transaction between two categories"):` (`app.py:1532`) with a separate transaction selector.
- **Proposed Polish:** Move the split form directly into `render_transaction_editor` as a segmented view or tab (`Category & Rule` | `Split Amount`), automatically targeting the selected transaction's canonical source (`selected.source_transaction_id or selected.id` and `selected.source_amount or selected.amount`). Remove the bottom page expander.
- **Key Files:** `app.py` (`render_transaction_editor`, `render_transactions`), `tests/test_ui_smoke.py`.
- **Pros:**
  - Single, predictable entry point for all transaction actions on both desktop and mobile.
  - Eliminates scrolling past 50–200 rows to split a purchase.
  - Removes the O(N) dropdown scan for picking a transaction to split.
- **Cons / Tradeoffs:**
  - Slightly increases the height/complexity of `@st.dialog("Edit a transaction")`.
  - When a user clicks a row that is *already* one half of a split projection, the UI must clearly explain that editing the split replaces the allocations on the parent purchase (`source_amount`).

---

### UX-2: Pre-Fill Cleaned Merchant Name in Category Rules
- **Current State:** Inside `render_transaction_editor` (`app.py:1425`), the `Categorize similar transactions` expander has an empty text input (`Merchant name or phrase (optional)`). Users have to manually type or copy-paste from raw bank descriptions like `"POS DEBIT VISA STARBUCKS STORE #19402"`.
- **Proposed Polish:** Use `merchant_name(selected)` from `src/services/analytics.py:79` to pre-populate or suggest the cleaned merchant name (e.g., `"Starbucks"`), and preview how many existing transactions in the current view/history match that phrase.
- **Key Files:** `app.py` (`render_transaction_editor`), `src/services/analytics.py` (`merchant_name`, `clean_merchant`).
- **Pros:**
  - Turns rule creation from a typing exercise into a one-click checkbox.
  - Reduces user typos that cause rules not to match.
- **Cons / Tradeoffs:**
  - `clean_merchant` can occasionally strip too much from obscure bank memos (e.g., generic payment processor strings like `"PayPal"` or `"Square"`). If the cleaned name is overly broad, pre-filling it could encourage a user to categorize all `"PayPal"` transactions the same way. Mitigation: warn when a keyword is a known payment processor or matches >25% of all transactions.

---

### UX-3: Smart Auto-Detection for Statement Import Formatting
- **Current State:** `statement_format_options` (`app.py:392`) asks users upfront to choose `"Amount column sign convention"` (`outflow_negative` vs `outflow_positive`), date format, number format, and reference handling before they see their parsed data.
- **Proposed Polish:**
  - Inspect the parsed rows automatically in `BankStatementParser`: if a single `Amount` column is present, infer the sign convention using known expense/income keywords (`CATEGORY_RULES` or keywords like `payroll`, `direct deposit`, `payment thank you` vs common retail merchants) or column headers.
  - Collapse all format overrides into an `"Adjust date, number, or sign format"` expander, and surface a prominent one-click **"Flip +/- signs"** button in the preview step (`3. Review the preview`) alongside a plain-language summary: *"Detected 38 expenses ($1,420.50) and 2 deposits ($3,100.00)."*
- **Key Files:** `src/services/parser.py`, `app.py` (`statement_format_options`, `render_import_statement`), `tests/test_parser.py`.
- **Pros:**
  - Dramatically lowers cognitive load for non-technical users importing their first CSV or PDF.
  - Prevents accidental inverted imports when users guess the wrong sign convention.
- **Cons / Tradeoffs:**
  - Note Finding #4 in `DATA_ARCHITECTURE_AUDIT.md`: heuristics must **never** silently override explicit `Debit`/`Credit` columns, and if a single-column file is ambiguous, the UI should still require or highlight the chosen sign convention before enabling `Save statement`.

---

### UX-4: Actionable "Possible Duplicates" Resolution
- **Current State:** `Transactions → Possible duplicates` (`app.py:1481`, `app.py:1585`) highlights transactions sharing the same account, date, amount, and normalized description via `AnalyticsService.duplicate_candidates`, but provides no way to delete a duplicate or mark two identical purchases (e.g., buying two $4.50 coffees on the same day) as legitimate.
- **Proposed Polish:** Replace the read-only duplicate list with interactive pair cards offering two actions:
  1. **Remove duplicate** (deletes the selected redundant copy, if it is a statement-imported row).
  2. **Keep both (dismiss warning)** (records the pair in user settings or a transaction flag so it no longer triggers the duplicate badge).
- **Key Files:** `app.py` (`render_transactions`), `src/repositories/transaction_repo.py`, `src/repositories/supabase_repo.py`.
- **Pros:**
  - Closes a dead-end workflow where users see a warning they cannot fix.
- **Cons / Tradeoffs:**
  - Deleting a Plaid-managed transaction directly would be recreated on the next sync unless reconciled; only statement-imported duplicates should be deletable, while Plaid duplicates should offer *"Dismiss"* or explain pending-to-posted reconciliation.

---

### UX-5: Surface Active Filter Chips & Disambiguate Stale Date Presets
- **Current State:**
  1. In `render_filter_bar` (`app.py:2448`), the active filter chips (`fb-chip`) and the `Reset filters` button live *inside* `st.expander("Filters")`. When collapsed, users only see `Filters (2 active)` without knowing *what* is filtered.
  2. `AnalyticsService.preset_range` (`src/services/analytics.py:241`) anchors `"This month"` and `"Last 30 days"` to `max_date` (the latest transaction in the database) rather than `date.today()`. If a user uploaded an old statement from 6 months ago, `"This month"` shows 6-month-old numbers without explanation.
- **Proposed Polish:**
  - Render the active filter chips row and `Reset filters` button *outside* the expander whenever `active_count > 0`.
  - When `max_date` is not in the current calendar month (`(max_date.year, max_date.month) != (date.today().year, date.today().month)`), format the preset label in the selectbox as `"Latest month in history (Sep 2026)"` instead of `"This month"`.
- **Key Files:** `app.py` (`render_filter_bar`), `src/ui/components.py`, `tests/test_ui_smoke.py`.
- **Pros:**
  - Prevents users from misinterpreting filtered or historical totals as their current-month spending (addresses Finding #5 in `UI_AUDIT.md`).
  - One-click filter reset without opening an expander.
- **Cons / Tradeoffs:**
  - Adds ~36px of vertical height below the date selector when filters are active.

---

### UX-6: Inline Card Actions & Modal Creation in `Plan` (Budgets & Goals)
- **Current State:** `render_budgets` (`app.py:1595`) and `render_goals` (`app.py:1691`) stack all progress bars at the top, followed by always-visible creation forms, followed by `"Remove a budget"` / `"Update or remove a goal"` expanders at the bottom.
- **Proposed Polish:**
  - Move `"Create budget"` and `"Create goal"` into `@st.dialog` modals triggered by a primary button in the section header.
  - Add compact inline action buttons (`+ Add funds` / `Edit` / `Remove`) directly on each goal or budget card.
- **Key Files:** `app.py` (`render_budgets`, `render_goals`, `render_plan`).
- **Pros:**
  - Much cleaner `Plan` page; updating progress on a savings goal takes 1 click right next to the progress bar instead of selecting the goal name from a dropdown inside a bottom expander.
- **Cons / Tradeoffs:**
  - Streamlit's `@st.dialog` allows only one open dialog per script run, so button state management must follow the same pattern used by `transaction_editor_open` (`app.py:1527`).

---

## 4. Part II: New Features to Add

### FEAT-1: Interactive Sample / Demo Data Mode on First Run
- **Problem:** New users who sign up land on an empty account (`not catalog["count"]`) and must either connect a live bank via Plaid or upload a bank statement file before seeing any part of FinanceBuddy's charts, budgets, or insights.
- **Solution:** Add an **"Explore with sample data"** button in `render_start_actions` (`app.py:590`) that populates a realistic 6-month synthetic dataset (salary paychecks, rent, utilities, groceries, dining, subscriptions, and 2 budgets/goals) tagged as demo data (or loaded into an isolated demo batch). Display a persistent top banner: *"Viewing sample data · [Clear sample data & start fresh]"*.
- **Key Files:** `app.py` (`render_start_actions`), `src/services/data_service.py`.
- **Pros:**
  - Zero-friction onboarding; users immediately see the value of Trends, Habits, Recurring detection, and Plan.
  - Makes UI testing and demos effortless.
- **Cons / Tradeoffs:**
  - If sample data is written to the database, clearing it must be 100% reliable and never delete real transactions the user added afterward. Using the existing `import_batch` mechanism (`data_service.import_statement` + storing `demo_batch_id` in user settings so `undo_import` can cleanly remove it) avoids schema changes!

---

### FEAT-2: Bulk "Uncategorized Merchant" Triage Queue
- **Problem:** After an initial statement import, a user may have dozens of `Uncategorized` transactions from local stores or restaurants. Categorizing them row-by-row takes too long.
- **Solution:** At the top of `Transactions → Needs a category`, group uncategorized transactions by `merchant_name(tx)` and display the top uncategorized merchant groups (e.g., *"Blue Bottle Coffee · 6 transactions · $41.50"*). Let the user pick a category and click **Apply to all 6 & save rule**, calling `data_service.correct_category(tx_id, category, merchant_keyword, apply_similar=True)`.
- **Key Files:** `app.py` (`render_transactions`), `src/services/analytics.py`, `src/services/data_service.py`.
- **Pros:**
  - High leverage: uses `data_service.correct_category(..., apply_similar=True)`, which is **already implemented and atomic in both SQLite and Supabase**!
  - Can categorize 80% of an imported statement in under 30 seconds.
- **Cons / Tradeoffs:**
  - Grouping relies on `clean_merchant`; the UI should show the exact keyword phrase that will be used for matching so the user can tweak it before applying.

---

### FEAT-3: Compare Saved History Periods (Month-over-Month / Quarter-over-Quarter)
- **Problem:** The `Compare` page (`render_compare` in `app.py:1797`) currently only compares 2–6 newly uploaded CSV/PDF files. Users who use Plaid or have already imported their statements cannot compare their saved months or quarters.
- **Solution:** Add a segmented view switcher at the top of `Compare`: **`Saved history`** | **`Uploaded files`**. In `Saved history` mode, let the user pick 2 to 6 months (or quarters/custom date ranges) from `catalog` and feed those slices of saved transactions directly into `AnalyticsService.compare_periods` and the existing `make_comparison_*` charts.
- **Key Files:** `app.py` (`render_compare`), `src/services/analytics.py` (`compare_periods`).
- **Pros:**
  - Reuses 100% of the existing comparison analytics and charts (`make_comparison_summary_chart`, `make_comparison_category_chart`, `make_comparison_delta_chart`).
  - Makes `Compare` useful for Plaid users and returning statement users.
- **Cons / Tradeoffs:**
  - None significant; low implementation cost with high user value.

---

### FEAT-4: Smart Budget Suggestions & "Unbudgeted Spending" Tracker
- **Problem:** When creating a budget in `Plan`, the default limit is a static `$500.00`, and `Plan → Budgets` only totals spending *in budgeted categories* (`Spent in budgeted categories`), hiding spending in categories that don't have a budget yet.
- **Solution:**
  1. In the budget creation form, compute and show the user's 3-month historical average and last month's spend for the selected category (using `AnalyticsService.complete_months`), with a one-click *"Use 3-month average ($X)"* helper.
  2. Add an **"Unbudgeted spending"** metric or card below the budgeted categories showing how much was spent in categories without a limit, with quick `+ Set budget` buttons for the top unbudgeted categories.
- **Key Files:** `app.py` (`render_budgets`), `src/services/analytics.py` (`budget_status`).
- **Pros:**
  - Prevents the common budgeting pitfall where users think they are "under budget" because half their spending happened in unbudgeted categories.
  - Grounds budget targets in real historical data.
- **Cons / Tradeoffs:**
  - Requires at least 1–3 months of history to show meaningful averages (gracefully fall back when history is empty).

---

### FEAT-5: Upcoming Bills & Payday Cash-Flow Timeline
- **Problem:** `AnalyticsService.upcoming_charges`, `income_summary`, and `safe_to_spend` already predict next charge dates and paydays over the next 30 days, but `Overview → Recurring & income` only renders a static table, and `Safe to spend` is hidden unless Plaid balances are connected.
- **Solution:**
  - Add a visual **Next 30 Days Timeline** in `Recurring & income` showing expected bills and paydays in chronological order, with a running cash-flow total.
  - Allow users who don't use Plaid to enter a manual starting cash balance on `Plan` or `Recurring & income` to calculate their **Safe to spend** before next payday.
  - Add an action to mark a false-positive recurring merchant as *"Not a recurring bill"* (stored in user settings) so everyday habits (like weekly grocery runs) don't skew `safe_to_spend` or the subscription insight (addresses Finding #6 in `UI_AUDIT.md`).
- **Key Files:** `app.py` (`render_overview_recurring`, `render_overview_balances`), `src/services/analytics.py` (`recurring_expenses`, `insights`).
- **Pros:**
  - Unlocked for statement-import users, not just Plaid users.
  - Directly fixes false-positive recurring charge detection by giving the user an override list.
- **Cons / Tradeoffs:**
  - Storing excluded recurring merchants in `settings` (`repo.get_setting` / `repo.set_setting`) means `AnalyticsService.recurring_expenses` or its caller needs to filter out user-dismissed merchants.

---

### FEAT-6: Manual Transaction Entry & Custom Tags / Notes
- **Problem:** Users cannot log cash purchases or one-off adjustments without creating a CSV, nor can they tag transactions across multiple categories for a single event (e.g., `#vacation-2026`, `#reimbursable`, `#tax-2026`).
- **Solution:**
  - Add a `+ Add transaction` modal in `Transactions` for manual entries (`category_origin="manual"`).
  - Add optional `notes: Optional[str]` and `tags: List[str]` fields to `Transaction` (`src/domain/models.py`) and the database schema, with tag filtering in `render_filter_bar`.
- **Key Files:** `src/domain/models.py`, `supabase/migrations/`, `src/repositories/`, `app.py`.
- **Pros:**
  - Essential for cross-category project/trip tracking and cash spending.
- **Cons / Tradeoffs:**
  - **Requires a database schema migration** (new columns on `transactions` + updating the Supabase RPCs for atomic import/backup/restore and `read_backup` version validation). Should be scheduled **after** UI-only features.

---

## 5. Prioritized Implementation Matrix for Agents

| ID | Item | Type | Touches DB Schema? | Effort | Impact | Recommended Order |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: |
| **FEAT-2** | Bulk Uncategorized Merchant Triage Queue | Feature | No | Low | High | **1** |
| **UX-1** | Unified Transaction Modal (Merge Edit & Split) | UX Polish | No | Low | High | **2** |
| **UX-2** | Pre-Fill Cleaned Merchant Name in Rules | UX Polish | No | Low | High | **2** (pair with UX-1) |
| **UX-5** | Visible Filter Chips & Stale Date Disambiguation | UX Polish | No | Low | Medium | **3** |
| **FEAT-3** | Compare Saved History Periods | Feature | No | Low | High | **4** |
| **FEAT-4** | Smart Budget Suggestions & Unbudgeted Tracker | Feature | No | Low–Med | High | **5** |
| **UX-6** | Inline Card Actions & Modals in `Plan` | UX Polish | No | Medium | Medium | **6** (pair with FEAT-4) |
| **FEAT-1** | Sample / Demo Data Mode (via Import Batch) | Feature | No | Medium | High | **7** |
| **UX-3** | Smart Auto-Detection for Statement Imports | UX Polish | No | Medium | High | **8** |
| **FEAT-5** | 30-Day Bills Timeline & Recurring Overrides | Feature | No (uses `settings`) | Medium | Medium | **9** |
| **UX-4** | Interactive Duplicate Resolution | UX Polish | Optional (`settings`) | Medium | Medium | **10** |
| **FEAT-6** | Manual Transactions & Custom Tags/Notes | Feature | **Yes** (Migration) | High | Medium | **11** |

---

## 6. Verification Checklist for Every Handoff PR

Whenever an agent completes one of the items above, run through this checklist before marking the task done:

1. **Automated Test Suite:**
   ```bash
   .venv/bin/python -m pytest -q
   ```
   Ensure all existing unit, repository, security, and UI smoke tests pass, and add coverage in `tests/test_ui_smoke.py` or `tests/test_analytics_features.py` for the new behavior.
2. **Session & Tenant Safety:**
   - Verify any new `st.session_state` keys are cleared on logout (`clear_private_session` in `src/services/security.py`).
3. **Money & Type Safety:**
   - Verify no `float` arithmetic is introduced in `AnalyticsService` or `DataService`—all monetary math must remain `Decimal`.
4. **Responsive & Theme Check:**
   - Verify new components at `1280×720` (desktop with sidebar open) and `390×844` (mobile viewport) in both light and dark themes.
