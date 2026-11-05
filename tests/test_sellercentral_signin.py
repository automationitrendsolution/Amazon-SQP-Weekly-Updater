"""Seller Central sign-in: Amazon's "Switch accounts" page (shown after a password change ends the session)."""
from __future__ import annotations

from app import sellercentral


class _Locator:
    def __init__(self, page, text):
        self.page, self.text = page, text
        self.first = self

    def count(self):
        if self.text == "#ap_email, #ap_password":
            return int(self.page.form)
        return sum(self.text.lower() in t.lower() for t in self.page.texts)

    def wait_for(self, state, timeout):
        if not self.count():
            raise TimeoutError(self.text)

    def click(self):
        self.page.clicked.append(self.text)


class _Page:
    def __init__(self, texts, form=False):
        self.texts, self.form, self.clicked = texts, form, []

    def get_by_text(self, text, exact=False):
        return _Locator(self, text)

    def locator(self, selector):
        return _Locator(self, selector)


def _no_wait(monkeypatch):
    monkeypatch.setattr(sellercentral, "_settle", lambda page, timeout_ms=0: None)


def test_the_saved_account_is_chosen_on_the_switch_accounts_page(monkeypatch):
    _no_wait(monkeypatch)
    page = _Page(["Switch accounts", "Deivanai", "Itrendsolution196@gmail.com", "Add account"])
    assert sellercentral._pick_saved_account(page, "itrendsolution196@gmail.com")
    assert page.clicked == ["itrendsolution196@gmail.com"]


def test_another_saved_account_means_add_account(monkeypatch):
    _no_wait(monkeypatch)
    page = _Page(["Switch accounts", "Someone Else", "other@example.com", "Add account"])
    assert sellercentral._pick_saved_account(page, "itrendsolution196@gmail.com")
    assert page.clicked == ["Add account"]


def test_a_sign_in_form_is_left_to_the_form_filling(monkeypatch):
    _no_wait(monkeypatch)
    assert not sellercentral._pick_saved_account(_Page(["Sign in"]), "a@b.com")
    page = _Page(["Switch accounts", "a@b.com"], form=True)  # password form with a "Switch accounts" link
    assert not sellercentral._pick_saved_account(page, "a@b.com") and page.clicked == []
