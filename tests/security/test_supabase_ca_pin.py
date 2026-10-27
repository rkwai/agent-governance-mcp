from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

PINNED_CA = Path("certs/supabase-prod-ca-2021.crt")
PINNED_CHECKSUM = Path("certs/supabase-prod-ca-2021.crt.sha256")
PINNED_SHA256 = "700723581420dd1ac98fd7e9ac529f0ef210eadcaf87fc868a3ad7d114c2f3b7"
PINNED_FINGERPRINT = (
    "80:70:25:AD:50:D4:ED:21:9D:2C:9C:7D:29:9C:00:4F:"
    "82:4E:B0:0C:F7:F6:5A:FE:F6:07:D0:7B:72:E6:CA:FA"
)
SUBJECT_CN = "Supabase Root 2021 CA"


def test_pinned_supabase_ca_matches_published_checksum_and_identity() -> None:
    pem = PINNED_CA.read_bytes()
    assert hashlib.sha256(pem).hexdigest() == PINNED_SHA256
    recorded = PINNED_CHECKSUM.read_text(encoding="ascii")
    assert recorded.split()[0] == PINNED_SHA256
    fingerprint = subprocess.check_output(
        ["openssl", "x509", "-in", str(PINNED_CA), "-noout", "-fingerprint", "-sha256"],
        text=True,
    )
    assert PINNED_FINGERPRINT in fingerprint.upper()
    subject = subprocess.check_output(
        ["openssl", "x509", "-in", str(PINNED_CA), "-noout", "-subject"],
        text=True,
    )
    assert SUBJECT_CN in subject
    completed = subprocess.run(
        ["sha256sum", "-c", PINNED_CHECKSUM.name],
        cwd=PINNED_CA.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "OK" in completed.stdout


def test_system_ca_bundle_does_not_include_supabase_root() -> None:
    bundle = Path("/etc/ssl/certs/ca-certificates.crt")
    if not bundle.is_file():
        pytest.skip("system CA bundle is not installed")
    text = bundle.read_text(encoding="utf-8", errors="replace")
    assert SUBJECT_CN not in text


def test_container_ci_imports_pinned_pem_instead_of_grepping_plaintext_cn() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert 'grep -q "Supabase Root 2021 CA" /etc/ssl/certs/ca-certificates.crt' not in workflow
    assert "payload_line" in workflow
    assert "grep -F -q" in workflow
    assert "/usr/local/share/ca-certificates/supabase-prod-ca-2021.crt" in workflow
