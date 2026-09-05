"""Runtime configuration shared by the frozen desktop analysis engine."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

TIKTOKEN_CACHE_KEY = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
TIKTOKEN_CACHE_SHA256 = (
    "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
)
_SSL_BUNDLE_NAME = "windows-ca-bundle.pem"


def tiktoken_cache_keys() -> tuple[str, str]:
    """Filenames tiktoken may use for cl100k_base across library versions."""
    return (TIKTOKEN_CACHE_KEY, TIKTOKEN_CACHE_SHA256)


def configure_runtime() -> dict[str, Path | None]:
    """Prepare offline tokenizer data and Windows TLS trust before imports."""
    return {
        "ssl_bundle": configure_windows_ssl_bundle(),
        "tiktoken_cache": configure_bundled_tiktoken_cache(),
    }


def configure_windows_ssl_bundle() -> Path | None:
    """Trust the Windows certificate store so corporate proxies can MITM TLS.

    Python requests/certifi rejects enterprise inspection CAs that Windows
    already trusts, which is what produces ``self-signed certificate in
    certificate chain`` on desktop startup.
    """
    if sys.platform != "win32":
        return None
    try:
        certificates = _windows_store_certificates()
        if not certificates:
            return None
        dest = _user_data_dir() / _SSL_BUNDLE_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_combined_ca_bundle(certificates), encoding="ascii")
    except OSError:
        return None
    dest_str = str(dest)
    os.environ["SSL_CERT_FILE"] = dest_str
    os.environ["REQUESTS_CA_BUNDLE"] = dest_str
    os.environ["CURL_CA_BUNDLE"] = dest_str
    return dest


def configure_bundled_tiktoken_cache() -> Path | None:
    """Point tiktoken at a verified encoding so it never downloads at import."""
    frozen = bool(getattr(sys, "frozen", False))
    if not frozen and "TIKTOKEN_CACHE_DIR" in os.environ:
        return None

    source = _find_verified_tiktoken_encoding()
    if source is None:
        if frozen:
            raise RuntimeError("Bundled tiktoken encoding is missing")
        return None

    payload = source.read_bytes()
    dest_dir = _user_tiktoken_cache_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    for key in tiktoken_cache_keys():
        dest = dest_dir / key
        if not _is_verified_encoding(dest):
            dest.write_bytes(payload)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(dest_dir)
    return dest_dir


def _user_data_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return root / "CodeInsight-AI"


def _user_tiktoken_cache_dir() -> Path:
    return _user_data_dir() / "tiktoken_cache"


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def _bundle_roots() -> list[Path]:
    if getattr(sys, "frozen", False):
        roots: list[Path] = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
        roots.append(Path(sys.executable).resolve().parent)
        return roots
    here = _package_dir()
    return [here, here.parent]


def _candidate_tiktoken_files() -> list[Path]:
    folders = ("tiktoken_cache", "tiktoken-cache")
    files: list[Path] = []
    seen: set[str] = set()
    for root in _bundle_roots():
        for folder in folders:
            for key in tiktoken_cache_keys():
                path = root / folder / key
                marker = str(path)
                if marker not in seen:
                    seen.add(marker)
                    files.append(path)
    return files


def _is_verified_encoding(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == TIKTOKEN_CACHE_SHA256


def _find_verified_tiktoken_encoding() -> Path | None:
    for path in _candidate_tiktoken_files():
        if _is_verified_encoding(path):
            return path
    if getattr(sys, "frozen", False):
        return None
    # Development: reuse a previously downloaded official cache if present.
    default_cache = Path(tempfile.gettempdir()) / "data-gym-cache"
    for key in tiktoken_cache_keys():
        candidate = default_cache / key
        if _is_verified_encoding(candidate):
            return candidate
    return None


def _combined_ca_bundle(certificates: list[bytes]) -> str:
    chunks: list[str] = []
    try:
        import certifi

        certifi_path = Path(certifi.where())
        if certifi_path.is_file():
            chunks.append(certifi_path.read_text(encoding="ascii", errors="ignore"))
    except (ImportError, OSError):
        pass
    for der in certificates:
        chunks.append(_der_to_pem(der))
    return "".join(chunks)


def _der_to_pem(der: bytes) -> str:
    import base64

    body = base64.encodebytes(der).decode("ascii")
    return f"-----BEGIN CERTIFICATE-----\n{body}-----END CERTIFICATE-----\n"


def _windows_store_certificates() -> list[bytes]:
    import ctypes
    from ctypes import wintypes

    class CERT_CONTEXT(ctypes.Structure):
        _fields_ = [
            ("dwCertEncodingType", wintypes.DWORD),
            ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
            ("cbCertEncoded", wintypes.DWORD),
            ("pCertInfo", ctypes.c_void_p),
            ("hCertStore", wintypes.HANDLE),
        ]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    cert_open_store = crypt32.CertOpenStore
    cert_open_store.restype = wintypes.HANDLE
    cert_open_store.argtypes = [
        wintypes.LPCVOID,
        wintypes.DWORD,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPCWSTR,
    ]
    cert_enum = crypt32.CertEnumCertificatesInStore
    cert_enum.restype = ctypes.POINTER(CERT_CONTEXT)
    cert_enum.argtypes = [wintypes.HANDLE, ctypes.POINTER(CERT_CONTEXT)]
    cert_close_store = crypt32.CertCloseStore
    cert_close_store.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    cert_store_prov_system_w = 10
    cert_system_store_current_user = 0x00010000
    cert_system_store_local_machine = 0x00020000
    cert_store_readonly_flag = 0x00008000
    stores = (
        cert_system_store_local_machine | cert_store_readonly_flag,
        cert_system_store_current_user | cert_store_readonly_flag,
    )

    certificates: list[bytes] = []
    seen: set[bytes] = set()
    for flags in stores:
        for name in ("ROOT", "CA"):
            store = cert_open_store(
                cert_store_prov_system_w,
                0,
                None,
                flags,
                name,
            )
            if not store:
                continue
            context = None
            try:
                while True:
                    context = cert_enum(store, context)
                    if not context:
                        break
                    encoded = context.contents
                    der = ctypes.string_at(encoded.pbCertEncoded, encoded.cbCertEncoded)
                    digest = hashlib.sha256(der).digest()
                    if digest not in seen:
                        seen.add(digest)
                        certificates.append(der)
            finally:
                cert_close_store(store, 0)
    return certificates
