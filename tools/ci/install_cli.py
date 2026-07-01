from __future__ import annotations

import argparse
import json
import re
import shutil
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
    if platform.startswith("darwin") or platform.startswith("macos") or platform.startswith("osx"):
        return r"./python/bin/python3"
    if platform.startswith("linux"):
        return r"python3"
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
        agent["child_args"] = ["-u", r"./agent/bootstrap.py"]
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
    for name in ["README.md", "LICENSE", "CONTACT", "requirements.txt"]:
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
    install_resource(source_dir, output_dir, args.version)
    install_chores(source_dir, output_dir)
    install_agent(source_dir, output_dir, args.platform)
    install_manifest_cache(source_dir, output_dir)
    rename_cli_binary(output_dir)

    print(f"Install to {output_dir} successfully.")


if __name__ == "__main__":
    main()
