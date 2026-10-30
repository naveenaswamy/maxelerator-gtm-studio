"""Storage for startup profiles, playbook sections and finished playbooks.

Layout (same keys locally and on S3):
    startups/<startup_id>.json
    playbooks/<startup_id>/<playbook_id>/meta.json
    playbooks/<startup_id>/<playbook_id>/sections/<section_id>.md
    playbooks/<startup_id>/<playbook_id>/playbook.md
"""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from .models import PlaybookMeta, StartupProfile


class NotFound(KeyError):
    pass


class BaseStore(ABC):
    _lock = threading.RLock()

    # -- primitives each backend implements
    @abstractmethod
    def _read(self, key: str) -> str | None: ...

    @abstractmethod
    def _write(self, key: str, body: str) -> None: ...

    @abstractmethod
    def _list(self, prefix: str) -> list[str]: ...

    # -- startups
    def put_startup(self, profile: StartupProfile) -> None:
        self._write(f"startups/{profile.startup_id}.json", profile.model_dump_json(indent=2))

    def get_startup(self, startup_id: str) -> StartupProfile:
        raw = self._read(f"startups/{startup_id}.json")
        if raw is None:
            raise NotFound(f"startup '{startup_id}' is not onboarded")
        return StartupProfile.model_validate_json(raw)

    def list_startups(self) -> list[StartupProfile]:
        keys = [k for k in self._list("startups/") if k.endswith(".json")]
        return [StartupProfile.model_validate_json(self._read(k)) for k in sorted(keys)]

    # -- playbooks
    def _pb(self, startup_id: str, playbook_id: str) -> str:
        return f"playbooks/{startup_id}/{playbook_id}"

    def put_meta(self, meta: PlaybookMeta) -> None:
        self._write(f"{self._pb(meta.startup_id, meta.playbook_id)}/meta.json", meta.model_dump_json(indent=2))

    def get_meta(self, startup_id: str, playbook_id: str) -> PlaybookMeta:
        raw = self._read(f"{self._pb(startup_id, playbook_id)}/meta.json")
        if raw is None:
            raise NotFound(f"playbook '{playbook_id}' not found for '{startup_id}'")
        return PlaybookMeta.model_validate_json(raw)

    def list_playbooks(self, startup_id: str) -> list[PlaybookMeta]:
        keys = [k for k in self._list(f"playbooks/{startup_id}/") if k.endswith("/meta.json")]
        metas = [PlaybookMeta.model_validate_json(self._read(k)) for k in keys]
        return sorted(metas, key=lambda m: m.created_at)

    def latest_playbook(self, startup_id: str) -> PlaybookMeta:
        metas = self.list_playbooks(startup_id)
        if not metas:
            raise NotFound(f"no playbooks yet for '{startup_id}'")
        return metas[-1]

    def put_section(self, startup_id: str, playbook_id: str, section_id: str, markdown: str) -> None:
        self._write(f"{self._pb(startup_id, playbook_id)}/sections/{section_id}.md", markdown)

    def get_section(self, startup_id: str, playbook_id: str, section_id: str) -> str | None:
        return self._read(f"{self._pb(startup_id, playbook_id)}/sections/{section_id}.md")

    def get_sections(self, startup_id: str, playbook_id: str) -> dict[str, str]:
        prefix = f"{self._pb(startup_id, playbook_id)}/sections/"
        out = {}
        for key in self._list(prefix):
            if key.endswith(".md"):
                out[key[len(prefix) : -3]] = self._read(key)
        return out

    def put_playbook_markdown(self, startup_id: str, playbook_id: str, markdown: str) -> str:
        key = f"{self._pb(startup_id, playbook_id)}/playbook.md"
        self._write(key, markdown)
        return key

    def get_playbook_markdown(self, startup_id: str, playbook_id: str) -> str | None:
        return self._read(f"{self._pb(startup_id, playbook_id)}/playbook.md")

    # -- pricing-model assignments: pricing/<startup_id>/<created_at>.json
    def put_pricing(self, result) -> str:
        key = f"pricing/{result.startup_id}/{result.created_at.replace(':', '')}.json"
        self._write(key, result.model_dump_json(indent=2))
        return key

    def list_pricing(self, startup_id: str | None = None) -> list:
        from .pricing import PricingResult

        prefix = f"pricing/{startup_id}/" if startup_id else "pricing/"
        keys = sorted(k for k in self._list(prefix) if k.endswith(".json"))
        return [(k, PricingResult.model_validate_json(self._read(k))) for k in keys]

    def latest_pricing(self, startup_id: str):
        items = self.list_pricing(startup_id)
        if not items:
            raise NotFound(f"no pricing model assigned yet for '{startup_id}'")
        return items[-1]

    def put_pricing_at(self, key: str, result) -> None:
        self._write(key, result.model_dump_json(indent=2))

    def locate(self, key: str) -> str:
        """Human-readable location of a key (file path or s3:// URI)."""
        return key


class LocalStore(BaseStore):
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"key escapes the data directory: {key}")
        return path

    def _read(self, key: str) -> str | None:
        p = self._path(key)
        return p.read_text() if p.exists() else None

    def _write(self, key: str, body: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(body)
        tmp.replace(p)

    def _list(self, prefix: str) -> list[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return [str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file() and not p.name.endswith(".tmp")]

    def locate(self, key: str) -> str:
        return str(self._path(key))


class S3Store(BaseStore):
    """S3 backend for deployments on AWS. Requires `pip install .[aws]`."""

    def __init__(self, bucket: str, prefix: str = ""):
        import boto3  # optional dependency

        self.s3 = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""

    def _read(self, key: str) -> str | None:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self.prefix + key)
        except self.s3.exceptions.NoSuchKey:
            return None
        return obj["Body"].read().decode()

    def _write(self, key: str, body: str) -> None:
        ctype = "text/markdown" if key.endswith(".md") else "application/json"
        self.s3.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=body.encode(), ContentType=ctype)

    def _list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix + prefix):
            keys += [o["Key"][len(self.prefix) :] for o in page.get("Contents", [])]
        return keys

    def locate(self, key: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}{key}"


def store_from_settings(settings) -> BaseStore:
    if settings.store == "s3":
        if not settings.s3_bucket:
            raise ValueError("MAXEL_STORE=s3 needs MAXEL_S3_BUCKET")
        return S3Store(settings.s3_bucket)
    return LocalStore(settings.data_dir)


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, default=str)
