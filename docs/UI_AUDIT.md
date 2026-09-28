# FinanceBuddy UI audit — September 27, 2026

## Verdict

The implementation has a coherent identity: warm neutral surfaces, Public Sans with Newsreader headings, blue/orange financial semantics, and task-based navigation. Preserve these foundations. The largest opportunity is progressive disclosure: show a useful spending answer first and reveal tools and analysis when requested.

Scope: read app.py, src/ui/components.py, src/ui/charts.py, analytics insight generation, theme configuration, and UI test fixtures. Visually inspected the actual Overview with 33 synthetic transactions at desktop 1280×720 and phone 390×844, in the device-selected dark theme. No real financial data was used. The temporary preview did not serve the local font assets, so exact font rendering was not verified; typography recommendations are grounded in the committed font configuration. This was an audit; application code was not changed. Other routes were assessed from source, not exhaustively exercised. Light mode, keyboard traversal, zoom, screen-reader operation, large datasets and production latency remain unverified.

The bundled detector returned [] for app.py and src/ui. This does not establish a clean bill of health: its coverage of Python-generated Streamlit markup is limited. Findings below are manually verified source or browser evidence.

## Provisional health score

| Dimension | Score / 4 | Evidence |
|---|---:|---|
| Accessibility | 2 | Focus styling and named controls; chart label contrast failures and heading structure gaps |
| Performance | 2 | All Overview tab bodies execute; repeated analytics and chart construction |
| Responsive design | 2 | Phone metric grid works; filters dominate phone viewport; desktop range strip clips |
| Theming | 3 | Both themes and shared CSS tokens; chart colors duplicated across configuration and code |
| Implementation integrity | 2 | Coherent styling; misleading empty quick views and unsupported insight wording |
| Total | **11/20** | Acceptable; significant work needed |

Scores describe the inspected implementation and are provisional, not accessibility certification or performance benchmarks. Findings: 0 P0, 4 P1, 6 P2. Avoid spending time on minor decoration before these.

## Priority findings

### 1. [P1] Overview delays the primary spending answer

Location: app.py:1247, app.py:790, app.py:2163. Category: Implementation integrity / information hierarchy.

Desktop evidence: the first viewport contains the header, filter card, four equally weighted metrics, three insights, tabs and a category warning; the main charts start below it. On a phone the filter card alone takes approximately 370 vertical pixels. Income precedes Spending despite the user's stated spending-tracking goal. Summary continues with exploration, an extra running-total chart, three change metrics, unusual expenses and a forecast.

Recommendation: make Spending the dominant number; keep Income and Net savings quieter, move savings rate into Trends. Follow with one ranked category chart, a few recent transactions, and at most one actionable insight. Move forecast, unusual expenses and extra charts to the relevant deeper view or a collapsed detail section. Consolidate repeated category-review prompts. Suggested command: $impeccable distill.

### 2. [P1] Date controls clip on desktop

Location: app.py:2191; src/ui/components.py:143. Category: Responsive.

At 1280px with the sidebar open, the eight-option date strip clips its first option and hides Custom. Wrapping is only applied below 640px. The merchant placeholder is also truncated.

Recommendation: use a single period selector, with a clear selected date range. Keep account selection and one Filters action nearby; move merchant search and category/amount filters into that panel on Overview. Transactions can retain prominent search. Test intermediate widths with sidebar open. Suggested command: $impeccable adapt.

### 3. [P1] Empty quick views silently show all transactions

Location: app.py:1321–1330. Category: Implementation integrity.

When Pending, Needs a category or Possible duplicates has zero results, the app shows a success message then assigns shown = transactions. This contradicts the selected scope and can make unrelated activity look pending or duplicated.

Recommendation: retain an empty list, show a short empty state and a deliberate View all transactions action; skip the table and editor. Suggested command: $impeccable harden.

### 4. [P1] Chart labels have insufficient contrast

Location: src/ui/charts.py:32, src/ui/charts.py:112. Category: Accessibility. Standard: WCAG 1.4.3.

White labels on teal #2E9C95, purple #9B7BD8 and brown #B7652B have approximately 3.33:1, 3.38:1 and 4.28:1 contrast. Donut labels are 15px, below the large-text exemption; the minimum is 4.5:1.

Recommendation: choose label text from measured luminance, darken relevant fills, or put values beside the chart. Verify every palette pairing in both themes, including treemaps. Suggested command: $impeccable harden.

### 5. [P2] The initial time scope is too broad and labels are ambiguous

Location: app.py:2163, app.py:531. Category: Implementation integrity / clarity.

Initial selection is All time. “This month” counts from the latest saved transaction rather than necessarily the calendar month. Aggregate savings and spending can be mistaken for current-month status, especially with old statements.

Recommendation: default to the current calendar month when activity exists; otherwise explicitly show Latest available month — September 2026. Always display the actual period adjacent to the main amount. Preserve deliberate user selections. Suggested command: $impeccable clarify.

### 6. [P2] Insight claims need more careful categorization

Location: src/services/analytics.py:1291. Category: Implementation integrity / copy.

The subscriptions insight treats recurring expenses outside FIXED_CATEGORIES as subscriptions and memberships. The synthetic fixture classified repeating shopping and grocery purchases this way, producing “8 subscriptions and memberships” despite having only one explicit subscription merchant. Recurrence alone does not establish a subscription.

Recommendation: reserve that wording for a verified subscription category/type, or call them recurring charges with a review link. Label estimates and inferred classifications clearly. Suggested command: $impeccable clarify.

### 7. [P2] Transaction editing is always exposed

Location: app.py:1333–1390; src/ui/components.py:244. Category: Responsive / hierarchy.

The desktop editor is always shown and automatically selects the first row. On phones cards are static markup with no direct selection affordance; the full edit form appears after up to 50 cards.

Recommendation: open editing only after selection; provide an Edit action per mobile card or show a selected transaction in a drawer/dialog. Keep merchant-rule creation behind an optional advanced section. Suggested command: $impeccable adapt.

### 8. [P2] Hidden Overview tabs still build all analysis

Location: app.py:1277–1288. Category: Performance.

All tab bodies execute on each Streamlit rerun. A simple filter or chart interaction constructs analytics and Plotly figures for Summary, Trends, Habits and Recurring & income; Balances is added when available. This is verified control flow, not measured latency.

Recommendation: use selected-view conditional rendering or the installed Streamlit version's supported lazy tab mechanism. Reuse analytics results within one rerun. Any cache must preserve account isolation and invalidate after imports, syncs and corrections. Suggested command: $impeccable optimize.

### 9. [P2] Chart options and colors compete with the data

Location: app.py:814, app.py:841; src/ui/charts.py:89; .streamlit/config.toml.

Each primary chart has a permanently visible style chooser. Donut categories use many hues, with small slices harder to compare. Chart palette values are duplicated across Python and Streamlit configuration.

Recommendation: default to sorted horizontal category bars, top five plus Other, with amounts and shares. Put style selection in Chart options. Preserve full categories through a detail table; use color primarily for state or selection. Centralize palette authority. Suggested command: $impeccable quieter.

### 10. [P2] Section headings do not provide a consistent semantic outline

Location: src/ui/components.py:159; app.py:864. Category: Accessibility.

Card titles are paragraph tags. Overview moves from h1 to Streamlit subheader h3 without an h2, making sections less useful for heading-based screen-reader navigation.

Recommendation: use a consistent h1 → h2 → h3 outline for meaningful sections while preserving restrained visual typography. Suggested command: $impeccable harden.

## What to retain

- Warm light/dark palettes, locally hosted fonts and restrained serif headings.
- Named form controls, explicit focus styling, 44px sidebar navigation targets, numeric alignment and escaped user content.
- Separate destinations for transactions, planning and accounts.
- Account/category popovers, optional category table, remembered chart preferences, transfer exclusion and period-aware comparisons.

## Recommended target composition

Overview should answer “How much did I spend, where did it go, and is anything worth reviewing?”

1. Header with explicit month, account scope and Filters action.
2. Prominent Spending amount with a comparable-period change; quieter Income and Net savings.
3. Ranked top spending categories with View all categories.
4. Recent activity with View all transactions.
5. One useful observation or review action, shown only when supported.

Keep deeper tools available in Trends, Habits, Recurring & income and Balances. Users should reveal detail by selecting a category, transaction or dedicated view. Do not remove tracking capability.

## Recommended implementation order

1. $impeccable distill — reorganize Overview around the spending answer.
2. $impeccable adapt — simplify filters and improve mobile transaction selection.
3. $impeccable harden — fix empty scopes, chart contrast and semantic headings.
4. $impeccable clarify — make dates and inferred insights accurate and explicit.
5. $impeccable optimize — render only the chosen analysis view.
6. $impeccable quieter — reduce chart controls and category-color noise.
7. $impeccable polish — finish spacing, typography and state consistency after structure is resolved.

Follow-up verification: desktop and phone in both themes, keyboard and enlarged text, zero-result quick views, stale statement dates, long merchant names, many categories, and large transaction histories. Re-run the audit after fixes. Capturing PRODUCT.md and DESIGN.md would help make future design choices consistent, but their absence does not block these changes.
