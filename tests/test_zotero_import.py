import json
import unittest
from urllib.parse import parse_qs

import httpx

from wos_zotero_kit import zotero_import as zi


class FakeZotero:
    """最小 Zotero Web API：集合、条目、搜索、format=keys。"""

    def __init__(self, items=None, drop_patches=False):
        self.items = {i["key"]: i for i in (items or [])}
        self.collections = {}
        self.drop_patches = drop_patches  # 模拟"PATCH 回 204 但没生效"
        self._n = 0

    def _key(self):
        self._n += 1
        return f"K{self._n:07d}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/users/1/", 1)[1]
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if request.method == "GET" and path == "collections":
            return httpx.Response(200, json=[{"key": k, "data": {"name": n}} for k, n in self.collections.items()])
        if request.method == "POST" and path == "collections":
            key = self._key()
            self.collections[key] = json.loads(request.content)[0]["name"]
            return httpx.Response(200, json={"successful": {"0": {"key": key}}})
        if request.method == "GET" and path == "items/top":
            words = q["q"].lower()
            hits = [
                {"key": k, "data": i["data"]}
                for k, i in self.items.items()
                if words in (i["data"].get("DOI") or "").lower() or words in i["data"]["title"].lower()
            ]
            return httpx.Response(200, json=hits)
        if request.method == "GET" and path.startswith("collections/") and path.endswith("/items/top"):
            ckey = path.split("/")[1]
            keys = [k for k, i in self.items.items() if ckey in i["data"].get("collections", [])]
            return httpx.Response(200, text="\n".join(keys))
        if request.method == "POST" and path == "items":
            ok = {}
            for idx, data in enumerate(json.loads(request.content)):
                key = self._key()
                self.items[key] = {"key": key, "data": {**data, "version": 1}}
                ok[str(idx)] = {"key": key}
            return httpx.Response(200, json={"successful": ok, "failed": {}})
        if path.startswith("items/"):
            key = path.split("/")[1]
            if request.method == "GET":
                return httpx.Response(200, json=self.items[key])
            if request.method == "PATCH":
                if not self.drop_patches:
                    self.items[key]["data"]["collections"] = json.loads(request.content)["collections"]
                return httpx.Response(204)
        return httpx.Response(404, text=f"unhandled {request.method} {path}")

    def client(self):
        http = httpx.Client(base_url="https://api.zotero.org", transport=httpx.MockTransport(self.handler))
        return zi.ZoteroClient("k", "1", http=http)


EXISTING = {"key": "OLD0001", "data": {"title": "Acu-TENS in COPD", "DOI": "10.1/abc", "date": "2015", "collections": []}}
PAPERS = [
    {"itemType": "journalArticle", "title": "Acu-TENS in COPD", "DOI": "10.1/ABC", "date": "2015", "abstractNote": "x"},
    {"itemType": "journalArticle", "title": "新方法 研究", "date": "2020"},
    {"itemType": "journalArticle", "title": "新方法 研究", "date": "2020"},  # 批内重复
]


class ImportPapersTest(unittest.TestCase):
    def test_reuses_existing_creates_new_and_everything_lands_in_collection(self):
        fake = FakeZotero(items=[json.loads(json.dumps(EXISTING))])
        summary = zi.import_papers(fake.client(), PAPERS, "COPD")
        self.assertEqual((summary.deduped, summary.reused, summary.created, summary.added_existing), (2, 1, 1, 1))
        self.assertEqual(summary.missing_after_import, [])
        self.assertTrue(summary.ok())
        ckey = summary.collection_key
        self.assertIn(ckey, fake.items["OLD0001"]["data"]["collections"])

    def test_silent_patch_failure_is_caught_by_readback(self):
        fake = FakeZotero(items=[json.loads(json.dumps(EXISTING))], drop_patches=True)
        summary = zi.import_papers(fake.client(), PAPERS, "COPD")
        self.assertEqual(summary.missing_after_import, ["OLD0001"])
        self.assertFalse(summary.ok())

    def test_dry_run_writes_nothing(self):
        fake = FakeZotero(items=[json.loads(json.dumps(EXISTING))])
        summary = zi.import_papers(fake.client(), PAPERS, "COPD", dry_run=True)
        self.assertEqual((summary.reused, summary.created), (1, 1))
        self.assertEqual(fake.collections, {})
        self.assertEqual(len(fake.items), 1)


class HelpersTest(unittest.TestCase):
    def test_chinese_titles_do_not_normalize_to_empty(self):
        self.assertEqual(zi.normalize_title("新方法：研究!"), "新方法 研究")

    def test_count_and_content_warnings(self):
        self.assertIn("50", zi.export_count_warning(125, 1000, 50))
        self.assertEqual(zi.export_count_warning(125, 1000, 125), "")
        self.assertEqual(zi.export_count_warning(None, 1000, 3), "")
        self.assertIn("Full Record", zi.export_content_warning([{"title": "a"}]))
        self.assertEqual(zi.export_content_warning([{"abstractNote": "x"}]), "")


if __name__ == "__main__":
    unittest.main()
