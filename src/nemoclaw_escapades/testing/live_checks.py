"""Shared live-endpoint checks used by both pytest and the sandbox runner.

This module lives under ``src/`` (not ``tests/``) because the sandbox
Docker image copies ``src/`` but not ``tests/``.  Placing it here makes
the check functions importable from both the local pytest suite and the
standalone sandbox runner without duplicating logic.

Each ``check_*`` coroutine takes a pre-configured client, exercises one
API call, and returns a :class:`CheckResult`.  Neither pytest nor
python-dotenv is imported here so the module works inside the minimal
sandbox image.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Outcome of a single live check."""

    name: str
    passed: bool
    detail: str
    data: Any = field(default=None, repr=False)


def _no_error(data: dict[str, Any] | list[Any], label: str) -> CheckResult | None:
    """Return a failing ``CheckResult`` if *data* is an error dict, else ``None``."""
    if isinstance(data, dict) and "error" in data:
        return CheckResult(label, False, str(data["error"]))
    return None


# ===================================================================
# GitLab
# ===================================================================


async def check_gitlab_whoami(client: Any) -> CheckResult:
    """Authenticate and return the current GitLab user's profile.

    Args:
        client: A configured ``GitLabClient`` instance.

    Returns:
        CheckResult with the username on success.
    """
    data = await client.get_current_user()
    if err := _no_error(data, "gitlab_whoami"):
        return err
    return CheckResult("gitlab_whoami", True, data.get("username", "?"), data)


async def check_gitlab_search_projects(client: Any, query: str = "nv_tools") -> CheckResult:
    """Search GitLab projects by name or path.

    Args:
        client: A configured ``GitLabClient`` instance.
        query: Free-text search string.

    Returns:
        CheckResult with the number of matching projects.
    """
    data = await client.search_projects(query, limit=5)
    if err := _no_error(data, "gitlab_search_projects"):
        return err
    return CheckResult("gitlab_search_projects", True, f"{len(data)} result(s)", data)


async def check_gitlab_get_project(client: Any, project_id: str) -> CheckResult:
    """Fetch a single GitLab project by numeric ID or path.

    Accepts both plain (``group/project``) and pre-encoded
    (``group%2Fproject``) forms — the client normalises both.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or ``group/project`` path.

    Returns:
        CheckResult with the project's ``path_with_namespace``.
    """
    data = await client.get_project(project_id)
    if err := _no_error(data, "gitlab_get_project"):
        return err
    return CheckResult("gitlab_get_project", True, data.get("path_with_namespace", "?"), data)


async def check_gitlab_get_project_encoded(client: Any, project_id: str) -> CheckResult:
    """Fetch a project using a pre-URL-encoded path (regression guard).

    Verifies that passing ``group%2Fproject`` does not cause double-encoding.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Plain ``group/project`` path (will be pre-encoded here).

    Returns:
        CheckResult confirming the pre-encoded path resolves correctly.
    """
    from urllib.parse import quote

    encoded = quote(project_id, safe="")
    data = await client.get_project(encoded)
    if err := _no_error(data, "gitlab_get_project_encoded"):
        return err
    return CheckResult(
        "gitlab_get_project_encoded",
        True,
        data.get("path_with_namespace", "?"),
        data,
    )


async def check_gitlab_list_merge_requests(
    client: Any,
    project_id: str,
    state: str = "all",
) -> CheckResult:
    """List merge requests for a GitLab project.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or path.
        state: MR state filter (``opened``, ``closed``, ``merged``, ``all``).

    Returns:
        CheckResult with the number of MRs found. The raw MR list is
        available in ``data`` for chaining (e.g. fetching a specific MR).
    """
    data = await client.list_merge_requests(project_id, state=state, limit=5)
    if err := _no_error(data, "gitlab_list_mrs"):
        return err
    return CheckResult("gitlab_list_mrs", True, f"{len(data)} MR(s)", data)


async def check_gitlab_get_merge_request(
    client: Any,
    project_id: str,
    mr_iid: int,
) -> CheckResult:
    """Fetch full details of a single merge request by IID.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        CheckResult with the MR title.
    """
    data = await client.get_merge_request(project_id, mr_iid)
    if err := _no_error(data, "gitlab_get_mr"):
        return err
    return CheckResult("gitlab_get_mr", True, data.get("title", "?")[:80], data)


async def check_gitlab_get_mr_changes(
    client: Any,
    project_id: str,
    mr_iid: int,
) -> CheckResult:
    """Fetch the diff/changes of a merge request.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        CheckResult with the number of diffs returned.
    """
    data = await client.get_merge_request_changes(project_id, mr_iid)
    if err := _no_error(data, "gitlab_get_mr_changes"):
        return err
    count = len(data) if isinstance(data, list) else "?"
    return CheckResult("gitlab_get_mr_changes", True, f"{count} diff(s)", data)


async def check_gitlab_list_pipelines(client: Any, project_id: str) -> CheckResult:
    """List recent CI/CD pipelines for a project.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or path.

    Returns:
        CheckResult with the number of pipelines returned.
    """
    data = await client.list_pipelines(project_id, limit=3)
    if err := _no_error(data, "gitlab_list_pipelines"):
        return err
    return CheckResult("gitlab_list_pipelines", True, f"{len(data)} pipeline(s)", data)


async def check_gitlab_list_mr_notes(
    client: Any,
    project_id: str,
    mr_iid: int,
) -> CheckResult:
    """List notes (comments) on a merge request.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        CheckResult with the number of notes.
    """
    data = await client.list_mr_notes(project_id, mr_iid, limit=10)
    if err := _no_error(data, "gitlab_list_mr_notes"):
        return err
    count = len(data) if isinstance(data, list) else "?"
    return CheckResult("gitlab_list_mr_notes", True, f"{count} note(s)", data)


async def check_gitlab_list_mr_discussions(
    client: Any,
    project_id: str,
    mr_iid: int,
) -> CheckResult:
    """List threaded discussions on a merge request.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        CheckResult with the number of discussion threads.
    """
    data = await client.list_mr_discussions(project_id, mr_iid, limit=10)
    if err := _no_error(data, "gitlab_list_mr_discussions"):
        return err
    count = len(data) if isinstance(data, list) else "?"
    return CheckResult("gitlab_list_mr_discussions", True, f"{count} discussion(s)", data)


async def check_gitlab_get_mr_approvals(
    client: Any,
    project_id: str,
    mr_iid: int,
) -> CheckResult:
    """Get the approval state of a merge request.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or path.
        mr_iid: Merge request internal ID.

    Returns:
        CheckResult with the approval status.
    """
    data = await client.get_mr_approvals(project_id, mr_iid)
    if err := _no_error(data, "gitlab_get_mr_approvals"):
        return err
    approved = data.get("approved", False)
    return CheckResult("gitlab_get_mr_approvals", True, f"approved={approved}", data)


async def check_gitlab_list_branches(client: Any, project_id: str) -> CheckResult:
    """List branches in a GitLab project.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or path.

    Returns:
        CheckResult with the number of branches.
    """
    data = await client.list_branches(project_id, limit=5)
    if err := _no_error(data, "gitlab_list_branches"):
        return err
    count = len(data) if isinstance(data, list) else "?"
    return CheckResult("gitlab_list_branches", True, f"{count} branch(es)", data)


async def check_gitlab_list_commits(client: Any, project_id: str) -> CheckResult:
    """List recent commits on the default branch.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or path.

    Returns:
        CheckResult with the number of commits returned.
    """
    data = await client.list_commits(project_id, limit=5)
    if err := _no_error(data, "gitlab_list_commits"):
        return err
    count = len(data) if isinstance(data, list) else "?"
    return CheckResult("gitlab_list_commits", True, f"{count} commit(s)", data)


async def check_gitlab_get_commit(client: Any, project_id: str, sha: str) -> CheckResult:
    """Fetch details of a specific commit.

    Args:
        client: A configured ``GitLabClient`` instance.
        project_id: Project ID or path.
        sha: Commit SHA.

    Returns:
        CheckResult with the commit title.
    """
    data = await client.get_commit(project_id, sha)
    if err := _no_error(data, "gitlab_get_commit"):
        return err
    return CheckResult("gitlab_get_commit", True, data.get("title", "?")[:80], data)


# ===================================================================
# Gerrit
# ===================================================================


async def check_gerrit_whoami(client: Any) -> CheckResult:
    """Authenticate and return the current Gerrit user's account info.

    Args:
        client: A configured ``GerritClient`` instance.

    Returns:
        CheckResult with the account display name.
    """
    data = await client.get_account()
    if err := _no_error(data, "gerrit_whoami"):
        return err
    return CheckResult("gerrit_whoami", True, data.get("name", data.get("username", "?")), data)


async def check_gerrit_get_change(client: Any, change_id: str) -> CheckResult:
    """Fetch a single Gerrit change by numeric ID.

    Args:
        client: A configured ``GerritClient`` instance.
        change_id: Numeric change ID or change-id triplet.

    Returns:
        CheckResult with the change number.
    """
    data = await client.get_change(change_id)
    if err := _no_error(data, "gerrit_get_change"):
        return err
    return CheckResult("gerrit_get_change", True, f"#{data.get('_number', '?')}", data)


async def check_gerrit_get_change_detail(client: Any, change_id: str) -> CheckResult:
    """Fetch detailed information about a Gerrit change (revisions, labels).

    Args:
        client: A configured ``GerritClient`` instance.
        change_id: Numeric change ID or change-id triplet.

    Returns:
        CheckResult listing the label names present on the change.
    """
    data = await client.get_change_detail(change_id)
    if err := _no_error(data, "gerrit_get_change_detail"):
        return err
    labels = list(data.get("labels", {}))
    return CheckResult("gerrit_get_change_detail", True, f"labels={labels}", data)


async def check_gerrit_list_files(client: Any, change_id: str) -> CheckResult:
    """List files modified in a Gerrit change (excluding ``/COMMIT_MSG``).

    Args:
        client: A configured ``GerritClient`` instance.
        change_id: Numeric change ID or change-id triplet.

    Returns:
        CheckResult with the file count. The raw file dict is available
        in ``data`` for chaining (e.g. picking a file to diff).
    """
    data = await client.list_files(change_id)
    if err := _no_error(data, "gerrit_list_files"):
        return err
    files = [f for f in data if f != "/COMMIT_MSG"]
    return CheckResult("gerrit_list_files", True, f"{len(files)} file(s)", data)


async def check_gerrit_get_comments(client: Any, change_id: str) -> CheckResult:
    """Fetch all review comments on a Gerrit change.

    Args:
        client: A configured ``GerritClient`` instance.
        change_id: Numeric change ID or change-id triplet.

    Returns:
        CheckResult with the total comment count across all files.
    """
    data = await client.get_comments(change_id)
    if err := _no_error(data, "gerrit_get_comments"):
        return err
    n = sum(len(v) for v in data.values()) if isinstance(data, dict) else 0
    return CheckResult("gerrit_get_comments", True, f"{n} comment(s)", data)


async def check_gerrit_get_diff(client: Any, change_id: str, file_path: str) -> CheckResult:
    """Fetch the diff for a specific file in a Gerrit change.

    Args:
        client: A configured ``GerritClient`` instance.
        change_id: Numeric change ID or change-id triplet.
        file_path: Repository-relative path of the file to diff.

    Returns:
        CheckResult confirming the diff was retrieved.
    """
    data = await client.get_diff(change_id, file_path)
    if err := _no_error(data, "gerrit_get_diff"):
        return err
    return CheckResult("gerrit_get_diff", True, f"diff for {file_path}", data)


async def check_gerrit_list_changes(
    client: Any,
    query: str = "owner:self",
    limit: int = 3,
) -> CheckResult:
    """Search Gerrit changes using a query string.

    Args:
        client: A configured ``GerritClient`` instance.
        query: Gerrit search query (e.g. ``"owner:self status:open"``).
        limit: Maximum number of results to return.

    Returns:
        CheckResult with the number of matching changes.
    """
    data = await client.list_changes(query, limit=limit)
    if err := _no_error(data, "gerrit_list_changes"):
        return err
    return CheckResult("gerrit_list_changes", True, f"{len(data)} change(s)", data)


# ===================================================================
# Jira
# ===================================================================


async def check_jira_whoami(client: Any) -> CheckResult:
    """Authenticate and return the current Jira user's profile.

    Args:
        client: A configured ``JiraClient`` instance.

    Returns:
        CheckResult with the user's display name.
    """
    data = await client.me()
    if err := _no_error(data, "jira_whoami"):
        return err
    return CheckResult("jira_whoami", True, data.get("displayName", "?"), data)


async def check_jira_get_issue(client: Any, issue_key: str) -> CheckResult:
    """Fetch a single Jira issue by key.

    Args:
        client: A configured ``JiraClient`` instance.
        issue_key: Issue identifier (e.g. ``PROJ-123``).

    Returns:
        CheckResult with the issue key and summary.
    """
    data = await client.get_issue(issue_key)
    if err := _no_error(data, "jira_get_issue"):
        return err
    summary = data.get("fields", {}).get("summary", "?")[:80]
    return CheckResult("jira_get_issue", True, f"{data.get('key')}: {summary}", data)


async def check_jira_search(
    client: Any,
    jql: str = "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC",
    limit: int = 5,
) -> CheckResult:
    """Search Jira issues using JQL.

    Args:
        client: A configured ``JiraClient`` instance.
        jql: JQL query string.
        limit: Maximum number of results to return.

    Returns:
        CheckResult with the total hit count and number shown.
    """
    data = await client.search(jql=jql, limit=limit)
    if err := _no_error(data, "jira_search"):
        return err
    total = data.get("total", 0)
    shown = len(data.get("issues", []))
    return CheckResult("jira_search", True, f"{total} total, showing {shown}", data)


async def check_jira_get_issue_comments(client: Any, issue_key: str) -> CheckResult:
    """Fetch the comments field of a Jira issue.

    Args:
        client: A configured ``JiraClient`` instance.
        issue_key: Issue identifier (e.g. ``PROJ-123``).

    Returns:
        CheckResult confirming the comment field was retrieved.
    """
    data = await client.get_issue(issue_key, fields="comment")
    if err := _no_error(data, "jira_get_issue_comments"):
        return err
    return CheckResult("jira_get_issue_comments", True, f"fields for {issue_key}", data)


async def check_jira_get_transitions(client: Any, issue_key: str) -> CheckResult:
    """List available status transitions for a Jira issue.

    Args:
        client: A configured ``JiraClient`` instance.
        issue_key: Issue identifier (e.g. ``PROJ-123``).

    Returns:
        CheckResult with the number of available transitions.
    """
    data = await client.get_transitions(issue_key)
    if err := _no_error(data, "jira_get_transitions"):
        return err
    count = len(data.get("transitions", []))
    return CheckResult("jira_get_transitions", True, f"{count} transition(s)", data)


# ===================================================================
# Confluence
# ===================================================================


async def check_confluence_get_page(client: Any, page_id: str) -> CheckResult:
    """Fetch a Confluence page by ID including its body content.

    Args:
        client: A configured ``ConfluenceClient`` instance.
        page_id: Numeric Confluence page ID.

    Returns:
        CheckResult with the page title.
    """
    data = await client.get_page(page_id)
    if err := _no_error(data, "confluence_get_page"):
        return err
    return CheckResult("confluence_get_page", True, data.get("title", "?")[:80], data)


async def check_confluence_search(client: Any, cql: str, limit: int = 3) -> CheckResult:
    """Search Confluence pages using CQL.

    Args:
        client: A configured ``ConfluenceClient`` instance.
        cql: CQL query string.
        limit: Maximum number of results to return.

    Returns:
        CheckResult with the number of matching pages.
    """
    data = await client.search(cql, limit=limit)
    if err := _no_error(data, "confluence_search"):
        return err
    count = len(data.get("results", []))
    return CheckResult("confluence_search", True, f"{count} result(s)", data)


async def check_confluence_get_children(client: Any, page_id: str) -> CheckResult:
    """List child pages of a Confluence page.

    Args:
        client: A configured ``ConfluenceClient`` instance.
        page_id: Numeric Confluence page ID.

    Returns:
        CheckResult with the number of child pages.
    """
    data = await client.get_page_children(page_id, limit=5)
    if err := _no_error(data, "confluence_get_children"):
        return err
    count = len(data.get("results", [])) if isinstance(data, dict) else "?"
    return CheckResult("confluence_get_children", True, f"{count} child page(s)", data)


async def check_confluence_get_labels(client: Any, page_id: str) -> CheckResult:
    """Fetch labels attached to a Confluence page.

    Args:
        client: A configured ``ConfluenceClient`` instance.
        page_id: Numeric Confluence page ID.

    Returns:
        CheckResult with the number of labels.
    """
    data = await client.get_labels(page_id)
    if err := _no_error(data, "confluence_get_labels"):
        return err
    count = len(data.get("results", [])) if isinstance(data, dict) else "?"
    return CheckResult("confluence_get_labels", True, f"{count} label(s)", data)


async def check_confluence_get_comments(client: Any, page_id: str) -> CheckResult:
    """Fetch comments on a Confluence page.

    Args:
        client: A configured ``ConfluenceClient`` instance.
        page_id: Numeric Confluence page ID.

    Returns:
        CheckResult with the number of comments.
    """
    data = await client.get_comments(page_id, limit=5)
    if err := _no_error(data, "confluence_get_comments"):
        return err
    count = len(data.get("results", [])) if isinstance(data, dict) else "?"
    return CheckResult("confluence_get_comments", True, f"{count} comment(s)", data)


# ===================================================================
# Slack
# ===================================================================


async def check_slack_search_messages(
    client: Any,
    query: str = "from:me",
    count: int = 3,
) -> CheckResult:
    """Search Slack messages across all accessible channels.

    Args:
        client: A configured ``SlackSearchClient`` instance.
        query: Slack search query string.
        count: Maximum number of results to return.

    Returns:
        CheckResult with the total number of matching messages.
    """
    data = await client.search_messages(query, count=count)
    if err := _no_error(data, "slack_search_messages"):
        return err
    total = data.get("messages", {}).get("total", 0)
    return CheckResult("slack_search_messages", True, f"{total} match(es)", data)


async def check_slack_list_channels(client: Any, limit: int = 5) -> CheckResult:
    """List Slack channels the user has access to.

    Args:
        client: A configured ``SlackSearchClient`` instance.
        limit: Maximum number of channels to return.

    Returns:
        CheckResult with the number of channels returned.
    """
    data = await client.list_channels(limit=limit)
    if err := _no_error(data, "slack_list_channels"):
        return err
    count = len(data.get("channels", []))
    return CheckResult("slack_list_channels", True, f"{count} channel(s)", data)


async def check_slack_get_thread(client: Any) -> CheckResult:
    """Find a recent thread the user participated in and fetch its replies.

    Searches for a message with ``from:me has:thread``, then calls
    ``conversations.replies`` on the first match.  Returns a passing
    result with "skipped" if no threaded messages are found.

    Args:
        client: A configured ``SlackSearchClient`` instance.

    Returns:
        CheckResult with the number of replies in the thread.
    """
    search = await client.search_messages("from:me has:thread", count=1)
    if isinstance(search, dict) and "error" in search:
        return CheckResult("slack_get_thread", False, str(search["error"]))
    matches = search.get("messages", {}).get("matches", [])
    if not matches:
        return CheckResult("slack_get_thread", True, "no threads found (skipped)")
    msg = matches[0]
    channel_id = msg["channel"]["id"]
    thread_ts = msg.get("ts", "")
    data = await client.get_thread_replies(channel_id, thread_ts, limit=5)
    if err := _no_error(data, "slack_get_thread"):
        return err
    count = len(data.get("messages", []))
    return CheckResult("slack_get_thread", True, f"{count} reply/replies", data)
