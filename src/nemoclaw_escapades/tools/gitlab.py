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
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, unquote

import httpx

from nemoclaw_escapades.config import GitLabConfig
from nemoclaw_escapades.observability.logging import get_logger
from nemoclaw_escapades.tools.registry import ToolRegistry, ToolSpec, tool

logger = get_logger("tools.gitlab")

# ── Constants ─────────────────────────────────────────────────────────

# Seconds before an HTTP request to the GitLab API is aborted
_REQUEST_TIMEOUT_S: float = 30.0
# Max characters of response body included in error messages
_ERROR_BODY_MAX_CHARS: int = 500
# HTTP status codes at or above this threshold are treated as errors
_HTTP_ERROR_THRESHOLD: int = 400
# Default page size for search operations
_DEFAULT_SEARCH_LIMIT: int = 10
# Default page size for list operations
_DEFAULT_PER_PAGE: int = 20
# Logical toolset name used by the registry for grouping
_TOOLSET: str = "gitlab"


# ── Async GitLab client ──────────────────────────────────────────────


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
                timeout=_REQUEST_TIMEOUT_S,
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


# ── Helpers ───────────────────────────────────────────────────────────


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


# ── Tool specs ────────────────────────────────────────────────────────

# -- READ tools --------------------------------------------------------


def _make_gitlab_search_projects(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_search_projects`` tool spec."""

    @tool(
        "gitlab_search_projects",
        "Search GitLab projects by name or path.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["query"],
        },
        display_name="Searching GitLab projects",
        toolset=_TOOLSET,
    )
    async def gitlab_search_projects(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
        """Search GitLab projects by name, path, or description."""
        return _format(await client.search_projects(query, limit=limit))

    return gitlab_search_projects


def _make_gitlab_get_project(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_get_project`` tool spec."""

    @tool(
        "gitlab_get_project",
        "Get a GitLab project by numeric ID or path (e.g. 'group/project').",
        {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID or path (e.g. 'dpickem/nv_tools')",
                },
            },
            "required": ["project_id"],
        },
        display_name="Getting GitLab project",
        toolset=_TOOLSET,
    )
    async def gitlab_get_project(project_id: str) -> str:
        """Fetch a GitLab project by numeric ID or path."""
        return _format(await client.get_project(project_id))

    return gitlab_get_project


def _make_gitlab_list_merge_requests(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_list_merge_requests`` tool spec."""

    @tool(
        "gitlab_list_merge_requests",
        "List merge requests for a GitLab project. Filterable by state.",
        {
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
        display_name="Listing merge requests",
        toolset=_TOOLSET,
    )
    async def gitlab_list_merge_requests(
        project_id: str, state: str = "opened", limit: int = _DEFAULT_PER_PAGE
    ) -> str:
        """List merge requests for a GitLab project."""
        return _format(await client.list_merge_requests(project_id, state=state, limit=limit))

    return gitlab_list_merge_requests


def _make_gitlab_get_merge_request(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_get_merge_request`` tool spec."""

    @tool(
        "gitlab_get_merge_request",
        "Get full details of a GitLab merge request by IID.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "mr_iid": {"type": "integer", "description": "Merge request IID"},
            },
            "required": ["project_id", "mr_iid"],
        },
        display_name="Getting merge request",
        toolset=_TOOLSET,
    )
    async def gitlab_get_merge_request(project_id: str, mr_iid: int) -> str:
        """Fetch full details of a single merge request."""
        return _format(await client.get_merge_request(project_id, mr_iid))

    return gitlab_get_merge_request


def _make_gitlab_get_merge_request_changes(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_get_merge_request_changes`` tool spec."""

    @tool(
        "gitlab_get_merge_request_changes",
        "Get the diff/changes of a GitLab merge request.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "mr_iid": {"type": "integer", "description": "Merge request IID"},
            },
            "required": ["project_id", "mr_iid"],
        },
        display_name="Getting MR changes",
        toolset=_TOOLSET,
    )
    async def gitlab_get_merge_request_changes(project_id: str, mr_iid: int) -> str:
        """Fetch the file-level diffs of a merge request."""
        return _format(await client.get_merge_request_changes(project_id, mr_iid))

    return gitlab_get_merge_request_changes


def _make_gitlab_list_pipelines(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_list_pipelines`` tool spec."""

    @tool(
        "gitlab_list_pipelines",
        "List CI/CD pipelines for a GitLab project.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "limit": {"type": "integer", "description": "Max results", "default": 20},
            },
            "required": ["project_id"],
        },
        display_name="Listing pipelines",
        toolset=_TOOLSET,
    )
    async def gitlab_list_pipelines(project_id: str, limit: int = _DEFAULT_PER_PAGE) -> str:
        """List CI/CD pipelines for a project, newest first."""
        return _format(await client.list_pipelines(project_id, limit=limit))

    return gitlab_list_pipelines


def _make_gitlab_get_pipeline(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_get_pipeline`` tool spec."""

    @tool(
        "gitlab_get_pipeline",
        "Get details of a specific CI/CD pipeline.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "pipeline_id": {"type": "integer", "description": "Pipeline ID"},
            },
            "required": ["project_id", "pipeline_id"],
        },
        display_name="Getting pipeline details",
        toolset=_TOOLSET,
    )
    async def gitlab_get_pipeline(project_id: str, pipeline_id: int) -> str:
        """Fetch details of a specific CI/CD pipeline."""
        return _format(await client.get_pipeline(project_id, pipeline_id))

    return gitlab_get_pipeline


def _make_gitlab_get_job_log(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_get_job_log`` tool spec."""

    @tool(
        "gitlab_get_job_log",
        "Get the log output of a CI/CD job. Returns raw text.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "job_id": {"type": "integer", "description": "Job ID"},
            },
            "required": ["project_id", "job_id"],
        },
        display_name="Getting job log",
        toolset=_TOOLSET,
    )
    async def gitlab_get_job_log(project_id: str, job_id: int) -> str:
        """Fetch the raw trace log of a CI/CD job."""
        return await client.get_job_log(project_id, job_id)

    return gitlab_get_job_log


def _make_gitlab_list_pipeline_jobs(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_list_pipeline_jobs`` tool spec."""

    @tool(
        "gitlab_list_pipeline_jobs",
        "List jobs for a CI/CD pipeline.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "pipeline_id": {"type": "integer", "description": "Pipeline ID"},
            },
            "required": ["project_id", "pipeline_id"],
        },
        display_name="Listing pipeline jobs",
        toolset=_TOOLSET,
    )
    async def gitlab_list_pipeline_jobs(project_id: str, pipeline_id: int) -> str:
        """List jobs belonging to a CI/CD pipeline."""
        return _format(await client.list_pipeline_jobs(project_id, pipeline_id))

    return gitlab_list_pipeline_jobs


def _make_gitlab_get_file(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_get_file`` tool spec."""

    @tool(
        "gitlab_get_file",
        (
            "Get a file from a GitLab repository. Returns file metadata and "
            "base64-encoded content. Use for reading config files, READMEs, etc."
        ),
        {
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
        display_name="Getting file from GitLab",
        toolset=_TOOLSET,
    )
    async def gitlab_get_file(project_id: str, file_path: str, ref: str = "main") -> str:
        """Fetch a file from a GitLab repository."""
        return _format(await client.get_file(project_id, file_path, ref=ref))

    return gitlab_get_file


def _make_gitlab_list_mr_notes(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_list_mr_notes`` tool spec."""

    @tool(
        "gitlab_list_mr_notes",
        (
            "List notes (comments) on a merge request. Includes both user "
            "comments and system notes (approvals, status changes)."
        ),
        {
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
        display_name="Listing MR comments",
        toolset=_TOOLSET,
    )
    async def gitlab_list_mr_notes(
        project_id: str, mr_iid: int, sort: str = "asc", limit: int = _DEFAULT_PER_PAGE
    ) -> str:
        """List notes (comments) on a merge request."""
        return _format(await client.list_mr_notes(project_id, mr_iid, sort=sort, limit=limit))

    return gitlab_list_mr_notes


def _make_gitlab_list_mr_discussions(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_list_mr_discussions`` tool spec."""

    @tool(
        "gitlab_list_mr_discussions",
        (
            "List threaded discussions on a merge request. Each discussion "
            "contains one or more notes grouped as a conversation thread, "
            "including whether the thread is resolved."
        ),
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "mr_iid": {"type": "integer", "description": "Merge request IID"},
                "limit": {"type": "integer", "description": "Max results", "default": 20},
            },
            "required": ["project_id", "mr_iid"],
        },
        display_name="Listing MR discussions",
        toolset=_TOOLSET,
    )
    async def gitlab_list_mr_discussions(
        project_id: str, mr_iid: int, limit: int = _DEFAULT_PER_PAGE
    ) -> str:
        """List threaded discussions on a merge request."""
        return _format(await client.list_mr_discussions(project_id, mr_iid, limit=limit))

    return gitlab_list_mr_discussions


def _make_gitlab_get_mr_approvals(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_get_mr_approvals`` tool spec."""

    @tool(
        "gitlab_get_mr_approvals",
        "Get the approval state of a merge request (who approved, rules, etc.).",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "mr_iid": {"type": "integer", "description": "Merge request IID"},
            },
            "required": ["project_id", "mr_iid"],
        },
        display_name="Getting MR approvals",
        toolset=_TOOLSET,
    )
    async def gitlab_get_mr_approvals(project_id: str, mr_iid: int) -> str:
        """Fetch the approval state of a merge request."""
        return _format(await client.get_mr_approvals(project_id, mr_iid))

    return gitlab_get_mr_approvals


def _make_gitlab_list_branches(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_list_branches`` tool spec."""

    @tool(
        "gitlab_list_branches",
        "List branches in a GitLab project. Optionally filter by name.",
        {
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
        display_name="Listing branches",
        toolset=_TOOLSET,
    )
    async def gitlab_list_branches(
        project_id: str, search: str = "", limit: int = _DEFAULT_PER_PAGE
    ) -> str:
        """List repository branches, optionally filtered by name."""
        return _format(await client.list_branches(project_id, search=search, limit=limit))

    return gitlab_list_branches


def _make_gitlab_list_commits(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_list_commits`` tool spec."""

    @tool(
        "gitlab_list_commits",
        "List recent commits on a branch or ref.",
        {
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
        display_name="Listing commits",
        toolset=_TOOLSET,
    )
    async def gitlab_list_commits(
        project_id: str, ref: str = "main", limit: int = _DEFAULT_PER_PAGE
    ) -> str:
        """List recent commits on a branch or ref."""
        return _format(await client.list_commits(project_id, ref=ref, limit=limit))

    return gitlab_list_commits


def _make_gitlab_get_commit(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_get_commit`` tool spec."""

    @tool(
        "gitlab_get_commit",
        "Get details of a specific commit (message, author, stats, parent SHAs).",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "sha": {"type": "string", "description": "Commit SHA (full or short)"},
            },
            "required": ["project_id", "sha"],
        },
        display_name="Getting commit details",
        toolset=_TOOLSET,
    )
    async def gitlab_get_commit(project_id: str, sha: str) -> str:
        """Fetch details of a specific commit."""
        return _format(await client.get_commit(project_id, sha))

    return gitlab_get_commit


def _make_gitlab_compare(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_compare`` tool spec."""

    @tool(
        "gitlab_compare",
        (
            "Compare two branches, tags, or commits. Returns the commits between "
            "them and the diff. Useful for understanding what changed between releases."
        ),
        {
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
        display_name="Comparing refs",
        toolset=_TOOLSET,
    )
    async def gitlab_compare(project_id: str, from_ref: str, to_ref: str) -> str:
        """Compare two branches, tags, or commits."""
        return _format(await client.compare(project_id, from_ref, to_ref))

    return gitlab_compare


def _make_gitlab_me(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_me`` tool spec."""

    @tool(
        "gitlab_me",
        "Get the authenticated GitLab user's profile.",
        {"type": "object", "properties": {}},
        display_name="Checking GitLab profile",
        toolset=_TOOLSET,
    )
    async def gitlab_me() -> str:
        """Fetch the authenticated GitLab user's profile."""
        return _format(await client.get_current_user())

    return gitlab_me


# -- WRITE tools -------------------------------------------------------


def _make_gitlab_create_mr_note(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_create_mr_note`` tool spec."""

    @tool(
        "gitlab_create_mr_note",
        "Add a comment/note to a GitLab merge request. Requires approval.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "mr_iid": {"type": "integer", "description": "Merge request IID"},
                "body": {"type": "string", "description": "Comment body (Markdown)"},
            },
            "required": ["project_id", "mr_iid", "body"],
        },
        display_name="Commenting on merge request",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gitlab_create_mr_note(project_id: str, mr_iid: int, body: str) -> str:
        """Post a new comment on a merge request."""
        return _format(await client.create_merge_request_note(project_id, mr_iid, body))

    return gitlab_create_mr_note


def _make_gitlab_update_mr_note(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_update_mr_note`` tool spec."""

    @tool(
        "gitlab_update_mr_note",
        "Edit an existing note/comment on a merge request. Requires approval.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "mr_iid": {"type": "integer", "description": "Merge request IID"},
                "note_id": {"type": "integer", "description": "Note ID to edit"},
                "body": {"type": "string", "description": "Updated comment body (Markdown)"},
            },
            "required": ["project_id", "mr_iid", "note_id", "body"],
        },
        display_name="Editing MR comment",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gitlab_update_mr_note(project_id: str, mr_iid: int, note_id: int, body: str) -> str:
        """Edit an existing note on a merge request."""
        return _format(await client.update_merge_request_note(project_id, mr_iid, note_id, body))

    return gitlab_update_mr_note


def _make_gitlab_reply_to_discussion(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_reply_to_discussion`` tool spec."""

    @tool(
        "gitlab_reply_to_discussion",
        (
            "Reply to an existing discussion thread on a merge request. "
            "Use gitlab_list_mr_discussions to get discussion IDs. Requires approval."
        ),
        {
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
        display_name="Replying to MR discussion",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gitlab_reply_to_discussion(
        project_id: str, mr_iid: int, discussion_id: str, body: str
    ) -> str:
        """Reply to an existing discussion thread on a merge request."""
        return _format(await client.reply_to_discussion(project_id, mr_iid, discussion_id, body))

    return gitlab_reply_to_discussion


def _make_gitlab_resolve_discussion(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_resolve_discussion`` tool spec."""

    @tool(
        "gitlab_resolve_discussion",
        (
            "Resolve or unresolve a discussion thread on a merge request. "
            "Use gitlab_list_mr_discussions to get discussion IDs. Requires approval."
        ),
        {
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
        display_name="Resolving MR discussion",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gitlab_resolve_discussion(
        project_id: str, mr_iid: int, discussion_id: str, resolved: bool = True
    ) -> str:
        """Resolve or unresolve a discussion thread on a merge request."""
        return _format(await client.resolve_discussion(project_id, mr_iid, discussion_id, resolved))

    return gitlab_resolve_discussion


def _make_gitlab_approve_mr(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_approve_mr`` tool spec."""

    @tool(
        "gitlab_approve_mr",
        "Approve a merge request. Requires approval.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "mr_iid": {"type": "integer", "description": "Merge request IID"},
            },
            "required": ["project_id", "mr_iid"],
        },
        display_name="Approving merge request",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gitlab_approve_mr(project_id: str, mr_iid: int) -> str:
        """Approve a merge request on behalf of the authenticated user."""
        return _format(await client.approve_merge_request(project_id, mr_iid))

    return gitlab_approve_mr


def _make_gitlab_unapprove_mr(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_unapprove_mr`` tool spec."""

    @tool(
        "gitlab_unapprove_mr",
        "Remove your approval from a merge request. Requires approval.",
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "mr_iid": {"type": "integer", "description": "Merge request IID"},
            },
            "required": ["project_id", "mr_iid"],
        },
        display_name="Removing MR approval",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gitlab_unapprove_mr(project_id: str, mr_iid: int) -> str:
        """Remove the authenticated user's approval from a merge request."""
        return _format(await client.unapprove_merge_request(project_id, mr_iid))

    return gitlab_unapprove_mr


def _make_gitlab_merge_mr(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_merge_mr`` tool spec."""

    @tool(
        "gitlab_merge_mr",
        (
            "Merge a merge request. Optionally squash commits and/or remove "
            "the source branch. Requires approval."
        ),
        {
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
        display_name="Merging merge request",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gitlab_merge_mr(
        project_id: str,
        mr_iid: int,
        merge_commit_message: str = "",
        squash: bool = False,
        should_remove_source_branch: bool = False,
    ) -> str:
        """Merge (accept) a merge request."""
        return _format(
            await client.merge_merge_request(
                project_id,
                mr_iid,
                merge_commit_message=merge_commit_message,
                squash=squash,
                should_remove_source_branch=should_remove_source_branch,
            )
        )

    return gitlab_merge_mr


def _make_gitlab_rebase_mr(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_rebase_mr`` tool spec."""

    @tool(
        "gitlab_rebase_mr",
        ("Rebase a merge request's source branch onto the target branch. Requires approval."),
        {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or path"},
                "mr_iid": {"type": "integer", "description": "Merge request IID"},
            },
            "required": ["project_id", "mr_iid"],
        },
        display_name="Rebasing merge request",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gitlab_rebase_mr(project_id: str, mr_iid: int) -> str:
        """Rebase a merge request's source branch onto the target branch."""
        return _format(await client.rebase_merge_request(project_id, mr_iid))

    return gitlab_rebase_mr


def _make_gitlab_update_mr(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_update_mr`` tool spec."""

    @tool(
        "gitlab_update_mr",
        (
            "Update merge request fields: title, description, labels, "
            "assignees, reviewers, target branch. Use state_event='close' or "
            "'reopen' to change MR state. Requires approval."
        ),
        {
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
        display_name="Updating merge request",
        toolset=_TOOLSET,
        is_read_only=False,
    )
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
        """Update one or more fields on a merge request."""
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
        return _format(await client.update_merge_request(project_id, mr_iid, **fields))

    return gitlab_update_mr


def _make_gitlab_create_mr(client: GitLabClient) -> ToolSpec:
    """Create the ``gitlab_create_mr`` tool spec."""

    @tool(
        "gitlab_create_mr",
        "Create a new merge request. Requires approval.",
        {
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
        display_name="Creating merge request",
        toolset=_TOOLSET,
        is_read_only=False,
    )
    async def gitlab_create_mr(
        project_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
    ) -> str:
        """Create a new merge request."""
        return _format(
            await client.create_merge_request(
                project_id, source_branch, target_branch, title, description=description
            )
        )

    return gitlab_create_mr


# ── Registration ──────────────────────────────────────────────────────


def register_gitlab_tools(registry: ToolRegistry, config: GitLabConfig) -> None:
    """Register all GitLab read and write tools with the tool registry.

    Creates a single ``GitLabClient`` from *config* and binds every tool
    handler to it via closures.  Tools whose ``check_fn`` returns
    ``False`` are silently skipped by the registry.

    Args:
        registry: The orchestrator's tool registry.
        config: GitLab connection settings (URL + token).
    """
    client = GitLabClient(base_url=config.url, token=config.token)

    def _check() -> bool:
        return client.configured

    for factory in (
        _make_gitlab_search_projects,
        _make_gitlab_get_project,
        _make_gitlab_list_merge_requests,
        _make_gitlab_get_merge_request,
        _make_gitlab_get_merge_request_changes,
        _make_gitlab_list_pipelines,
        _make_gitlab_get_pipeline,
        _make_gitlab_get_job_log,
        _make_gitlab_list_pipeline_jobs,
        _make_gitlab_get_file,
        _make_gitlab_list_mr_notes,
        _make_gitlab_list_mr_discussions,
        _make_gitlab_get_mr_approvals,
        _make_gitlab_list_branches,
        _make_gitlab_list_commits,
        _make_gitlab_get_commit,
        _make_gitlab_compare,
        _make_gitlab_me,
        _make_gitlab_create_mr_note,
        _make_gitlab_update_mr_note,
        _make_gitlab_reply_to_discussion,
        _make_gitlab_resolve_discussion,
        _make_gitlab_approve_mr,
        _make_gitlab_unapprove_mr,
        _make_gitlab_merge_mr,
        _make_gitlab_rebase_mr,
        _make_gitlab_update_mr,
        _make_gitlab_create_mr,
    ):
        spec = factory(client)
        spec.check_fn = _check
        registry.register(spec)
