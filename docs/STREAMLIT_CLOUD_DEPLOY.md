# Deploying FinanceBuddy to Streamlit Community Cloud

Streamlit Community Cloud is free and runs Streamlit apps directly from a GitHub repo. It replaces the Render Docker deployment. Community Cloud does not use the `Dockerfile` or `scripts/start.py`, which stay in the repo only for self-hosting.

Why move: Render's free tier spins the service down after 15 minutes without traffic, and the next visitor waits roughly a minute while the container cold-starts. Community Cloud only puts an app to sleep after about 12 hours without visitors, so most visits hit an app that is already running.

## How this app maps to Community Cloud

| Concern | On Render | On Community Cloud |
| --- | --- | --- |
| Build | `Dockerfile` | `requirements.txt` (installed automatically) |
| Start command | `scripts/start.py` writes `secrets.toml` from env vars | Streamlit runs `app.py` directly |
| Config | Env vars in the Render dashboard | Secrets pasted into the app dashboard (TOML) |
| Theme, fonts | `.streamlit/config.toml`, `static/fonts/` | Same, read from the repo |
| Data | Supabase | Supabase (unchanged) |

No code changes are needed. The app already reads `st.secrets` and falls back to env vars, and Community Cloud exposes secrets both ways.

## Before you start

- [ ] The repo is on GitHub (public or private; private repos work on the free tier).
- [ ] `.streamlit/secrets.toml` and `.env` are **not** committed (they are already in `.gitignore`).
- [ ] You have your Supabase URL and publishable key, and your Plaid client ID and secret.

## 1. Push the repo

```bash
git push origin main
```

Community Cloud deploys from the branch you choose and redeploys on every push.

## 2. Create the app

1. Sign in at <https://share.streamlit.io> with GitHub and authorize access to the repo.
2. Click **Create app** and choose **Deploy a public app from GitHub** (or the equivalent for a private repo).
3. Set:
   - **Repository**: your FinanceBuddy repo
   - **Branch**: `main`
   - **Main file path**: `app.py`
   - **App URL**: pick a subdomain, e.g. `financebuddy` gives `https://financebuddy.streamlit.app`
4. Open **Advanced settings** and:
   - Set **Python version** to `3.12` (matches the Dockerfile).
   - Paste the secrets from the next section.
5. Click **Deploy**.

## 3. Secrets

Paste this into **Advanced settings → Secrets** (or later under **App settings → Secrets**). Replace every placeholder.

```toml
# Top-level string secrets are also exposed to the app as environment variables.
FINANCEBUDDY_ENV = "production"

[supabase]
url = "https://YOUR_PROJECT_REF.supabase.co"
publishable_key = "sb_publishable_YOUR_KEY"
# The URL Streamlit gives you in step 2, no trailing path.
public_app_url = "https://financebuddy.streamlit.app"

[plaid]
client_id = "your-plaid-client-id"
secret = "your-plaid-production-secret"
environment = "production"
country_codes = ["US"]
# A long random string. Keep it stable: changing it makes stored Plaid tokens unreadable.
token_encryption_key = "replace-with-a-separate-stable-random-secret"
```

Notes:

- **`FINANCEBUDDY_ENV = "production"`** turns on the startup checks in `src/services/production.py` (HTTPS-only URLs, rejects Supabase `service_role` keys, requires Plaid values). It must be at the **top** of the secrets, above any `[section]`; a line placed after `[supabase]` or `[plaid]` belongs to that section and is ignored.
- **`public_app_url`** must be a public `https://` origin. `localhost` is rejected in production mode.
- **`token_encryption_key`**: copy the existing value from Render (**Dashboard → financebuddy → Environment → `PLAID_TOKEN_ENCRYPTION_KEY`**). Render generated it for you, and existing bank connections were encrypted with it; a new key makes them unreadable and users would have to relink their banks. Only for a brand-new setup, generate one:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```
- Use the Supabase **publishable** key only. Never paste a `service_role` or `sb_secret_...` key; the app refuses to start with one.

## 4. Update Supabase

In the Supabase dashboard, go to **Authentication → URL Configuration**:

- Set **Site URL** to `https://financebuddy.streamlit.app` (your real URL).
- Add the same URL to **Redirect URLs**.

Without this, email confirmation and password-reset links will point at the old Render URL or be rejected.

## 5. Verify

- [ ] The app loads and shows the login screen (not the "production configuration is incomplete" error).
- [ ] Sign up or log in works, and the confirmation email links back to the new URL.
- [ ] The custom fonts and theme load (they are served from `static/fonts/`, enabled by `enableStaticServing` in `.streamlit/config.toml`).
- [ ] Linking a bank through Plaid works.
- [ ] Data is the same as before, since it lives in Supabase and not on the host.

If the app shows a configuration error, it lists the missing field names (never the values). Fix them under **App settings → Secrets**; the app restarts automatically.

## Limits and behavior to know

- **Sleeping**: apps with no traffic for about 12 hours go to sleep, and the next visit shows a "wake up" button. Waking is much faster than Render's free-tier cold start, but not instant.
- **Resources**: about 2.7 GB RAM and shared CPU. This is enough for FinanceBuddy but not for heavy data crunching.
- **Ephemeral disk**: anything written to local disk (e.g. `data/finance.db`, `data/users/`) is lost on restart or redeploy. This is fine as long as all persistent data is in Supabase. The `data/*.db` files are gitignored and will not be deployed.
- **Plaid OAuth**: the embedded Link flow in this release does not support OAuth redirects, so OAuth-only institutions will not work on any host.
- **Public URL**: a free app is reachable by anyone with the link. Access control is your Supabase login. For private repos you can also restrict viewers under **Share** in the dashboard.
- **Ports and health checks**: not needed. `PORT`, `HEALTHCHECK`, and `/_stcore/health` from the Docker setup are handled by the platform.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Build fails installing packages | Check the build log. Make sure **Python version** is 3.12; pins like `pandas==3.0.6` need a recent Python. |
| "Missing required environment variables" | You are running `scripts/start.py`. Make sure **Main file path** is `app.py`. |
| "production configuration is incomplete" | A secret is missing or invalid. Compare against section 3. Common causes: `FINANCEBUDDY_ENV` placed under a `[section]`, or a non-HTTPS `public_app_url`. |
| Emails link to the wrong site | Update Supabase **Site URL** and **Redirect URLs** (section 4). |
| Fonts fall back to system fonts | Confirm `static/fonts/*.woff2` are committed and `enableStaticServing = true` is in `.streamlit/config.toml`. |
| Plaid tokens stop working | `token_encryption_key` differs from the one used to encrypt them. Restore the original value. |

## Cleaning up Render

Before deleting anything, copy `PLAID_TOKEN_ENCRYPTION_KEY` out of the Render environment (see section 3). Once the new app works, delete the Render service in the Render dashboard. `render.yaml` has been removed from the repo; `Dockerfile` and `scripts/start.py` remain for self-hosting.
