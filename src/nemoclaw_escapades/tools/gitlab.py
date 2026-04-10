"""GitLab REST API v4 tools for the orchestrator.

Provides an async GitLab client and registers individual operations as
tools.  Lifted from ``nv_tools.clients.gitlab.GitLabClient`` and
converted to async httpx + tool-registry integration.

**Auth:** Uses a ``PRIVATE-TOKEN`` header populated from ``GITLAB_TOKEN``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

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


class GitLabClient:
    """Async GitLab REST API v4 client.

    Attributes:
        base_url: GitLab API v4 base URL (no trailing slash).
    """

    def __init__(self, base_url: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/").rstrip("/api/v4")
        self._token = token
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """Return ``True`` when base URL and token are set."""
        return bool(self.base_url and self._token)

    async def _get_client(self) -> httpx.AsyncClient:
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
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
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
        """GET request that returns raw text (e.g. job logs)."""
        if not self.configured:
            return '{"error": "GitLab not configured. Set GITLAB_TOKEN."}'
        client = await self._get_client()
        response = await client.get(endpoint, **kwargs)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            return json.dumps({
                "error": f"GitLab API returned {response.status_code}",
                "status_code": response.status_code,
                "body": response.text[:_ERROR_BODY_MAX_CHARS],
            })
        return response.text

    # -- READ operations ---------------------------------------------------

    async def search_projects(
        self, query: str, limit: int = _DEFAULT_SEARCH_LIMIT
    ) -> dict[str, Any] | list[Any]:
        return await self._request(
            "GET", "/projects", params={"search": query, "per_page": limit}
        )

    async def get_project(self, project_id: str) -> dict[str, Any]:
        encoded = quote(project_id, safe="")
        return await self._request("GET", f"/projects/{encoded}")

    async def list_merge_requests(
        self,
        project_id: str,
        state: str = "opened",
        limit: int = _DEFAULT_PER_PAGE,
    ) -> dict[str, Any] | list[Any]:
        encoded = quote(project_id, safe="")
        return await self._request(
            "GET",
            f"/projects/{encoded}/merge_requests",
            params={"state": state, "per_page": limit},
        )

    async def get_merge_request(
        self, project_id: str, mr_iid: int
    ) -> dict[str, Any]:
        encoded = quote(project_id, safe="")
        return await self._request("GET", f"/projects/{encoded}/merge_requests/{mr_iid}")

    async def get_merge_request_changes(
        self, project_id: str, mr_iid: int
    ) -> dict[str, Any]:
        encoded = quote(project_id, safe="")
        return await self._request("GET", f"/projects/{encoded}/merge_requests/{mr_iid}/diffs")

    async def list_pipelines(
        self,
        project_id: str,
        limit: int = _DEFAULT_PER_PAGE,
    ) -> dict[str, Any] | list[Any]:
        encoded = quote(project_id, safe="")
        return await self._request(
            "GET", f"/projects/{encoded}/pipelines", params={"per_page": limit}
        )

    async def get_pipeline(self, project_id: str, pipeline_id: int) -> dict[str, Any]:
        encoded = quote(project_id, safe="")
        return await self._request("GET", f"/projects/{encoded}/pipelines/{pipeline_id}")

    async def list_pipeline_jobs(
        self, project_id: str, pipeline_id: int
    ) -> dict[str, Any] | list[Any]:
        encoded = quote(project_id, safe="")
        return await self._request(
            "GET", f"/projects/{encoded}/pipelines/{pipeline_id}/jobs"
        )

    async def get_job_log(self, project_id: str, job_id: int) -> str:
        encoded = quote(project_id, safe="")
        return await self._request_text(f"/projects/{encoded}/jobs/{job_id}/trace")

    async def get_file(
        self, project_id: str, file_path: str, ref: str = "main"
    ) -> dict[str, Any]:
        proj = quote(project_id, safe="")
        fpath = quote(file_path, safe="")
        return await self._request(
            "GET", f"/projects/{proj}/repository/files/{fpath}", params={"ref": ref}
        )

    async def get_current_user(self) -> dict[str, Any]:
        return await self._request("GET", "/user")

    # -- WRITE operations --------------------------------------------------

    async def create_merge_request_note(
        self, project_id: str, mr_iid: int, body: str
    ) -> dict[str, Any]:
        encoded = quote(project_id, safe="")
        return await self._request(
            "POST", f"/projects/{encoded}/merge_requests/{mr_iid}/notes", json={"body": body}
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _get_client() -> GitLabClient:
    if _gitlab_config is None:
        raise RuntimeError("GitLab tools not initialised — call register_gitlab_tools first")
    return GitLabClient(base_url=_gitlab_config.url, token=_gitlab_config.token)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def gitlab_search_projects(query: str, limit: int = _DEFAULT_SEARCH_LIMIT) -> str:
    """Search GitLab projects by name or path."""
    return _format(await _get_client().search_projects(query, limit=limit))


async def gitlab_get_project(project_id: str) -> str:
    """Get a GitLab project by ID or URL-encoded path."""
    return _format(await _get_client().get_project(project_id))


async def gitlab_list_merge_requests(
    project_id: str, state: str = "opened", limit: int = _DEFAULT_PER_PAGE
) -> str:
    """List merge requests for a GitLab project."""
    return _format(await _get_client().list_merge_requests(project_id, state=state, limit=limit))


async def gitlab_get_merge_request(project_id: str, mr_iid: int) -> str:
    """Get details of a specific merge request."""
    return _format(await _get_client().get_merge_request(project_id, mr_iid))


async def gitlab_get_merge_request_changes(project_id: str, mr_iid: int) -> str:
    """Get the diff/changes of a merge request."""
    return _format(await _get_client().get_merge_request_changes(project_id, mr_iid))


async def gitlab_list_pipelines(
    project_id: str, limit: int = _DEFAULT_PER_PAGE
) -> str:
    """List CI pipelines for a project."""
    return _format(await _get_client().list_pipelines(project_id, limit=limit))


async def gitlab_get_pipeline(project_id: str, pipeline_id: int) -> str:
    """Get details of a specific pipeline."""
    return _format(await _get_client().get_pipeline(project_id, pipeline_id))


async def gitlab_get_job_log(project_id: str, job_id: int) -> str:
    """Get the log output of a CI job."""
    return await _get_client().get_job_log(project_id, job_id)


async def gitlab_me() -> str:
    """Get the authenticated GitLab user's profile."""
    return _format(await _get_client().get_current_user())


async def gitlab_create_mr_note(project_id: str, mr_iid: int, body: str) -> str:
    """Add a comment/note to a merge request."""
    return _format(await _get_client().create_merge_request_note(project_id, mr_iid, body))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _gitlab_available() -> bool:
    return _gitlab_config is not None and _get_client().configured


def register_gitlab_tools(registry: ToolRegistry, config: GitLabConfig) -> None:
    """Register all GitLab tools with the orchestrator's tool registry."""
    global _gitlab_config  # noqa: PLW0603
    _gitlab_config = config

    _ts = "gitlab"
    _ck = _gitlab_available

    registry.register(ToolSpec(
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
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gitlab_get_project",
        display_name="Getting GitLab project",
        description="Get a GitLab project by numeric ID or URL-encoded path (e.g. 'group%2Fproject').",
        input_schema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID or URL-encoded path"},
            },
            "required": ["project_id"],
        },
        handler=gitlab_get_project,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
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
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
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
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
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
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
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
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
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
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
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
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
        name="gitlab_me",
        display_name="Checking GitLab profile",
        description="Get the authenticated GitLab user's profile.",
        input_schema={"type": "object", "properties": {}},
        handler=gitlab_me,
        toolset=_ts, check_fn=_ck,
    ))

    registry.register(ToolSpec(
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
        toolset=_ts, check_fn=_ck,
    ))
