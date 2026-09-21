import hashlib
import os

import pytest

from src.services.security import AuthenticationError, authenticated_user_id, user_database_path


def test_oidc_identity_maps_to_stable_non_pii_database(tmp_path):
    claims = {"iss": "https://identity.example/", "sub": "provider-user-123", "email": "user@example.com"}

    path = user_database_path(claims, str(tmp_path / "users"))

    expected = hashlib.sha256(
        b"https://identity.example/\x00provider-user-123"
    ).hexdigest()
    assert path.endswith(f"{expected}.db")
    assert "user@example.com" not in path
    if os.name != "nt":
        assert oct(os.stat(tmp_path / "users").st_mode & 0o777) == "0o700"


def test_oidc_identity_requires_subject():
    with pytest.raises(AuthenticationError):
        authenticated_user_id({"iss": "https://identity.example/"})


def test_different_oidc_subjects_cannot_share_database_path(tmp_path):
    first = user_database_path(
        {"iss": "https://identity.example/", "sub": "user-one"}, str(tmp_path / "users")
    )
    second = user_database_path(
        {"iss": "https://identity.example/", "sub": "user-two"}, str(tmp_path / "users")
    )

    assert first != second
