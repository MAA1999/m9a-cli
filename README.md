# m9a-cli

M9A command-line release packaging repository.

This repository owns only the MaaPiCli package pipeline. `MAA1999/M9A` remains
the source of truth for resources, tasks, agent code, dependencies, release
version, and release notes.

## Release Flow

1. `MAA1999/M9A` publishes a normal release.
2. The M9A workflow sends a `repository_dispatch` event to `MAA1999/m9a-cli`.
3. This repository clones the matching M9A ref, resolves MaaFramework from an exact version or the stable/beta/alpha channel, and
   builds `PiCLI` artifacts.
4. The artifacts are published to the same tag name in this repository, reusing
   the release notes passed from `MAA1999/M9A`.

Manual runs are available through `workflow_dispatch`.

## Local Smoke Test

From this repository root:

```powershell
python tools/ci/install_cli.py --source-dir D:\02code\M9A --deps-dir D:\02code\M9A\deps --output-dir install-cli-smoke --version v0.0.0-local --platform win32
```

The command expects a prepared MaaFramework `deps` directory. CI downloads it
from `MaaXYZ/MaaFramework`. `--python-dir` defaults to `install/python`, which
`tools/ci/setup_embed_python.py` prepares.

The build strips the MaaFramework copy bundled in the embedded interpreter's `maa`
wheel -- the Agent reaches the flattened copy at the package root through
`MAAFW_BINARY_PATH` -- and then runs the packaged Agent once. A `--source-dir` that
predates the `agent/maafw_paths.py` helper is rejected before anything is stripped,
so a stale M9A ref cannot produce a package that only breaks on the user machine.
