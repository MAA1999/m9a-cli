from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast


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


def rename_cli_binary(output_dir: Path) -> None:
    candidates = [
        (output_dir / "MaaPiCli.exe", output_dir / "M9A.exe"),
        (output_dir / "MaaPiCli", output_dir / "M9A"),
    ]
    for source, target in candidates:
        if source.exists():
            if target.exists():
                target.unlink()
            source.rename(target)
            return
    print("Warning: MaaPiCli binary not found; skip rename.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build M9A MaaPiCli release package.")
    root = Path(__file__).resolve().parents[2]
    parser.add_argument("--source-dir", type=Path, default=root / "M9A")
    parser.add_argument("--deps-dir", type=Path, default=root / "deps")
    parser.add_argument("--output-dir", type=Path, default=root / "install-cli")
    parser.add_argument("--python-dir", type=Path, default=root / "install" / "python")
    parser.add_argument("--version", default="v0.0.1")
    parser.add_argument("--platform", default=sys.platform)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source_dir = args.source_dir.resolve()
    deps_dir = args.deps_dir.resolve()
    output_dir = args.output_dir.resolve()

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
    rename_cli_binary(output_dir)
    smoke_packaged_agent(args.python_dir.resolve(), output_dir, args.platform)

    print(f"Install to {output_dir} successfully.")


if __name__ == "__main__":
    main()
