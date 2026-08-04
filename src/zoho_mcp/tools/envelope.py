"""The shared result shape for every enumeration tool and batch write."""


def counted(key: str, items: list, **extra: object) -> dict:
    """Wrap a list result so its size travels with it.

    A bare list leaves "how many are there?" to whoever reads the result,
    which in practice means an LLM tallying items while composing prose --
    a step that produces a plausible number rather than a derived one. The
    count is arithmetic the server can do once, so it does.

    The same argument applies to a batch write returning nothing at all,
    which is worse: there the number an LLM reports is composed from the
    request it just wrote, with no result to check it against.

    Args:
        key: the domain name the items go under ("emails", "notes", ...),
            or what the write did to them ("marked_read", "moved").
        items: the normalized records being returned, or the message ids a
            write submitted.
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
