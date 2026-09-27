"""
RIS 记录 → Zotero 集合（只走 Zotero Web API）。

流程：批内去重 → 在整个库里按 DOI / 标题+年份查已有条目 → 新条目创建时直接挂进集合，
已有条目 PATCH 加集合 → 回读集合核对每一条都在。

最后那步回读是必须的：ResearchFlow 曾出现过 manifest 写着成功、集合里却只有 40/125 条的情况。
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

API_BASE = "https://api.zotero.org"
TIMEOUT = 120
BATCH = 50  # Zotero API 单次写入上限


def normalize_doi(doi: str | None) -> str:
    value = (doi or "").strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return re.sub(r"^doi:\s*", "", value)


def normalize_title(title: str | None) -> str:
    # 保留各语种的字母数字；只做 ASCII 折叠的话中文标题会被归一成空串，查重直接失效。
    value = unicodedata.normalize("NFKC", title or "").lower()
    value = re.sub(r"[\W_]+", " ", value)
    return " ".join(value.split())


def extract_year(value: str | None) -> str:
    match = re.search(r"\b(\d{4})\b", value or "")
    return match.group(1) if match else ""


def dedupe_papers(papers: list[dict]) -> tuple[list[dict], int]:
    unique: list[dict] = []
    seen: set[tuple] = set()
    skipped = 0
    for paper in papers:
        doi = normalize_doi(paper.get("DOI"))
        if doi:
            key: tuple = ("doi", doi)
        else:
            title = normalize_title(paper.get("title"))
            if not title:
                skipped += 1
                continue
            key = ("title", title, extract_year(paper.get("date")))
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        unique.append(paper)
    return unique, skipped


def export_count_warning(result_count: int | None, max_records: int, parsed_count: int) -> str:
    """WoS 导出范围没设上时默认只导当前页 50 条，浏览器侧不报错，只能靠对账发现。"""
    if not result_count:
        return ""
    expected = min(result_count, max_records or result_count, 1000)
    if parsed_count >= expected:
        return ""
    return (
        f"RIS 只解析出 {parsed_count} 条，少于应导出的 {expected} 条"
        f"（命中 {result_count}，上限 {max_records}）；导出范围可能没设上。"
    )


def export_content_warning(papers: list[dict]) -> str:
    """整批没有一条摘要，基本可以断定导出内容停在默认的 "Author, Title, Source"。"""
    if not papers or any(p.get("abstractNote") for p in papers):
        return ""
    return f"RIS 共 {len(papers)} 条但没有一条带摘要；导出内容可能没切到 Full Record。"


class ZoteroClient:
    def __init__(self, api_key: str, user_id: str, http: httpx.Client | None = None):
        if not api_key or not user_id:
            raise ValueError("需要 ZOTERO_API_KEY 和 ZOTERO_USER_ID。")
        self.user_id = str(user_id)
        self._http = http or httpx.Client(
            base_url=API_BASE,
            timeout=TIMEOUT,
            headers={"Zotero-API-Key": api_key, "Zotero-API-Version": "3"},
        )

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"/users/{self.user_id}/{path.lstrip('/')}"
        for attempt in range(5):
            resp = self._http.request(method, url, **kwargs)
            # Zotero 限流时回 429 / 503 并给 Retry-After
            if resp.status_code in (429, 503):
                time.sleep(float(resp.headers.get("Retry-After", 2 ** attempt)))
                continue
            if "Backoff" in resp.headers:
                time.sleep(float(resp.headers["Backoff"]))
            return resp
        return resp

    def _get_all(self, path: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        start = 0
        while True:
            resp = self._request("GET", path, params={**(params or {}), "limit": 100, "start": start})
            resp.raise_for_status()
            batch = resp.json()
            items.extend(batch)
            if len(batch) < 100:
                return items
            start += len(batch)

    def find_match(self, paper: dict) -> dict | None:
        doi = normalize_doi(paper.get("DOI"))
        if doi:
            for cand in self.search(doi):
                if normalize_doi(cand["data"].get("DOI")) == doi:
                    return cand
        title = normalize_title(paper.get("title"))
        if not title:
            return None
        year = extract_year(paper.get("date"))
        for cand in self.search(" ".join((paper.get("title") or "").split()[:12])):
            data = cand["data"]
            if normalize_title(data.get("title")) != title:
                continue
            if year and extract_year(data.get("date")) != year:
                continue
            return cand
        return None

    def search(self, query: str) -> list[dict]:
        # items/top 排除子附件和子笔记；只有顶层条目才能加进集合
        resp = self._request(
            "GET", "items/top", params={"q": query, "limit": 30, "itemType": "-attachment"}
        )
        resp.raise_for_status()
        return resp.json()

    def ensure_collection(self, name: str, create: bool = True) -> str:
        for coll in self._get_all("collections"):
            data = coll["data"]
            if data.get("name") == name and not data.get("parentCollection"):
                return coll["key"]
        if not create:
            return ""
        resp = self._request("POST", "collections", json=[{"name": name}])
        resp.raise_for_status()
        created = resp.json().get("successful", {})
        if not created:
            raise RuntimeError(f"创建 Zotero 集合失败: {name}: {resp.text[:300]}")
        return next(iter(created.values()))["key"]

    def collection_item_keys(self, collection_key: str) -> set[str]:
        resp = self._request("GET", f"collections/{collection_key}/items/top", params={"format": "keys"})
        resp.raise_for_status()
        return {line.strip() for line in resp.text.splitlines() if line.strip()}

    def create_items(self, papers: list[dict], collection_key: str) -> tuple[list[str], list[str]]:
        created: list[str] = []
        errors: list[str] = []
        for i in range(0, len(papers), BATCH):
            batch = [{**p, "collections": [collection_key]} for p in papers[i : i + BATCH]]
            resp = self._request("POST", "items", json=batch)
            if resp.status_code not in (200, 201):
                errors.append(f"HTTP {resp.status_code}: {resp.text[:300]}")
                continue
            body = resp.json()
            created.extend(item["key"] for item in body.get("successful", {}).values())
            errors.extend(json.dumps(v, ensure_ascii=False)[:300] for v in body.get("failed", {}).values())
        return created, errors

    def add_to_collection(self, item_key: str, collection_key: str) -> str:
        """返回空串表示成功，否则是错误信息。"""
        resp = self._request("GET", f"items/{item_key}")
        if resp.status_code != 200:
            return f"{item_key}: GET HTTP {resp.status_code}"
        data = resp.json()["data"]
        collections = list(data.get("collections", []))
        if collection_key in collections:
            return ""
        resp = self._request(
            "PATCH",
            f"items/{item_key}",
            json={"collections": collections + [collection_key]},
            headers={"If-Unmodified-Since-Version": str(data.get("version", 0))},
        )
        if resp.status_code not in (200, 204):
            return f"{item_key}: PATCH HTTP {resp.status_code}: {resp.text[:200]}"
        return ""


@dataclass
class ImportSummary:
    collection_name: str
    collection_key: str
    dry_run: bool
    parsed: int
    deduped: int
    created: int = 0
    reused: int = 0
    added_existing: int = 0
    missing_after_import: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.missing_after_import and not any(w.startswith("失败") for w in self.warnings)


def import_papers(
    client: ZoteroClient,
    papers: list[dict],
    collection_name: str,
    dry_run: bool = False,
    extra_warnings: list[str] | None = None,
    manifest_dir: Path | None = None,
) -> ImportSummary:
    unique, _ = dedupe_papers(papers)
    collection_key = client.ensure_collection(collection_name, create=not dry_run)
    summary = ImportSummary(
        collection_name=collection_name,
        collection_key=collection_key,
        dry_run=dry_run,
        parsed=len(papers),
        deduped=len(unique),
        warnings=list(extra_warnings or []),
    )

    reused_keys: list[str] = []
    to_create: list[dict] = []
    for paper in unique:
        match = client.find_match(paper)
        if match is None:
            to_create.append(paper)
        else:
            reused_keys.append(match["key"])
    summary.reused = len(reused_keys)

    if dry_run:
        summary.created = len(to_create)  # 预计新建数
        _write_manifest(summary, manifest_dir)
        return summary

    already = client.collection_item_keys(collection_key)
    for key in sorted(set(reused_keys) - already):
        error = client.add_to_collection(key, collection_key)
        if error:
            summary.warnings.append(f"失败：加入集合 {error}")
        else:
            summary.added_existing += 1

    created_keys, errors = client.create_items(to_create, collection_key)
    summary.created = len(created_keys)
    summary.warnings.extend(f"失败：新建条目 {e}" for e in errors)

    # 回读核对：本批每一条都必须已在集合里
    in_collection = client.collection_item_keys(collection_key)
    summary.missing_after_import = sorted(set(reused_keys + created_keys) - in_collection)
    if summary.missing_after_import:
        summary.warnings.append(
            f"失败：回读集合时有 {len(summary.missing_after_import)} 条不在集合里。"
        )
    _write_manifest(summary, manifest_dir)
    return summary


def _write_manifest(summary: ImportSummary, manifest_dir: Path | None) -> None:
    if manifest_dir is None:
        return
    manifest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = manifest_dir / f"{stamp}-zotero-import.json"
    path.write_text(json.dumps(summary.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
