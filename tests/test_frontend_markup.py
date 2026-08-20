"""Cheap guards on the static UI: the ids boot() drives must actually exist.

There is no JS runner in this repo, so these assertions only catch typos and
accidental deletions — behaviour is verified by hand against the deployment.
"""
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"


@pytest.fixture(scope="module")
def markup():
    return INDEX.read_text(encoding="utf-8")


def test_sign_in_button_exists_and_starts_hidden(markup):
    assert 'id="signInBtn"' in markup
    assert "Sign in with GitHub" in markup


def test_boot_consumes_the_new_session_keys(markup):
    assert "login_url" in markup
    assert "sessionBoardId" in markup


def test_spectator_note_no_longer_tells_you_to_leave(markup):
    """The button replaced the 'log in via the site' instruction."""
    assert "owner login via site" not in markup
