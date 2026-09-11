"""Offline tests of first-install DLL integrity and interrupted downloads."""
import hashlib
import io
import urllib.error

import pytest

from terminal import ghostty_vt


PAYLOAD = b"test native artifact, never loaded"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
URL = "https://example.invalid/ghostty-vt.dll"


def test_matching_dll_is_reused_without_network(tmp_path, monkeypatch):
    path = tmp_path / "ghostty-vt.dll"
    path.write_bytes(PAYLOAD)

    def no_network(*args, **kwargs):
        pytest.fail("an already verified DLL must not trigger a download")

    monkeypatch.setattr(ghostty_vt.urllib.request, "urlopen", no_network)
    assert ghostty_vt.ensure_dll(str(path), URL, DIGEST) == str(path)


def test_first_install_creates_directory_and_verified_dll(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "ghostty-vt.dll"
    requests = []

    def download(url, timeout):
        requests.append((url, timeout))
        return io.BytesIO(PAYLOAD)

    monkeypatch.setattr(ghostty_vt.urllib.request, "urlopen", download)
    assert ghostty_vt.ensure_dll(str(path), URL, DIGEST, timeout=7) == str(path)
    assert requests == [(URL, 7)]
    assert path.read_bytes() == PAYLOAD
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("existing", [False, True])
def test_bad_download_never_replaces_target_or_leaves_temp_file(tmp_path, monkeypatch, existing):
    path = tmp_path / "ghostty-vt.dll"
    if existing:
        path.write_bytes(b"previous artifact")
    monkeypatch.setattr(
        ghostty_vt.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"corrupted"),
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        ghostty_vt.ensure_dll(str(path), URL, DIGEST)
    assert not list(tmp_path.glob("*.download"))
    if existing:
        assert path.read_bytes() == b"previous artifact"
    else:
        assert not path.exists()


def test_offline_install_cleans_temporary_download(tmp_path, monkeypatch):
    path = tmp_path / "ghostty-vt.dll"

    def offline(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(ghostty_vt.urllib.request, "urlopen", offline)
    with pytest.raises(urllib.error.URLError, match="offline"):
        ghostty_vt.ensure_dll(str(path), URL, DIGEST)
    assert not list(tmp_path.iterdir())


def test_permission_denied_temp_file_fails_fast_not_retries_forever(tmp_path, monkeypatch):
    """Regression test: ensure_dll must NOT use tempfile.mkstemp() for the
    download temp file. mkstemp's Windows-specific PermissionError handler
    retries as long as os.access(dir, os.W_OK) claims the directory is
    writable -- but os.access on Windows only reflects the
    FILE_ATTRIBUTE_READONLY bit, not real ACL deny rules, so an ACL-denied
    (but not attribute-readonly) package folder makes that check misreport
    "writable" and retry effectively forever, stalling the single-threaded
    Sublime plugin host instead of raising a catchable error (confirmed
    live in an isolated Sublime install: >90s stall, had to be force-killed).
    """
    path = tmp_path / "bin" / "ghostty-vt.dll"

    def denied_open(*args, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(ghostty_vt.os, "open", denied_open)
    monkeypatch.setattr(
        ghostty_vt.urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("must fail before ever touching the network"),
    )
    with pytest.raises(OSError, match="could not create a temp file"):
        ghostty_vt.ensure_dll(str(path), URL, DIGEST)
