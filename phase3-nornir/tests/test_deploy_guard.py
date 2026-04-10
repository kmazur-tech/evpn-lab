"""Tests for assert_safe_to_deploy() - the on-disk pre-deploy guard.

This is the safety net whose absence caused the placeholder-hash
credential lockout. NAPALM compare_config silently passes placeholder
hashes because Junos masks SECRET-DATA fields in diffs. The on-disk
guard scans the rendered file independently before any NAPALM call
and rejects sentinel strings or malformed hash shapes.

Each scenario here corresponds to a real failure mode that should
NEVER reach a device.
"""
import pytest

from deploy import assert_safe_to_deploy, DEPLOY_SENTINELS


# Minimal valid system stanza fragment with a real-shaped SHA-512 hash.
# 86 chars after the second `$` is the SHA-512 crypt format.
VALID_HASH = "$6$evpnlab1$x/0MmAitK3rDmZWPb.mNqW4YglzhbN5D0g0aGRtoWAaSUUMM1Om/FGfcPT3nmCP26uu2srtayTb46F1Id6Z/x."

CLEAN_CONFIG = f"""system {{
    host-name dc1-spine1;
    root-authentication {{
        encrypted-password "{VALID_HASH}";
    }}
    login {{
        user admin {{
            authentication {{
                encrypted-password "{VALID_HASH}";
            }}
        }}
    }}
}}
"""


def test_clean_config_passes():
    """Real-shaped hash, no sentinels, no malformed lines -> OK."""
    assert_safe_to_deploy(CLEAN_CONFIG, "test-host")


@pytest.mark.parametrize("sentinel", DEPLOY_SENTINELS)
def test_sentinel_rejected(sentinel):
    """Every documented sentinel must be caught wherever it appears."""
    poisoned = CLEAN_CONFIG + f"\n# {sentinel} should never reach a device\n"
    with pytest.raises(RuntimeError, match=f"sentinel '{sentinel}'"):
        assert_safe_to_deploy(poisoned, "test-host")


def test_placeholder_in_hash_rejected():
    """The exact bug that caused the lockout: $6$PLACEHOLDER$render-time-only."""
    poisoned = CLEAN_CONFIG.replace(
        f'"{VALID_HASH}"',
        '"$6$PLACEHOLDER$render-time-only"',
        1,
    )
    with pytest.raises(RuntimeError, match="sentinel 'PLACEHOLDER'"):
        assert_safe_to_deploy(poisoned, "test-host")


def test_truncated_hash_rejected():
    """Hash that has $6$<salt>$ prefix but the digest is too short."""
    poisoned = CLEAN_CONFIG.replace(
        f'"{VALID_HASH}"',
        '"$6$evpnlab1$tooshort"',
        1,
    )
    with pytest.raises(RuntimeError, match="does not match valid SHA-512"):
        assert_safe_to_deploy(poisoned, "test-host")


def test_cleartext_password_rejected():
    """encrypted-password "TestLabPass1" - cleartext, no $6$ at all."""
    poisoned = CLEAN_CONFIG.replace(
        f'"{VALID_HASH}"',
        '"TestLabPass1"',
        1,
    )
    with pytest.raises(RuntimeError, match="does not match valid SHA-512"):
        assert_safe_to_deploy(poisoned, "test-host")


def test_md5_hash_rejected():
    """$1$ is MD5 crypt - not SHA-512, must be rejected."""
    md5 = "$1$abcd1234$abcdefghijklmnopqrstu/"  # 22 chars, MD5 shape
    poisoned = CLEAN_CONFIG.replace(
        f'"{VALID_HASH}"',
        f'"{md5}"',
        1,
    )
    with pytest.raises(RuntimeError, match="does not match valid SHA-512"):
        assert_safe_to_deploy(poisoned, "test-host")


def test_empty_password_rejected():
    """An empty `encrypted-password ""` line is malformed."""
    poisoned = CLEAN_CONFIG.replace(
        f'"{VALID_HASH}"',
        '""',
        1,
    )
    with pytest.raises(RuntimeError, match="does not match valid SHA-512"):
        assert_safe_to_deploy(poisoned, "test-host")


def test_host_name_in_error_message():
    """Error must identify which device failed - phased rollout depends on it."""
    poisoned = CLEAN_CONFIG.replace(VALID_HASH, "TestLabPass1", 1)
    with pytest.raises(RuntimeError, match="dc1-leaf2:"):
        assert_safe_to_deploy(poisoned, "dc1-leaf2")


def test_config_without_encrypted_password_passes():
    """Stanzas with no encrypted-password lines must not error
    (e.g. interfaces, routing-options stanzas rendered standalone)."""
    cfg = "interfaces {\n    ge-0/0/0 {\n        mtu 9192;\n    }\n}\n"
    assert_safe_to_deploy(cfg, "test-host")


def test_multiple_valid_hashes_pass():
    """Both root and admin hashes valid -> OK (the common case)."""
    assert_safe_to_deploy(CLEAN_CONFIG, "test-host")
