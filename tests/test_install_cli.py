from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "ci"))

import install_cli


def test_normalize_arch_maps_release_asset_names() -> None:
    assert install_cli.normalize_arch("AMD64") == "x86_64"
    assert install_cli.normalize_arch("x86_64") == "x86_64"
    assert install_cli.normalize_arch("arm64") == "aarch64"
    assert install_cli.normalize_arch("aarch64") == "aarch64"


def test_normalize_arch_rejects_unknown_machine() -> None:
    with pytest.raises(RuntimeError):
        install_cli.normalize_arch("mips")


def test_maapicli_asset_name_matches_release_assets() -> None:
    assert (
        install_cli.maapicli_asset_name("win32", "x86_64", "v5.14.3")
        == "MAA-MaaPiCli-windows-x86_64-v5.14.3.zip"
    )
    assert (
        install_cli.maapicli_asset_name("linux", "aarch64", "v5.14.3")
        == "MAA-MaaPiCli-linux-aarch64-v5.14.3.zip"
    )
    assert (
        install_cli.maapicli_asset_name("darwin", "arm64", "v5.14.3")
        == "MAA-MaaPiCli-macos-aarch64-v5.14.3.zip"
    )


def test_maapicli_asset_os_rejects_unknown_platform() -> None:
    with pytest.raises(RuntimeError):
        install_cli.maapicli_asset_os("freebsd")


def test_cli_binary_names_per_platform() -> None:
    assert install_cli.cli_binary_name("win32") == "M9A.exe"
    assert install_cli.cli_binary_name("linux") == "M9A"
    assert install_cli.maapicli_binary_name("win32") == "MaaPiCli.exe"
    assert install_cli.maapicli_binary_name("darwin") == "MaaPiCli"


def test_resolve_maapicli_tag_prefers_explicit_tag(tmp_path: Path) -> None:
    # An explicit tag must not reach the releases API.
    assert install_cli.resolve_maapicli_tag("v5.14.3") == "v5.14.3"


def test_maapicli_binary_path_finds_bin_layout(tmp_path: Path) -> None:
    binary = tmp_path / "bin" / "MaaPiCli.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"cli")

    assert install_cli.maapicli_binary_path(tmp_path, "win32") == binary


def test_maapicli_binary_path_fails_when_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        install_cli.maapicli_binary_path(tmp_path, "linux")


def test_install_maapicli_replaces_bundled_binary(tmp_path: Path) -> None:
    maapicli_dir = tmp_path / "maapicli"
    extracted = maapicli_dir / "bin" / "MaaPiCli.exe"
    extracted.parent.mkdir(parents=True)
    extracted.write_bytes(b"fixed cli")

    output_dir = tmp_path / "install-cli"
    output_dir.mkdir()
    (output_dir / "M9A.exe").write_bytes(b"stale m9a")
    (output_dir / "MaaPiCli.exe").write_bytes(b"bundled cli")

    target = install_cli.install_maapicli(output_dir, maapicli_dir, "win32")

    assert target == output_dir / "M9A.exe"
    assert target.read_bytes() == b"fixed cli"
    # The copy MaaFramework still bundles must not survive next to the entrypoint.
    assert not (output_dir / "MaaPiCli.exe").exists()


def test_subprocess_env_drops_release_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = install_cli.subprocess_env_without_credentials()

    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"


def test_assert_interface_parse_output_accepts_controller_prompt() -> None:
    install_cli.assert_interface_parse_output("### Select controller ###\n\n\t1. Controller.Adb\n")


def test_assert_interface_parse_output_rejects_schema_mismatch() -> None:
    with pytest.raises(RuntimeError, match="json is not an InterfaceData"):
        install_cli.assert_interface_parse_output(
            'json is not an InterfaceData [error_key=welcome] [json={"welcome":["a"]}]'
        )
    with pytest.raises(RuntimeError):
        install_cli.assert_interface_parse_output(
            "[ERR] Failed to parse interface.json\n\nPress Enter to continue...\n"
        )


def test_assert_interface_parse_output_rejects_missing_prompt() -> None:
    with pytest.raises(RuntimeError, match="controller prompt"):
        install_cli.assert_interface_parse_output("Loading...\n")
