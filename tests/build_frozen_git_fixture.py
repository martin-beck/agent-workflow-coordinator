# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT

"""Build disposable, pinned ASB-scale Git state/product fixtures.

The source repositories are only observed. The fixture has its own frozen bare
origins and fresh tracked-file checkouts; ignored build output is not copied.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str
    branch: str | None


def git(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(  # noqa: S603
        ["/usr/bin/git", *args],
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=capture,
        text=True,
    )
    return completed.stdout.strip() if capture else ""


def parse_worktrees(raw: str) -> list[Worktree]:
    records: list[Worktree] = []
    for block in raw.split("\n\n"):
        lines = block.splitlines()
        if not lines:
            continue
        if not lines[0].startswith("worktree ") or any(
            line.startswith("prunable ") for line in lines
        ):
            raise ValueError("product worktree inventory is incomplete or prunable")
        path = Path(lines[0][9:])
        head = next((line[5:] for line in lines if line.startswith("HEAD ")), "")
        if not path.is_absolute() or re.fullmatch(r"[0-9a-f]{40,64}", head) is None:
            raise ValueError("product worktree has no absolute path or exact commit")
        branch_ref = next((line[7:] for line in lines if line.startswith("branch ")), "")
        branch = branch_ref.removeprefix("refs/heads/") if branch_ref else None
        if branch_ref and (not branch_ref.startswith("refs/heads/") or not branch):
            raise ValueError("product worktree has an unsupported branch reference")
        records.append(Worktree(path, head, branch))
    if not records:
        raise ValueError("product repository has no worktree inventory")
    return records


def assert_free_space(path: Path, minimum_gib: int) -> None:
    if shutil.disk_usage(path).free < minimum_gib * 1024**3:
        raise RuntimeError(f"fixture build stopped below {minimum_gib} GiB free")


def assert_no_source_overlap(output: Path, sources: tuple[Path, ...]) -> None:
    for source in sources:
        resolved = source.resolve()
        if output.is_relative_to(resolved) or resolved.is_relative_to(output):
            raise ValueError("fixture output overlaps a source checkout")


def prepare_bare(source: Path, destination: Path) -> None:
    git("clone", "--mirror", "--no-local", "-q", str(source), str(destination))


def pin_object(bare: Path, source: Path, head: str, index: int) -> None:
    probe = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-C", str(bare), "cat-file", "-e", f"{head}^{{commit}}"],
        check=False,
        capture_output=True,
    )
    if probe.returncode:
        git("-C", str(bare), "fetch", "-q", "--no-tags", str(source), head)
    # Normal clones fetch branch refs but not arbitrary refs/fixture/* names.
    git("-C", str(bare), "update-ref", f"refs/heads/fixture-worktree-{index:04d}", head)


def create_product_worktrees(
    product: Path, output: Path, selected: list[Worktree], minimum_free_gib: int
) -> None:
    primary = selected[0]
    if primary.branch:
        git("-C", str(product), "checkout", "-q", "-B", primary.branch, primary.head)
    else:
        git("-C", str(product), "checkout", "-q", "--detach", primary.head)
    for index, item in enumerate(selected[1:], 1):
        if index % 16 == 0:
            assert_free_space(output, minimum_free_gib)
        target = output / "worktrees" / f"{index:04d}" / item.path.name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if item.branch:
            git(
                "-C",
                str(product),
                "worktree",
                "add",
                "-q",
                "-b",
                item.branch,
                str(target),
                item.head,
            )
        else:
            git("-C", str(product), "worktree", "add", "-q", "--detach", str(target), item.head)


def install_frozen_remote_observation(output: Path, product: Path, bare: Path) -> None:
    """Keep canonical origin identity while serving only the pinned main ref locally."""
    bin_dir = output / "bin"
    bin_dir.mkdir(mode=0o700)
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        f"product = Path({str(product.resolve())!r})\n"
        f"bare = {str(bare.resolve())!r}\n"
        "if len(args) == 5 and args[0] == '-C' and Path(args[1]).resolve() == product "
        "and args[2:] == ['ls-remote', 'origin', 'refs/heads/main']:\n"
        "    raise SystemExit(subprocess.call(['/usr/bin/git', '-C', bare, "
        "'ls-remote', '.', 'refs/heads/main']))\n"
        "os.execv('/usr/bin/git', ['/usr/bin/git', *args])\n"
    )
    wrapper.chmod(0o700)


def build(
    source_state: Path,
    source_product: Path,
    output: Path,
    max_worktrees: int | None,
    minimum_free_gib: int,
) -> dict[str, object]:
    source_state = source_state.resolve()
    source_product = source_product.resolve()
    output = output.resolve()
    state_head = git("-C", str(source_state), "rev-parse", "HEAD", capture=True)
    origin_main = git("-C", str(source_product), "rev-parse", "origin/main", capture=True)
    worktrees = parse_worktrees(
        git("-C", str(source_product), "worktree", "list", "--porcelain", capture=True)
    )
    state_worktrees = parse_worktrees(
        git("-C", str(source_state), "worktree", "list", "--porcelain", capture=True)
    )
    if worktrees[0].path.resolve() != source_product.resolve():
        raise ValueError("source product is not the primary listed worktree")
    assert_no_source_overlap(
        output,
        (
            source_state,
            *(item.path for item in state_worktrees),
            *(item.path for item in worktrees),
        ),
    )
    if output.exists() and any(output.iterdir()):
        raise ValueError("fixture output must not contain existing data")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    selected = worktrees if max_worktrees is None else worktrees[:max_worktrees]
    if not selected:
        raise ValueError("at least one worktree is required")
    assert_free_space(output, minimum_free_gib)

    state_bare = output / "state-origin.git"
    product_bare = output / "product-origin.git"
    prepare_bare(source_state, state_bare)
    prepare_bare(source_product, product_bare)
    for index, item in enumerate(selected):
        pin_object(product_bare, source_product, item.head, index)
    git("-C", str(product_bare), "update-ref", "refs/heads/main", origin_main)

    state = output / "state"
    product = output / "product"
    git("clone", "--shared", "-q", str(state_bare), str(state))
    git("-C", str(state), "checkout", "-q", "-B", "frozen-fixture", state_head)
    state_origin = git("-C", str(source_state), "remote", "get-url", "origin", capture=True)
    git("-C", str(state), "remote", "set-url", "origin", state_origin)
    git("clone", "--shared", "-q", str(product_bare), str(product))
    primary = selected[0]
    create_product_worktrees(product, output, selected, minimum_free_gib)
    git("-C", str(product), "update-ref", "refs/remotes/origin/main", origin_main)
    product_origin = git("-C", str(source_product), "remote", "get-url", "origin", capture=True)
    git("-C", str(product), "remote", "set-url", "origin", product_origin)
    install_frozen_remote_observation(output, product, product_bare)
    observed = git("-C", str(product), "worktree", "list", "--porcelain", capture=True)
    if len(parse_worktrees(observed)) != len(selected):
        raise RuntimeError("frozen product worktree count does not match its input")
    if git("-C", str(state), "rev-parse", "HEAD", capture=True) != state_head:
        raise RuntimeError("frozen state head differs from the pinned source commit")
    result: dict[str, object] = {
        "state": str(state),
        "product": str(product),
        "state_head": state_head,
        "product_primary_head": primary.head,
        "product_origin_main": origin_main,
        "worktrees": len(selected),
        "source_worktrees": len(worktrees),
        "ignored_build_output_copied": False,
        "remote_main_source": "frozen_local_bare_intercept",
    }
    (output / "fixture.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-state", type=Path, required=True)
    parser.add_argument("--source-product", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-worktrees", type=int)
    parser.add_argument("--minimum-free-gib", type=int, default=30)
    args = parser.parse_args()
    if args.max_worktrees is not None and args.max_worktrees < 1:
        parser.error("--max-worktrees must be positive")
    if args.minimum_free_gib < 1:
        parser.error("--minimum-free-gib must be positive")
    print(
        json.dumps(
            build(
                args.source_state.resolve(),
                args.source_product.resolve(),
                args.output.resolve(),
                args.max_worktrees,
                args.minimum_free_gib,
            ),
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
