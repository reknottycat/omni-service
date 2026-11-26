"""Small, versioned local knowledge base with approved, multilingual answers.

The model selects an answer; it cannot create facts, prices or delivery promises.
JSON is the executable source. A same-stem Markdown file is its readable companion.
No network lookup, embedding service, or customer data is used by this module.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .models import ChannelError


MAX_KNOWLEDGE_BYTES = 1_048_576
MAX_ENTRIES = 200
ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    title: str
    tags: tuple[str, ...]
    facts: str
    answers: dict[str, str]
    source: str
    requires_human: bool = False

    def context(self):
        """Only approved data is exposed to the classifier, never a file path."""
        return {"id": self.id, "title": self.title, "facts": self.facts,
                "answers": dict(self.answers), "requires_human": self.requires_human}


def _bounded_text(value, limit=12_000):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


class KnowledgeBase:
    def __init__(self, path):
        location = Path(path)
        if location.is_dir():
            paths = sorted(location.glob("*.json"))
        elif location.suffix.lower() == ".md" and location.with_suffix(".json").is_file():
            paths = [location.with_suffix(".json")]
        else:
            paths = [location]
        if not paths or len(paths) > 20:
            raise ChannelError("invalid_knowledge", "A local JSON knowledge file is required.")
        entries, digest, synthetic = {}, hashlib.sha256(), False
        total_bytes = 0
        try:
            for file in paths:
                if file.suffix.lower() != ".json":
                    raise ValueError("knowledge format")
                with file.open("rb") as stream:
                    raw = stream.read(MAX_KNOWLEDGE_BYTES + 1)
                total_bytes += len(raw)
                if total_bytes > MAX_KNOWLEDGE_BYTES:
                    raise ValueError("knowledge too large")
                data = json.loads(raw)
                if (not isinstance(data, dict) or data.get("schema") != 1
                        or type(data.get("synthetic")) is not bool
                        or not isinstance(data.get("entries"), list)):
                    raise ValueError("knowledge schema")
                synthetic |= data["synthetic"]
                digest.update(file.name.encode())
                digest.update(b"\0")
                digest.update(raw)
                for row in data["entries"]:
                    if not isinstance(row, dict):
                        raise ValueError("knowledge entry")
                    identifier = row.get("id")
                    tags, answers = row.get("tags"), row.get("answers")
                    if (not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier)
                            or identifier in entries or not _bounded_text(row.get("title"), 200)
                            or not _bounded_text(row.get("facts"))
                            or not isinstance(tags, list) or not tags or len(tags) > 100
                            or any(not _bounded_text(tag, 100) for tag in tags)
                            or not isinstance(answers, dict) or not answers
                            or any(not re.fullmatch(r"[a-z]{2,3}", key)
                                   or not _bounded_text(answer, 4000)
                                   for key, answer in answers.items())
                            or type(row.get("requires_human", False)) is not bool):
                        raise ValueError("knowledge entry")
                    entries[identifier] = KnowledgeEntry(
                        identifier, row["title"].strip(), tuple(tag.casefold() for tag in tags),
                        row["facts"].strip(), {key: value.strip() for key, value in answers.items()},
                        file.name, row.get("requires_human", False))
                    if len(entries) > MAX_ENTRIES:
                        raise ValueError("too many entries")
        except (OSError, ValueError, TypeError, UnicodeError):
            raise ChannelError("invalid_knowledge", "The local knowledge file is invalid.") from None
        if not entries:
            raise ChannelError("invalid_knowledge", "The local knowledge file has no entries.")
        self.entries = entries
        self.revision = digest.hexdigest()
        self.synthetic = synthetic

    def retrieve(self, text, limit=3):
        """Deterministic lexical retrieval; unrelated questions return no records.

        Tags intentionally include Chinese substrings and Russian word stems.
        Whole Latin words prevent, for example, 'hi' matching 'shipping'.
        Retrieval is a candidate filter, not evidence of translation/model quality.
        """
        if not isinstance(text, str) or not text.strip():
            return []
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10")
        query = text.casefold()
        scored = []
        for entry in self.entries.values():
            score = 0
            for tag in entry.tags:
                if re.fullmatch(r"[a-z ]+", tag):
                    found = re.search(r"(?<!\w)" + re.escape(tag) + r"(?!\w)", query)
                else:
                    found = tag in query
                if found:
                    score += 1 + min(len(tag), 15) / 100
            if score:
                scored.append((score, entry.id, entry))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[:limit]]

    def get(self, identifier):
        return self.entries.get(identifier)
