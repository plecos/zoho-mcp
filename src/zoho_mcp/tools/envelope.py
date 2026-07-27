"""The shared result shape for every enumeration tool."""


def counted(key: str, items: list[dict], **extra) -> dict:
    """Wrap an enumeration result so its size travels with it.

    A bare list leaves "how many are there?" to whoever reads the result,
    which in practice means an LLM tallying items while composing prose --
    a step that produces a plausible number rather than a derived one. The
    count is arithmetic the server can do once, so it does.

    Args:
        key: the domain name the items go under ("emails", "notes", ...).
        items: the normalized records being returned.
        **extra: additional envelope fields, e.g. ``has_more``.

    Returns:
        ``{key: items, "count": len(items), **extra}``.

    Raises:
        ValueError: if ``extra`` would overwrite ``key`` or ``count``,
            which would decouple the reported count from the list.
    """
    for reserved in (key, "count"):
        if reserved in extra:
            raise ValueError(
                f"{reserved!r} is set by counted() and can't be overridden"
            )
    return {key: items, "count": len(items), **extra}
