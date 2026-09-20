from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_app_renders_primary_navigation_without_exceptions():
    app_path = Path(__file__).parents[1] / "app.py"
    app = AppTest.from_file(app_path).run(timeout=20)

    assert not app.exception
    assert app.title[0].value == "💸 FinanceBuddy"
    labels = [tab.label for tab in app.tabs]
    for label in ["Overview", "Transactions", "Budgets & goals", "Compare", "Import & data"]:
        assert label in labels
