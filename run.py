"""
WoS 检索 → RIS 导出 → Zotero 集合，监督式命令行入口。

    python run.py --query 'TS=(...)' --collection 我的集合

遇到登录页或 captcha 时脚本会停下等你在浏览器里处理完、回终端按回车。
退出码：0 全部成功；3 成功但有警告（导出条数不足 / 无摘要）；1 失败。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from wos_zotero_kit import ris_parser, wos_browser, zotero_import


def _pause(message: str) -> None:
    print(f"\n>>> {message}")
    input(">>> 处理完后按回车继续（Ctrl+C 放弃）...")


async def export_ris(args: argparse.Namespace) -> wos_browser.WosExportResult:
    download_dir = str(Path(args.download_dir).resolve())
    profile_dir = str(Path(args.profile_dir).resolve())

    opened = await wos_browser.open_wos_for_manual_handoff(download_dir, profile_dir=profile_dir)
    print(f"[1/4] 浏览器已打开：{opened.status}  {opened.url}")
    if opened.verification_state is not None:
        _pause(f"WoS 需要人工处理（{opened.verification_state}）：请在浏览器里登录或过验证，直到看到 Advanced Search。")

    searched = await wos_browser.search_only_on_active_session(args.query)
    print(f"[2/4] 检索：{searched.status}，命中 {searched.result_count}")
    if searched.status != "ready_to_export" or not wos_browser._is_result_page(searched.url):
        _pause("检索没有直接到结果页（多半是 captcha）：请在浏览器里过验证，必要时手动点 Search，看到结果列表后回来。")

    exported = await wos_browser.resume_wos_search_and_export(
        query=args.query,
        download_dir=download_dir,
        max_records=args.max_records,
        profile_dir=profile_dir,
        skip_search=True,
        close_session=True,
    )
    print(f"[3/4] 导出：{exported.exported_files}")
    return exported


def main() -> int:
    parser = argparse.ArgumentParser(description="WoS → RIS → Zotero（监督式）")
    parser.add_argument("--query", required=True, help="完整 WoS 高级检索式，如 TS=(...)")
    parser.add_argument("--collection", default="", help="Zotero 集合名（顶层；不存在则创建）")
    parser.add_argument("--max-records", type=int, default=1000, help="最多导出条数，WoS 单次上限 1000")
    parser.add_argument("--download-dir", default="wos-downloads")
    parser.add_argument("--profile-dir", default=".wos-profile", help="浏览器配置目录，登录状态存在这里")
    parser.add_argument("--dry-run", action="store_true", help="只查重不写 Zotero")
    parser.add_argument("--no-import", action="store_true", help="只导出 RIS，不入库")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(name)s: %(message)s", stream=sys.stderr)

    if not args.no_import and not args.collection:
        parser.error("入库需要 --collection；只导出请加 --no-import")
    api_key, user_id = os.environ.get("ZOTERO_API_KEY", ""), os.environ.get("ZOTERO_USER_ID", "")
    if not args.no_import and not (api_key and user_id):
        parser.error("入库需要环境变量 ZOTERO_API_KEY 和 ZOTERO_USER_ID")

    try:
        exported = asyncio.run(export_ris(args))
    except KeyboardInterrupt:
        print("已放弃。")
        return 1
    except wos_browser.WosAutomationError as exc:
        print(f"\n导出失败：{exc}")
        return 1

    papers: list[dict] = []
    for path in exported.exported_files:
        papers.extend(ris_parser.parse_ris_file(path))
    warnings = list(exported.warnings)
    for warning in (
        zotero_import.export_count_warning(exported.result_count, args.max_records, len(papers)),
        zotero_import.export_content_warning(papers),
    ):
        if warning:
            warnings.append(warning)
    print(f"      解析 {len(papers)} 条，其中 {sum(1 for p in papers if p.get('abstractNote'))} 条带摘要")

    if args.no_import:
        for warning in warnings:
            print(f"  ! {warning}")
        return 3 if warnings else 0

    client = zotero_import.ZoteroClient(api_key, user_id)
    summary = zotero_import.import_papers(
        client,
        papers,
        args.collection,
        dry_run=args.dry_run,
        extra_warnings=warnings,
        manifest_dir=Path(args.download_dir) / "manifests",
    )
    verb = "预计" if args.dry_run else "实际"
    print(
        f"[4/4] Zotero 集合「{summary.collection_name}」：{verb}新建 {summary.created}，"
        f"复用已有 {summary.reused}（本次新加入集合 {summary.added_existing}）"
    )
    for warning in summary.warnings:
        print(f"  ! {warning}")
    if not summary.ok():
        return 1
    return 3 if summary.warnings else 0


if __name__ == "__main__":
    sys.exit(main())
