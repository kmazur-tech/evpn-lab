"""Deterministic SHA-512 login hash derivation.

Reads JUNOS_LOGIN_PASSWORD + JUNOS_LOGIN_SALT from env, runs them
through passlib's pure-Python sha512_crypt backend with rounds=5000,
returns the hash. Hard-fails if either env var is missing - this
function NEVER returns a placeholder.

History: an earlier version had a placeholder fallback that got
rendered into a real config and committed to all 4 lab devices,
locking out SSH. The hard-fail here is the postmortem fix; the
on-disk deploy guard in deploy.py is the second layer.
"""

import os

from passlib.hash import sha512_crypt

# stdlib `crypt` is deprecated in Python 3.13 and removed in 3.14.
# passlib.hash.sha512_crypt with rounds=5000 produces byte-identical
# output to glibc crypt() for `$6$<salt>$` inputs - verified against
# the lab's existing committed hashes. SHA-512 crypt's default rounds
# (no `$rounds=` prefix in the salt) is 5000.
#
# Force the pure-Python backend so passlib does NOT use stdlib `crypt`
# at runtime. (passlib's os_crypt module still imports it at module-
# load time, which we silence via pytest filterwarnings until 3.13
# ships and passlib drops the os_crypt backend entirely.)
sha512_crypt.set_backend("builtin")
SHA512_CRYPT_ROUNDS = 5000


def derive_login_hash() -> str:
    """Compute the deterministic SHA-512 crypt hash from env-supplied
    plaintext + fixed salt. Used for both root-authentication and the
    admin user (same lab plaintext).

    Env contract:
      JUNOS_LOGIN_PASSWORD = "<plaintext>"
      JUNOS_LOGIN_SALT     = "$6$<salt>$"     (full crypt-format prefix)
    """
    plaintext = os.environ.get("JUNOS_LOGIN_PASSWORD")
    salt_full = os.environ.get("JUNOS_LOGIN_SALT")
    if not plaintext or not salt_full:
        raise RuntimeError(
            "JUNOS_LOGIN_PASSWORD and JUNOS_LOGIN_SALT must be set "
            "(see evpn-lab-env/env.sh). Refusing to render system "
            "stanza without real credential material."
        )
    # Parse `$6$<salt>$` -> "<salt>". passlib takes the salt as a bare
    # string (no $ delimiters) and prepends the crypt format on output.
    parts = salt_full.split("$")
    if len(parts) < 4 or parts[1] != "6":
        raise RuntimeError(
            f"JUNOS_LOGIN_SALT must be in `$6$<salt>$` format; got "
            f"{salt_full!r}"
        )
    salt = parts[2]
    return sha512_crypt.using(salt=salt, rounds=SHA512_CRYPT_ROUNDS).hash(plaintext)
