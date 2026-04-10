"""Tests for normalize() - the regression-diff normalizer.

These rules apply ONLY to the regression diff comparison, never to
the file written to disk. The deploy guard
(see test_deploy_guard.py) is the independent on-disk safety net
that catches placeholder hashes the normalizer would otherwise mask.

If you change normalize(), update these tests AND verify the deploy
guard still rejects every scenario in test_deploy_guard.py.
"""
from deploy import normalize


def test_strip_last_changed_line():
    text = "## Last changed: 2026-04-05 18:13:32 UTC\nversion 23.2R1.14;\nsystem {\n}\n"
    out = normalize(text)
    assert "Last changed" not in out
    assert "system {" in out


def test_strip_version_line():
    text = "version 23.2R1.14;\nsystem {\n}\n"
    out = normalize(text)
    assert "version" not in out
    assert "system {" in out


def test_mask_encrypted_password():
    """Salted hashes have noise content; the regression gate ignores it.
    The on-disk guard validates real shape independently."""
    text = 'encrypted-password "$6$evpnlab1$xyz/long/hash/here";'
    out = normalize(text)
    assert out == 'encrypted-password "<HASH>";'


def test_mask_encrypted_password_preserves_surrounding():
    """Mask must not eat adjacent config lines."""
    text = """root-authentication {
    encrypted-password "$6$saltsalt$hashhash";
}
"""
    out = normalize(text)
    assert "root-authentication" in out
    assert "<HASH>" in out
    assert "$6$saltsalt$hashhash" not in out


def test_normalize_idempotent():
    """Running normalize twice yields the same result."""
    text = '## Last changed: foo\nversion bar;\nencrypted-password "$6$x$y";\n'
    once = normalize(text)
    twice = normalize(once)
    assert once == twice


def test_normalize_does_not_touch_non_secret_fields():
    """router-id, hostnames, anything else - left alone."""
    text = "router-id 10.1.0.1;\nhost-name dc1-spine1;\n"
    out = normalize(text)
    assert out == text


def test_normalize_handles_empty():
    assert normalize("") == ""


def test_normalize_two_passwords_same_line_treatment():
    """Both root and admin hashes in one config -> both masked."""
    text = """root-authentication {
    encrypted-password "$6$rootSALT$roothash";
}
login {
    user admin {
        authentication {
            encrypted-password "$6$adminSALT$adminhash";
        }
    }
}
"""
    out = normalize(text)
    assert out.count('<HASH>') == 2
    assert "rootSALT" not in out
    assert "adminSALT" not in out
