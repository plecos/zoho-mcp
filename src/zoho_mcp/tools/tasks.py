"""MCP tool wrappers for Zoho Mail Tasks.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed
here.
"""

from zoho_mcp.zoho.client import ZohoClient


async def list_tasks(client: ZohoClient, limit: int = 20, offset: int = 0) -> dict:
    """List the user's personal Zoho Mail tasks.

    Args:
        client: injected Zoho client.
        limit: maximum number of tasks to return (1-499).
        offset: how many tasks to skip before returning results.

    Returns:
        ``{"tasks": [...], "has_more": bool}``. Each task has id, title,
        description, status, priority, due_date, project, assignee, tags,
        subtask_count, recurring, created_at, modified_at. ``has_more`` is
        True if more tasks exist beyond ``limit`` -- raise limit or
        increase offset rather than assuming the count you got back is
        the full total.

    Raises:
        ZohoAPIError: if limit/offset are out of range, or the Tasks API
            rejects or fails the request.
    """
    tasks, has_more = await client.list_tasks(limit=limit, offset=offset)
    return {"tasks": tasks, "has_more": has_more}


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
