"""小型业务术语表（通用概念，非行业专属；可替换或扩充）。"""

import json
from importlib import resources
from typing import Any


class Glossary:
    """term → {"category": ..., "entities": [...]} 的查表。"""

    def __init__(self, terms: dict[str, dict[str, Any]], version: str = "glossary-v1") -> None:
        self._terms = {key.lower(): value for key, value in terms.items()}
        self.version = version

    @classmethod
    def load_default(cls) -> "Glossary":
        raw = json.loads(resources.files("app.semantics").joinpath("glossary.json").read_text("utf-8"))
        return cls(raw.get("terms", {}), version=raw.get("version", "glossary-v1"))

    @classmethod
    def load(cls, path: str) -> "Glossary":
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        return cls(raw.get("terms", {}), version=raw.get("version", "glossary-v1"))

    def lookup(self, token: str) -> dict[str, Any] | None:
        return self._terms.get(token.lower())

    def hit(self, tokens: list[str]) -> dict[str, Any] | None:
        """先整词命中；中文长词（如“订单金额”）再做最长子词匹配。"""
        for token in tokens:
            found = self.lookup(token)
            if found:
                return {"term": token, **found}
        best: tuple[int, str, dict[str, Any]] | None = None
        for token in tokens:
            for term, entry in self._terms.items():
                if term and term in token and len(token) > len(term) and (
                    best is None or len(term) > best[0]
                ):
                    best = (len(term), term, entry)
        if best:
            return {"term": best[1], **best[2]}
        return None
