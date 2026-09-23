# FinanceBuddy

FinanceBuddy is an authenticated personal finance dashboard built with Python, Streamlit, Supabase PostgreSQL, Pandas, and Plotly.

The project follows Clean Architecture so the domain models, parser, categorizer, analytics, and repository can be mapped to Swift, SwiftData, and SwiftUI later without moving business logic out of the UI.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
streamlit run app.py
```

## Supabase setup and authentication

FinanceBuddy uses one Supabase project for authentication and durable PostgreSQL storage.
No Supabase secret/service-role key is used by the app. Each database request carries the
signed-in user's short-lived access token, so PostgreSQL row-level security is the final
authority for every read and write.

1. Create a Supabase project and link it with `supabase link --project-ref <project-ref>`.
2. Apply the checked-in migrations with `supabase db push --linked --skip-vault` before
   deploying this version of the app. For an existing project where the first migration
   was applied manually, verify its schema before marking it applied in migration history;
   do not run that schema migration a second time.
3. Under **Project Settings → API**, copy the project URL and publishable key. Do not use a
   secret key or legacy `service_role` key.
4. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and set the project
   URL, publishable key, and local `public_app_url`.
5. Under **Authentication → URL Configuration**, set the Site URL to the final deployed
   HTTPS Render URL and allowlist that exact URL. Do not allowlist localhost in the
   production Supabase project; use a separate development project for local email-link
   testing. Keep `PUBLIC_APP_URL` identical to the allowlisted production URL.
6. Keep **Confirm email** enabled. Under **Authentication → Emails → Templates**, use
   `supabase/templates/confirm-sign-up.html` for **Confirm sign up** and
   `supabase/templates/reset-password.html` for **Reset password**. These templates offer
   a one-click link and a code fallback; FinanceBuddy exchanges the one-time link token
   with Supabase, removes it from the address bar, and then opens the dashboard or reset form.
7. Configure the existing SendGrid account under **Authentication → SMTP Settings**. Use
   SendGrid host `smtp.sendgrid.net`, port `587`, username `apikey`, the SendGrid API key
   as the password, and the verified From address.

Supabase Auth provides account creation, email verification, login, logout, token refresh,
and password recovery. Session tokens live only in server-side Streamlit session state; passwords and
Supabase secret keys are never stored by FinanceBuddy.

Security boundaries implemented by the app include:

- Complete authentication gate before financial UI or data access
- Per-user transaction, budget, goal, settings, backup, and Plaid rows
- PostgreSQL row-level security based on the immutable Supabase Auth user UUID
- Explicit ownership filters in addition to database policies for defense in depth
- Encrypted Plaid access tokens and server-side public-token exchange
- CORS and cross-site request-forgery protection enabled in Streamlit
- No application password database; password hashing, recovery, verification, and optional MFA stay with Supabase Auth

The earlier local SQLite files are no longer used by the running app. Export them as a JSON
backup and restore that backup after signing in to migrate legacy activity deliberately.

```bash
python -m scripts.export_sqlite data/finance.db financebuddy-backup.json
```

After logging in with the intended Supabase account, open **Import & data → Backup &
restore** and upload that JSON file. This deliberately assigns the imported rows to that
authenticated Supabase user; the migration never guesses account ownership.

## Dashboard features

- Account-aware metrics for checking and credit-card statements
- Global date, account, category, transaction-type, merchant, and amount filters with clear/select-all/reset controls
- Responsive desktop tables and mobile transaction cards
- Editable categories, reusable merchant rules, and split transactions
- Monthly budgets, savings goals, recurring-charge detection, unusual-expense review, and a simple cash-flow estimate
- Comparison of up to six statements with automatic date ordering
- Multi-account append or account-scoped replacement with import preview, duplicate detection, and confirmation; a current-session undo is available for unchanged appended rows
- Filtered CSV export plus full JSON backup and restore
- Secure Plaid Link connections with encrypted access-token storage and incremental transaction sync
- Interactive spending donut plus a zero-baseline gain/loss cash-flow chart

## Plaid setup

FinanceBuddy supports Plaid alongside statement imports. Plaid credentials and permanent access tokens are never sent to the browser. The browser receives only a short-lived Link token, and the one-time public token is exchanged in Python.

1. Create a Plaid application and copy `.env.example` to `.env`.
2. Add `PLAID_CLIENT_ID`, your **Production** secret, and `PLAID_ENV=production`. Plaid Trial uses the Production environment and real institution data; its ten-Item allowance is enforced by Plaid.
3. Set a private `PLAID_TOKEN_ENCRYPTION_KEY`. Keep this value stable when rotating Plaid secrets, because it encrypts stored access tokens.
4. Run the app and open **Import & data → Bank connections**.

For a hosted deployment, configure the same values in the host's encrypted secret settings instead of committing `.env`. Streamlit secrets are also supported with this shape:

```toml
[plaid]
client_id = "..."
secret = "..."
environment = "production"
country_codes = ["US"]
token_encryption_key = "..."
```

The current embedded Plaid Link UI does not resume an OAuth redirect after returning to
FinanceBuddy. Leave `PLAID_REDIRECT_URI` unset; OAuth-only institutions are not supported
by this release. Production access and institution availability are controlled in Plaid.

The first Transactions Sync response can contain no activity while Plaid prepares transaction history. Use **Sync transactions** again after Plaid finishes processing; subsequent syncs use a saved cursor and apply added, modified, and removed records without overwriting a category you corrected manually.

> Deployment note: this repository is a Streamlit/Python app, not a Next.js app. A Vercel preset that expects `next` in `package.json` will fail. Deploy it to a Streamlit-capable host, or wrap/migrate the UI to a framework Vercel supports as a persistent web app.

## Test

```bash
python -m pytest
```

## Production deployment

The included `Dockerfile` and `render.yaml` define a stateless free-tier Render deployment:
the process runs as a non-root user, dependencies are pinned, health checks use Streamlit's
health endpoint, secrets are assembled from environment variables at startup, and all
durable state lives in Supabase.

1. Create a Render Blueprint from this repository. It declares the free web-service plan
   and does not request a disk or payment method.
2. Set `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `PUBLIC_APP_URL`. `PUBLIC_APP_URL`
   must be the final HTTPS `onrender.com` URL. If Render has not assigned it yet,
   create the service first, then set the exact assigned URL and redeploy. The app will
   intentionally refuse to start until that value is valid.
3. Set `PLAID_CLIENT_ID` and `PLAID_SECRET`. Render generates a private
   `PLAID_TOKEN_ENCRYPTION_KEY` on the initial Blueprint deployment. Keep that value
   stable: rotating it makes existing encrypted bank tokens unreadable.
4. Keep `PLAID_ENV=production`; use the Production secret supplied for the Plaid Trial.
   Leave `PLAID_REDIRECT_URI` unset until the OAuth return flow is implemented. Set
   `PLAID_WEBHOOK_URL` only if you run a separate HTTPS webhook receiver for automatic
   Transactions updates; Link tokens will register it with Plaid.
5. Keep Supabase **Confirm email** enabled, configure SendGrid custom SMTP, and install
   the two email templates above before inviting real users. Review the available CAPTCHA,
   rate limits, and MFA policies for your threat model.
6. Verify login, email verification, Plaid Link, sync, disconnect, export, and restore on
   the deployed HTTPS URL. Export periodic encrypted backups; Supabase Free does not include
   automatic database backups and may pause after low activity.
7. Once this app version is deployed and Plaid connections have been tested, run the
   reviewed cleanup in `supabase/staged/drop_public_plaid_token_after_app_rollout.sql`.
   It removes the compatibility ciphertext column from the public Data API. Do not run it
   while an older app version still reads that column.

Required production variables are `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`,
`PUBLIC_APP_URL`, `PLAID_CLIENT_ID`, `PLAID_SECRET`, and
`PLAID_TOKEN_ENCRYPTION_KEY`. The app refuses to start when these are missing, Supabase or
application URLs are unsafe, or Plaid is not in Production.

Before public launch, verify sign-up, confirmation link and code, password recovery,
cross-account isolation, Plaid Link, transaction sync, and disconnect on the deployed
HTTPS URL. The free Render service sleeps when idle and has an ephemeral filesystem;
Supabase Free does not provide downloadable database backups. These free tiers are useful
for a pilot but do not provide always-on availability or managed backup recovery.

## CSV import behavior

The importer accepts CSV and text-based PDF statements. It detects common delimiters and encodings, extracts PDF tables or date-description-amount text rows, maps common bank header names, supports signed `Amount` or `Debit`/`Credit` columns, and refuses to commit an import when fewer than 80% of data rows are valid. Image-only/scanned PDFs are not supported yet because they require OCR.

Choose `Checking` or `Credit Card` before importing. Credit-card purchases are normalized as outflows, while payments, refunds, and credits are normalized as inflows. The import preview reports its detected period, parsed and skipped rows, and exact duplicates. Appending is the default and ignores duplicates. The current-session undo removes only appended rows that have not changed since import. Replacement clears only the named account and is not undoable; download a backup first.

The dashboard includes expanded categories, accessible cash-flow and category charts, a category share table, and an optional monthly category heatmap. Comparison statements are sorted automatically by their detected transaction dates.

Transaction identifiers are deterministic hashes of date, amount, and description, so re-importing the same statement does not create duplicates.
