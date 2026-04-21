"""Git tools for the coding agent — diff, commit, and log.

All commands run in the workspace root via ``git`` subprocess calls.
The sandbox policy controls which git operations are allowed at the
network level (e.g. push requires network access, which coding agents
typically don't have).

**Why subprocess instead of a Python git library?**

- ``GitPython`` shells out to the ``git`` binary under the hood — same
  thing with an extra abstraction layer.
- ``pygit2`` (libgit2 bindings) requires compiling a C library, doesn't
  support all porcelain commands (e.g. ``git log --oneline`` has no
  clean equivalent), and behaves subtly differently from the CLI in
  edge cases (config resolution, credential helpers, hooks).
- Subprocess guarantees identical behaviour to typing ``git diff`` in a
  terminal — same binary, same config, same hooks.
- The ``bash`` tool already provides arbitrary git access for anything
  the dedicated tools don't cover.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.git")

# ── Constants ─────────────────────────────────────────────────────────

# Max commits shown by git_log
_DEFAULT_LOG_LIMIT: int = 20
# Character cap on combined git stdout/stderr before truncation
_OUTPUT_MAX_BYTES: int = 65_536
# Logical toolset name used by the registry for grouping
_TOOLSET: str = "git"
# Default timeout (seconds) for subprocess git invocations
_GIT_TIMEOUT_S: int = 30
# Longer timeout for git clone (network transfer of repository contents).
_GIT_CLONE_TIMEOUT_S: int = 300


# ── Helpers ───────────────────────────────────────────────────────────


def _build_git_env() -> dict[str, str]:
    """Build the env dict git inherits, with a ``GIT_SSL_CAINFO`` backfill.

    OpenShell's L7 proxy terminates TLS inside the sandbox and presents
    a cert signed by OpenShell's internal CA.  Python / curl trust it
    because they read ``REQUESTS_CA_BUNDLE`` / ``SSL_CERT_FILE`` — env
    vars pointing at the OpenShell CA bundle that OpenShell injects.
    Git, however, has its own env-var namespace; it reads
    ``GIT_SSL_CAINFO`` / ``GIT_SSL_CAPATH`` and otherwise falls back to
    the Debian system trust store, which does **not** include the
    OpenShell CA.

    Without this backfill, every in-sandbox ``git clone`` / ``fetch``
    fails with ``SSL_ERROR_UNKNOWN_ROOT_CA`` and the model has to work
    around by invoking ``bash`` with ``GIT_SSL_NO_VERIFY=1`` — effective
    but disables cert verification entirely, which is worse than
    trusting the right CA.

    Semantics:

    - Starts from ``os.environ.copy()`` so git inherits everything
      else (proxy config, ``PATH``, user env vars) unchanged.
    - If ``GIT_SSL_CAINFO`` is already set in the operator's env,
      leaves it alone — explicit operator override wins.
    - Otherwise, if ``SSL_CERT_FILE`` or ``REQUESTS_CA_BUNDLE`` is
      set, uses that as git's trust bundle.
    - No-op in local dev (neither env var is set), so this doesn't
      change anything outside the sandbox.
    """
    env = os.environ.copy()
    if "GIT_SSL_CAINFO" in env:
        return env
    ca_bundle = env.get("SSL_CERT_FILE") or env.get("REQUESTS_CA_BUNDLE")
    if ca_bundle:
        env["GIT_SSL_CAINFO"] = ca_bundle
    return env


async def _run_git(workspace_root: str, *args: str, timeout: int = _GIT_TIMEOUT_S) -> str:
    """Run a git command and return its output.

    Args:
        workspace_root: Working directory for the git command.
        *args: Git subcommand and arguments.
        timeout: Maximum seconds before the process is killed; defaults to
            ``_GIT_TIMEOUT_S``.

    Returns:
        Combined stdout + stderr with exit code prefix on failure.
    """
    cmd = ["git", *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_root,
            env=_build_git_env(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        return f"Error: git command timed out after {timeout}s"
    except FileNotFoundError:
        return "Error: git is not installed"

    out = stdout.decode(errors="replace") if stdout else ""
    err = stderr.decode(errors="replace") if stderr else ""

    if proc.returncode != 0:
        return f"Exit code: {proc.returncode}\n{err.strip()}"

    output = out if out else err
    if len(output) > _OUTPUT_MAX_BYTES:
        output = output[:_OUTPUT_MAX_BYTES] + f"\n... (truncated at {_OUTPUT_MAX_BYTES} bytes)"
    return output


# ── Tool specs ────────────────────────────────────────────────────────


def _make_git_diff(workspace_root: str) -> ToolSpec:
    """Create the ``git_diff`` tool spec bound to *workspace_root*."""

    @tool(
        "git_diff",
        "Show uncommitted changes in the workspace. Use staged=true for staged-only.",
        {
            "type": "object",
            "properties": {
                "staged": {
                    "type": "boolean",
                    "description": "Show only staged changes.",
                    "default": False,
                },
            },
        },
        display_name="Checking git diff",
        toolset=_TOOLSET,
    )
    async def git_diff(staged: bool = False) -> str:
        """Show uncommitted changes in the working tree or staged area.

        Args:
            staged: When True, show only staged changes (``--cached``).

        Returns:
            Diff text, or a short message when there are no changes.
        """
        args = ["diff"]
        if staged:
            args.append("--cached")
        result = await _run_git(workspace_root, *args)
        return result if result.strip() else "No uncommitted changes."

    return git_diff


def _make_git_commit(workspace_root: str) -> ToolSpec:
    """Create the ``git_commit`` tool spec bound to *workspace_root*."""

    @tool(
        "git_commit",
        "Stage all changes and commit with a message.",
        {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message."},
                "add_all": {
                    "type": "boolean",
                    "description": "Stage all changes first.",
                    "default": True,
                },
            },
            "required": ["message"],
        },
        display_name="Committing changes",
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def git_commit(message: str, add_all: bool = True) -> str:
        """Stage changes (optionally) and create a commit with *message*.

        Args:
            message: Commit message passed to ``git commit -m``.
            add_all: When True, run ``git add -A`` before committing.

        Returns:
            Git output on success, or an error string from staging or commit.
        """
        if add_all:
            add_result = await _run_git(workspace_root, "add", "-A")
            if add_result.startswith("Error:") or add_result.startswith("Exit code:"):
                return f"Failed to stage: {add_result}"
        return await _run_git(workspace_root, "commit", "-m", message)

    return git_commit


def _make_git_log(workspace_root: str) -> ToolSpec:
    """Create the ``git_log`` tool spec bound to *workspace_root*."""

    @tool(
        "git_log",
        "Show recent commit history (one line per commit).",
        {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max commits to show.",
                    "default": _DEFAULT_LOG_LIMIT,
                },
            },
        },
        display_name="Checking git log",
        toolset=_TOOLSET,
    )
    async def git_log(limit: int = _DEFAULT_LOG_LIMIT) -> str:
        """Show recent commit history as one-line abbreviated hashes.

        Args:
            limit: Maximum number of commits to include.

        Returns:
            Output of ``git log --oneline`` (possibly truncated by helpers).
        """
        return await _run_git(
            workspace_root, "log", f"--max-count={limit}", "--oneline", "--no-decorate"
        )

    return git_log


def _make_git_checkout(workspace_root: str) -> ToolSpec:
    """Create the ``git_checkout`` tool spec bound to *workspace_root*."""

    @tool(
        "git_checkout",
        (
            "Switch to an existing branch or commit in the workspace.  "
            "Does NOT create new branches — use ``bash`` with ``git checkout -b`` "
            "for that.  Requires the ref to already exist locally."
        ),
        {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Branch name or commit SHA to check out.",
                },
            },
            "required": ["ref"],
        },
        display_name="Switching branch",
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def git_checkout(ref: str) -> str:
        """Check out an existing branch or commit.

        Args:
            ref: Branch name or commit SHA.

        Returns:
            Git output on success, or an error string.
        """
        return await _run_git(workspace_root, "checkout", ref)

    return git_checkout


def _parse_allowed_hosts(raw: str) -> frozenset[str]:
    """Parse ``GIT_CLONE_ALLOWED_HOSTS`` into a set of host names.

    Accepts comma- or whitespace-separated entries.  Empty string →
    empty set, which fails ``git_clone`` closed.
    """
    return frozenset(h.strip() for h in raw.replace(",", " ").split() if h.strip())


def _make_git_clone(workspace_root: str, allowed_hosts: frozenset[str]) -> ToolSpec:
    """Create the ``git_clone`` tool spec.

    Clones are scoped to *workspace_root* (the ``dest`` path is
    resolved under it and must not escape via ``..``) and restricted
    to an explicit host allowlist supplied by config.

    Args:
        workspace_root: Working directory that contains the clone.
        allowed_hosts: Set of URL host names the clone accepts.  Empty
            means the tool is disabled (fail-closed).  The set is
            baked into the tool's model-visible description so Claude
            doesn't have to guess whether a URL is permitted — an
            opaque "operator-configured allowlist" phrasing triggers
            safety reasoning that occasionally causes the model to
            refuse listed tools claiming they "don't exist".
    """
    if allowed_hosts:
        hosts_note = f"Approved hosts: {', '.join(sorted(allowed_hosts))}.  "
    else:
        hosts_note = (
            "This tool is DISABLED on this deployment (no hosts approved) — any "
            "call will be rejected.  "
        )

    @tool(
        "git_clone",
        (
            "Clone a remote git repository into a subdirectory of the workspace.  "
            f"{hosts_note}"
            "Paths are rooted in the workspace; relative ``..`` in ``dest`` is rejected."
        ),
        {
            "type": "object",
            "properties": {
                "repo_url": {
                    "type": "string",
                    "description": "HTTPS or SSH git URL to clone.",
                },
                "dest": {
                    "type": "string",
                    "description": (
                        "Relative destination directory under workspace_root.  "
                        "Defaults to the repo's basename (e.g. 'myproj.git' → 'myproj')."
                    ),
                    "default": "",
                },
            },
            "required": ["repo_url"],
        },
        display_name="Cloning repository",
        toolset=_TOOLSET,
        is_read_only=False,
        is_concurrency_safe=False,
    )
    async def git_clone(repo_url: str, dest: str = "") -> str:
        """Clone *repo_url* into *workspace_root*/*dest*.

        Host-allowlist gate and path-traversal gate are enforced here
        rather than in the sandbox policy — both layers must agree.

        Args:
            repo_url: HTTPS or SSH git URL.
            dest: Optional relative destination directory.  When empty,
                the repo's basename is used.

        Returns:
            Git output on success, or an error string describing which
            gate rejected the call.
        """
        if not allowed_hosts:
            return (
                "Error: git_clone is disabled (GIT_CLONE_ALLOWED_HOSTS is empty). "
                "Ask the operator to configure an allowlist."
            )

        host = _extract_git_url_host(repo_url)
        if host is None:
            return f"Error: could not parse host from URL {repo_url!r}"
        if host not in allowed_hosts:
            return (
                f"Error: host {host!r} is not in GIT_CLONE_ALLOWED_HOSTS. "
                f"Allowed: {sorted(allowed_hosts)}"
            )

        # Path traversal check: resolve dest under workspace_root and
        # refuse if it escapes.
        workspace_abs = Path(workspace_root).resolve()
        if not dest:
            dest = _default_clone_dest(repo_url)
        dest_abs = (workspace_abs / dest).resolve()
        try:
            dest_abs.relative_to(workspace_abs)
        except ValueError:
            return f"Error: destination {dest!r} escapes the workspace root"

        if dest_abs.exists():
            return f"Error: destination {dest!r} already exists"

        return await _run_git(
            workspace_root,
            "clone",
            repo_url,
            str(dest_abs),
            timeout=_GIT_CLONE_TIMEOUT_S,
        )

    return git_clone


def _extract_git_url_host(repo_url: str) -> str | None:
    """Extract the host component from an HTTPS or SSH git URL.

    Handles both ``https://host/path`` and ``git@host:path`` forms.

    Args:
        repo_url: The git URL.

    Returns:
        The host name, or ``None`` if parsing fails.
    """
    # Git's scp-style URL: user@host:path/to/repo.git
    if "@" in repo_url and ":" in repo_url and "://" not in repo_url:
        after_at = repo_url.split("@", 1)[1]
        host = after_at.split(":", 1)[0]
        return host or None

    # Standard URL (https://, ssh://, git://)
    parsed = urlparse(repo_url)
    return parsed.hostname


def _default_clone_dest(repo_url: str) -> str:
    """Compute a sensible default destination directory from a repo URL.

    ``https://example.com/org/myproj.git`` → ``myproj``.
    """
    # Strip trailing slash, take the last path component, drop ``.git``.
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail or "repo"


# ── Registration ──────────────────────────────────────────────────────


def register_git_tools(
    registry: ToolRegistry,
    workspace_root: str,
    git_clone_allowed_hosts: str = "",
    *,
    include_commit: bool = True,
) -> None:
    """Register git tools (diff, log, checkout, clone, optionally commit).

    Args:
        registry: The tool registry to populate.
        workspace_root: Working directory for git commands.
        git_clone_allowed_hosts: Comma/space-separated hostnames that
            ``git_clone`` accepts.  Empty disables ``git_clone``
            (fail-closed for security).
        include_commit: When ``True`` (default), registers ``git_commit``
            alongside the other tools.  The orchestrator-side registry
            keeps this on because the orchestrator is the component
            that finalises work (``push_and_create_pr`` composes
            ``git_commit`` under the hood).  Sub-agents pass ``False``:
            per design §7.1 they describe their changes and let the
            orchestrator assemble the commit, so ``git_commit`` is
            not part of their surface.
    """
    registry.register(_make_git_diff(workspace_root))
    if include_commit:
        registry.register(_make_git_commit(workspace_root))
    registry.register(_make_git_log(workspace_root))
    registry.register(_make_git_checkout(workspace_root))
    allowed = _parse_allowed_hosts(git_clone_allowed_hosts)
    registry.register(_make_git_clone(workspace_root, allowed))
