from __future__ import annotations
import hashlib
import ipaddress
import json
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
_BODY = re.compile(r"(?m)^\[正文\][ \t]*\r?$\n?")
_NON_BODY = re.compile(r"(?m)^\[(?:SEO 信息|站内链接|页面图片URL)\]")
def page_body_text(text: str) -> str:
 marker = _BODY.search(text)
 if marker is None:
  return "" if _NON_BODY.search(text) else text
 body = text[marker.end():]
 ending = _NON_BODY.search(body)
 return body[:ending.start()] if ending else body
def _evidence_display_char(char: str) -> str:
 return "'" if char in {"'", "\u2019"} else char
def _inline_tokens(text: str):
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
 count = 0
 index -= 1
 while index >= 0 and text[index] == "\\":
  count += 1
  index -= 1
 return bool(count % 2)
def _emphasis_span_maps(text: str) -> tuple[set[int], dict[int, int], dict[int, int]]:
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
 visible, _ = _project(text)
 return unicodedata.normalize("NFKC", visible).casefold()
def _qualification_reference_and_anchor_nodes(text):
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
 body = page_body_text(source)
 visible, spans, links = _qualification_projection(body)
 requested, _requested_spans, _requested_links = _qualification_projection(quote)
 if not requested or "\x00" in requested:
  return None, False
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
SCHEMA_VERSION = 1
SKILL_ID = "9d22d4db-6f99-4e6f-82f7-e1bc30a539bb"
HOOK_ID = "qualify_candidate_batch"
DECISION_ID = "nightly_candidate_batch"
DECISION_PATH = "hen44_candidate_decision.json"
TARGET_WIKI = "B端客户开发"
TARGET_FILENAME = "customer_feedback_registry.md"
TABLE_HEADER = (
 "| canonical_domain | official_name | normalized_location | feedback_status | "
 "business_owner | last_contacted_by | last_contacted_at | feedback_reason | "
 "feedback_source | updated_at | recheck_after | candidate_found_at | "
 "candidate_qualification_urls | candidate_address_url | candidate_contact_url | "
 "candidate_phone | candidate_email | candidate_contact_name | "
 "candidate_contact_title | candidate_linkedin_url | candidate_product_url | "
 "candidate_product_focus | candidate_priority_scores | candidate_review_note | notes |"
)
TABLE_SEPARATOR = "| " + " | ".join(["---"] * 25) + " |"
LEGACY_TAIL_CELL_COUNT = 12
LEGACY_TAIL_STATUS_INDEX = 2
PENDING_STATUS = "candidate_pending_review"
DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}\.)+[a-z]{2,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_PATH_RE = re.compile(
 r"^artifacts/spill_[0-9a-f]{32}\.(?:json|txt)$"
)
RESERVED_HOST_SUFFIXES = (
 ".example",
 ".invalid",
 ".localhost",
 ".local",
 ".internal",
 ".lan",
 ".home",
 ".test",
)
COMMON_COUNTRY_CODE_SECOND_LEVEL_SUFFIXES = frozenset(
 {"ac", "co", "com", "edu", "gov", "mil", "net", "org"}
)
SCORE_KEYS = (
 "type",
 "category",
 "proximity",
 "evidence",
 "product",
 "contactability",
 "total",
)
SCORE_ALLOWED = {
 "type": {0, 10, 20},
 "category": {0, 15, 25},
 "proximity": {0, 5, 10, 15, 20},
 "evidence": {0, 5, 10, 15},
 "product": {0, 5, 10, 15},
 "contactability": {0, 3, 5},
}
MAX_DECISION_BYTES = 1_000_000
MAX_ARTIFACT_BYTES = 2_000_000
MAX_CANDIDATES = 20
MAX_TOOL_FACTS = 128
class DecisionRejected(ValueError):
 def __init__(self, message: str, *, binding_failures=None, repair_hints=None):
  super().__init__(message)
  self.binding_failures = list(binding_failures or ())
  self.repair_hints = list(repair_hints or ())
def reject(message: str) -> None:
 raise DecisionRejected(message)
def incomplete_candidate_repair_hints(
 actual_count: int, required_count: int
) -> list[dict[str, object]]:
 return [
  {
   "kind": "persisted_workspace_draft",
   "decision_artifact_path": DECISION_PATH,
   "actual_count": actual_count,
   "required_count": required_count,
   "host_recognizes": "only evidence-backed candidates persisted in this decision artifact",
   "authority_status": "not_authorized_to_write_registry_or_reduce_required_count",
  },
  {
   "kind": "repair_before_resubmit",
   "decision_artifact_path": DECISION_PATH,
   "action": "file_patch_or_file_write",
   "guidance": "preserve the draft; add or repair evidence-backed candidates, then rerun the Hook",
   "authority_status": "not_approval_not_target_relaxation",
  },
 ]
def sha256_bytes(raw: bytes) -> str:
 return hashlib.sha256(raw).hexdigest()
def sha256_text(text: str) -> str:
 return sha256_bytes(text.encode("utf-8"))
def canonical_json(value: object) -> str:
 return json.dumps(
  value,
  ensure_ascii=False,
  sort_keys=True,
  separators=(",", ":"),
 )
def exact_object(value: object, fields: set[str], label: str) -> dict:
 if not isinstance(value, dict):
  reject(f"{label} object required")
 actual = set(value)
 if actual != fields:
  missing = sorted(fields - actual)
  extra = sorted(actual - fields)
  detail = ", ".join(
   part
   for part in (
    "missing=" + ",".join(missing) if missing else "",
    "extra=" + ",".join(extra) if extra else "",
   )
   if part
  )
  reject(f"{label} fields are invalid ({detail[:512]})")
 return value
def bounded_text(value: object, label: str, *, empty: bool = False) -> str:
 if not isinstance(value, str) or len(value) > 100_000 or "\x00" in value:
  reject(f"{label} is invalid")
 if not empty and not value.strip():
  reject(f"{label} is empty")
 return value
def bounded_int(value: object, label: str) -> int:
 if isinstance(value, bool) or type(value) is not int or not 0 <= value <= 1_000_000:
  reject(f"{label} is invalid")
 return value
def sha256_value(value: object, label: str) -> str:
 if not isinstance(value, str):
  reject(f"{label} is invalid")
 digest = value.strip().lower()
 if SHA256_RE.fullmatch(digest) is None:
  reject(f"{label} is invalid")
 return digest
def artifact_path(value: object, label: str, *, suffix: str) -> Path:
 text = bounded_text(value, label)
 if ARTIFACT_PATH_RE.fullmatch(text) is None or not text.endswith(suffix):
  reject(f"{label} is not a host artifact path")
 path = PurePosixPath(text)
 if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
  reject(f"{label} escapes the workspace")
 return Path(*path.parts)
def read_verified_artifact(
 path_value: object,
 digest_value: object,
 size_value: object,
 label: str,
 *,
 suffix: str,
) -> tuple[Path, bytes]:
 path = artifact_path(path_value, f"{label}.artifact_path", suffix=suffix)
 digest = sha256_value(digest_value, f"{label}.artifact_sha256")
 size = bounded_int(size_value, f"{label}.artifact_bytes")
 if not 0 < size <= MAX_ARTIFACT_BYTES:
  reject(f"{label} artifact bytes are outside the limit")
 if not path.is_file():
  reject(f"{label} artifact is unavailable")
 raw = path.read_bytes()
 if len(raw) != size:
  reject(f"{label} artifact bytes do not match host evidence")
 if sha256_bytes(raw) != digest:
  reject(f"{label} artifact sha256 does not match host evidence")
 return path, raw
def public_url(value: object, label: str) -> tuple[str, str]:
 text = bounded_text(value, label).strip()
 try:
  parts = urlsplit(text)
  port = parts.port
 except ValueError:
  reject(f"{label} is not a public http(s) URL")
 if (
  parts.scheme not in {"http", "https"}
  or not parts.netloc
  or parts.username is not None
  or parts.password is not None
  or port == 0
 ):
  reject(f"{label} is not a public http(s) URL")
 raw_host = parts.hostname
 if not raw_host or raw_host.endswith("."):
  reject(f"{label} is not a public http(s) URL")
 try:
  host = raw_host.encode("idna").decode("ascii").casefold()
 except UnicodeError:
  reject(f"{label} is not a public http(s) URL")
 try:
  address = ipaddress.ip_address(host)
 except ValueError:
  if (
   len(host) > 253
   or DOMAIN_RE.fullmatch(host) is None
   or host.endswith(RESERVED_HOST_SUFFIXES)
  ):
   reject(f"{label} is not a public http(s) URL")
 else:
  if not address.is_global:
   reject(f"{label} is not a public http(s) URL")
 return (parts._replace(path="/").geturl() if not parts.path else text), host
def domain_matches_host(domain: str, host: str) -> bool:
 return host == domain or host.endswith("." + domain)
def reject_common_public_suffix_shape(domain: str, label: str) -> None:
 labels = domain.split(".")
 if (
  len(labels) == 2
  and len(labels[1]) == 2
  and labels[0] in COMMON_COUNTRY_CODE_SECOND_LEVEL_SUFFIXES
 ):
  reject(f"{label} is a common country-code public suffix, not a registrable domain")
def normalized_claim_text(value: str) -> str:
 return normalize_evidence_text(value)
def parse_trusted_context(value: object) -> tuple[dict[str, int], list[dict], list[dict]]:
 context = exact_object(
  value,
  {"schema_version", "skill_id", "hook_id", "hook_input", "tool_evidence"},
  "trusted Hook context",
 )
 if (
  context["schema_version"] != SCHEMA_VERSION
  or context["skill_id"] != SKILL_ID
  or context["hook_id"] != HOOK_ID
 ):
  reject("trusted Hook context binding is invalid")
 raw_task = exact_object(
  context["hook_input"],
  {"inventory_limit", "full_candidate_target", "candidate_add_limit"},
  "trusted hook_input",
 )
 task = {key: bounded_int(raw_task[key], f"hook_input.{key}") for key in raw_task}
 evidence = exact_object(
  context["tool_evidence"],
  {"web_fetches", "knowledge_reads"},
  "trusted tool_evidence",
 )
 web, pages = evidence["web_fetches"], evidence["knowledge_reads"]
 if (
  not isinstance(web, list)
  or not isinstance(pages, list)
  or len(web) > MAX_TOOL_FACTS
  or not 1 <= len(pages) <= MAX_TOOL_FACTS
 ):
  reject("trusted tool_evidence lists are invalid")
 return task, pages, web
def build_web_index(values: list[dict]) -> dict[tuple[str, str], dict]:
 index: dict[tuple[str, str], dict] = {}
 paths: dict[str, str] = {}
 for item_index, value in enumerate(values):
  label = f"tool_evidence.web_fetches[{item_index}]"
  fact = exact_object(
   value,
   {"url", "artifact_path", "artifact_sha256", "artifact_bytes"},
   label,
  )
  url, _host = public_url(fact["url"], f"{label}.url")
  path = artifact_path(fact["artifact_path"], f"{label}.artifact_path", suffix=".txt")
  digest = sha256_value(fact["artifact_sha256"], f"{label}.artifact_sha256")
  size = bounded_int(fact["artifact_bytes"], f"{label}.artifact_bytes")
  if not 0 < size <= MAX_ARTIFACT_BYTES:
   reject(f"{label}.artifact_bytes is outside the limit")
  path_text = path.as_posix()
  if path_text in paths and paths[path_text] != url:
   reject("one host web_fetch artifact is bound to multiple URLs")
  paths[path_text] = url
  key = (url, path_text)
  normalized = {
   "url": url,
   "artifact_path": path_text,
   "artifact_sha256": digest,
   "artifact_bytes": size,
  }
  if key in index and index[key] != normalized:
   reject("conflicting host web_fetch evidence is present")
  index[key] = normalized
 return index
def phone_source_matches(body: str):
 pattern = re.compile(r"(?<!\w)\+?\d[\d \t().-]{5,}\d(?!\w)")
 for match in pattern.finditer(body):
  opening = match.group().find("(")
  if opening >= 0 and sum(c.isdecimal() for c in match.group()[:opening]) > 3:
   match = pattern.search(body, match.start() + opening, match.end())
  if match is not None:
   yield match
def contact_source_quote(body: str, values: dict, label: str) -> str:
    body = page_body_text(body)
    """Derive an exact source span only from a supplied public channel."""
    email = unicodedata.normalize("NFKC", values["email"]).casefold()
    phone = "".join(c for c in unicodedata.normalize("NFKC", values["phone"]) if c.isdecimal())
    match = None
    if email:
        match = next((item for item in re.finditer(r"[\w.+%'-]+@(?:[\w-]+\.)+[\w-]+", body)
                      if unicodedata.normalize("NFKC", item.group()).casefold() == email), None)
    if match is None and len(phone) >= 7:
        match = next((item for item in phone_source_matches(body)
                      if "".join(c for c in unicodedata.normalize("NFKC", item.group()) if c.isdecimal()) == phone), None)
    if match is None and values["form_url"]:
        match = re.search(re.escape(values["form_url"]), body)
    if match is None:
        reject(f"{label} cannot derive quote from its supplied public channels")
    start, end = match.span()
    # Short valid channels need a little literal source context, never invented text.
    if end - start < 8:
        start, end = max(0, start - 8), min(len(body), end + 8)
    return body[start:end]
def evidence_fact(
 value: object,
 label: str,
 *,
 domain: str,
 web_index: dict[tuple[str, str], dict],
 artifact_cache: dict[str, str],
 value_required: bool = False,
 contact_values: dict | None = None,
) -> dict:
 fields = {"url", "evidence_file", "quote"}
 if value_required:
  fields.add("value")
 quote_omitted = value_required and isinstance(value, dict) and "quote" not in value
 fact = exact_object(value, fields - {"quote"} if quote_omitted else fields, label)
 url, host = public_url(fact["url"], f"{label}.url")
 if not domain_matches_host(domain, host):
  reject(f"{label}.url host does not match canonical_domain")
 path = artifact_path(fact["evidence_file"], f"{label}.evidence_file", suffix=".txt")
 trusted = web_index.get((url, path.as_posix()))
 if trusted is None:
  url_matches = [
   item
   for (trusted_url, _trusted_path), item in web_index.items()
   if trusted_url == url
  ]
  if len(url_matches) != 1:
   reject(f"{label} is not bound to host web_fetch evidence")
  trusted = url_matches[0]
  path = artifact_path(
   trusted["artifact_path"],
   f"{label}.evidence_file",
   suffix=".txt",
  )
 body = artifact_cache.get(path.as_posix())
 if body is None:
  _path, raw = read_verified_artifact(
   trusted["artifact_path"],
   trusted["artifact_sha256"],
   trusted["artifact_bytes"],
   "web_fetch",
   suffix=".txt",
  )
  try:
   body = raw.decode("utf-8")
  except UnicodeDecodeError:
   reject("web_fetch artifact is not UTF-8 text")
  artifact_cache[path.as_posix()] = body
 quote = fact["value"] if quote_omitted else fact["quote"]
 if contact_values is not None and quote in (None, ""):
  quote = contact_source_quote(body, contact_values, label)
 quote = bounded_text(quote, f"{label}.quote")
 if len(quote.strip()) < 8:
  reject(f"{label}.quote is too short")
 qualification_gate = (
  label.startswith("eligible_candidates[")
  and ".qualification_gates." in label
 )
 link_only = False
 if contact_values is not None and quote in body:
  grounded_quote = quote
 elif qualification_gate:
  grounded_quote, link_only = qualification_canonical_source_quote(body, quote)
 else:
  grounded_quote = canonical_source_quote(body, quote)
 if grounded_quote is None:
  if link_only:
   raise DecisionRejected(f"{label}.quote is only present in hyperlink label text")
  raise DecisionRejected(
   f"{label}.quote is not present in its host web_fetch artifact",
   binding_failures=[{"field": label, "domain": domain, "quote": quote, "bound_url": url}],
  )
 quote = bounded_text(grounded_quote, f"{label}.quote")
 if value_required:
  claim = bounded_text(fact["value"], f"{label}.value").strip()
  normalized_claim = normalized_claim_text(claim)
  minimum = 8 if label.endswith(".address") else 3
  if (
   len(normalized_claim) < minimum
   or normalized_claim not in normalized_claim_text(quote)
   or normalized_claim not in normalized_claim_text(body)
  ):
   reject(f"{label}.value is not supported by its verified quote and artifact")
 return {**fact, "quote": quote, "evidence_file": path.as_posix()}
def source_binding_repair_hints(failures: list[dict], web_index: dict, artifact_cache: dict) -> list[dict]:
 hints: list[dict] = []
 remaining = 1_000_000
 verified: dict[str, str] = {}
 for failure in failures[:6]:
  try:
   _claimed_url, claimed_host = public_url(failure["bound_url"], "repair_claim.url")
   if not domain_matches_host(failure["domain"], claimed_host):
    continue
  except (DecisionRejected, KeyError, TypeError):
   continue
  matches: list[dict] = []
  seen: set[tuple[str, str]] = set()
  for trusted in reversed(list(web_index.values())):
   try:
    url, host = public_url(trusted["url"], "repair_source.url")
    if url == failure["bound_url"] or not domain_matches_host(failure["domain"], host):
     continue
    key = (url, trusted["artifact_path"])
    if key in seen:
     continue
    seen.add(key)
    size = trusted["artifact_bytes"]
    if size > remaining:
     continue
    remaining -= size
    cache_key = trusted["artifact_path"] + ":" + trusted["artifact_sha256"]
    body = verified.get(cache_key)
    if body is None:
     _path, raw = read_verified_artifact(
      trusted["artifact_path"], trusted["artifact_sha256"],
      size, "web_fetch", suffix=".txt",
     )
     body = raw.decode("utf-8")
     verified[cache_key] = body
    if canonical_source_quote(page_body_text(body), failure["quote"]) is None:
     continue
    matches.append({"url": url, "evidence_file": trusted["artifact_path"]})
    if len(matches) == 2:
     break
   except (DecisionRejected, OSError, UnicodeError, KeyError, TypeError):
    continue
  if matches:
   proposed = hints + [{"field": failure["field"], "claimed_url": failure["bound_url"], "matching_sources": matches}]
   if len(canonical_json(proposed).encode("utf-8")) > 4096:
    break
   hints = proposed
 return hints
def contact_fact(
    value: object,
    label: str,
    *,
    domain: str,
    web_index: dict[tuple[str, str], dict],
    artifact_cache: dict[str, str],
) -> dict:
    required_fields = {"url", "evidence_file"}
    if not isinstance(value, dict):
        reject(f"{label} must be an object")
    missing = required_fields.difference(value)
    if missing:
        reject(f"{label} missing required fields: {sorted(missing)}")
    fact = {
        key: value.get(key, "")
        for key in ("url", "evidence_file", "quote", "phone", "email", "form_url")
    }
    # Omitted and JSON-null optional channels both mean unknown.
    # A candidate still requires at least one verified public channel below.
    for key in ("phone", "email", "form_url"):
        if fact[key] is None:
            fact[key] = ""
    phone = bounded_text(fact["phone"], f"{label}.phone", empty=True).strip()
    email = bounded_text(fact["email"], f"{label}.email", empty=True).strip()
    form_url = bounded_text(fact["form_url"], f"{label}.form_url", empty=True).strip()
    base = evidence_fact(
        {key: fact[key] for key in ("url", "evidence_file", "quote")},
        label,
        domain=domain,
        web_index=web_index,
        artifact_cache=artifact_cache,
        contact_values={"phone": phone, "email": email, "form_url": form_url},
    )
    body = page_body_text(artifact_cache[base["evidence_file"]])
    if phone:
        phone_digits = "".join(
            char for char in unicodedata.normalize("NFKC", phone) if char.isdecimal()
        )
        phone_fragments = [match.group() for match in phone_source_matches(unicodedata.normalize("NFKC", body))]
        supported = any(
            "".join(char for char in fragment if char.isdecimal()) == phone_digits
            for fragment in phone_fragments
        )
        if len(phone_digits) < 7 or not supported:
            phone = ""
    if email:
        normalized_email = unicodedata.normalize("NFKC", email).casefold()
        normalized_body = unicodedata.normalize("NFKC", body).casefold()
        email_pattern = re.compile(
            r"(?<![a-z0-9._%+-])" + re.escape(normalized_email) + r"(?![a-z0-9._%+-])"
        )
        if (
            re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized_email) is None
            or email_pattern.search(normalized_body) is None
        ):
            email = ""
    if form_url:
        try:
            _form_url, host = public_url(form_url, f"{label}.form_url")
        except DecisionRejected:
            form_url = ""
        else:
            if not domain_matches_host(domain, host) or form_url not in body:
                form_url = ""
    if not email:
        reject(f"{label} requires a verified public business email")
    return {**base, "phone": phone, "email": email, "form_url": form_url}
def score_object(value: object, label: str) -> dict[str, int]:
 score = exact_object(value, set(SCORE_KEYS), label)
 normalized = {key: bounded_int(score[key], f"{label}.{key}") for key in SCORE_KEYS}
 for key, allowed in SCORE_ALLOWED.items():
  if normalized[key] not in allowed:
   reject(f"{label}.{key} is outside the discrete score set")
 expected = sum(normalized[key] for key in SCORE_KEYS if key != "total")
 if normalized["total"] != expected:
  reject(f"{label}.total does not equal the six components")
 if normalized["type"] == 0 or normalized["category"] == 0:
  reject(f"{label} is not a qualified customer candidate")
 if normalized["evidence"] not in {10, 15}:
  reject(f"{label}.evidence lacks automatic public-source coverage")
 if normalized["contactability"] == 0:
  reject(f"{label}.contactability is incomplete")
 return normalized
def split_markdown_cells(line: str) -> list[str]:
 if not line.startswith("|") or not line.endswith("|"):
  reject("mutation row is not a Markdown table row")
 cells: list[str] = []
 current: list[str] = []
 escaped = False
 for char in line[1:-1]:
  if escaped:
   current.append(char)
   escaped = False
  elif char == "\\":
   escaped = True
  elif char == "|":
   cells.append("".join(current).strip())
   current = []
  else:
   current.append(char)
 if escaped:
  reject("mutation row ends with an incomplete escape")
 cells.append("".join(current).strip())
 return cells
def split_markdown_row(line: str) -> list[str]:
 cells = split_markdown_cells(line)
 if len(cells) != 25:
  reject("mutation row must contain exactly 25 cells")
 return cells
def is_supported_legacy_tail_row(
 lines: list[str],
 row_index: int,
 cells: list[str],
 *,
 after_blank_boundary: bool,
) -> bool:
 if (
  not after_blank_boundary
  or len(cells) != LEGACY_TAIL_CELL_COUNT
  or DOMAIN_RE.fullmatch(cells[0].casefold()) is None
  or cells[LEGACY_TAIL_STATUS_INDEX] != PENDING_STATUS
 ):
  return False
 next_index = row_index + 1
 saw_blank = False
 while next_index < len(lines) and not lines[next_index].strip():
  saw_blank = True
  next_index += 1
 if not saw_blank or next_index >= len(lines):
  return False
 return lines[next_index].startswith("## ")
def parse_scores_cell(value: str) -> dict[str, int]:
 parsed: dict[str, int] = {}
 for part in value.split(";"):
  key, separator, raw = part.strip().partition("=")
  if not separator or not raw.strip().isdigit() or key in parsed:
   reject("candidate_priority_scores row cell is invalid")
  parsed[key] = int(raw.strip())
 return score_object(parsed, "candidate_priority_scores")
def knowledge_page(value: object, index: int) -> dict:
 label = f"tool_evidence.knowledge_reads[{index}]"
 fact = exact_object(
  value,
  {
   "requested_wiki",
   "wiki_id",
   "wiki_name",
   "file_id",
   "filename",
   "content_hash",
   "total_chars",
   "offset",
   "end",
   "next_offset",
   "artifact_path",
   "artifact_sha256",
   "artifact_bytes",
  },
  label,
 )
 if (
  fact["requested_wiki"] != TARGET_WIKI
  or fact["wiki_name"] != TARGET_WIKI
  or fact["filename"] != TARGET_FILENAME
 ):
  reject(f"{label} does not target the registry")
 for key in ("wiki_id", "wiki_name", "file_id", "filename"):
  bounded_text(fact[key], f"{label}.{key}")
 digest = sha256_value(fact["content_hash"], f"{label}.content_hash")
 total = bounded_int(fact["total_chars"], f"{label}.total_chars")
 offset = bounded_int(fact["offset"], f"{label}.offset")
 end = bounded_int(fact["end"], f"{label}.end")
 next_offset = fact["next_offset"]
 if next_offset is not None:
  next_offset = bounded_int(next_offset, f"{label}.next_offset")
 if not offset <= end <= total:
  reject(f"{label} range is invalid")
 _path, raw = read_verified_artifact(
  fact["artifact_path"],
  fact["artifact_sha256"],
  fact["artifact_bytes"],
  "knowledge_read",
  suffix=".json",
 )
 try:
  payload = json.loads(raw.decode("utf-8"))
 except (UnicodeDecodeError, json.JSONDecodeError):
  reject(f"{label} artifact is not valid UTF-8 JSON")
 if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
  reject(f"{label} artifact payload is invalid")
 expected_payload = {
  "status": "ok",
  "wiki_id": fact["wiki_id"],
  "wiki_name": fact["wiki_name"],
  "file_id": fact["file_id"],
  "filename": fact["filename"],
  "content_hash": digest,
  "total_chars": total,
  "offset": offset,
  "next_offset": next_offset,
 }
 if any(payload.get(key) != expected for key, expected in expected_payload.items()):
  reject(f"{label} artifact payload does not match host page facts")
 content = payload["content"]
 if offset + len(content) != end:
  reject(f"{label} artifact content does not match its range")
 return {
  "identity": (
   fact["wiki_id"],
   fact["wiki_name"],
   fact["file_id"],
   fact["filename"],
  ),
  "content_hash": digest,
  "total_chars": total,
  "offset": offset,
  "end": end,
  "next_offset": next_offset,
  "content": content,
 }
def registry_snapshot(raw_pages: list[dict]) -> tuple[str, set[str], int]:
 pages = [knowledge_page(value, index) for index, value in enumerate(raw_pages)]
 identities = {page["identity"] for page in pages}
 hashes = {page["content_hash"] for page in pages}
 totals = {page["total_chars"] for page in pages}
 if len(identities) != 1:
  reject("registry pages do not describe the same file")
 if len(hashes) != 1:
  reject("registry pages do not share the same content_hash")
 if len(totals) != 1:
  reject("registry pages do not share the same total_chars")
 pages.sort(key=lambda page: page["offset"])
 expected_offset = 0
 chunks: list[str] = []
 for page_index, page in enumerate(pages):
  if page["offset"] != expected_offset:
   reject("registry pages are not continuous from offset zero")
  expected_offset = page["end"]
  is_last = page_index == len(pages) - 1
  expected_next = None if is_last else expected_offset
  if page["next_offset"] != expected_next:
   reject("registry page next_offset chain is not continuous")
  chunks.append(page["content"])
 total = next(iter(totals))
 digest = next(iter(hashes))
 if expected_offset != total:
  reject("registry pages do not provide complete coverage")
 text = "".join(chunks)
 if len(text) != total or sha256_text(text) != digest:
  reject("reconstructed registry does not match content_hash and total_chars")
 if text.count(TABLE_HEADER) != 1:
  reject("registry table header must occur exactly once in the full text")
 if text.count(TABLE_SEPARATOR) != 1:
  reject("registry table separator must occur exactly once in the full text")
 lines = text.splitlines()
 header_index = lines.index(TABLE_HEADER)
 if header_index + 1 >= len(lines) or lines[header_index + 1] != TABLE_SEPARATOR:
  reject("registry table separator is invalid")
 domains: set[str] = set()
 pending_count = 0
 table_started = False
 after_blank_boundary = False
 for line_index in range(header_index + 2, len(lines)):
  line = lines[line_index]
  if not line.strip():
   if table_started:
    after_blank_boundary = True
   continue
  if not line.startswith("|"):
   if table_started:
    break
   reject("registry table has content before its first data row")
  table_started = True
  cells = split_markdown_cells(line)
  if len(cells) != 25:
   if is_supported_legacy_tail_row(
    lines,
    line_index,
    cells,
    after_blank_boundary=after_blank_boundary,
   ):
    domain = cells[0].casefold()
    if domain in domains:
     reject("registry contains duplicate canonical_domain rows")
    domains.add(domain)
    pending_count += 1
    break
   reject("mutation row must contain exactly 25 cells")
  after_blank_boundary = False
  domain = cells[0].casefold()
  if domain:
   if domain in domains:
    reject("registry contains duplicate canonical_domain rows")
   domains.add(domain)
  if cells[3] == PENDING_STATUS:
   pending_count += 1
 return digest, domains, pending_count
def validate_candidate(
 value: object,
 index: int,
 *,
 web_index: dict[tuple[str, str], dict],
 artifact_cache: dict[str, str],
) -> dict:
 label = f"eligible_candidates[{index}]"
 candidate = exact_object(
  value,
  {
   "canonical_domain",
   "official_name",
   "normalized_location",
   "qualification_gates",
   "address",
   "contact",
   "product",
   "priority_scores",
  },
  label,
 )
 domain = bounded_text(candidate["canonical_domain"], f"{label}.canonical_domain").strip().casefold()
 if (
  domain.startswith("www.")
  or DOMAIN_RE.fullmatch(domain) is None
  or domain.endswith(RESERVED_HOST_SUFFIXES)
 ):
  reject(f"{label}.canonical_domain is not canonical public DNS")
 reject_common_public_suffix_shape(domain, f"{label}.canonical_domain")
 official_name = bounded_text(candidate["official_name"], f"{label}.official_name")
 location = bounded_text(candidate["normalized_location"], f"{label}.normalized_location")
 gates = exact_object(
  candidate["qualification_gates"],
  {"entity_type", "independent_operation", "regional_scope"},
  f"{label}.qualification_gates",
 )
 mechanical_errors: list[str] = []
 binding_failures: list[dict] = []
 def collect(validator):
  try:
   return validator()
  except DecisionRejected as exc:
   mechanical_errors.append(str(exc))
   binding_failures.extend(exc.binding_failures)
   return None
 normalized_gates = {
  key: collect(
   lambda key=key: evidence_fact(
    gates[key],
    f"{label}.qualification_gates.{key}",
    domain=domain,
    web_index=web_index,
    artifact_cache=artifact_cache,
   ),
  )
  for key in ("entity_type", "independent_operation", "regional_scope")
 }
 address = collect(
  lambda: evidence_fact(
   candidate["address"],
   f"{label}.address",
   domain=domain,
   web_index=web_index,
   artifact_cache=artifact_cache,
   value_required=True,
  ),
 )
 contact = collect(
  lambda: contact_fact(
   candidate["contact"],
   f"{label}.contact",
   domain=domain,
   web_index=web_index,
   artifact_cache=artifact_cache,
  ),
 )
 product = collect(
  lambda: evidence_fact(
   candidate["product"],
   f"{label}.product",
   domain=domain,
   web_index=web_index,
   artifact_cache=artifact_cache,
   value_required=True,
  ),
 )
 scores = collect(
  lambda: score_object(candidate["priority_scores"], f"{label}.priority_scores"),
 )
 if mechanical_errors:
  raise DecisionRejected("; ".join(mechanical_errors), binding_failures=binding_failures)
 return {
  "canonical_domain": domain,
  "official_name": official_name,
  "normalized_location": location,
  "qualification_gates": normalized_gates,
  "address": address,
  "contact": contact,
  "product": product,
  "priority_scores": scores,
 }
def aware_timestamp(value: str, label: str) -> datetime:
 try:
  parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
 except (TypeError, ValueError):
  reject(f"{label} is invalid")
 if parsed.tzinfo is None or parsed.utcoffset() is None:
  reject(f"{label} must include a timezone")
 return parsed
def candidate_business_summary(candidate: dict) -> str:
    labels = ("经营类型", "独立经营", "区域范围")
    keys = ("entity_type", "independent_operation", "regional_scope")
    return "；".join(
        label + "：" + " ".join(candidate["qualification_gates"][key]["quote"].split())
        for label, key in zip(labels, keys)
    )


def validate_row(cells: list[str], candidate: dict) -> None:
    if cells[0].casefold() != candidate["canonical_domain"]:
        reject("mutation row canonical_domain does not match the candidate")
    if cells[1] != candidate["official_name"] or cells[2] != candidate["normalized_location"]:
        reject("mutation row identity does not match the candidate")
    if cells[3] != PENDING_STATUS:
        reject("mutation row feedback_status must be candidate_pending_review")
    for index in (4, 5, 6, 10, 23):
        if cells[index]:
            reject("mutation row writes a forbidden internal-review field")
    if cells[7] != candidate_business_summary(candidate) or cells[8] != "自动夜间调研":
        reject("mutation row qualification reason or feedback_source is invalid")
    aware_timestamp(cells[9], "mutation row updated_at")
    aware_timestamp(cells[11], "mutation row candidate_found_at")
    if cells[9] != cells[11]:
        reject("mutation row timestamps must describe the same discovery event")
    expected_gate_urls = "; ".join(
        candidate["qualification_gates"][key]["url"]
        for key in ("entity_type", "independent_operation", "regional_scope")
    )
    if cells[12] != expected_gate_urls:
        reject("mutation row qualification URLs do not exactly match the candidate")
    if cells[13] != candidate["address"]["url"]:
        reject("mutation row address URL does not match")
    if cells[14] != candidate["contact"]["url"]:
        reject("mutation row contact URL does not match")
    if cells[15] != candidate["contact"]["phone"] or cells[16] != candidate["contact"]["email"]:
        reject("mutation row public contact fields do not match")
    if any(cells[index] for index in (17, 18, 19)):
        reject("mutation row adds unverified contact identity fields")
    if cells[20] != candidate["product"]["url"] or cells[21] != candidate["product"]["value"]:
        reject("mutation row product fields do not match")
    if parse_scores_cell(cells[22]) != candidate["priority_scores"]:
        reject("mutation row score cell does not match")
    if cells[24] != "官方完整地址：" + " ".join(candidate["address"]["value"].split()):
        reject("mutation row full address does not match the verified candidate")
def markdown_row(cells: list[str]) -> str:
 if len(cells) != 25:
  reject("canonical mutation row must contain exactly 25 cells")
 escaped = [item.replace("\\", "\\\\").replace("|", "\\|") for item in cells]
 return "| " + " | ".join(escaped) + " |"
def canonical_candidate_row(candidate: dict, timestamp: str) -> str:
    cells = [""] * 25
    cells[0] = candidate["canonical_domain"]
    cells[1] = candidate["official_name"]
    cells[2] = candidate["normalized_location"]
    cells[3] = PENDING_STATUS
    cells[7] = candidate_business_summary(candidate)
    cells[8] = "自动夜间调研"
    cells[9] = timestamp
    cells[11] = timestamp
    cells[12] = "; ".join(
        candidate["qualification_gates"][key]["url"]
        for key in ("entity_type", "independent_operation", "regional_scope")
    )
    cells[13] = candidate["address"]["url"]
    cells[14] = candidate["contact"]["url"]
    cells[15] = candidate["contact"]["phone"]
    cells[16] = candidate["contact"]["email"]
    cells[20] = candidate["product"]["url"]
    cells[21] = candidate["product"]["value"]
    cells[22] = "; ".join(
        f"{key}={candidate['priority_scores'][key]}" for key in SCORE_KEYS
    )
    cells[24] = "官方完整地址：" + " ".join(candidate["address"]["value"].split())
    return markdown_row(cells)
def separator_scaffold_line(value: str) -> bool:
 text = value.strip()
 if len(text) > 4096 or "\n" in text or "\r" in text:
  return False
 if not text.startswith("|") or not text.endswith("|"):
  return False
 cells = text[1:-1].split("|")
 return 1 <= len(cells) <= 128 and all(
  re.fullmatch(r":?-{3,}:?", cell.strip()) is not None for cell in cells
 )
def canonical_plan(
 value: object,
 basis_hash: str,
 candidates: list[dict],
) -> tuple[dict, dict]:
 plan = exact_object(
  value,
  {"wiki", "filename", "expected_content_hash", "old_text", "new_text", "patches"},
  "mutation_plan",
 )
 if plan["wiki"] != TARGET_WIKI or plan["filename"] != TARGET_FILENAME:
  reject("mutation_plan target is invalid")
 expected_hash = sha256_value(
  plan["expected_content_hash"], "mutation_plan.expected_content_hash"
 )
 if expected_hash != basis_hash:
  reject("mutation_plan expected_content_hash is stale")
 if plan["patches"] is not None:
  reject("mutation_plan must use the single canonical legacy patch")
 old_text = bounded_text(plan["old_text"], "mutation_plan.old_text")
 new_text = bounded_text(plan["new_text"], "mutation_plan.new_text")
 lines = new_text.splitlines()
 if (
  not separator_scaffold_line(old_text)
  or not lines
  or not separator_scaffold_line(lines[0])
 ):
  reject("mutation_plan must use separator placeholders before timestamp rows")
 rows = [line for line in lines[1:] if line.strip()]
 if len(rows) != len(candidates):
  reject("mutation_plan row count does not match eligible_count")
 timestamps: list[str] = []
 for row in rows:
  scaffold = row.strip()
  if (
   scaffold.startswith("|") and scaffold.endswith("|")
   and "|" not in scaffold[1:-1]
  ):
   timestamp = scaffold[1:-1].strip()
   aware_timestamp(timestamp, "mutation plan timestamp scaffold")
   timestamps.append(timestamp)
  elif scaffold.startswith("|"):
   cells = split_markdown_row(scaffold)
   updated_at = cells[9]
   candidate_found_at = cells[11]
   aware_timestamp(updated_at, "mutation row updated_at")
   aware_timestamp(candidate_found_at, "mutation row candidate_found_at")
   if updated_at != candidate_found_at:
    reject("mutation row timestamps must describe the same discovery event")
   timestamps.append(updated_at)
  else:
   aware_timestamp(scaffold, "mutation plan timestamp scaffold")
   timestamps.append(scaffold)
 normalized_new_text = TABLE_SEPARATOR + "\n" + "\n".join(
  canonical_candidate_row(candidate, timestamp)
  for candidate, timestamp in zip(candidates, timestamps, strict=True)
 )
 normalized_artifact_plan = {
  "wiki": TARGET_WIKI,
  "filename": TARGET_FILENAME,
  "expected_content_hash": expected_hash,
  "old_text": TABLE_SEPARATOR,
  "new_text": normalized_new_text,
  "patches": None,
 }
 platform_plan = {
  "version": 1,
  "tool": "knowledge_file_patch",
  "target": {"wiki": TARGET_WIKI, "filename": TARGET_FILENAME},
  "expected_content_hash": expected_hash,
  "patches": [{"old_text": TABLE_SEPARATOR, "new_text": normalized_new_text}],
 }
 return platform_plan, normalized_artifact_plan
def decide(path: Path, trusted_context: object) -> dict:
 task, raw_pages, raw_web = parse_trusted_context(trusted_context)
 if not path.is_file() or path.stat().st_size > MAX_DECISION_BYTES:
  reject("decision artifact is missing or too large")
 value = json.loads(path.read_text(encoding="utf-8"))
 artifact = exact_object(
  value,
  {"schema_version", "decision_id", "target", "eligible_candidates", "mutation_plan"},
  "decision artifact",
 )
 if artifact["schema_version"] != SCHEMA_VERSION or artifact["decision_id"] != DECISION_ID:
  reject("decision artifact version or decision_id is invalid")
 target = exact_object(artifact["target"], {"wiki", "filename"}, "target")
 if target != {"wiki": TARGET_WIKI, "filename": TARGET_FILENAME}:
  reject("decision target is invalid")
 basis_hash, existing_domains, pending_count = registry_snapshot(raw_pages)
 remaining = max(0, task["inventory_limit"] - pending_count)
 actual_target = min(
  task["full_candidate_target"],
  task["candidate_add_limit"],
  remaining,
 )
 raw_candidates = artifact["eligible_candidates"]
 if not isinstance(raw_candidates, list) or len(raw_candidates) > MAX_CANDIDATES:
  reject("eligible_candidates must be a bounded list")
 web_index = build_web_index(raw_web)
 artifact_cache: dict[str, str] = {}
 candidates = []
 mechanical_errors: list[str] = []
 binding_failures: list[dict] = []
 for index, item in enumerate(raw_candidates):
  try:
   candidates.append(
    validate_candidate(
     item,
     index,
     web_index=web_index,
     artifact_cache=artifact_cache,
    )
   )
  except DecisionRejected as exc:
   mechanical_errors.append(str(exc))
   binding_failures.extend(exc.binding_failures)
 if mechanical_errors:
  try:
   hints = source_binding_repair_hints(binding_failures, web_index, artifact_cache)
  except Exception:
   hints = []
  raise DecisionRejected(
   "mechanical validation errors: " + "; ".join(mechanical_errors),
   binding_failures=binding_failures, repair_hints=hints,
  )
 candidate_domains = [item["canonical_domain"] for item in candidates]
 if len(set(candidate_domains)) != len(candidate_domains):
  reject("eligible_candidates contains duplicate canonical_domain values")
 if set(candidate_domains).intersection(existing_domains):
  reject("eligible_candidates contains a registry-history duplicate")
 if actual_target == 0:
  if candidates or artifact["mutation_plan"] is not None:
   reject("trusted actual_target is zero, so the decision must be zero-write")
  mutation_plan_digest = None
  mutation_plan = None
  eligible_count = 0
 else:
  if len(candidates) < actual_target:
   raise DecisionRejected(
    "candidate batch incomplete: "
    f"actual_count={len(candidates)} required_count={actual_target} "
    f"decision_artifact_path={DECISION_PATH}",
    repair_hints=incomplete_candidate_repair_hints(
     len(candidates), actual_target
    ),
   )
  if len(candidates) > actual_target:
   reject("eligible_candidates exceeds the trusted actual target")
  plan, normalized_artifact_plan = canonical_plan(
   artifact["mutation_plan"], basis_hash, candidates
  )
  mutation_plan_digest = "sha256:" + sha256_text(canonical_json(plan))
  mutation_plan = normalized_artifact_plan
  eligible_count = len(candidates)
 input_payload = {
  "schema_version": SCHEMA_VERSION,
  "decision_id": DECISION_ID,
  "target": target,
  "task": task,
  "current_pending_count": pending_count,
  "actual_target": actual_target,
  "basis_content_hash": basis_hash,
  "eligible_candidates": candidates,
 }
 result = {
  "eligible_count": eligible_count,
  "input_digest": "sha256:" + sha256_text(canonical_json(input_payload)),
  "basis_content_hash": basis_hash,
  "mutation_plan_digest": mutation_plan_digest,
 }
 if mutation_plan is not None:
  result["mutation_plan"] = mutation_plan
  result["review_input"] = input_payload
 return result
def main() -> int:
 try:
  if len(sys.argv) != 2 or sys.argv[1] != DECISION_PATH:
   reject("expected the fixed decision artifact path")
  context = globals().get("HENGHAI_SKILL_HOOK_CONTEXT")
  result = decide(Path(DECISION_PATH), context)
 except (DecisionRejected, OSError, UnicodeError, json.JSONDecodeError) as exc:
  print(f"conditional decision rejected: {exc}", file=sys.stderr)
  if isinstance(exc, DecisionRejected) and exc.repair_hints:
   print("conditional decision repair hints (not approval): " + canonical_json(exc.repair_hints), file=sys.stderr)
  return 2
 print(canonical_json(result))
 return 0
if __name__ == "__main__":
 raise SystemExit(main())
