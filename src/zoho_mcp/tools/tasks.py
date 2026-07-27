"""MCP tool wrappers for Zoho Mail Tasks.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed
here.
"""

from zoho_mcp.tools.envelope import counted
from zoho_mcp.zoho.client import ZohoClient


async def list_tasks(
    client: ZohoClient,
    limit: int = 20,
    offset: int = 0,
    group_id: str | None = None,
    view: str | None = None,
) -> dict:
    """List Zoho Mail tasks -- personal, a group's, or a cross-group view.

    Args:
        client: injected Zoho client.
        limit: maximum number of tasks to return (1-499).
        offset: how many tasks to skip before returning results.
        group_id: list a shared group's tasks instead of personal ones.
            Ids come from list_groups.
        view: "assigned_to_me" or "created_by_me" -- Zoho's two views
            that span every group the user belongs to. On an account
            with no groups these return the same tasks as the personal
            list. Cannot be combined with group_id.

    Returns:
        ``{"tasks": [...], "count": int, "has_more": bool}``. Each task
        has id, title, description, status, priority, due_date, project,
        assignee, tags, subtask_count, recurring, created_at,
        modified_at. ``count`` is this page's size; ``has_more`` is True
        if more tasks exist beyond ``limit`` -- raise limit or increase
        offset rather than treating ``count`` as the full total.

    Raises:
        ZohoAPIError: if limit/offset are out of range, view isn't a
            recognized value, group_id and view are both given, or the
            Tasks API rejects or fails the request.
    """
    tasks, has_more = await client.list_tasks(
        limit=limit, offset=offset, group_id=group_id, view=view
    )
    return counted("tasks", tasks, has_more=has_more)


async def create_task(
    client: ZohoClient,
    title: str,
    description: str = "",
    priority: str = "",
    group_id: str | None = None,
) -> dict:
    """Create a personal or group task.

    Args:
        client: injected Zoho client.
        title: the task title -- the only field Zoho requires.
        description: optional free-text body.
        priority: optional; Zoho's samples show "low"/"high". Passed
            through unvalidated since the accepted set isn't documented.
        group_id: create in a shared group instead of personal tasks.
            Ids come from list_groups.

    Returns:
        The created task, normalized the same way as list_tasks entries
        (Zoho returns the whole task on create, unlike notes/bookmarks).

    Raises:
        ZohoAPIError: if title is blank, or the Tasks API rejects or
            fails the request.
    """
    return await client.create_task(
        title=title, description=description, priority=priority, group_id=group_id
    )


async def get_task(client: ZohoClient, task_id: str) -> dict:
    """Fetch one task's full details by id.

    Args:
        client: injected Zoho client.
        task_id: a task's ``id`` from a prior ``list_tasks`` result.

    Raises:
        ZohoAPIError: if no task is found for task_id, or the Tasks API
            rejects or fails the request.
    """
    return await client.get_task(task_id)
