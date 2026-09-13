"""Operator-declared API identity. Address similarity never authorizes a merge."""

from typing import Literal

from pydantic import Field

from .schema import Configuration, Engine, Record


class MergeEngines(Record):
    revision: int = Field(ge=0)
    source_id: str
    target_id: str
    preferred_url: str
    credential_source: Literal["target", "source", "none"]


def merged_configuration(config: Configuration, merge: MergeEngines) -> Configuration:
    if merge.source_id == merge.target_id:
        raise ValueError("Choose two different engines")
    engines = {engine.id: engine for engine in config.engines}
    if merge.source_id not in engines or merge.target_id not in engines:
        raise ValueError("Both engines must still exist")
    source, target = engines[merge.source_id], engines[merge.target_id]
    urls = list(dict.fromkeys([*target.endpoint_urls, *source.endpoint_urls]))
    preferred = Engine.validate_url(merge.preferred_url)
    if preferred not in urls:
        raise ValueError("Choose a preferred URL from the engines being merged")
    result = config.model_dump()
    result["revision"] = merge.revision
    result["engines"] = [
        engine for engine in result["engines"] if engine["id"] != source.id
    ]
    kept = next(engine for engine in result["engines"] if engine["id"] == target.id)
    kept.update(base_url=preferred, aliases=[url for url in urls if url != preferred])
    # The selected endpoint owns its completion operation contract. Merging
    # aliases must not broaden a chat-only or legacy-only API into the other
    # engine's unproven operation set.
    preferred_engine = (
        source if preferred in source.endpoint_urls else target
    )
    kept["completion_paths"] = list(preferred_engine.completion_paths)

    def relink(ids):
        return list(
            dict.fromkeys(target.id if value == source.id else value for value in ids)
        )

    for route in result["routes"]:
        for selector in [route["primary"], route["fallback"]]:
            if selector is not None:
                selector["engine_ids"] = relink(selector["engine_ids"])
    for client in result["clients"]:
        client["engine_ids"] = relink(client["engine_ids"])
    return Configuration.model_validate(result)
