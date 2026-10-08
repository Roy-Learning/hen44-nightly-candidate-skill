"""Deterministic claim placement checks over verified web-fetch renderings.

Sections are emitted by agents.tools.web.runtime and its full-page adapters.
Full sources remain available for contradiction review; this check only
restricts which literal excerpts may support a declared claim.
"""
from __future__ import annotations

import re


_BODY = re.compile(r"(?m)^\[正文\][ \t]*\r?$\n?")
_NON_BODY = re.compile(r"(?m)^\[(?:SEO 信息|站内链接|页面图片URL)\]")


def page_body_text(text: str) -> str:
    marker = _BODY.search(text)
    if marker is None:
        # Existing adapters may return plain extracted prose. Metadata-only
        # renderings must not be mistaken for those plain-body responses.
        return "" if _NON_BODY.search(text) else text
    body = text[marker.end():]
    ending = _NON_BODY.search(body)
    return body[:ending.start()] if ending else body


def require_page_body_quotes(claims: object, sources: list[dict]) -> None:
    """Reject a claim absent from its own source's body before any model call."""
    bodies = [(source, page_body_text(source["text"])) for source in sources]

    def visit(value, path):
        if isinstance(value, dict):
            quote = value.get("quote")
            if isinstance(quote, str) and quote:
                artifact, url = value.get("evidence_file"), value.get("url")
                found = any(
                    (not artifact or artifact == source.get("artifact_path"))
                    and (not url or url == source.get("url"))
                    and quote in body
                    for source, body in bodies
                )
                if not found:
                    raise ValueError("declared quote is absent from its own page body: " + ".".join(path))
            for key, child in value.items():
                visit(child, path + [str(key)])
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + [str(index)])

    visit(claims, [])
