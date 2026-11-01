from __future__ import annotations

import argparse
import json
import os
import platform as host_platform
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, cast

MAA_PICLI_REPO = "MaaXYZ/MaaPiCli"

# MaaPiCli release assets are named MAA-MaaPiCli-<os>-<arch>-<tag>.zip.
MAA_PICLI_ASSET_OS = {
    "win32": "windows",
    "windows": "windows",
    "linux": "linux",
    "darwin": "macos",
    "macos": "macos",
    "osx": "macos",
}

MAA_PICLI_ASSET_ARCH = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}

# MaaPiCli prints these when the interface tree does not match the ProjectInterface
# schema it was built against (a `welcome` array against a string-only field, say).
INTERFACE_PARSE_FAILURE_MARKERS = (
    "json is not an InterfaceData",
    "Failed to parse interface.json",
)

# The CLI resolves interface.json, every import and every option before asking for a
# controller, so the prompt is proof that the whole tree parsed.
CONTROLLER_PROMPT_MARKER = "Select controller"

CLI_SMOKE_TIMEOUT_SECONDS = 60


def strip_html_tags(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    return re.sub(r"<[^>]+>", "", text)


def strip_html_from_interface(obj: dict[str, Any] | list[Any]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "description" and isinstance(value, str):
                obj[key] = strip_html_tags(value)
            elif isinstance(value, (dict, list)):
                strip_html_from_interface(value)
    else:
        for item in obj:
            if isinstance(item, (dict, list)):
                strip_html_from_interface(item)


def release_agent_child_exec(platform: str) -> str:
    if platform.startswith("win"):
        return r"./python/python.exe"
    if (
        platform.startswith("darwin")
        or platform.startswith("macos")
        or platform.startswith("osx")
        or platform.startswith("linux")
    ):
        return r"./python/bin/python3"
    raise RuntimeError(f"Unsupported release platform: {platform}")


def iter_agent_configs(interface: dict[str, Any]) -> list[dict[str, Any]]:
    agent = interface.get("agent")
    if isinstance(agent, dict):
        return [cast(dict[str, Any], agent)]
    if isinstance(agent, list) and all(isinstance(item, dict) for item in agent):
        return cast(list[dict[str, Any]], agent)
    raise RuntimeError("interface.json must contain an agent object or object list")


def configure_release_agent(interface: dict[str, Any], platform: str) -> None:
    for agent in iter_agent_configs(interface):
        agent["child_exec"] = release_agent_child_exec(platform)
        # Every package ships its own interpreter with the Agent dependencies preinstalled,
        # so the Agent always starts through agent/main.py (no bootstrap, no runtime install).
        agent["child_args"] = ["-u", r"./agent/main.py"]
    assert_release_agent_config(interface)


def assert_release_agent_config(interface: dict[str, Any]) -> None:
    for agent in iter_agent_configs(interface):
        child_exec = agent.get("child_exec")
        child_args = agent.get("child_args", [])
        if child_exec == "uv" or (isinstance(child_args, list) and "uv" in child_args):
            raise RuntimeError("release interface.json must not launch Agent through uv")


def require_path(path: Path, label: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    return path


def copytree_clean(src: Path, dst: Path, ignore: shutil.IgnorePattern | None = None) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=ignore)


def configure_ocr_model(source_dir: Path) -> None:
    source = source_dir / "MaaCommonAssets" / "OCR" / "ppocr_v4" / "zh_cn"
    target = source_dir / "resource" / "base" / "model" / "ocr"
    if source.exists():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        print(f"Warning: OCR model source not found, skip configure: {source}")


def install_deps(deps_dir: Path, output_dir: Path) -> None:
    copytree_clean(
        require_path(deps_dir / "bin", "MaaFramework bin"),
        output_dir,
        shutil.ignore_patterns(
            "*MaaDbgControlUnit*",
            "*MaaThriftControlUnit*",
            "*MaaRpc*",
            "*MaaHttp*",
        ),
    )
    shutil.copytree(
        require_path(deps_dir / "share" / "MaaAgentBinary", "MaaAgentBinary"),
        output_dir / "MaaAgentBinary",
        dirs_exist_ok=True,
    )


def framework_libraries(platform: str) -> tuple[str, str]:
    if platform.startswith("win"):
        return ("MaaFramework.dll", "MaaAgentServer.dll")
    if platform.startswith(("darwin", "macos", "osx")):
        return ("libMaaFramework.dylib", "libMaaAgentServer.dylib")
    return ("libMaaFramework.so", "libMaaAgentServer.so")


def strip_agent_native_runtime(python_dir: Path, output_dir: Path, platform: str) -> None:
    """Drop the MaaFramework copy bundled in the `maa` wheel of the embedded interpreter.

    install_deps already flattens the same set of libraries at the package root, and the
    agent resolves them through its maafw_paths helper (the flat package-root candidate
    shipped with create-maa-project agent projects), so the wheel copy is pure dead weight
    (tens of MiB per package). Stripping only happens when both framework libraries are
    present at the package root; otherwise the agent would fall back to the wheel copy and
    crash at import time.
    """
    framework, agent_server = framework_libraries(platform)
    for name in (framework, agent_server):
        if not (output_dir / name).is_file():
            raise FileNotFoundError(
                f"MaaFramework library not found at package root, refusing to strip the wheel copy: {output_dir / name}"
            )

    if not python_dir.is_dir():
        print(f"Embedded Python not found, skip stripping: {python_dir}")
        return

    candidate_dirs = [python_dir / "Lib" / "site-packages" / "maa" / "bin"]
    candidate_dirs.extend(sorted(python_dir.glob("lib/python3.*/site-packages/maa/bin")))
    stripped = False
    for candidate in candidate_dirs:
        if candidate.is_dir():
            shutil.rmtree(candidate)
            print(f"Stripped duplicate MaaFW native runtime: {candidate}")
            stripped = True
    if not stripped:
        print("No bundled MaaFW native runtime found in the embedded Python.")


SHARED_RUNTIME_HELPER = Path("agent") / "maafw_paths.py"

SMOKE_SCRIPT = (
    "import sys; sys.path.insert(0, 'agent'); "
    "import maafw_paths; "
    "resolved = maafw_paths.ensure_maafw_binary_path(); "
    "assert resolved, 'the shared MaaFramework runtime was not resolved'; "
    "import loguru, maa, PIL, pytz, requests; "
    "import agent_runtime, custom; "
    "custom.register_all(); "
    "from maa.library import Library; "
    "print('embedded Python smoke passed:', Library.version(), Library.framework_libpath)"
)


def require_shared_runtime_support(source_dir: Path) -> None:
    """Refuse to strip the wheel copy when the M9A source cannot resolve the shared runtime.

    The Agent reaches the libraries flattened at the package root through its maafw_paths helper
    and MAAFW_BINARY_PATH. An older M9A ref has no such helper, so stripping the wheel copy would
    leave a package that only breaks on the user machine, at import time.
    """
    helper = require_path(source_dir / SHARED_RUNTIME_HELPER, "shared runtime helper")
    entrypoint = require_path(source_dir / "agent" / "main.py", "Agent entrypoint")
    if "ensure_maafw_binary_path" not in entrypoint.read_text(encoding="utf-8"):
        raise RuntimeError(f"Agent entrypoint does not resolve the shared runtime: {entrypoint}")
    print(f"Shared runtime helper: {helper}")


def smoke_packaged_agent(python_dir: Path, output_dir: Path, platform: str) -> None:
    """Run the packaged Agent once so a broken package cannot be released.

    The wheel copy is already gone from the interpreter by now, so this exercises what the device
    does: the Agent has to reach the framework libraries through MAAFW_BINARY_PATH.
    """
    interpreter = python_dir / ("python.exe" if platform.startswith("win") else "bin/python3")
    if not interpreter.is_file():
        raise FileNotFoundError(f"Embedded Python interpreter not found: {interpreter}")

    result = subprocess.run(
        [str(interpreter), "-I", "-c", SMOKE_SCRIPT],
        cwd=output_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Packaged Agent smoke failed: the embedded Python cannot load the shared MaaFramework "
            f"runtime from the package root.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
    print(result.stdout.strip())


def install_resource(source_dir: Path, output_dir: Path, version: str) -> None:
    configure_ocr_model(source_dir)
    shutil.copytree(require_path(source_dir / "resource", "resource"), output_dir / "resource")
    shutil.copytree(require_path(source_dir / "data", "data"), output_dir / "data")
    shutil.copytree(require_path(source_dir / "tasks", "tasks"), output_dir / "tasks")

    with open(require_path(source_dir / "interface.json", "interface.json"), encoding="utf-8") as f:
        interface = json.load(f)

    interface["version"] = version
    interface["title"] = f"M9A {version} | 亿韭韭韭小助手"
    strip_html_from_interface(interface)

    with open(output_dir / "interface.json", "w", encoding="utf-8") as f:
        json.dump(interface, f, ensure_ascii=False, indent=4)
        f.write("\n")

    for json_file in (output_dir / "tasks").rglob("*.json"):
        with open(json_file, encoding="utf-8") as f:
            data = json.load(f)
        strip_html_from_interface(data)
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.write("\n")


def install_chores(source_dir: Path, output_dir: Path) -> None:
    # requirements.txt is intentionally not shipped: the package interpreter already has
    # every dependency preinstalled.
    for name in ["README.md", "LICENSE", "CONTACT"]:
        source = source_dir / name
        if source.exists():
            shutil.copy2(source, output_dir / name)


def install_agent(source_dir: Path, output_dir: Path, platform: str) -> None:
    shutil.copytree(require_path(source_dir / "agent", "agent"), output_dir / "agent")

    interface_file = output_dir / "interface.json"
    with open(interface_file, encoding="utf-8") as f:
        interface = json.load(f)

    configure_release_agent(interface, platform)

    with open(interface_file, "w", encoding="utf-8") as f:
        json.dump(interface, f, ensure_ascii=False, indent=4)
        f.write("\n")


def install_manifest_cache(source_dir: Path, output_dir: Path) -> None:
    sys.path.insert(0, str(source_dir / "tools" / "ci"))
    try:
        from generate_manifest_cache import generate_manifest_cache

        success = generate_manifest_cache(output_dir / "data")
    except Exception as exc:
        print(f"Warning: manifest cache generation failed: {exc}")
        success = False

    if success:
        print("Manifest cache generated successfully.")
    else:
        print("Warning: users will do full manifest check on first run.")


def cli_binary_name(platform_name: str) -> str:
    return "M9A.exe" if platform_name.startswith("win") else "M9A"


def maapicli_binary_name(platform_name: str) -> str:
    return "MaaPiCli.exe" if platform_name.startswith("win") else "MaaPiCli"


def normalize_arch(machine: str) -> str:
    arch = MAA_PICLI_ASSET_ARCH.get(machine.strip().lower())
    if arch is None:
        raise RuntimeError(f"Unsupported MaaPiCli architecture: {machine}")
    return arch


def host_arch() -> str:
    machine = host_platform.machine()
    # Windows ARM64 shells can report AMD64 from platform.machine().
    if host_platform.system() == "Windows" and "ARM64" in os.environ.get("PROCESSOR_IDENTIFIER", ""):
        machine = "aarch64"
    return normalize_arch(machine)


def maapicli_asset_os(platform_name: str) -> str:
    asset_os = MAA_PICLI_ASSET_OS.get(platform_name)
    if asset_os is None:
        raise RuntimeError(f"Unsupported MaaPiCli release platform: {platform_name}")
    return asset_os


def maapicli_asset_name(platform_name: str, arch: str, tag: str) -> str:
    return f"MAA-MaaPiCli-{maapicli_asset_os(platform_name)}-{normalize_arch(arch)}-{tag}.zip"


def github_headers(accept: str) -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": "m9a-cli-packaging"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_json(url: str) -> Any:
    request = urllib.request.Request(url, headers=github_headers("application/vnd.github+json"))
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def resolve_maapicli_tag(tag: str) -> str:
    if tag:
        return tag
    release = github_json(f"https://api.github.com/repos/{MAA_PICLI_REPO}/releases/latest")
    resolved = release.get("tag_name") if isinstance(release, dict) else None
    if not isinstance(resolved, str) or not resolved:
        raise RuntimeError(f"No MaaPiCli release found in {MAA_PICLI_REPO}")
    return resolved


def maapicli_binary_path(root: Path, platform_name: str) -> Path:
    name = maapicli_binary_name(platform_name)
    for candidate in (root / "bin" / name, root / name):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"MaaPiCli binary not found under {root} (expected bin/{name})")


def download_maapicli(maapicli_dir: Path, platform_name: str, arch: str, tag: str) -> Path:
    resolved_tag = resolve_maapicli_tag(tag)
    asset = maapicli_asset_name(platform_name, arch, resolved_tag)
    url = f"https://github.com/{MAA_PICLI_REPO}/releases/download/{resolved_tag}/{asset}"
    maapicli_dir.mkdir(parents=True, exist_ok=True)
    archive = maapicli_dir / asset

    print(f"Downloading MaaPiCli {resolved_tag}: {url}")
    # Public release assets, and urllib forwards custom headers across the redirect to the
    # object host, so the token is only used for the API lookup above.
    request = urllib.request.Request(url, headers={"User-Agent": "m9a-cli-packaging"})
    with urllib.request.urlopen(request, timeout=300) as response, open(archive, "wb") as out_file:
        shutil.copyfileobj(response, out_file)
    with zipfile.ZipFile(archive) as zip_file:
        zip_file.extractall(maapicli_dir)
    archive.unlink()

    framework_version = maapicli_dir / "maafw-version.txt"
    if framework_version.is_file():
        print(f"MaaPiCli {resolved_tag} targets {framework_version.read_text(encoding='utf-8').strip()}")
    return maapicli_binary_path(maapicli_dir, platform_name)


def install_maapicli(output_dir: Path, maapicli_dir: Path, platform_name: str) -> Path:
    """Ship the MaaPiCli entrypoint from MaaXYZ/MaaPiCli instead of the MaaFramework bundle.

    MaaFramework is moving MaaPiCli out of its MAA-* runtime packages, and the copy it still
    bundles lags the ProjectInterface revisions: MaaFramework v5.15.0-beta.1 deserializes
    `welcome` as a single string, so an interface.json using the ordered array of PI 2.10
    fails to load. The MaaPiCli releases carry the current parser, so the entrypoint is
    taken from them and any copy left in the runtime bundle is dropped.
    """
    binary = maapicli_binary_path(maapicli_dir, platform_name)
    target = output_dir / cli_binary_name(platform_name)
    for stale in (target, output_dir / maapicli_binary_name(platform_name)):
        if stale.exists():
            stale.unlink()

    shutil.copy2(binary, target)
    if not platform_name.startswith("win"):
        target.chmod(0o755)
    print(f"CLI entrypoint: {target} (from {binary})")
    return target


def assert_interface_parse_output(output: str) -> None:
    for marker in INTERFACE_PARSE_FAILURE_MARKERS:
        if marker in output:
            raise RuntimeError(
                f"Packaged CLI cannot parse the packaged interface.json ({marker}):\n{output[-4000:]}"
            )
    if CONTROLLER_PROMPT_MARKER not in output:
        raise RuntimeError(
            "Packaged CLI never reached the controller prompt, so interface.json did not "
            f"fully resolve:\n{output[-4000:]}"
        )


def smoke_interface_parse(output_dir: Path, platform_name: str) -> None:
    """Run the packaged CLI once so a package the CLI cannot load never ships.

    Reads stdin from /dev/null so the controller prompt does not block, and accepts a
    timeout only when the prompt already appeared in the captured output.
    """
    binary = output_dir / cli_binary_name(platform_name)
    if not binary.is_file():
        raise FileNotFoundError(f"CLI entrypoint not found: {binary}")

    try:
        result = subprocess.run(
            [str(binary)],
            cwd=output_dir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=CLI_SMOKE_TIMEOUT_SECONDS,
            check=False,
        )
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"") + (exc.stderr or b"")
        assert_interface_parse_output(partial.decode("utf-8", "replace"))
        print("Packaged CLI interface smoke passed (timed out at the controller prompt).")
        return

    assert_interface_parse_output(output)
    print("Packaged CLI interface smoke passed.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build M9A MaaPiCli release package.")
    root = Path(__file__).resolve().parents[2]
    parser.add_argument("--source-dir", type=Path, default=root / "M9A")
    parser.add_argument("--deps-dir", type=Path, default=root / "deps")
    parser.add_argument("--output-dir", type=Path, default=root / "install-cli")
    parser.add_argument("--python-dir", type=Path, default=root / "install" / "python")
    parser.add_argument("--version", default="v0.0.1")
    parser.add_argument("--platform", default=sys.platform)
    parser.add_argument(
        "--arch",
        default=host_arch(),
        help="Release architecture (x86_64 or aarch64). Defaults to the host.",
    )
    parser.add_argument("--maapicli-dir", type=Path, default=root / "install" / "maapicli")
    parser.add_argument(
        "--maapicli-tag",
        default="",
        help="Exact MaaXYZ/MaaPiCli release tag. Empty resolves the latest release.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_dir = args.source_dir.resolve()
    deps_dir = args.deps_dir.resolve()
    output_dir = args.output_dir.resolve()
    maapicli_dir = args.maapicli_dir.resolve()

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    install_deps(deps_dir, output_dir)
    require_shared_runtime_support(source_dir)
    strip_agent_native_runtime(args.python_dir.resolve(), output_dir, args.platform)
    install_resource(source_dir, output_dir, args.version)
    install_chores(source_dir, output_dir)
    install_agent(source_dir, output_dir, args.platform)
    install_manifest_cache(source_dir, output_dir)
    download_maapicli(maapicli_dir, args.platform, args.arch, args.maapicli_tag)
    install_maapicli(output_dir, maapicli_dir, args.platform)
    smoke_interface_parse(output_dir, args.platform)
    smoke_packaged_agent(args.python_dir.resolve(), output_dir, args.platform)

    print(f"Install to {output_dir} successfully.")


if __name__ == "__main__":
    main()
