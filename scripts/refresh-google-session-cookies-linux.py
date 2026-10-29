#!/usr/bin/env python3
"""Refresh OpenMessage Google account cookies from a local Chrome profile.

This is intentionally Linux-specific. It updates only auth_data.cookies in
OpenMessage's session.json and never prints cookie values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


REQUIRED_COOKIES = {
    (".google.com", "SID"),
    (".google.com", "HSID"),
    (".google.com", "SSID"),
    (".google.com", "APISID"),
    (".google.com", "SAPISID"),
}
HOST_PRIORITY = {
    "messages.google.com": 0,
    ".google.com": 1,
    "accounts.google.com": 2,
}

# Chromium uses this fixed secret when it is launched with
# --password-store=basic. Prefer KWallet, but keep this as a narrowly gated
# fallback so a temporary wallet outage does not strand otherwise valid
# cookies. The loader still requires all five exact .google.com account
# cookies to decrypt before it will update the OpenMessage session.
LINUX_BASIC_STORAGE_SECRET = b"peanuts"


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        default=os.environ.get(
            "OPENMESSAGE_CHROME_PROFILE",
            str(home / ".config/google-chrome/Default"),
        ),
        help="Chrome profile directory containing Network/Cookies",
    )
    parser.add_argument(
        "--session",
        default=os.environ.get(
            "OPENMESSAGE_SESSION_PATH",
            str(home / ".local/share/openmessage/session.json"),
        ),
        help="OpenMessage session.json path",
    )
    parser.add_argument(
        "--wallet",
        default=os.environ.get("OPENMESSAGE_KWALLET", "kdewallet"),
        help="KWallet wallet name",
    )
    parser.add_argument(
        "--wallet-folder",
        default=os.environ.get("OPENMESSAGE_KWALLET_FOLDER", "Chrome Keys"),
        help="KWallet folder containing Chrome Safe Storage",
    )
    parser.add_argument(
        "--wallet-entry",
        default=os.environ.get("OPENMESSAGE_KWALLET_ENTRY", "Chrome Safe Storage"),
        help="KWallet password entry name",
    )
    parser.add_argument("--quiet", action="store_true", help="only print errors")
    parser.add_argument("--no-backup", action="store_true", help="do not write a session backup")
    return parser.parse_args()


def chrome_safe_storage_secret(args: argparse.Namespace) -> bytes:
    value = subprocess.check_output(
        [
            "kwallet-query",
            "--folder",
            args.wallet_folder,
            "--read-password",
            args.wallet_entry,
            args.wallet,
        ],
        text=False,
    ).rstrip(b"\n")
    if not value:
        raise RuntimeError("Chrome Safe Storage key was empty")
    return value


def derive_key(secret: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", secret, b"saltysalt", iterations, 16)


def decrypt_cookie(encrypted_value: bytes, host: str, key: bytes) -> str | None:
    if not encrypted_value:
        return None
    encrypted_value = bytes(encrypted_value)
    if not (encrypted_value.startswith(b"v10") or encrypted_value.startswith(b"v11")):
        return None

    decryptor = Cipher(
        algorithms.AES(key),
        modes.CBC(b" " * 16),
        backend=default_backend(),
    ).decryptor()
    padded = decryptor.update(encrypted_value[3:]) + decryptor.finalize()
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16 or padded[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("invalid cookie padding")

    plain = padded[:-pad_len]
    host_hash = hashlib.sha256(host.encode()).digest()
    if plain.startswith(host_hash):
        plain = plain[32:]
    return plain.decode("utf-8")


def chrome_safe_storage_candidates(
    args: argparse.Namespace,
) -> tuple[list[tuple[str, bytes]], Exception | None]:
    candidates: list[tuple[str, bytes]] = []
    wallet_error = None
    try:
        candidates.append(("kwallet", chrome_safe_storage_secret(args)))
    except Exception as exc:
        wallet_error = exc

    if not any(secret == LINUX_BASIC_STORAGE_SECRET for _name, secret in candidates):
        candidates.append(("basic", LINUX_BASIC_STORAGE_SECRET))
    return candidates, wallet_error


def load_chrome_cookies(
    profile: Path,
    secret_candidates: list[tuple[str, bytes]],
) -> tuple[dict[str, str], str, str, int, int]:
    cookie_db = profile / "Network" / "Cookies"
    if not cookie_db.exists():
        raise FileNotFoundError(f"Chrome cookie DB not found: {cookie_db}")

    # Read Chrome's live WAL so a refresh cannot replace a rotated OpenMessage
    # session with an older cookie snapshot. Expired persistent rows are also
    # excluded; session cookies have expires_utc=0 and remain eligible.
    connection = sqlite3.connect(f"file:{cookie_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            select host_key, name, encrypted_value, value
            from cookies
            where (host_key in ('.google.com','messages.google.com','accounts.google.com')
                   or host_key like '%.google.com')
              and (expires_utc = 0
                   or expires_utc > (cast(strftime('%s','now') as integer) + 11644473600) * 1000000)
            order by host_key, name
            """
        ).fetchall()
    finally:
        connection.close()

    # A Chrome profile can legitimately contain multiple Linux encryption
    # schemes at once (for example v10/basic and v11/KWallet after changing
    # password-store backends). Pick the best key independently for each
    # on-disk scheme instead of forcing every cookie through one backend.
    key_modes = [
        (backend, iterations, derive_key(secret, iterations))
        for backend, secret in secret_candidates
        for iterations in (1, 1003)
    ]
    encrypted_schemes = {
        bytes(encrypted_value[:3])
        for _host, _name, encrypted_value, plain_value in rows
        if not plain_value
        and encrypted_value
        and bytes(encrypted_value[:3]) in (b"v10", b"v11")
    }
    selected_modes: dict[bytes, tuple[str, int, bytes]] = {}
    for scheme in encrypted_schemes:
        best_mode: tuple[int, int, str, int, bytes] | None = None
        for mode_index, (backend, iterations, key) in enumerate(key_modes):
            decrypted = 0
            for host, _name, encrypted_value, plain_value in rows:
                if plain_value or not encrypted_value or bytes(encrypted_value[:3]) != scheme:
                    continue
                try:
                    if decrypt_cookie(encrypted_value, host, key) is not None:
                        decrypted += 1
                except Exception:
                    pass
            candidate = (decrypted, -mode_index, backend, iterations, key)
            if best_mode is None or candidate[:2] > best_mode[:2]:
                best_mode = candidate
        if best_mode is not None and best_mode[0] > 0:
            _decrypted, _priority, backend, iterations, key = best_mode
            selected_modes[scheme] = (backend, iterations, key)

    values: dict[str, tuple[str, str]] = {}
    ok = 0
    failed = 0
    used_modes: list[tuple[str, int]] = []
    for host, name, encrypted_value, plain_value in rows:
        value = plain_value or None
        if not value and encrypted_value:
            selected = selected_modes.get(bytes(encrypted_value[:3]))
            if selected is None:
                failed += 1
                continue
            backend, iterations, key = selected
            try:
                value = decrypt_cookie(encrypted_value, host, key)
            except Exception:
                failed += 1
                continue
            if (backend, iterations) not in used_modes:
                used_modes.append((backend, iterations))
        if value is None:
            continue
        ok += 1
        previous = values.get(name)
        if previous is None or HOST_PRIORITY.get(host, 10) < HOST_PRIORITY.get(previous[0], 10):
            values[name] = (host, value)

    present = {
        (host, name)
        for name, (host, _value) in values.items()
        if (host, name) in REQUIRED_COOKIES
    }
    if not values:
        raise RuntimeError("no Google cookies found in Chrome profile")
    missing = sorted(REQUIRED_COOKIES - present)
    if missing:
        missing_text = ", ".join(f"{host}:{name}" for host, name in missing)
        raise RuntimeError(f"missing required cookies: {missing_text}")

    backends = "+".join(dict.fromkeys(backend for backend, _iterations in used_modes))
    iterations = "+".join(str(value) for value in dict.fromkeys(value for _backend, value in used_modes))
    return {name: value for name, (_host, value) in values.items()}, backends, iterations, ok, failed


def update_session(session_path: Path, cookies: dict[str, str], backup: bool) -> Path | None:
    if not session_path.exists():
        raise FileNotFoundError(f"OpenMessage session not found: {session_path}")

    backup_path = None
    if backup:
        backup_path = session_path.with_name(f"session.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(session_path, backup_path)

    data = json.loads(session_path.read_text())
    data.setdefault("auth_data", {})["cookies"] = cookies

    fd, tmp_name = tempfile.mkstemp(
        prefix=".session-cookie-refresh-", suffix=".json", dir=session_path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as tmp_file:
            os.fchmod(tmp_file.fileno(), 0o600)
            tmp_file.write(json.dumps(data, separators=(",", ":")) + "\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp, session_path)
        dir_fd = os.open(session_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return backup_path


def main() -> int:
    args = parse_args()
    wallet_error = None
    try:
        secret_candidates, wallet_error = chrome_safe_storage_candidates(args)
        cookies, backend, iterations, decrypted, failed = load_chrome_cookies(
            Path(args.profile), secret_candidates
        )
        backup_path = update_session(Path(args.session), cookies, backup=not args.no_backup)
    except Exception as exc:
        detail = f"refresh_google_session_cookies: {exc}"
        if wallet_error is not None:
            detail += f" (KWallet unavailable: {wallet_error})"
        print(detail, file=sys.stderr)
        return 1

    if not args.quiet:
        print("session_cookie_refresh_ok")
        if backup_path:
            print(f"backup: {backup_path}")
        print(f"cookies_written: {len(cookies)}")
        print(f"decrypt_backend: {backend}")
        print(f"decrypt_iterations: {iterations}")
        print(f"decrypted_cookies: {decrypted}")
        print(f"decrypt_failures: {failed}")
        print("required_present: APISID,HSID,SAPISID,SID,SSID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
