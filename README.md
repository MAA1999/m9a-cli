# m9a-cli

M9A command-line release packaging repository.

This repository owns only the MaaPiCli package pipeline. `MAA1999/M9A` remains
the source of truth for resources, tasks, agent code, dependencies, release
version, and release notes.

## Release Flow

1. `MAA1999/M9A` publishes a normal release.
2. The M9A workflow sends a `repository_dispatch` event to `MAA1999/m9a-cli`.
3. This repository clones the matching M9A ref, resolves MaaFramework from an exact version or the stable/beta/alpha channel, takes the
   MaaPiCli entrypoint from the `MAAXYZ/MaaPiCli` release assets, and builds `PiCLI` artifacts.
4. The artifacts are published to the same tag name in this repository, reusing
   the release notes passed from `MAA1999/M9A`.

Manual runs are available through `workflow_dispatch`.

The entrypoint is no longer taken from the MaaFramework runtime bundle: that copy
lags the ProjectInterface revisions (MaaFramework v5.15.0-beta.1 still parsed
`welcome` as a single string, so an interface.json using the PI 2.10 array failed
to load), and MaaFramework is dropping MaaPiCli from its `MAA-*` packages anyway.
`--maapicli-tag` pins an exact MaaXYZ/MaaPiCli release; empty resolves the latest.

## Local Smoke Test

From this repository root:

```powershell
python tools/ci/install_cli.py --source-dir D:\02code\M9A --deps-dir D:\02code\M9A\deps --output-dir install-cli-smoke --version v0.0.0-local --platform win32 --arch x86_64
```

The command expects a prepared MaaFramework `deps` directory. CI downloads it
from `MaaXYZ/MaaFramework`. `--python-dir` defaults to `install/python`, which
`tools/ci/setup_embed_python.py` prepares. MaaPiCli is downloaded from
`MaaXYZ/MaaPiCli` into `install/maapicli` (set `GH_TOKEN` to avoid API rate
limits); pass `--maapicli-tag v5.14.3` to pin it.

The build strips the MaaFramework copy bundled in the embedded interpreter's `maa`
wheel -- the Agent reaches the flattened copy at the package root through
`MAAFW_BINARY_PATH` -- and then runs the packaged Agent once. A `--source-dir` that
predates the `agent/maafw_paths.py` helper is rejected before anything is stripped,
so a stale M9A ref cannot produce a package that only breaks on the user machine.

Finally the packaged CLI is started once against the packaged `interface.json` and
must reach the controller prompt, so a CLI build that cannot load the interface
fails before the artifact is uploaded.
