"""Ground visible evidence wording back to the original page-body excerpt."""
from __future__ import annotations

import unicodedata

from agents.semantic_review_source_scope import page_body_text


def _evidence_display_char(char: str) -> str:
    """Collapse the only observed interchangeable apostrophe display forms."""
    return "'" if char in {"'", "\u2019"} else char


def _inline_tokens(text: str):
    """Locate inline-link labels; incomplete links/code are opaque barriers.

    This is a conservative projection of the web-fetch Markdown format, not a
    general Markdown renderer. Destinations never become evidence text. The
    scanner advances past each destination once, including malformed input.
    """
    opens: list[int] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char in "\r\n":
            opens.clear()
        elif char == "`":
            end_run = index + 1
            while end_run < len(text) and text[end_run] == "`":
                end_run += 1
            closing = text.find(text[index:end_run], end_run)
            end = len(text) if closing < 0 else closing + end_run - index
            yield index, end, None, None
            opens.clear()
            index = end
            continue
        elif char == "[":
            opens.append(index)
        elif char == "]" and opens:
            start = opens.pop()
            if index + 1 < len(text) and text[index + 1] == "(":
                cursor, depth = index + 2, 1
                while cursor < len(text) and text[cursor] not in "\r\n":
                    if text[cursor] == "\\":
                        cursor += 2
                        continue
                    if text[cursor] == "(":
                        depth += 1
                    elif text[cursor] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    cursor += 1
                if depth == 0:
                    end = cursor + 1
                    if start > 0 and text[start - 1] == "!":
                        # Image alt describes an image; it is not visible page
                        # body evidence for an address, product, or ownership.
                        yield start - 1, end, None, None
                    else:
                        yield start, end, start + 1, index
                else:
                    end = min(cursor, len(text))
                    yield start, end, None, None
                opens.clear()
                index = end
                continue
        index += 1


def _is_escaped(text: str, index: int) -> bool:
    """True when an odd run of backslashes escapes this ASCII punctuation."""
    count = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        count += 1
        index -= 1
    return bool(count % 2)


def _emphasis_span_maps(text: str) -> tuple[set[int], dict[int, int], dict[int, int]]:
    """Project only an unambiguous, source-separated emphasis subset.

    Links and code are structural tokens before emphasis.  This deliberately
    leaves marker runs inside those tokens, intraword underscores, escaped
    delimiters, unmatched delimiters, and other ambiguous Markdown literal.
    """
    protected = [(start, end) for start, end, _label_start, _label_end in _inline_tokens(text)]

    def protected_at(index: int) -> bool:
        return any(start <= index < end for start, end in protected)

    skipped: set[int] = set()
    starts: dict[int, int] = {}
    ends: dict[int, int] = {}
    index = 0
    while index < len(text):
        if text[index] not in "*_" or protected_at(index) or _is_escaped(text, index):
            index += 1
            continue
        run_end = index + 1
        while run_end < len(text) and text[run_end] == text[index]:
            run_end += 1
        marker = text[index:run_end]
        before = text[index - 1] if index else ""
        after = text[run_end] if run_end < len(text) else ""
        if len(marker) > 3 or (before and not before.isspace()) or not after or after.isspace():
            index = run_end
            continue
        cursor = run_end
        closing = None
        while cursor < len(text) and text[cursor] not in "\r\n":
            if protected_at(cursor):
                cursor += 1
                continue
            if text[cursor] != text[index] or _is_escaped(text, cursor):
                cursor += 1
                continue
            close_end = cursor + 1
            while close_end < len(text) and text[close_end] == text[index]:
                close_end += 1
            following = text[close_end] if close_end < len(text) else ""
            previous = text[cursor - 1] if cursor else ""
            if (
                text[cursor:close_end] == marker
                and previous and not previous.isspace()
                and (
                    not following
                    or following.isspace()
                    or unicodedata.category(following)[0] in {"P", "S"}
                )
            ):
                closing = (cursor, close_end)
                break
            cursor = close_end
        if closing is None:
            index = run_end
            continue
        close_start, close_end = closing
        skipped.update(range(index, run_end))
        skipped.update(range(close_start, close_end))
        starts[run_end] = index
        ends[close_start - 1] = close_end
        index = close_end
    return skipped, starts, ends


def _project(text: str) -> tuple[str, list[tuple[int, int]]]:
    chars: list[str] = []
    spans: list[tuple[int, int]] = []

    def emit(value: str, span: tuple[int, int]) -> None:
        for char in unicodedata.normalize("NFKC", value):
            char = _evidence_display_char(char)
            # Web extractors may inject U+200B inside otherwise continuous
            # visible prose.  Ignore only that formatting character for the
            # comparison projection; the returned span still includes it.
            if char == "\u200b":
                continue
            if char.isspace():
                if not chars:
                    continue
                if chars[-1] == " ":
                    spans[-1] = (spans[-1][0], span[1])
                    continue
                char = " "
            chars.append(char)
            spans.append(span)

    emphasis_skip, emphasis_starts, emphasis_ends = _emphasis_span_maps(text)

    def plain(start: int, end: int) -> None:
        for index in range(start, end):
            if index not in emphasis_skip:
                emit(
                    text[index],
                    (emphasis_starts.get(index, index), emphasis_ends.get(index, index + 1)),
                )

    cursor, previous_link_end = 0, None
    for start, end, label_start, label_end in _inline_tokens(text):
        plain(cursor, start)
        if label_start is None:
            emit("\x00", (start, end))
            previous_link_end = None
        else:
            # Web extractors serialize adjacent block anchors without a gap.
            # Keep each label's words, restoring only a separating whitespace.
            if previous_link_end == start:
                emit(" ", (start, start))
            for index in range(label_start, label_end):
                if index not in emphasis_skip:
                    emit(text[index], (start, end))
            previous_link_end = end
        cursor = end
    plain(cursor, len(text))
    if chars and chars[-1] == " ":
        chars.pop()
        spans.pop()
    return "".join(chars), spans


def canonical_source_quote(source: str, quote: str) -> str | None:
    """Return a contiguous original excerpt, never reconstructed evidence.

    Only whitespace, Unicode compatibility spelling and inline link wrappers
    are normalized. Case, punctuation, numbers and intervening visible words
    remain significant. The host can still apply its literal body-only gate to
    the returned excerpt and keep all semantic qualification checks unchanged.
    """
    body = page_body_text(source)
    visible, spans = _project(body)
    requested, _ = _project(quote)
    if not requested or "\x00" in requested:
        return None
    offset = visible.find(requested)
    if offset < 0:
        return None
    return body[spans[offset][0]:spans[offset + len(requested) - 1][1]]


def normalize_evidence_text(text: str) -> str:
    """Compare claim values against visible words, without link destinations."""
    visible, _ = _project(text)
    return unicodedata.normalize("NFKC", visible).casefold()



def _qualification_reference_and_anchor_nodes(text):
    """Return source-ordered reference and HTML anchor nodes as full/label spans."""
    from html.parser import HTMLParser

    definitions = set()
    for line in text.splitlines():
        line = line.lstrip(" \t")
        end = line.find("]:")
        if line.startswith("[") and end > 1:
            definitions.add(unicodedata.normalize("NFKC", line[1:end]).casefold())
    references = []
    label = -1
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            label = index
        elif char == "]" and label >= 0:
            value = unicodedata.normalize("NFKC", text[label + 1:index]).casefold()
            if index + 1 < len(text) and text[index + 1] == "[":
                line_end = text.find("\n", index + 2)
                line_end = len(text) if line_end < 0 else line_end
                end = text.find("]", index + 2, line_end)
                if end >= 0:
                    references.append((label, end + 1, label + 1, index))
                    index = end
                else:
                    index = line_end
            elif value in definitions:
                references.append((label, index + 1, label + 1, index))
            label = -1
        index += 1
    offsets = [0]
    for index, char in enumerate(text):
        if char == "\n":
            offsets.append(index + 1)
    order = []
    bounds = {}
    class Anchors(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.starts = []
        def position(self):
            line, column = self.getpos()
            return offsets[line - 1] + column
        def handle_starttag(self, tag, attrs):
            if tag.casefold() == "a":
                start = self.position()
                opening = text.find(">", start)
                if opening >= 0:
                    self.starts.append((start, opening + 1))
                    order.append(start)
        def handle_endtag(self, tag):
            if tag.casefold() == "a" and self.starts:
                start, label_start = self.starts.pop()
                close = self.position()
                end = text.find(">", close)
                bounds[start] = (len(text) if end < 0 else end + 1, label_start, close)
    parser = Anchors()
    parser.feed(text)
    parser.close()
    for start, label_start in parser.starts:
        bounds[start] = (len(text), label_start, len(text))
    anchors = [(start, *bounds[start]) for start in order]
    return references, anchors


def _qualification_nodes(text):
    inline = [(start, end, label_start, label_end) for start, end, label_start, label_end in _inline_tokens(text) if label_start is not None]
    references, anchors = _qualification_reference_and_anchor_nodes(text)
    streams = (inline, references, anchors)
    positions = [0, 0, 0]
    nodes = []
    while True:
        choices = [(rows[index][0], stream) for stream, (rows, index) in enumerate(zip(streams, positions)) if index < len(rows)]
        if not choices:
            return nodes
        _start, stream = min(choices)
        nodes.append(streams[stream][positions[stream]])
        positions[stream] += 1


def _qualification_projection(text):
    """Project link labels with original spans and a per-character link-origin bit."""
    chars, spans, links = [], [], []
    emphasis_skip, emphasis_starts, emphasis_ends = _emphasis_span_maps(text)
    def emit(start, end, linked, node=None):
        for index in range(start, end):
            if index in emphasis_skip:
                continue
            value = unicodedata.normalize("NFKC", text[index])
            for char in value:
                char = _evidence_display_char(char)
                if char == "\u200b":
                    continue
                if char.isspace():
                    if not chars:
                        continue
                    if chars[-1] == " ":
                        links[-1] = links[-1] and linked
                        continue
                    char = " "
                chars.append(char)
                spans.append(
                    node
                    if node is not None
                    else (emphasis_starts.get(index, index), emphasis_ends.get(index, index + 1))
                )
                links.append(linked)
    cursor = 0
    for start, end, label_start, label_end in _qualification_nodes(text):
        if start < cursor:
            continue
        emit(cursor, start, False)
        emit(label_start, label_end, True, (start, end))
        cursor = end
    emit(cursor, len(text), False)
    while chars and chars[-1] == " ":
        chars.pop(); spans.pop(); links.pop()
    return "".join(chars), spans, links


def qualification_canonical_source_quote(source, quote):
    """Return ordinary-body source span, or report that every meaningful char is link-only."""
    body = page_body_text(source)
    visible, spans, links = _qualification_projection(body)
    requested, _requested_spans, _requested_links = _qualification_projection(quote)
    if not requested or "\x00" in requested:
        return None, False
    # Keep old code/image/malformed barriers for the exact raw span we select.
    original_quote = canonical_source_quote(body, quote)
    original_visible, original_spans = _project(body)
    original_requested, _ = _project(quote)
    original_matches = set()
    if original_quote is not None and original_requested:
        original_offset = original_visible.find(original_requested)
        while original_offset >= 0:
            original_matches.add((original_spans[original_offset][0], original_spans[original_offset + len(original_requested) - 1][1]))
            original_offset = original_visible.find(original_requested, original_offset + 1)
    plain = [0]
    for char, linked in zip(visible, links):
        plain.append(plain[-1] + int(char.isalnum() and not linked))
    offset = visible.find(requested)
    link_only = False
    if offset < 0:
        compact = [(char, linked) for char, linked in zip(visible, links) if char.isalnum()]
        wanted = "".join(char for char in requested if char.isalnum())
        if not wanted:
            return None, False
        compact_text = "".join(char for char, _linked in compact)
        linked_prefix = [0]
        for _char, linked in compact:
            linked_prefix.append(linked_prefix[-1] + int(not linked))
        offset = compact_text.find(wanted)
        while offset >= 0:
            if linked_prefix[offset + len(wanted)] == linked_prefix[offset]:
                link_only = True
            else:
                return None, False
            offset = compact_text.find(wanted, offset + 1)
        return None, link_only
    while offset >= 0:
        start, end = spans[offset][0], spans[offset + len(requested) - 1][1]
        if plain[offset + len(requested)] != plain[offset]:
            if (start, end) in original_matches:
                return body[start:end], False
        else:
            link_only = True
        offset = visible.find(requested, offset + 1)
    return None, link_only
