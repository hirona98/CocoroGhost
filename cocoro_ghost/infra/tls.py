"""
TLS（自己署名）証明書の生成と検証。

目的:
    - LAN 内でも HTTPS を必須にするため、自己署名証明書を自動生成する。
    - 配布（PyInstaller）環境でも動くよう、openssl 外部コマンドに依存しない。

方針:
    - `config/tls/cert.pem` と `config/tls/key.pem` が無ければ生成する。
    - SAN には最低限 localhost/127.0.0.1 を入れる（可能なら ::1 も入れる）。
    - 追加で LAN IP を入れるかは任意（警告を完全に消すのは非ゴール）。
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional


logger = logging.getLogger(__name__)


def _get_tls_dir() -> Path:
    """TLSファイルの保存先ディレクトリ（config/tls）を返し、存在しなければ作成する。"""

    # --- config は exe 隣を既定にする（paths の方針に従う） ---
    from cocoro_ghost.infra.paths import get_config_dir

    tls_dir = (get_config_dir() / "tls").resolve()
    tls_dir.mkdir(parents=True, exist_ok=True)
    return tls_dir


def get_tls_cert_path() -> Path:
    """証明書（PEM）の保存パスを返す。"""

    return (_get_tls_dir() / "cert.pem").resolve()


def get_tls_key_path() -> Path:
    """秘密鍵（PEM）の保存パスを返す。"""

    return (_get_tls_dir() / "key.pem").resolve()


def _build_san_ips(extra_ips: Optional[Iterable[str]] = None) -> list[ipaddress._BaseAddress]:
    """SAN に入れる IP リストを作る。"""

    # --- 最低限（開発/ローカル用） ---
    ips: list[ipaddress._BaseAddress] = [
        ipaddress.ip_address("127.0.0.1"),
    ]

    # --- 可能なら IPv6 loopback ---
    try:
        ips.append(ipaddress.ip_address("::1"))
    except Exception:  # noqa: BLE001
        pass

    # --- 追加IP（LANなど） ---
    for raw in list(extra_ips or []):
        s = str(raw or "").strip()
        if not s:
            continue
        try:
            ips.append(ipaddress.ip_address(s))
        except Exception:  # noqa: BLE001
            continue

    # --- 重複除去 ---
    dedup: dict[str, ipaddress._BaseAddress] = {str(x): x for x in ips}
    return list(dedup.values())


def ensure_self_signed_tls_files(*, extra_san_ips: Optional[Iterable[str]] = None) -> tuple[Path, Path]:
    """
    自己署名TLS証明書（cert.pem/key.pem）が無ければ生成して返す。

    Returns:
        (cert_path, key_path)
    """

    cert_path = get_tls_cert_path()
    key_path = get_tls_key_path()

    # --- 既に揃っていれば何もしない ---
    if cert_path.exists() and key_path.exists():
        return (cert_path, key_path)

    # --- cryptography を遅延 import（起動経路の依存解析を安定させる） ---
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    # --- RSA 鍵を生成（互換性優先） ---
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # --- Subject/Issuer（自己署名なので同じ） ---
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "CocoroGhost"),
        ]
    )

    # --- SAN を構築 ---
    san = x509.SubjectAlternativeName(
        [
            # DNS: localhost はブラウザが見に行く可能性が高い
            x509.DNSName("localhost"),
            # IP: 127.0.0.1 / ::1 / 追加IP
            *[x509.IPAddress(ip) for ip in _build_san_ips(extra_san_ips)],
        ]
    )

    # --- 有効期限 ---
    now = datetime.now(timezone.utc)
    not_before = now - timedelta(minutes=1)
    not_after = now + timedelta(days=3650)

    # --- 証明書を構築 ---
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(san, critical=False)
        .sign(private_key, hashes.SHA256())
    )

    # --- PEM 書き出し（パスの親は既に作成済み） ---
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    key_path.write_bytes(key_pem)
    cert_path.write_bytes(cert_pem)

    logger.info("self-signed TLS files generated", extra={"cert_path": str(cert_path), "key_path": str(key_path)})
    return (cert_path, key_path)

