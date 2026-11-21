"""Task-container IO for the orchestrator: inventory, digests, validation IO.

Everything here goes through `taskbridge.Session` — the harness container has
no direct access to task files, so every read, listing, and hash is a relay
round trip. `RelayIO` implements `contract.TaskIO` on top of the session;
`workspace_inventory` and `workspace_digests` give the roles their inputs and
the verifier its tamper-evidence.
"""

from __future__ import annotations

import json

from .contract import MissingFile, TaskIO

_INVENTORY_MAX_FILES = 4000


class RelayIO(TaskIO):
  """`contract.TaskIO` implemented over a taskbridge session."""

  def __init__(self, session, workdir: str):
    self.session = session
    self.workdir = (workdir or "/").rstrip("/") or "/"

  async def read_bytes(self, path: str) -> bytes:
    from taskbridge import TaskError

    path = _resolve(path, self.workdir)
    try:
      return await self.session.read_file(path)
    except TaskError as exc:
      raise MissingFile(f"{path}: {exc}") from exc

  async def exists(self, path: str) -> bool:
    result = await self.session.exec(
        f"test -e {_shquote(_resolve(path, self.workdir))} && echo yes || echo no",
        timeout_sec=30)
    return result.get("output", "").strip().startswith("yes")


def _resolve(path: str, workdir: str) -> str:
  import posixpath

  if path.startswith("/"):
    return posixpath.normpath(path)
  return posixpath.normpath(f"{workdir.rstrip('/')}/{path}")


def _shquote(text: str) -> str:
  return "'" + text.replace("'", "'\\''") + "'"


async def workspace_inventory(session, workdir: str, max_bytes: int = 90000) -> str:
  """A text inventory of the workspace for the planner's prompt.

  Runs a bounded `find` through the relay: file list with sizes, deepest
  first trimmed to `max_bytes`. This is orchestrator-executed (not the
  model's job) so the planner starts from a verified listing.
  """
  root = _resolve(".", workdir)
  command = (
      f"cd {_shquote(root)} 2>/dev/null || exit 0; "
      f"find . -type f -printf '%10s  %p\\n' 2>/dev/null | sort -k2 | "
      f"head -c {max_bytes}; echo; "
      f"echo '---'; find . -maxdepth 1 -type d 2>/dev/null | head -50"
  )
  result = await session.exec(command, timeout_sec=120)
  listing = result.get("output", "")
  if result.get("truncated"):
    listing += "\n[inventory truncated by the 100 KB output limit]"
  if result.get("timed_out"):
    listing += "\n[inventory listing timed out]"
  return listing or "(empty workspace or listing unavailable)"


async def workspace_digests(session, workdir: str,
                            paths: list[str] | None = None) -> dict[str, str]:
  """sha256 digests of workspace files (or the given paths only).

  Uses `find -print0 | xargs -0 sha256sum` so spaces survive; filenames with
  embedded newlines are the one known gap (they break line parsing and are
  reported in the error field rather than silently mis-hashed).
  """
  targets = "-type f" if not paths else "-type f (" + " -o ".join(
      f"-path {_shquote(_resolve(p, workdir))}" for p in paths) + ")"
  command = (
      f"cd {_shquote(_resolve('.', workdir))} 2>/dev/null || exit 0; "
      f"find . {targets} -print0 2>/dev/null | xargs -0 -r sha256sum 2>/dev/null"
  )
  result = await session.exec(command, timeout_sec=300)
  if result.get("timed_out"):
    raise RuntimeError("digesting the workspace timed out")
  digests: dict[str, str] = {}
  unparsed = 0
  for line in (result.get("output") or "").splitlines():
    parts = line.strip().split(None, 1)
    if len(parts) == 2 and len(parts[0]) == 64:
      digests[_relativize(parts[1], workdir)] = parts[0]
    elif line.strip():
      unparsed += 1
  if unparsed:
    raise RuntimeError(
        f"{unparsed} digest line(s) could not be parsed (filenames with "
        "newlines are not supported by the digest check)")
  return digests


def _relativize(path: str, workdir: str) -> str:
  path = path.strip().lstrip("./")
  root = workdir.strip().lstrip("/")
  if root and path.startswith(root + "/"):
    return path[len(root) + 1:]
  return path


def write_state(state_dir_local, name: str, payload) -> None:
  """Persists orchestrator state (plan, evidence, verdicts) to local scratch.

  State lives in the serve container's scratch directory, NOT in the task
  workspace — deliverable grading reads exact paths and must not see stray
  harness files.
  """
  import pathlib

  target = pathlib.Path(state_dir_local) / name
  target.parent.mkdir(parents=True, exist_ok=True)
  text = payload if isinstance(payload, str) else json.dumps(payload, indent=1, sort_keys=True)
  target.write_text(text)
