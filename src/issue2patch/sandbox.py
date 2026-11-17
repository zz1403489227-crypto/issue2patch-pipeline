"""One fresh Docker container per task, network off, image pinned by ID."""
from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass

CONDA = "source /opt/miniconda3/bin/activate && conda activate testbed"
# PATH used for agent processes so that `python`/`pytest` resolve to the task env.
AGENT_PATH = (
    "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin:/opt/i2p-tools"
)

# Remove everything that could reveal the future of the repository (later
# commits, tags, remotes), keeping only what HEAD reaches.
GIT_PREPARE = r"""
set -e
git config --global --add safe.directory '*'
git config --global user.email i2p@localhost
git config --global user.name i2p
before=$(git for-each-ref | wc -l)
cur=$(git symbolic-ref -q --short HEAD || true)
git for-each-ref --format='%(refname)' | while read -r ref; do
  if [ -n "$cur" ] && [ "$ref" = "refs/heads/$cur" ]; then continue; fi
  git update-ref -d "$ref"
done
for r in $(git remote); do git remote remove "$r"; done
git stash clear || true
git reflog expire --expire=now --all
git gc --prune=now --quiet
echo "REFS_REMOVED=$before"
echo "HEAD=$(git rev-parse HEAD)"
echo "PARENT=$(git rev-parse -q --verify HEAD^ || true)"
echo "HEAD_SUBJECT=$(git log -1 --format=%s | tr ' ' '_')"
echo "DIRTY=$(git status --porcelain | wc -l)"
echo "ALL_COMMITS_AHEAD=$(git rev-list --all --not HEAD | wc -l)"
"""


class InfraError(Exception):
    """Infrastructure failure (Docker, container, model endpoint). Retried as-is with backoff."""


@dataclass
class ExecResult:
    code: int
    output: str
    timed_out: bool
    seconds: float


# Exit codes produced by `docker exec` itself (not by the command inside): 125 = docker error,
# 126 = cannot invoke, 127 = not found; plus the daemon's messages for a dead container.
_DOCKER_FAILURE = ("No such container", "is not running", "Cannot connect to the Docker daemon",
                   "Error response from daemon")


def _docker(args: list[str], timeout: int = 120, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(["docker", *args], input=input, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired as e:
        raise InfraError(f"docker {' '.join(args[:2])} timed out after {timeout}s") from e
    if p.returncode != 0:
        raise InfraError(f"docker {' '.join(args[:2])} failed ({p.returncode}): {p.stderr[-2000:]}")
    return p


class DockerSandbox:
    def __init__(self, container: str, image_id: str, workdir: str = "/testbed"):
        self.container = container
        self.image_id = image_id
        self.workdir = workdir

    @classmethod
    def create(cls, image: str, name: str, mounts: list[tuple[str, str, str]], cpus: str,
               memory: str, workdir: str = "/testbed") -> "DockerSandbox":
        image_id = _docker(["image", "inspect", "--format", "{{.Id}}", image]).stdout.strip()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        args = ["run", "-d", "--name", name, "--network", "none", "--cpus", cpus,
                "--memory", memory, "--label", "i2p=1", "-w", workdir]
        for host, ctr, mode in mounts:
            args += ["-v", f"{host}:{ctr}:{mode}"]
        args += [image_id, "sleep", "infinity"]
        _docker(args, timeout=300)
        return cls(name, image_id, workdir)

    def exec(self, script: str, timeout: int = 300, conda: bool = True, input: str | None = None,
             cwd: str | None = None) -> ExecResult:
        """Run a bash script inside the sandbox; stdout and stderr merged."""
        body = (CONDA + "\n" if conda else "") + f"cd {shlex.quote(cwd or self.workdir)}\n" + script
        inner = f"timeout -s KILL {int(timeout)} bash -c {shlex.quote(body)} 2>&1"
        args = ["docker", "exec"] + (["-i"] if input is not None else []) + [self.container, "bash", "-c", inner]
        t0 = time.time()
        try:
            p = subprocess.run(args, input=input, capture_output=True, text=True,
                               timeout=timeout + 60, errors="replace")
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return ExecResult(-9, out, True, time.time() - t0)
        dt = time.time() - t0
        if p.returncode in (125, 126, 127) and any(s in p.stderr for s in _DOCKER_FAILURE):
            raise InfraError(f"docker exec on {self.container} failed ({p.returncode}): {p.stderr[-500:]}")
        return ExecResult(p.returncode, p.stdout + p.stderr, p.returncode == 137 and dt >= timeout - 1, dt)

    def is_running(self) -> bool:
        p = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", self.container],
                           capture_output=True, text=True)
        return p.returncode == 0 and p.stdout.strip() == "true"

    def worktree_patch(self) -> str:
        """Binary diff of the whole working tree (tracked + untracked) against HEAD."""
        r = self.exec("git add -A && git diff --cached --binary HEAD; rc=$?; git reset -q; exit $rc",
                      conda=False, timeout=300)
        if r.code != 0:
            raise InfraError(f"worktree_patch failed: {r.output[-500:]}")
        return r.output

    def apply_worktree_patch(self, patch: str) -> None:
        if not patch.strip():
            return
        r = self.exec("git apply --binary --whitespace=nowarn -", conda=False, input=patch, timeout=300)
        if r.code != 0:
            raise InfraError(f"could not re-apply saved worktree: {r.output[-500:]}")

    def write_file(self, path: str, content: str) -> None:
        r = self.exec(f"mkdir -p \"$(dirname {shlex.quote(path)})\" && cat > {shlex.quote(path)}",
                      conda=False, input=content, timeout=60)
        if r.code != 0:
            raise RuntimeError(f"write_file {path} failed: {r.output[-500:]}")

    def read_file(self, path: str) -> str | None:
        r = self.exec(f"cat {shlex.quote(path)}", conda=False, timeout=60)
        return r.output if r.code == 0 else None

    # git helpers -------------------------------------------------------------
    def git(self, cmd: str, timeout: int = 300) -> ExecResult:
        return self.exec(f"git {cmd}", conda=False, timeout=timeout)

    def changed_files(self) -> list[str]:
        r = self.git("status --porcelain --untracked-files=all -z")
        files: list[str] = []
        parts = r.output.split("\0")
        i = 0
        while i < len(parts):
            entry = parts[i]
            if len(entry) < 4:
                i += 1
                continue
            status, path = entry[:2], entry[3:]
            files.append(path)
            if status.startswith("R") or status.startswith("C"):
                i += 1  # skip the source path of a rename/copy
            i += 1
        return sorted(set(files))

    def diff(self, files: list[str]) -> str:
        if not files:
            return ""
        quoted = " ".join(shlex.quote(f) for f in files)
        self.git(f"add -N -- {quoted}")
        r = self.git(f"diff HEAD --no-color -- {quoted}")
        self.git("reset -q")
        return r.output

    def revert(self, files: list[str]) -> None:
        for f in files:
            q = shlex.quote(f)
            self.exec(f"if git cat-file -e HEAD:{q} 2>/dev/null; then git checkout HEAD -- {q}; "
                      f"else rm -rf -- {q}; fi", conda=False, timeout=60)

    def snapshot(self) -> str:
        """Tree object of the whole working tree (tracked + untracked), worktree untouched."""
        r = self.exec("git add -A && git write-tree && git reset -q", conda=False, timeout=120)
        tree = r.output.strip().splitlines()[-1] if r.output.strip() else ""
        if r.code != 0 or len(tree) != 40:
            raise InfraError(f"snapshot failed: {r.output[-500:]}")
        return tree

    def restore(self, tree: str) -> None:
        r = self.exec(f"git add -A && git read-tree -u --reset {tree} && git reset -q",
                      conda=False, timeout=120)
        if r.code != 0:
            raise InfraError(f"restore failed: {r.output[-500:]}")

    def destroy(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
