"""Owner-private people and relationship bindings.

The language model should understand stable human references such as
"my spouse" without seeing or inventing provider identifiers.  Real names,
relationships, and channel IDs live in gitignored ``data/person_registry.json``.
This module validates that private registry, exposes an ID-free prompt
projection, and resolves exact aliases for action executors.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


class PersonRegistryError(RuntimeError):
    """The private people registry cannot support a safe resolution."""


class PersonNotFound(PersonRegistryError):
    """No person has the requested exact name or relationship alias."""


class PersonAmbiguous(PersonRegistryError):
    """More than one person claims the requested alias."""


class PersonRegistryInvalid(PersonRegistryError):
    """The private registry violates its schema or identity invariants."""


_PERSON_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_LARK_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]{8,128}$")
_LARK_CHAT_ID_RE = re.compile(r"^oc_[A-Za-z0-9_-]{8,128}$")
_EIGENFLUX_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_NORMALIZED_PROVIDER_TOKEN_RE = re.compile(
    r"(?:(?:ou|oc)[a-z0-9]{8,128}|\d{15,24})"
)
_CHANNEL_KEYS = {
    "lark": {"open_id", "chat_id", "name", "verified_at"},
    "eigenflux": {"agent_id", "agent_name", "verified_at"},
}
_CHANNEL_ID_KEYS = {"lark": "open_id", "eigenflux": "agent_id"}
_TOP_LEVEL_KEYS = {"version", "people"}
_PERSON_KEYS = {
    "person_id",
    "name",
    "aliases",
    "relationships",
    "channels",
    "boundaries",
}


def normalize_person_label(value: str) -> str:
    """Normalize only presentation differences; matching remains exact."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s·•._'\-]+", "", value).strip()


def _text(value: Any, field: str, *, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise PersonRegistryInvalid(f"人物登记册缺少 {field}")
    if len(result) > 300 or "\n" in result or "\r" in result:
        raise PersonRegistryInvalid(f"人物登记册字段 {field} 无效")
    return result


def _text_list(value: Any, field: str, *, limit: int = 30) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > limit:
        raise PersonRegistryInvalid(f"人物登记册字段 {field} 必须是有限列表")
    result: list[str] = []
    for item in value:
        text = _text(item, field, required=True)
        if text not in result:
            result.append(text)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Person:
    person_id: str
    name: str
    aliases: tuple[str, ...]
    relationships: tuple[str, ...]
    channels: dict[str, dict[str, str]]
    boundaries: tuple[str, ...]

    @property
    def labels(self) -> tuple[str, ...]:
        return (self.name, *self.aliases, *self.relationships)


class PersonRegistry:
    """Validated, exact-match resolver for owner-private people facts."""

    def __init__(
        self,
        root: str | Path | None = None,
        path: str | Path | None = None,
        today: date | None = None,
    ):
        self.root = Path(root or Path(__file__).resolve().parent.parent)
        self.path = Path(path or self.root / "data" / "person_registry.json")
        self.today = today or date.today()
        self._people: tuple[Person, ...] | None = None
        self._labels: dict[str, Person] | None = None

    @property
    def configured(self) -> bool:
        return self.path.is_file()

    def _load(self) -> tuple[Person, ...]:
        if self._people is not None:
            return self._people
        if not self.path.is_file():
            self._people = ()
            self._labels = {}
            return self._people
        if os.name != "nt":
            try:
                mode = stat.S_IMODE(self.path.stat().st_mode)
            except OSError as exc:
                raise PersonRegistryInvalid("人物登记册权限无法验证") from exc
            if mode & 0o077:
                raise PersonRegistryInvalid(
                    "人物登记册权限过宽；请执行 chmod 600 data/person_registry.json"
                )
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise PersonRegistryInvalid("人物登记册无法读取或不是有效 JSON") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise PersonRegistryInvalid("人物登记册版本无效")
        if set(payload) - _TOP_LEVEL_KEYS:
            raise PersonRegistryInvalid("人物登记册包含不支持的顶层字段")
        rows = payload.get("people")
        if not isinstance(rows, list) or len(rows) > 200:
            raise PersonRegistryInvalid("人物登记册 people 必须是有限列表")

        people: list[Person] = []
        labels: dict[str, Person] = {}
        ids: set[str] = set()
        provider_ids: dict[tuple[str, str], str] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise PersonRegistryInvalid("人物登记册人物条目必须是对象")
            if set(row) - _PERSON_KEYS:
                raise PersonRegistryInvalid("人物登记册人物条目包含不支持的字段")
            person_id = _text(row.get("person_id"), "person_id", required=True)
            if not _PERSON_ID_RE.fullmatch(person_id) or person_id in ids:
                raise PersonRegistryInvalid("人物登记册 person_id 无效或重复")
            ids.add(person_id)
            name = _text(row.get("name"), "name", required=True)
            aliases = _text_list(row.get("aliases"), f"people[{index}].aliases")
            relationships = _text_list(
                row.get("relationships"), f"people[{index}].relationships"
            )
            boundaries = _text_list(
                row.get("boundaries"), f"people[{index}].boundaries", limit=20
            )
            raw_channels = row.get("channels") or {}
            if not isinstance(raw_channels, dict):
                raise PersonRegistryInvalid("人物登记册 channels 必须是对象")
            channels: dict[str, dict[str, str]] = {}
            for channel, raw_binding in raw_channels.items():
                if channel not in _CHANNEL_KEYS or not isinstance(raw_binding, dict):
                    raise PersonRegistryInvalid("人物登记册包含不支持的渠道")
                if set(raw_binding) - _CHANNEL_KEYS[channel]:
                    raise PersonRegistryInvalid(f"人物登记册 {channel} 渠道字段无效")
                binding: dict[str, str] = {}
                for key, value in raw_binding.items():
                    cleaned = _text(value, f"{channel}.{key}")
                    if cleaned:
                        binding[key] = cleaned
                required_id = _CHANNEL_ID_KEYS[channel]
                if not binding.get(required_id):
                    raise PersonRegistryInvalid(
                        f"人物登记册 {channel} 渠道缺少 {required_id}"
                    )
                try:
                    verified_at = date.fromisoformat(binding.get("verified_at", ""))
                except ValueError as exc:
                    raise PersonRegistryInvalid(
                        f"人物登记册 {channel} 渠道缺少有效 verified_at"
                    ) from exc
                if verified_at > self.today:
                    raise PersonRegistryInvalid(
                        f"人物登记册 {channel} verified_at 不能晚于今天"
                    )
                if channel == "lark":
                    if binding.get("open_id") and not _LARK_OPEN_ID_RE.fullmatch(
                        binding["open_id"]
                    ):
                        raise PersonRegistryInvalid("人物登记册 Lark open_id 无效")
                    if binding.get("chat_id") and not _LARK_CHAT_ID_RE.fullmatch(
                        binding["chat_id"]
                    ):
                        raise PersonRegistryInvalid("人物登记册 Lark chat_id 无效")
                if channel == "eigenflux" and binding.get("agent_id"):
                    if not _EIGENFLUX_ID_RE.fullmatch(binding["agent_id"]):
                        raise PersonRegistryInvalid("人物登记册 EigenFlux agent_id 无效")
                for id_key in ("open_id", "chat_id", "agent_id"):
                    provider_id = binding.get(id_key)
                    if not provider_id:
                        continue
                    provider_key = (id_key, provider_id)
                    previous_owner = provider_ids.get(provider_key)
                    if previous_owner is not None and previous_owner != person_id:
                        raise PersonRegistryInvalid(
                            f"人物登记册 {channel} 身份被多个人物重复绑定"
                        )
                    provider_ids[provider_key] = person_id
                channels[channel] = binding
            person = Person(
                person_id=person_id,
                name=name,
                aliases=aliases,
                relationships=relationships,
                channels=channels,
                boundaries=boundaries,
            )
            for label in person.labels:
                normalized = normalize_person_label(label)
                if not normalized:
                    raise PersonRegistryInvalid("人物登记册包含空称谓")
                previous = labels.get(normalized)
                if previous is not None and previous.person_id != person_id:
                    raise PersonAmbiguous(f"人物称谓“{label}”绑定了多人")
                labels[normalized] = person
            people.append(person)
        private_ids = [
            normalize_person_label(provider_id)
            for _key, provider_id in provider_ids
        ]
        for person in people:
            prompt_values = (
                person.name,
                *person.aliases,
                *person.relationships,
                *person.boundaries,
            )
            for value in prompt_values:
                normalized = normalize_person_label(value)
                if _NORMALIZED_PROVIDER_TOKEN_RE.search(normalized) or any(
                    provider_id in normalized for provider_id in private_ids
                ):
                    raise PersonRegistryInvalid(
                        "人物登记册可展示字段包含私密渠道身份"
                    )
        self._people = tuple(people)
        self._labels = labels
        return self._people

    def people(self) -> tuple[Person, ...]:
        return self._load()

    def resolve(self, query: str) -> Person:
        wanted = normalize_person_label(query)
        if not wanted:
            raise PersonNotFound("人物称谓为空")
        self._load()
        person = (self._labels or {}).get(wanted)
        if person is None:
            raise PersonNotFound(f"人物登记册中没有“{str(query).strip()}”")
        return person

    def resolve_channel(self, query: str, channel: str) -> tuple[Person, dict[str, str]]:
        person = self.resolve(query)
        binding = person.channels.get(channel, {})
        if not binding:
            raise PersonNotFound(f"“{str(query).strip()}”没有已验证的 {channel} 身份")
        return person, dict(binding)

    def prompt_context(self) -> str:
        """Render owner-only facts without exposing provider identifiers."""
        people = self.people()
        if not people:
            return ""
        lines = [
            "## Known People (owner-private, verified)",
            "关系称谓是确定性绑定：命中下面称谓时不要再问‘是谁’。",
            "支持人物参数的动作只传姓名/关系称谓；底层会使用私有 ID 并再次校验。",
        ]
        for person in people:
            labels = list(dict.fromkeys((*person.relationships, *person.aliases)))[:10]
            channels = [name for name, binding in person.channels.items() if binding]
            detail = [
                f"- {person.name}",
                f"称谓：{'、'.join(labels)}" if labels else "",
                f"可用渠道：{'、'.join(channels)}" if channels else "",
                f"边界：{'；'.join(person.boundaries)}" if person.boundaries else "",
            ]
            lines.append("；".join(part for part in detail if part))
        return "\n".join(lines)


def owner_people_prompt_context(root: str | Path) -> str:
    """Best-effort prompt projection; action resolution remains fail-closed."""
    try:
        return PersonRegistry(root=root).prompt_context()
    except PersonRegistryError:
        return ""
