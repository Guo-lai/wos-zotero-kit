"""
RIS / BibTeX 文件解析工具
将 WoS 导出的 RIS 文件转换为 Zotero API 可接受的 JSON 格式
"""

import re
from pathlib import Path
from typing import Any


# RIS 字段 → Zotero 字段映射
RIS_TO_ZOTERO = {
    "TI": "title",
    "T1": "title",
    "AU": "author",
    "A1": "author",
    "AB": "abstractNote",
    "PY": "date",
    "Y1": "date",
    "JO": "publicationTitle",
    "JF": "publicationTitle",
    "J2": "journalAbbreviation",
    "VL": "volume",
    "IS": "issue",
    "SP": "pages",
    "EP": "endPage",
    "DO": "DOI",
    "UR": "url",
    "KW": "tags",
    "N2": "abstractNote",
    "SN": "ISSN",
}

# RIS 文献类型 → Zotero itemType
RIS_TYPE_MAP = {
    "JOUR": "journalArticle",
    "JFULL": "journalArticle",
    "ABST": "journalArticle",
    "CONF": "conferencePaper",
    "BOOK": "book",
    "CHAP": "bookSection",
    "THES": "thesis",
    "RPRT": "report",
    "GEN": "document",
}


def parse_ris_file(filepath: str) -> list[dict]:
    """解析 RIS 文件，返回 Zotero JSON 格式的文献列表"""
    content = Path(filepath).read_text(encoding="utf-8", errors="replace")
    return parse_ris_text(content)


def parse_ris_text(text: str) -> list[dict]:
    """解析 RIS 文本内容"""
    records = []
    current: dict[str, Any] = {}
    authors: list[str] = []
    tags: list[str] = []
    item_type = "journalArticle"
    start_page = ""

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        # 记录结束
        if line == "ER  -":
            if current or authors:
                record = _build_zotero_record(current, authors, tags, item_type, start_page)
                if record.get("title"):
                    records.append(record)
            current = {}
            authors = []
            tags = []
            item_type = "journalArticle"
            start_page = ""
            continue

        # 解析字段
        match = re.match(r"^([A-Z][A-Z0-9])\s+-\s+(.*)", line)
        if not match:
            # 续行：追加到上一个字段（仅对摘要有意义）
            if "abstractNote" in current:
                current["abstractNote"] += " " + line
            continue

        tag, value = match.group(1), match.group(2).strip()

        if tag == "TY":
            item_type = RIS_TYPE_MAP.get(value, "journalArticle")
        elif tag in ("AU", "A1", "A2"):
            authors.append(value)
        elif tag == "KW":
            tags.append(value)
        elif tag == "SP":
            start_page = value
        elif tag == "EP" and start_page:
            current["pages"] = f"{start_page}-{value}"
        elif tag in RIS_TO_ZOTERO:
            zotero_field = RIS_TO_ZOTERO[tag]
            if zotero_field not in current:
                current[zotero_field] = value
            elif zotero_field == "abstractNote":
                current[zotero_field] += " " + value

    return records


def _build_zotero_record(
    fields: dict, authors: list[str], tags: list[str], item_type: str, start_page: str
) -> dict:
    """构建 Zotero API JSON 格式"""
    record: dict[str, Any] = {"itemType": item_type}
    record.update(fields)

    # 处理作者
    creators = []
    for author in authors:
        parts = author.split(",", 1)
        if len(parts) == 2:
            creators.append({
                "creatorType": "author",
                "lastName": parts[0].strip(),
                "firstName": parts[1].strip(),
            })
        else:
            creators.append({
                "creatorType": "author",
                "name": author.strip(),
            })
    if creators:
        record["creators"] = creators

    # 处理标签
    if tags:
        record["tags"] = [{"tag": t} for t in tags]

    # 处理页码（只有起始页的情况）
    if start_page and "pages" not in record:
        record["pages"] = start_page

    # 清理年份，只保留4位数字
    if "date" in record:
        year_match = re.search(r"\d{4}", record["date"])
        if year_match:
            record["date"] = year_match.group()

    return record


def parse_bibtex_file(filepath: str) -> list[dict]:
    """简单解析 BibTeX 文件（基础支持）"""
    content = Path(filepath).read_text(encoding="utf-8", errors="replace")
    records = []
    entries = re.findall(r"@\w+\{([^,]+),(.*?)\n\}", content, re.DOTALL)
    for citekey, body in entries:
        fields = {}
        for field_match in re.finditer(r"(\w+)\s*=\s*\{([^}]*)\}", body):
            fields[field_match.group(1).lower()] = field_match.group(2).strip()
        if "title" not in fields:
            continue
        record = {
            "itemType": "journalArticle",
            "title": fields.get("title", ""),
            "abstractNote": fields.get("abstract", ""),
            "date": fields.get("year", ""),
            "publicationTitle": fields.get("journal", ""),
            "volume": fields.get("volume", ""),
            "issue": fields.get("number", ""),
            "pages": fields.get("pages", ""),
            "DOI": fields.get("doi", ""),
        }
        # 处理作者
        if "author" in fields:
            authors = [a.strip() for a in fields["author"].split(" and ")]
            creators = []
            for author in authors:
                parts = author.split(",", 1)
                if len(parts) == 2:
                    creators.append({
                        "creatorType": "author",
                        "lastName": parts[0].strip(),
                        "firstName": parts[1].strip(),
                    })
                else:
                    creators.append({"creatorType": "author", "name": author})
            record["creators"] = creators
        records.append(record)
    return records
