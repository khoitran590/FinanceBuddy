from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_app_stops_at_authentication_boundary_without_exceptions():
    app_path = Path(__file__).parents[1] / "app.py"
    app = AppTest.from_file(app_path).run(timeout=20)

    assert not app.exception
    assert app.title[0].value == "💸 FinanceBuddy"
    messages = [item.value for item in app.error] + [item.value for item in app.subheader]
    assert any(
        expected in message
        for expected in (
            "Account security must be configured",
            "Your finances stay private to your account",
        )
        for message in messages
    )


def test_plaid_link_component_mounts_without_invalid_defaults():
    app = AppTest.from_string(
        """
import importlib
import src.ui.plaid_link as plaid_link
plaid_link = importlib.reload(plaid_link)
plaid_link.render_plaid_link("link-production-test-token")
"""
    ).run(timeout=20)

    assert not app.exception
