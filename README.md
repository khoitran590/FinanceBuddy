# FinanceBuddy

FinanceBuddy is an authenticated personal finance dashboard built with Python, Streamlit, SQLite, Pandas, and Plotly.

The project follows Clean Architecture so the domain models, parser, categorizer, analytics, and repository can be mapped to Swift, SwiftData, and SwiftUI later without moving business logic out of the UI.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
streamlit run app.py
```

Each authenticated OIDC identity receives a separate SQLite database under `data/users/`. Database paths are derived from a hash of the provider issuer and immutable subject—not an email or browser-supplied value—and are not committed to Git.

## Login and account creation

FinanceBuddy requires OIDC authentication before any dashboard, transaction, backup, or Plaid code is available. Auth0 is a suitable provider because its Universal Login screen supports login, account creation, email verification, password recovery, breached-password protection, and MFA without FinanceBuddy storing passwords.

1. Create an Auth0 Regular Web Application and enable database or social connections for login and sign-up.
2. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`.
3. Set a long random `cookie_secret`, the Auth0 client ID and secret, and your tenant's metadata URL.
4. Register `http://localhost:8501/oauth2callback` as a local callback URL. For deployment, replace it with the HTTPS deployment URL ending in `/oauth2callback` and register that exact URL with Auth0.
5. Enable email verification and MFA in the Auth0 tenant before accepting real users.

For the Auth0 **Verification Email** template, set **Redirect To** to
`http://localhost:8501/?email_verified=1` locally, or the equivalent HTTPS deployment URL.
FinanceBuddy detects that return marker, refreshes the signed OIDC identity, and opens the
dashboard when Auth0 reports the email as verified.

The **Log in** and **Create account** buttons both open the provider's secure Universal Login experience. FinanceBuddy validates the signed OIDC identity through Streamlit, rejects expired sessions and explicitly unverified email claims, provides logout on every authenticated page, and derives all storage access from the authenticated subject.

Security boundaries implemented by the app include:

- Complete authentication gate before financial UI or data access
- Per-user transaction, budget, goal, settings, backup, and Plaid storage
- Non-PII, server-derived user database identifiers and Plaid `client_user_id` values
- Owner-only database file permissions where supported by the operating system
- Encrypted Plaid access tokens and server-side public-token exchange
- CORS and cross-site request-forgery protection enabled in Streamlit
- No application password database; password policy, recovery, verification, and MFA stay with the identity provider

The earlier single-user database remains at `data/finance.db` and is not exposed to authenticated web users. Export or migrate that legacy profile deliberately before removing it; never make it a shared fallback database.

## Dashboard features

- Account-aware metrics for checking and credit-card statements
- Global date, account, category, transaction-type, merchant, and amount filters with clear/select-all/reset controls
- Responsive desktop tables and mobile transaction cards
- Editable categories, reusable merchant rules, and split transactions
- Monthly budgets, savings goals, recurring-charge detection, unusual-expense review, and a simple cash-flow estimate
- Comparison of up to six statements with automatic date ordering
- Multi-account append or account-scoped replacement with import preview, duplicate detection, confirmation, and undo
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

If you enable OAuth institutions, also set `PLAID_REDIRECT_URI` (or `plaid.redirect_uri`) to an exact redirect URI registered in the Plaid Dashboard. Production access and OAuth institution approval are controlled in Plaid.

The first Transactions Sync response can contain no activity while Plaid prepares transaction history. Use **Sync transactions** again after Plaid finishes processing; subsequent syncs use a saved cursor and apply added, modified, and removed records without overwriting a category you corrected manually.

> Deployment note: this repository is a Streamlit/Python app, not a Next.js app. A Vercel preset that expects `next` in `package.json` will fail. Deploy it to a Streamlit-capable host, or wrap/migrate the UI to a framework Vercel supports as a persistent web app.

## Test

```bash
python -m pytest
```

## Production deployment

The included `Dockerfile` and `render.yaml` define a small-production deployment: the
process runs as a non-root user, dependencies are pinned, health checks use Streamlit's
health endpoint, secrets are assembled from environment variables at startup, and each
user's SQLite database is stored on a persistent disk. Do not deploy this repository as
a Vercel Next.js project.

1. Create a Render Blueprint from this repository. The declared Starter service and its
   persistent disk are paid resources; confirm the current Render pricing before creation.
2. Set every secret environment variable prompted by the Blueprint. Generate
   `AUTH_COOKIE_SECRET` and `PLAID_TOKEN_ENCRYPTION_KEY` independently, with at least
   32 random bytes each. Never reuse or casually rotate the Plaid encryption key.
3. Set `AUTH_REDIRECT_URI` to `https://YOUR_HOST/oauth2callback`. Add that exact value to
   Auth0's Allowed Callback URLs, add `https://YOUR_HOST` to Allowed Logout URLs and
   Allowed Web Origins, and change the verification-email **Redirect To** URL to
   `https://YOUR_HOST/?email_verified=1`. Remove localhost URLs from the production Auth0 app.
4. Keep `PLAID_ENV=production`; use the Production secret supplied for the Plaid Trial.
   Register `PLAID_REDIRECT_URI` if OAuth institutions are enabled. Set
   `PLAID_WEBHOOK_URL` to a separate HTTPS webhook receiver if you enable automatic
   Transactions updates; Link tokens will register it with Plaid.
5. Enable Auth0 email verification, breached-password protection, and MFA before inviting
   real users. Configure SendGrid domain authentication for production mail delivery.
6. Verify login, email verification, Plaid Link, sync, disconnect, export, and restore on
   the deployed HTTPS URL. Configure backups or disk snapshots for `/app/data`.

Required production variables are `AUTH_REDIRECT_URI`, `AUTH_COOKIE_SECRET`,
`AUTH_CLIENT_ID`, `AUTH_CLIENT_SECRET`, `AUTH_SERVER_METADATA_URL`, `PLAID_CLIENT_ID`,
`PLAID_SECRET`, and `PLAID_TOKEN_ENCRYPTION_KEY`. The app refuses to start in production
when these are missing, Auth/Plaid URLs are unsafe, Plaid is not in Production, or the
data directory is not an absolute persistent-disk path.

This SQLite design intentionally supports one running service instance. Render persistent
disks are attached to one instance, so do not enable horizontal scaling. Migrate the
repositories to a managed PostgreSQL database before running multiple instances or before
your expected user count exceeds a small private beta.

## CSV import behavior

The importer accepts CSV and text-based PDF statements. It detects common delimiters and encodings, extracts PDF tables or date-description-amount text rows, maps common bank header names, supports signed `Amount` or `Debit`/`Credit` columns, and refuses to commit an import when fewer than 80% of data rows are valid. Image-only/scanned PDFs are not supported yet because they require OCR.

Choose `Checking` or `Credit Card` before importing. Credit-card purchases are normalized as outflows, while payments, refunds, and credits are normalized as inflows. The import preview reports its detected period, parsed and skipped rows, and exact duplicates. Appending is the default and ignores duplicates; replacement only clears the named account and can be undone during the current session.

The dashboard includes expanded categories, accessible cash-flow and category charts, a category share table, and an optional monthly category heatmap. Comparison statements are sorted automatically by their detected transaction dates.

Transaction identifiers are deterministic hashes of date, amount, and description, so re-importing the same statement does not create duplicates.
