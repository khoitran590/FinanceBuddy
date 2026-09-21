from __future__ import annotations

import streamlit as st


_PLAID_LINK_HTML = """
<div class="plaid-link-shell">
  <button id="plaid-link-button" type="button" disabled>Loading secure connection…</button>
  <p id="plaid-link-status" role="status" aria-live="polite">
    Plaid opens a secure window where you choose your institution and accounts.
  </p>
</div>
"""

_PLAID_LINK_CSS = """
.plaid-link-shell { font-family: var(--st-font); }
button {
  appearance: none; border: 0; border-radius: .55rem; cursor: pointer;
  padding: .62rem 1rem; font: 600 1rem var(--st-font);
  color: white; background: var(--st-primary-color);
}
button:hover:not(:disabled) { filter: brightness(.94); }
button:focus-visible { outline: 3px solid color-mix(in srgb, var(--st-primary-color), white 45%); }
button:disabled { cursor: wait; opacity: .65; }
p { color: var(--st-secondary-text-color); font-size: .88rem; margin: .45rem 0 0; }
"""

_PLAID_LINK_JS = """
export default function({ parentElement, data, setTriggerValue }) {
  const button = parentElement.querySelector("#plaid-link-button");
  const status = parentElement.querySelector("#plaid-link-status");
  let handler = null;
  let disposed = false;

  const initialize = () => {
    if (disposed || !window.Plaid) return;
    handler = window.Plaid.create({
      token: data.link_token,
      onSuccess: (publicToken, metadata) => {
        button.disabled = true;
        button.textContent = "Finishing connection…";
        status.textContent = "Securely exchanging the one-time token with FinanceBuddy.";
        setTriggerValue("success", { public_token: publicToken, metadata });
      },
      onExit: (error, metadata) => {
        if (error) {
          status.textContent = error.display_message || error.error_message || "Connection was not completed.";
          setTriggerValue("error", {
            error_code: error.error_code || "LINK_EXIT",
            message: status.textContent,
            metadata,
          });
        } else {
          status.textContent = "Connection canceled. You can try again whenever you're ready.";
        }
      },
      onLoad: () => {
        button.disabled = false;
        button.textContent = data.button_label || "Connect a bank";
        status.textContent = "Your bank credentials are handled by Plaid and are never shown to FinanceBuddy.";
      },
    });
    button.onclick = () => handler.open();
  };

  if (window.Plaid) {
    initialize();
  } else {
    let script = document.querySelector('script[data-financebuddy-plaid-link]');
    if (!script) {
      script = document.createElement("script");
      script.src = "https://cdn.plaid.com/link/v2/stable/link-initialize.js";
      script.dataset.financebuddyPlaidLink = "true";
      document.head.appendChild(script);
    }
    script.addEventListener("load", initialize, { once: true });
    script.addEventListener("error", () => {
      button.textContent = "Plaid could not load";
      status.textContent = "Check your network connection and reload this page.";
      setTriggerValue("error", { error_code: "SCRIPT_LOAD", message: status.textContent });
    }, { once: true });
  }

  return () => {
    disposed = true;
    if (handler) handler.destroy();
  };
}
"""


plaid_link_component = st.components.v2.component(
    "financebuddy_plaid_link",
    html=_PLAID_LINK_HTML,
    css=_PLAID_LINK_CSS,
    js=_PLAID_LINK_JS,
)


def _handle_component_event() -> None:
    """Register Plaid's one-time trigger names with Streamlit."""


def render_plaid_link(link_token: str, button_label: str = "Connect a bank", key: str = "plaid_link"):
    """Render Plaid Link and return success/error trigger values to Python."""
    return plaid_link_component(
        key=key,
        data={"link_token": link_token, "button_label": button_label},
        on_success_change=_handle_component_event,
        on_error_change=_handle_component_event,
    )
