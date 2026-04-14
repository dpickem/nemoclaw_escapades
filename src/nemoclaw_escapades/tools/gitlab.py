"""GitLab REST API v4 tools for the orchestrator.

Provides an async GitLab client and registers individual operations as
tools.  Lifted from ``nv_tools.clients.gitlab.GitLabClient`` and
converted to async httpx + tool-registry integration.

**Auth model:**  Uses ``Authorization: Bearer <PAT>`` populated from
the ``GITLAB_TOKEN`` environment variable.  Inside an OpenShell sandbox
the token is a proxy placeholder that the L7 proxy resolves at request
time — the application never sees the real credential.

**Tool categories:**

- **Read** (18 tools) — projects, MRs, pipelines, commits, branches,
  notes, discussions, approvals, file contents, diffs.
- **Write** (10 tools) — create/update MRs, comment, reply to /
  resolve discussions, approve/unapprove, merge, rebase.  All write
  tools are registered with ``is_read_only=False`` so the approval gate
  presents Approve / Deny buttons before execution.

**Module-level state:**  ``_gitlab_config`` stores the active
``GitLabConfig``; it is set once by ``register_gitlab_tools`` and read
by every handler via ``_get_client()``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, unquote

import httpx

from nemoclaw_escapades.config import GitLabConfig
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec

logger = get_logger("tools.gitlab")

_gitlab_config: GitLabConfig | None = None

_REQUEST_TIMEOUT_SECONDS: float = 30.0
_ERROR_BODY_MAX_CHARS: int = 500
_HTTP_ERROR_THRESHOLD: int = 400
_DEFAULT_SEARCH_LIMIT: int = 10
_DEFAULT_PER_PAGE: int = 20


# ---------------------------------------------------------------------------
# Async GitLab client
# ---------------------------------------------------------------------------


def _encode_path(value: str) -> str:
    """URL-encode a project path or file path, tolerating pre-encoded input.

    Decodes first so that ``dpickem%2Fnv_tools`` and ``dpickem/nv_tools``
    both produce ``dpickem%2Fnv_tools`` — never double-encoded.
    """
    return quote(unquote(value), safe="")


class GitLabClient:
    """Async GitLab REST API v4 client.

    Wraps ``httpx.AsyncClient`` with lazy initialisation and automatic
    Bearer-token injection.  All public methods return parsed JSON
    (``dict`` or ``list``) on success, or a dict with an ``"error"``
    key on failure — callers never need to inspect HTTP status codes.

    Attributes:
        base_url: GitLab instance URL (scheme + host, no ``/api/v4``
            suffix).  The ``/api/v4`` prefix is appended internally.
    """

    def __init__(self, base_url: str, token: str = "") -> None:
        """Initialise the client.

        Args:
            base_url: GitLab instance URL.  Trailing ``/`` and
                ``/api/v4`` are stripped automatically so callers can
                pass either ``https://gitlab.example.com`` or
                ``https://gitlab.example.com/api/v4``.
            token: Personal Access Token (``glpat-…``) or proxy
                placeholder.  An empty string disables all API calls
                (``configured`` returns ``False``).
        """
        self.base_url = base_url.rstrip("/").rstrip("/api/v4")
        self._token = token
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """Return ``True`` when both base URL and token are set."""
        return bool(self.base_url and self._token)

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared ``httpx.AsyncClient``, creating it on first call.

        Returns:
            A long-lived client with Bearer auth and JSON content-type
            headers pre-configured.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.base_url}/api/v4",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._token}",
                },
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP connection pool and release resources."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """Execute an API request and return parsed JSON.

        Args:
            method: HTTP method (``GET``, ``POST``, ``PUT``, …).
            endpoint: API path relative to ``/api/v4``
                (e.g. ``/projects/123/merge_requests``).
            **kwargs: Forwarded to ``httpx.AsyncClient.request``
                (common: ``params``, ``json``).

        Returns:
            Parsed JSON response on success (2xx/3xx), or a dict
            containing ``error``, ``status_code``, and truncated
            ``body`` on 4xx/5xx.
        """
        if not self.configured:
            return {"error": "GitLab not configured. Set GITLAB_TOKEN."}
        client = await self._get_client()
        response = await client.request(method, endpoint, **kwargs)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            return {
                "error": f"GitLab API returned {response.status_code}",
                "status_code": response.status_code,
                "body": response.text[:_ERROR_BODY_MAX_CHARS],
            }
        return response.json()  # type: ignore[no-any-return]

    async def _request_text(self, endpoint: str, **kwargs: Any) -> str:
        """Execute a GET request and return the raw response body as text.

        Used for endpoints that return non-JSON content (e.g. CI job
        trace logs).  Error responses are returned as a JSON string so
        callers can always attempt ``json.loads`` for error detection.

        Args:
            endpoint: API path relative to ``/api/v4``.
            **kwargs: Forwarded to ``httpx.AsyncClient.get``.

        Returns:
            Raw response text on success, or a JSON-encoded error dict
            on failure.
        """
        if not self.configured:
            return '{"error": "GitLab not configured. Set GITLAB_TOKEN."}'
        client = await self._get_client()
        response = await client.get(endpoint, **kwargs)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            return json.dumps(
                {
                    "error": f"GitLab API returned {response.status_code}",
                    "status_code": response.status_code,
                    "body": response.text[:_ERROR_BODY_MAX_CHARS],
                }
            )
        return response.text

    # -- READ operations ---------------------------------------------------

    async def search_projects(
        self, query: str, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any] | list[Any]:
        """Search projects by name, path, or description.

        Args:
            query: Free-text search string.
            limit: Maximum number of results to return.

        Returns:
            A list of project dicts, or an error dict.
        """
        return await self._request("GET", "/projects", params={"search": query, "per_page": limit})

    async def get_project(self, project_id: str) -> dict[str, Any]:
        """Fetch a single project by numeric ID or ``group/project`` path.

        Args:
            project_id: Numeric ID or slash-separated path.  Both
                plain (``group/project``) and pre-encoded
                (``group%2Fproject``) forms are accepted.

        Returns:
            Full project metadata dict, or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request("GET", f"/projects/{encoded}")

    async def list_merge_requests(
        self,
        project_id: str,
        state: str = "opened",
        limit: int = _DEFAULT_PER_PAGE,
    ) -> dict[str, Any] | list[Any]:
        """List merge requests for a project.

        Args:
            project_id: Project ID or path.
            state: Filter by MR state (``opened``, ``closed``,
                ``merged``, ``all``).
            limit: Maximum number of MRs to return.

        Returns:
            A list of MR summary dicts, or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "GET",
            f"/projects/{encoded}/merge_requests",
            params={"state": state, "per_page": limit},
        )

    async def get_merge_request(self, project_id: str, mr_iid: int) -> dict[str, Any]:
        """Fetch full details of a single merge request.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID (the ``!N`` number).

        Returns:
            MR detail dict (title, author, reviewers, labels, etc.),
            or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request("GET", f"/projects/{encoded}/merge_requests/{mr_iid}")

    async def get_merge_request_changes(self, project_id: str, mr_iid: int) -> dict[str, Any]:
        """Fetch the diffs (file-level changes) of a merge request.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.

        Returns:
            A list of diff dicts (old/new path, diff content, renamed
            flag, etc.), or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request("GET", f"/projects/{encoded}/merge_requests/{mr_iid}/diffs")

    async def list_pipelines(
        self,
        project_id: str,
        limit: int = _DEFAULT_PER_PAGE,
    ) -> dict[str, Any] | list[Any]:
        """List CI/CD pipelines for a project, newest first.

        Args:
            project_id: Project ID or path.
            limit: Maximum number of pipelines to return.

        Returns:
            A list of pipeline summary dicts, or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "GET", f"/projects/{encoded}/pipelines", params={"per_page": limit}
        )

    async def get_pipeline(self, project_id: str, pipeline_id: int) -> dict[str, Any]:
        """Fetch details of a specific CI/CD pipeline.

        Args:
            project_id: Project ID or path.
            pipeline_id: Pipeline ID.

        Returns:
            Pipeline detail dict (status, ref, duration, coverage,
            etc.), or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request("GET", f"/projects/{encoded}/pipelines/{pipeline_id}")

    async def list_pipeline_jobs(
        self, project_id: str, pipeline_id: int
    ) -> dict[str, Any] | list[Any]:
        """List jobs belonging to a CI/CD pipeline.

        Args:
            project_id: Project ID or path.
            pipeline_id: Pipeline ID.

        Returns:
            A list of job dicts (name, stage, status, duration, etc.),
            or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request("GET", f"/projects/{encoded}/pipelines/{pipeline_id}/jobs")

    async def get_job_log(self, project_id: str, job_id: int) -> str:
        """Fetch the raw trace log of a CI/CD job.

        Args:
            project_id: Project ID or path.
            job_id: Job ID.

        Returns:
            Plain-text job log output, or a JSON error string.
        """
        encoded = _encode_path(project_id)
        return await self._request_text(f"/projects/{encoded}/jobs/{job_id}/trace")

    async def get_file(self, project_id: str, file_path: str, ref: str = "main") -> dict[str, Any]:
        """Fetch a file's metadata and base64-encoded content from a repository.

        Args:
            project_id: Project ID or path.
            file_path: Repository-relative path (e.g. ``src/main.py``).
            ref: Branch, tag, or commit SHA to read from.

        Returns:
            Dict with ``file_name``, ``file_path``, ``size``,
            ``encoding``, ``content`` (base64), ``ref``, etc., or
            an error dict.
        """
        proj = _encode_path(project_id)
        fpath = _encode_path(file_path)
        return await self._request(
            "GET", f"/projects/{proj}/repository/files/{fpath}", params={"ref": ref}
        )

    async def list_mr_notes(
        self,
        project_id: str,
        mr_iid: int,
        sort: str = "asc",
        limit: int = _DEFAULT_PER_PAGE,
    ) -> dict[str, Any] | list[Any]:
        """List notes (comments) on a merge request.

        Returns both human-authored comments and system-generated notes
        (approval events, status changes, pipeline results).

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.
            sort: Sort order — ``asc`` (oldest first) or ``desc``.
            limit: Maximum number of notes to return.

        Returns:
            A list of note dicts (``id``, ``body``, ``author``,
            ``system``, ``created_at``, etc.), or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "GET",
            f"/projects/{encoded}/merge_requests/{mr_iid}/notes",
            params={"sort": sort, "per_page": limit},
        )

    async def list_mr_discussions(
        self,
        project_id: str,
        mr_iid: int,
        limit: int = _DEFAULT_PER_PAGE,
    ) -> dict[str, Any] | list[Any]:
        """List threaded discussions on a merge request.

        Each discussion groups one or more notes into a conversation
        thread.  Inline code-review comments share a discussion when
        they form a reply chain.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.
            limit: Maximum number of discussion threads to return.

        Returns:
            A list of discussion dicts, each containing ``id``,
            ``individual_note``, ``notes`` (list), and for resolvable
            threads, ``resolved`` and ``resolved_by``.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "GET",
            f"/projects/{encoded}/merge_requests/{mr_iid}/discussions",
            params={"per_page": limit},
        )

    async def get_mr_approvals(self, project_id: str, mr_iid: int) -> dict[str, Any]:
        """Fetch the approval state of a merge request.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.

        Returns:
            Dict with ``approved``, ``approved_by`` (list of users),
            ``approval_rules_left``, etc., or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request("GET", f"/projects/{encoded}/merge_requests/{mr_iid}/approvals")

    async def list_branches(
        self,
        project_id: str,
        search: str = "",
        limit: int = _DEFAULT_PER_PAGE,
    ) -> dict[str, Any] | list[Any]:
        """List repository branches, optionally filtered by name substring.

        Args:
            project_id: Project ID or path.
            search: If non-empty, only branches whose name contains
                this substring are returned.
            limit: Maximum number of branches to return.

        Returns:
            A list of branch dicts (``name``, ``merged``, ``protected``,
            ``commit``, etc.), or an error dict.
        """
        encoded = _encode_path(project_id)
        params: dict[str, Any] = {"per_page": limit}
        if search:
            params["search"] = search
        return await self._request("GET", f"/projects/{encoded}/repository/branches", params=params)

    async def list_commits(
        self,
        project_id: str,
        ref: str = "main",
        limit: int = _DEFAULT_PER_PAGE,
    ) -> dict[str, Any] | list[Any]:
        """List commits on a branch or ref, newest first.

        Args:
            project_id: Project ID or path.
            ref: Branch name, tag, or commit SHA.
            limit: Maximum number of commits to return.

        Returns:
            A list of commit dicts (``id``, ``short_id``, ``title``,
            ``author_name``, ``created_at``, etc.), or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "GET",
            f"/projects/{encoded}/repository/commits",
            params={"ref_name": ref, "per_page": limit},
        )

    async def get_commit(self, project_id: str, sha: str) -> dict[str, Any]:
        """Fetch details of a specific commit.

        Args:
            project_id: Project ID or path.
            sha: Full or abbreviated commit SHA.

        Returns:
            Commit detail dict (``message``, ``author_name``,
            ``stats``, ``parent_ids``, ``last_pipeline``, etc.), or
            an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request("GET", f"/projects/{encoded}/repository/commits/{sha}")

    async def compare(self, project_id: str, from_ref: str, to_ref: str) -> dict[str, Any]:
        """Compare two branches, tags, or commits.

        Useful for generating release notes or understanding what
        changed between two points in history.

        Args:
            project_id: Project ID or path.
            from_ref: Base ref (older).
            to_ref: Head ref (newer).

        Returns:
            Dict with ``commits`` (list) and ``diffs`` (list of file
            diffs), or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "GET",
            f"/projects/{encoded}/repository/compare",
            params={"from": from_ref, "to": to_ref},
        )

    async def get_current_user(self) -> dict[str, Any]:
        """Fetch the profile of the authenticated user.

        Returns:
            User dict (``id``, ``username``, ``name``, ``email``,
            ``avatar_url``, etc.), or an error dict.
        """
        return await self._request("GET", "/user")

    # -- WRITE operations --------------------------------------------------

    async def create_merge_request_note(
        self, project_id: str, mr_iid: int, body: str
    ) -> dict[str, Any]:
        """Post a new note (comment) on a merge request.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.
            body: Comment text (Markdown).

        Returns:
            The created note dict, or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "POST", f"/projects/{encoded}/merge_requests/{mr_iid}/notes", json={"body": body}
        )

    async def update_merge_request_note(
        self, project_id: str, mr_iid: int, note_id: int, body: str
    ) -> dict[str, Any]:
        """Edit the body of an existing note on a merge request.

        Only the note's author (or an admin) can update it.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.
            note_id: ID of the note to edit.
            body: Replacement text (Markdown).

        Returns:
            The updated note dict, or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "PUT",
            f"/projects/{encoded}/merge_requests/{mr_iid}/notes/{note_id}",
            json={"body": body},
        )

    async def reply_to_discussion(
        self, project_id: str, mr_iid: int, discussion_id: str, body: str
    ) -> dict[str, Any]:
        """Add a reply note to an existing MR discussion thread.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.
            discussion_id: Discussion ID (from ``list_mr_discussions``).
            body: Reply text (Markdown).

        Returns:
            The created note dict within the discussion, or an error
            dict.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "POST",
            f"/projects/{encoded}/merge_requests/{mr_iid}/discussions/{discussion_id}/notes",
            json={"body": body},
        )

    async def resolve_discussion(
        self, project_id: str, mr_iid: int, discussion_id: str, resolved: bool = True
    ) -> dict[str, Any]:
        """Resolve or unresolve an MR discussion thread.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.
            discussion_id: Discussion ID (from ``list_mr_discussions``).
            resolved: ``True`` to mark resolved, ``False`` to reopen.

        Returns:
            The updated discussion dict, or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "PUT",
            f"/projects/{encoded}/merge_requests/{mr_iid}/discussions/{discussion_id}",
            json={"resolved": resolved},
        )

    async def approve_merge_request(self, project_id: str, mr_iid: int) -> dict[str, Any]:
        """Approve a merge request on behalf of the authenticated user.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.

        Returns:
            Empty dict on success (HTTP 201), or an error dict if
            already approved or approval rules prevent it.
        """
        encoded = _encode_path(project_id)
        return await self._request("POST", f"/projects/{encoded}/merge_requests/{mr_iid}/approve")

    async def unapprove_merge_request(self, project_id: str, mr_iid: int) -> dict[str, Any]:
        """Remove the authenticated user's approval from a merge request.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.

        Returns:
            Empty dict on success, or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request("POST", f"/projects/{encoded}/merge_requests/{mr_iid}/unapprove")

    async def merge_merge_request(
        self,
        project_id: str,
        mr_iid: int,
        merge_commit_message: str = "",
        squash: bool = False,
        should_remove_source_branch: bool = False,
    ) -> dict[str, Any]:
        """Merge (accept) a merge request.

        The MR must be in a mergeable state (no conflicts, pipeline
        passed if required, approvals met).

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.
            merge_commit_message: Custom merge commit message.  If
                empty, GitLab uses its default template.
            squash: Squash all commits into a single commit before
                merging.
            should_remove_source_branch: Delete the source branch
                after a successful merge.

        Returns:
            The merged MR dict, or an error dict (e.g. 406 if the MR
            cannot be merged).
        """
        encoded = _encode_path(project_id)
        payload: dict[str, Any] = {}
        if merge_commit_message:
            payload["merge_commit_message"] = merge_commit_message
        if squash:
            payload["squash"] = True
        if should_remove_source_branch:
            payload["should_remove_source_branch"] = True
        return await self._request(
            "PUT", f"/projects/{encoded}/merge_requests/{mr_iid}/merge", json=payload
        )

    async def rebase_merge_request(self, project_id: str, mr_iid: int) -> dict[str, Any]:
        """Rebase the MR's source branch onto the target branch.

        This is an asynchronous operation on the server side; the
        returned dict includes ``rebase_in_progress`` to indicate
        whether the rebase is still running.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.

        Returns:
            Dict with ``rebase_in_progress`` flag, or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request("PUT", f"/projects/{encoded}/merge_requests/{mr_iid}/rebase")

    async def update_merge_request(
        self,
        project_id: str,
        mr_iid: int,
        **fields: Any,
    ) -> dict[str, Any]:
        """Update one or more fields on a merge request.

        Accepts any field supported by the GitLab MR update API:
        ``title``, ``description``, ``labels``, ``assignee_ids``,
        ``reviewer_ids``, ``target_branch``, ``state_event``
        (``close`` / ``reopen``), etc.

        Args:
            project_id: Project ID or path.
            mr_iid: Merge request internal ID.
            **fields: MR fields to update (passed as the JSON body).

        Returns:
            The updated MR dict, or an error dict.
        """
        encoded = _encode_path(project_id)
        return await self._request(
            "PUT", f"/projects/{encoded}/merge_requests/{mr_iid}", json=fields
        )

    async def create_merge_request(
        self,
        project_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Create a new merge request.

        Args:
            project_id: Project ID or path.
            source_branch: Branch containing the changes.
            target_branch: Branch to merge into.
            title: MR title.
            description: MR description (Markdown).  Optional.

        Returns:
            The created MR dict, or an error dict.
        """
        encoded = _encode_path(project_id)
        payload: dict[str, Any] = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
        }
        if description:
            payload["description"] = description
        return await self._request("POST", f"/projects/{encoded}/merge_requests", json=payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format(data: Any) -> str:
    """Serialize *data* to a pretty-printed JSON string.

    Uses ``default=str`` so non-serializable values (e.g. dates) are
    converted to strings rather than raising.

    Args:
        data: Any JSON-serializable value.

    Returns:
        Indented JSON string.
    """
    return json.dumps(data, indent=2, default=str)


def _get_client() -> GitLabClient:
    """Return a fresh ``GitLabClient`` using the module-level config.

    A new client is created on each call (the underlying httpx client
    is lazily initialised inside ``GitLabClient``).

    Returns:
        Configured ``GitLabClient``.

    Raises:
        RuntimeError: If ``register_gitlab_tools`` has not been called.
    """
    if _gitlab_config is None:
        raise RuntimeError("GitLab tools not initialised — call register_gitlab_tools first")
    return GitLabClient(base_url=_gitlab_config.url, token=_gitlab_config.token)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def gitlab_search_projects(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
    """Search GitLab projects by name, path, or description.

    Args:
        query: Free-text search string.
        limit: Maximum number of results.

    Returns:
        JSON array of matching project objects.
    """
    return _format(await _get_client().search_projects(query, limit=limit))


async def gitlab_get_project(project_id: str) -> str:
    """Fetch a GitLab project by numeric ID or ``group/project`` path.

    Args:
        project_id: Numeric ID or slash-separated path.

    Returns:
        JSON object with full project metadata.
    """
    return _format(await _get_client().get_project(project_id))


async def gitlab_list_merge_requests(
    project_id: str, state: str = "opened", limit: int = _DEFAULT_PER_PAGE
) -> str:
    """List merge requests for a GitLab project.

    Args:
        project_id: Project ID or path.
        state: MR state filter (``opened``, ``closed``, ``merged``,
            ``all``).
        limit: Maximum number of MRs.

    Returns:
        JSON array of MR summary objects.
    """
    return _format(await _get_client().list_merge_requests(project_id, state=state, limit=limit))


async def gitlab_get_merge_request(project_id: str, mr_iid: int) -> str:
    """Fetch full details of a single merge request.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        JSON object with MR metadata (title, author, reviewers,
        labels, pipeline status, etc.).
    """
    return _format(await _get_client().get_merge_request(project_id, mr_iid))


async def gitlab_get_merge_request_changes(project_id: str, mr_iid: int) -> str:
    """Fetch the file-level diffs of a merge request.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        JSON array of diff objects (old/new path, diff content).
    """
    return _format(await _get_client().get_merge_request_changes(project_id, mr_iid))


async def gitlab_list_pipelines(project_id: str, limit: int = _DEFAULT_PER_PAGE) -> str:
    """List CI/CD pipelines for a project, newest first.

    Args:
        project_id: Project ID or path.
        limit: Maximum number of pipelines.

    Returns:
        JSON array of pipeline summary objects.
    """
    return _format(await _get_client().list_pipelines(project_id, limit=limit))


async def gitlab_get_pipeline(project_id: str, pipeline_id: int) -> str:
    """Fetch details of a specific CI/CD pipeline.

    Args:
        project_id: Project ID or path.
        pipeline_id: Pipeline ID.

    Returns:
        JSON object with pipeline status, ref, duration, etc.
    """
    return _format(await _get_client().get_pipeline(project_id, pipeline_id))


async def gitlab_get_job_log(project_id: str, job_id: int) -> str:
    """Fetch the raw trace log of a CI/CD job.

    Args:
        project_id: Project ID or path.
        job_id: Job ID.

    Returns:
        Plain-text job log output.
    """
    return await _get_client().get_job_log(project_id, job_id)


async def gitlab_me() -> str:
    """Fetch the authenticated GitLab user's profile.

    Returns:
        JSON object with user ID, username, name, email, etc.
    """
    return _format(await _get_client().get_current_user())


async def gitlab_list_pipeline_jobs(project_id: str, pipeline_id: int) -> str:
    """List jobs belonging to a CI/CD pipeline.

    Args:
        project_id: Project ID or path.
        pipeline_id: Pipeline ID.

    Returns:
        JSON array of job objects (name, stage, status, duration).
    """
    return _format(await _get_client().list_pipeline_jobs(project_id, pipeline_id))


async def gitlab_get_file(project_id: str, file_path: str, ref: str = "main") -> str:
    """Fetch a file from a GitLab repository.

    Args:
        project_id: Project ID or path.
        file_path: Repository-relative file path.
        ref: Branch, tag, or commit SHA.

    Returns:
        JSON object with file metadata and base64-encoded content.
    """
    return _format(await _get_client().get_file(project_id, file_path, ref=ref))


async def gitlab_list_mr_notes(
    project_id: str, mr_iid: int, sort: str = "asc", limit: int = _DEFAULT_PER_PAGE
) -> str:
    """List notes (comments) on a merge request.

    Includes both human-authored comments and system-generated notes.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.
        sort: Sort order (``asc`` or ``desc``).
        limit: Maximum number of notes.

    Returns:
        JSON array of note objects.
    """
    return _format(await _get_client().list_mr_notes(project_id, mr_iid, sort=sort, limit=limit))


async def gitlab_list_mr_discussions(
    project_id: str, mr_iid: int, limit: int = _DEFAULT_PER_PAGE
) -> str:
    """List threaded discussions on a merge request.

    Each discussion contains one or more notes grouped as a
    conversation thread, with resolution status.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.
        limit: Maximum number of discussion threads.

    Returns:
        JSON array of discussion objects.
    """
    return _format(await _get_client().list_mr_discussions(project_id, mr_iid, limit=limit))


async def gitlab_get_mr_approvals(project_id: str, mr_iid: int) -> str:
    """Fetch the approval state of a merge request.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        JSON object with ``approved``, ``approved_by``, and approval
        rules.
    """
    return _format(await _get_client().get_mr_approvals(project_id, mr_iid))


async def gitlab_list_branches(
    project_id: str, search: str = "", limit: int = _DEFAULT_PER_PAGE
) -> str:
    """List repository branches, optionally filtered by name.

    Args:
        project_id: Project ID or path.
        search: Substring filter on branch name.
        limit: Maximum number of branches.

    Returns:
        JSON array of branch objects.
    """
    return _format(await _get_client().list_branches(project_id, search=search, limit=limit))


async def gitlab_list_commits(
    project_id: str, ref: str = "main", limit: int = _DEFAULT_PER_PAGE
) -> str:
    """List recent commits on a branch or ref.

    Args:
        project_id: Project ID or path.
        ref: Branch name, tag, or commit SHA.
        limit: Maximum number of commits.

    Returns:
        JSON array of commit summary objects.
    """
    return _format(await _get_client().list_commits(project_id, ref=ref, limit=limit))


async def gitlab_get_commit(project_id: str, sha: str) -> str:
    """Fetch details of a specific commit.

    Args:
        project_id: Project ID or path.
        sha: Full or abbreviated commit SHA.

    Returns:
        JSON object with message, author, stats, and parent SHAs.
    """
    return _format(await _get_client().get_commit(project_id, sha))


async def gitlab_compare(project_id: str, from_ref: str, to_ref: str) -> str:
    """Compare two branches, tags, or commits.

    Args:
        project_id: Project ID or path.
        from_ref: Base ref (older).
        to_ref: Head ref (newer).

    Returns:
        JSON object with ``commits`` and ``diffs`` arrays.
    """
    return _format(await _get_client().compare(project_id, from_ref, to_ref))


async def gitlab_create_mr_note(project_id: str, mr_iid: int, body: str) -> str:
    """Post a new comment on a merge request.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.
        body: Comment text (Markdown).

    Returns:
        JSON object of the created note.
    """
    return _format(await _get_client().create_merge_request_note(project_id, mr_iid, body))


async def gitlab_update_mr_note(project_id: str, mr_iid: int, note_id: int, body: str) -> str:
    """Edit an existing note on a merge request.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.
        note_id: ID of the note to edit.
        body: Replacement text (Markdown).

    Returns:
        JSON object of the updated note.
    """
    return _format(await _get_client().update_merge_request_note(project_id, mr_iid, note_id, body))


async def gitlab_reply_to_discussion(
    project_id: str, mr_iid: int, discussion_id: str, body: str
) -> str:
    """Reply to an existing discussion thread on a merge request.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.
        discussion_id: Discussion ID (from ``gitlab_list_mr_discussions``).
        body: Reply text (Markdown).

    Returns:
        JSON object of the created reply note.
    """
    return _format(await _get_client().reply_to_discussion(project_id, mr_iid, discussion_id, body))


async def gitlab_resolve_discussion(
    project_id: str, mr_iid: int, discussion_id: str, resolved: bool = True
) -> str:
    """Resolve or unresolve a discussion thread on a merge request.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.
        discussion_id: Discussion ID (from ``gitlab_list_mr_discussions``).
        resolved: ``True`` to mark resolved, ``False`` to reopen.

    Returns:
        JSON object of the updated discussion.
    """
    return _format(
        await _get_client().resolve_discussion(project_id, mr_iid, discussion_id, resolved)
    )


async def gitlab_approve_mr(project_id: str, mr_iid: int) -> str:
    """Approve a merge request on behalf of the authenticated user.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        JSON confirmation or error if already approved.
    """
    return _format(await _get_client().approve_merge_request(project_id, mr_iid))


async def gitlab_unapprove_mr(project_id: str, mr_iid: int) -> str:
    """Remove the authenticated user's approval from a merge request.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        JSON confirmation or error.
    """
    return _format(await _get_client().unapprove_merge_request(project_id, mr_iid))


async def gitlab_merge_mr(
    project_id: str,
    mr_iid: int,
    merge_commit_message: str = "",
    squash: bool = False,
    should_remove_source_branch: bool = False,
) -> str:
    """Merge (accept) a merge request.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.
        merge_commit_message: Custom merge commit message.
        squash: Squash commits into one before merging.
        should_remove_source_branch: Delete the source branch after
            merge.

    Returns:
        JSON object of the merged MR, or error if not mergeable.
    """
    return _format(
        await _get_client().merge_merge_request(
            project_id,
            mr_iid,
            merge_commit_message=merge_commit_message,
            squash=squash,
            should_remove_source_branch=should_remove_source_branch,
        )
    )


async def gitlab_rebase_mr(project_id: str, mr_iid: int) -> str:
    """Rebase a merge request's source branch onto the target branch.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        JSON object with ``rebase_in_progress`` status flag.
    """
    return _format(await _get_client().rebase_merge_request(project_id, mr_iid))


async def gitlab_update_mr(
    project_id: str,
    mr_iid: int,
    title: str = "",
    description: str = "",
    labels: str = "",
    assignee_ids: str = "",
    reviewer_ids: str = "",
    target_branch: str = "",
    state_event: str = "",
) -> str:
    """Update one or more fields on a merge request.

    Only non-empty arguments are sent to the API, so callers can
    selectively update individual fields without affecting others.

    Args:
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.
        title: New MR title.
        description: New MR description (Markdown).
        labels: Comma-separated labels (replaces all existing).
        assignee_ids: Comma-separated user IDs to assign.
        reviewer_ids: Comma-separated user IDs to add as reviewers.
        target_branch: Change the target branch.
        state_event: State transition (``close`` or ``reopen``).

    Returns:
        JSON object of the updated MR, or an error if no fields were
        provided.
    """
    fields: dict[str, Any] = {}
    if title:
        fields["title"] = title
    if description:
        fields["description"] = description
    if labels:
        fields["labels"] = labels
    if assignee_ids:
        fields["assignee_ids"] = [int(i) for i in assignee_ids.split(",")]
    if reviewer_ids:
        fields["reviewer_ids"] = [int(i) for i in reviewer_ids.split(",")]
    if target_branch:
        fields["target_branch"] = target_branch
    if state_event:
        fields["state_event"] = state_event
    if not fields:
        return _format({"error": "No fields provided to update"})
    return _format(await _get_client().update_merge_request(project_id, mr_iid, **fields))


async def gitlab_create_mr(
    project_id: str,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str = "",
) -> str:
    """Create a new merge request.

    Args:
        project_id: Project ID or path.
        source_branch: Branch containing the changes.
        target_branch: Branch to merge into.
        title: MR title.
        description: MR description (Markdown).

    Returns:
        JSON object of the created MR.
    """
    return _format(
        await _get_client().create_merge_request(
            project_id, source_branch, target_branch, title, description=description
        )
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _gitlab_available() -> bool:
    """Return ``True`` when the GitLab module is configured and usable.

    Called by each ``ToolSpec.check_fn`` at registration time.  If this
    returns ``False`` (e.g. token not set), the entire ``gitlab``
    toolset is skipped.
    """
    return _gitlab_config is not None and _get_client().configured


def register_gitlab_tools(registry: ToolRegistry, config: GitLabConfig) -> None:
    """Register all GitLab read and write tools with the tool registry.

    Sets the module-level ``_gitlab_config`` and registers 18 read
    tools and 10 write tools.  Write tools are marked
    ``is_read_only=False`` so the approval gate intercepts them.

    If the config is missing a token (``configured`` is ``False``),
    all tools are silently skipped at registration time.

    Args:
        registry: The orchestrator's tool registry.
        config: GitLab connection settings (URL + token).
    """
    global _gitlab_config  # noqa: PLW0603
    _gitlab_config = config

    _ts = "gitlab"
    _ck = _gitlab_available

    registry.register(
        ToolSpec(
            name="gitlab_search_projects",
            display_name="Searching GitLab projects",
            description="Search GitLab projects by name or path.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results", "default": 10},
                },
                "required": ["query"],
            },
            handler=gitlab_search_projects,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_get_project",
            display_name="Getting GitLab project",
            description=("Get a GitLab project by numeric ID or path (e.g. 'group/project')."),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project ID or path (e.g. 'dpickem/nv_tools')",
                    },
                },
                "required": ["project_id"],
            },
            handler=gitlab_get_project,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_list_merge_requests",
            display_name="Listing merge requests",
            description="List merge requests for a GitLab project. Filterable by state.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "state": {
                        "type": "string",
                        "description": "MR state filter: opened, closed, merged, all",
                        "default": "opened",
                    },
                    "limit": {"type": "integer", "description": "Max results", "default": 20},
                },
                "required": ["project_id"],
            },
            handler=gitlab_list_merge_requests,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_get_merge_request",
            display_name="Getting merge request",
            description="Get full details of a GitLab merge request by IID.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                },
                "required": ["project_id", "mr_iid"],
            },
            handler=gitlab_get_merge_request,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_get_merge_request_changes",
            display_name="Getting MR changes",
            description="Get the diff/changes of a GitLab merge request.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                },
                "required": ["project_id", "mr_iid"],
            },
            handler=gitlab_get_merge_request_changes,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_list_pipelines",
            display_name="Listing pipelines",
            description="List CI/CD pipelines for a GitLab project.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "limit": {"type": "integer", "description": "Max results", "default": 20},
                },
                "required": ["project_id"],
            },
            handler=gitlab_list_pipelines,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_get_pipeline",
            display_name="Getting pipeline details",
            description="Get details of a specific CI/CD pipeline.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "pipeline_id": {"type": "integer", "description": "Pipeline ID"},
                },
                "required": ["project_id", "pipeline_id"],
            },
            handler=gitlab_get_pipeline,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_get_job_log",
            display_name="Getting job log",
            description="Get the log output of a CI/CD job. Returns raw text.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "job_id": {"type": "integer", "description": "Job ID"},
                },
                "required": ["project_id", "job_id"],
            },
            handler=gitlab_get_job_log,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_list_pipeline_jobs",
            display_name="Listing pipeline jobs",
            description="List jobs for a CI/CD pipeline.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "pipeline_id": {"type": "integer", "description": "Pipeline ID"},
                },
                "required": ["project_id", "pipeline_id"],
            },
            handler=gitlab_list_pipeline_jobs,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_get_file",
            display_name="Getting file from GitLab",
            description=(
                "Get a file from a GitLab repository. Returns file metadata and "
                "base64-encoded content. Use for reading config files, READMEs, etc."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file in the repo (e.g. 'src/main.py')",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Branch, tag, or commit SHA",
                        "default": "main",
                    },
                },
                "required": ["project_id", "file_path"],
            },
            handler=gitlab_get_file,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_list_mr_notes",
            display_name="Listing MR comments",
            description=(
                "List notes (comments) on a merge request. Includes both user "
                "comments and system notes (approvals, status changes)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                    "sort": {
                        "type": "string",
                        "description": "Sort order: asc or desc",
                        "default": "asc",
                    },
                    "limit": {"type": "integer", "description": "Max results", "default": 20},
                },
                "required": ["project_id", "mr_iid"],
            },
            handler=gitlab_list_mr_notes,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_list_mr_discussions",
            display_name="Listing MR discussions",
            description=(
                "List threaded discussions on a merge request. Each discussion "
                "contains one or more notes grouped as a conversation thread, "
                "including whether the thread is resolved."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                    "limit": {"type": "integer", "description": "Max results", "default": 20},
                },
                "required": ["project_id", "mr_iid"],
            },
            handler=gitlab_list_mr_discussions,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_get_mr_approvals",
            display_name="Getting MR approvals",
            description="Get the approval state of a merge request (who approved, rules, etc.).",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                },
                "required": ["project_id", "mr_iid"],
            },
            handler=gitlab_get_mr_approvals,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_list_branches",
            display_name="Listing branches",
            description="List branches in a GitLab project. Optionally filter by name.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "search": {
                        "type": "string",
                        "description": "Filter branches by name substring",
                        "default": "",
                    },
                    "limit": {"type": "integer", "description": "Max results", "default": 20},
                },
                "required": ["project_id"],
            },
            handler=gitlab_list_branches,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_list_commits",
            display_name="Listing commits",
            description="List recent commits on a branch or ref.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "ref": {
                        "type": "string",
                        "description": "Branch name, tag, or commit SHA",
                        "default": "main",
                    },
                    "limit": {"type": "integer", "description": "Max results", "default": 20},
                },
                "required": ["project_id"],
            },
            handler=gitlab_list_commits,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_get_commit",
            display_name="Getting commit details",
            description="Get details of a specific commit (message, author, stats, parent SHAs).",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "sha": {"type": "string", "description": "Commit SHA (full or short)"},
                },
                "required": ["project_id", "sha"],
            },
            handler=gitlab_get_commit,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_compare",
            display_name="Comparing refs",
            description=(
                "Compare two branches, tags, or commits. Returns the commits between "
                "them and the diff. Useful for understanding what changed between releases."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "from_ref": {
                        "type": "string",
                        "description": "Base ref (branch, tag, or SHA)",
                    },
                    "to_ref": {
                        "type": "string",
                        "description": "Head ref (branch, tag, or SHA)",
                    },
                },
                "required": ["project_id", "from_ref", "to_ref"],
            },
            handler=gitlab_compare,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_me",
            display_name="Checking GitLab profile",
            description="Get the authenticated GitLab user's profile.",
            input_schema={"type": "object", "properties": {}},
            handler=gitlab_me,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    # -- WRITE tools (all require approval) -----------------------------------

    registry.register(
        ToolSpec(
            name="gitlab_create_mr_note",
            display_name="Commenting on merge request",
            description="Add a comment/note to a GitLab merge request. Requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                    "body": {"type": "string", "description": "Comment body (Markdown)"},
                },
                "required": ["project_id", "mr_iid", "body"],
            },
            handler=gitlab_create_mr_note,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_update_mr_note",
            display_name="Editing MR comment",
            description="Edit an existing note/comment on a merge request. Requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                    "note_id": {"type": "integer", "description": "Note ID to edit"},
                    "body": {"type": "string", "description": "Updated comment body (Markdown)"},
                },
                "required": ["project_id", "mr_iid", "note_id", "body"],
            },
            handler=gitlab_update_mr_note,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_reply_to_discussion",
            display_name="Replying to MR discussion",
            description=(
                "Reply to an existing discussion thread on a merge request. "
                "Use gitlab_list_mr_discussions to get discussion IDs. Requires approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                    "discussion_id": {
                        "type": "string",
                        "description": "Discussion ID (from gitlab_list_mr_discussions)",
                    },
                    "body": {"type": "string", "description": "Reply body (Markdown)"},
                },
                "required": ["project_id", "mr_iid", "discussion_id", "body"],
            },
            handler=gitlab_reply_to_discussion,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_resolve_discussion",
            display_name="Resolving MR discussion",
            description=(
                "Resolve or unresolve a discussion thread on a merge request. "
                "Use gitlab_list_mr_discussions to get discussion IDs. Requires approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                    "discussion_id": {
                        "type": "string",
                        "description": "Discussion ID (from gitlab_list_mr_discussions)",
                    },
                    "resolved": {
                        "type": "boolean",
                        "description": "True to resolve, false to unresolve",
                        "default": True,
                    },
                },
                "required": ["project_id", "mr_iid", "discussion_id"],
            },
            handler=gitlab_resolve_discussion,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_approve_mr",
            display_name="Approving merge request",
            description="Approve a merge request. Requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                },
                "required": ["project_id", "mr_iid"],
            },
            handler=gitlab_approve_mr,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_unapprove_mr",
            display_name="Removing MR approval",
            description="Remove your approval from a merge request. Requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                },
                "required": ["project_id", "mr_iid"],
            },
            handler=gitlab_unapprove_mr,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_merge_mr",
            display_name="Merging merge request",
            description=(
                "Merge a merge request. Optionally squash commits and/or remove "
                "the source branch. Requires approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                    "merge_commit_message": {
                        "type": "string",
                        "description": "Custom merge commit message (optional)",
                    },
                    "squash": {
                        "type": "boolean",
                        "description": "Squash commits into one before merging",
                        "default": False,
                    },
                    "should_remove_source_branch": {
                        "type": "boolean",
                        "description": "Delete the source branch after merge",
                        "default": False,
                    },
                },
                "required": ["project_id", "mr_iid"],
            },
            handler=gitlab_merge_mr,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_rebase_mr",
            display_name="Rebasing merge request",
            description=(
                "Rebase a merge request's source branch onto the target branch. Requires approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                },
                "required": ["project_id", "mr_iid"],
            },
            handler=gitlab_rebase_mr,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_update_mr",
            display_name="Updating merge request",
            description=(
                "Update merge request fields: title, description, labels, "
                "assignees, reviewers, target branch. Use state_event='close' or "
                "'reopen' to change MR state. Requires approval."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "mr_iid": {"type": "integer", "description": "Merge request IID"},
                    "title": {"type": "string", "description": "New title"},
                    "description": {"type": "string", "description": "New description (Markdown)"},
                    "labels": {
                        "type": "string",
                        "description": "Comma-separated labels (replaces all existing)",
                    },
                    "assignee_ids": {
                        "type": "string",
                        "description": "Comma-separated user IDs to assign",
                    },
                    "reviewer_ids": {
                        "type": "string",
                        "description": "Comma-separated user IDs to add as reviewers",
                    },
                    "target_branch": {"type": "string", "description": "Change target branch"},
                    "state_event": {
                        "type": "string",
                        "description": "State transition: 'close' or 'reopen'",
                    },
                },
                "required": ["project_id", "mr_iid"],
            },
            handler=gitlab_update_mr,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )

    registry.register(
        ToolSpec(
            name="gitlab_create_mr",
            display_name="Creating merge request",
            description="Create a new merge request. Requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID or path"},
                    "source_branch": {"type": "string", "description": "Branch with the changes"},
                    "target_branch": {"type": "string", "description": "Branch to merge into"},
                    "title": {"type": "string", "description": "MR title"},
                    "description": {
                        "type": "string",
                        "description": "MR description (Markdown)",
                    },
                },
                "required": ["project_id", "source_branch", "target_branch", "title"],
            },
            handler=gitlab_create_mr,
            is_read_only=False,
            toolset=_ts,
            check_fn=_ck,
        )
    )
