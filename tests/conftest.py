"""Shared fixtures.

Most tests were written against the Chinese interface, so they run in Chinese;
tests of the default (English) interface switch language explicitly.
"""

import pytest

from nori.i18n import set_language


@pytest.fixture(autouse=True)
def chinese_interface(monkeypatch):
    monkeypatch.setenv("NORI_LANG", "zh")  # for code paths that go through main()
    set_language("zh")
    yield
    set_language("en")
