"""Website helpers (pure functions) — no database needed."""
from glorybot.web import auth
from glorybot.web.app import iso, plain, safe_html


def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse 9")
    assert auth.verify_password(h, "correct horse 9")
    assert not auth.verify_password(h, "wrong")
    assert not auth.verify_password("garbage", "x")


def test_username_rules():
    assert auth.valid_username("anil_25")
    assert not auth.valid_username("ab")
    assert not auth.valid_username("has space")
    assert not auth.valid_username("x" * 21)


def test_safe_html_escapes_everything_but_basic_tags():
    out = safe_html("<b>Hi</b> <script>alert(1)</script> <i>x</i>\nline")
    assert "<b>Hi</b>" in out and "<i>x</i>" in out
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<br>" in out
    assert plain("<b>a</b> b") == "a b"


def test_iso_is_utc_z():
    from datetime import datetime
    assert iso(datetime(2026, 10, 12, 1, 2, 3)).endswith("Z")
    assert iso(None) is None
