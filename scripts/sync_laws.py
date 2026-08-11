#!/usr/bin/env python3
"""Mirror currently effective Japanese laws from e-Gov as Markdown."""

from __future__ import annotations

import argparse
import csv
import concurrent.futures
import datetime as dt
import io
import json
import os
import random
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Iterable

DEFAULT_API_BASE = "https://laws.e-gov.go.jp/api/2"
BULK_URL = "https://laws.e-gov.go.jp/bulkdownload?file_section=1&only_xml_flag=true"
UPDATE_URL = "https://laws.e-gov.go.jp/bulkdownload?file_section=3&update_date={date}&only_xml_flag=true"
PAGE_SIZE = 100
USER_AGENT = os.environ.get("EGOV_USER_AGENT", "japanese-laws-git-mirror/2.0")

TYPE_NAMES = {
    "Constitution": "憲法", "Act": "法律", "CabinetOrder": "政令",
    "ImperialOrder": "勅令", "MinisterialOrdinance": "府省令",
    "Rule": "規則", "Misc": "その他",
}

BLOCK_TITLES = {
    "PartTitle": "##", "ChapterTitle": "##", "SectionTitle": "###",
    "SubsectionTitle": "###", "DivisionTitle": "###",
    "ArticleTitle": "###", "SupplProvisionLabel": "##",
    "AppdxTableTitle": "##", "AppdxStyleTitle": "##",
    "AppdxFigTitle": "##", "AppdxFormatTitle": "##", "AppdxNoteTitle": "##",
}
INLINE_NUMBER_TAGS = {
    "ParagraphNum", "ItemTitle", "Subitem1Title", "Subitem2Title",
    "Subitem3Title", "Subitem4Title", "Subitem5Title", "Subitem6Title",
    "Subitem7Title", "Subitem8Title", "Subitem9Title", "Subitem10Title",
}
SKIP_TAGS = {"TOC"}


def request_json(url: str, retries: int = 5) -> dict[str, Any]:
    """GET JSON with bounded exponential backoff."""
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"GET failed after {retries} attempts: {url}") from exc
            time.sleep((2**attempt) + random.random())
    raise AssertionError("unreachable")


def download_file(url: str, target: Path, retries: int = 3) -> None:
    """Download a large file without loading it into memory."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=180) as response, target.open("wb") as out:
                total = int(response.headers.get("Content-Length", 0))
                done = 0
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"一括データ: {done / 1024 / 1024:.0f}/{total / 1024 / 1024:.0f} MiB", file=sys.stderr)
                    elif done % (25 * 1024 * 1024) < len(chunk):
                        print(f"一括データ: {done / 1024 / 1024:.0f} MiB", file=sys.stderr)
            return
        except (urllib.error.URLError, TimeoutError) as exc:
            target.unlink(missing_ok=True)
            if attempt == retries - 1:
                raise RuntimeError(f"download failed after {retries} attempts: {url}") from exc
            time.sleep((2**attempt) + random.random())


def api_url(base: str, path: str, **params: object) -> str:
    query = urllib.parse.urlencode(params)
    return f"{base.rstrip('/')}/{path.lstrip('/')}?{query}"


def fetch_current_laws(base: str, limit: int | None = None) -> list[dict[str, Any]]:
    laws: list[dict[str, Any]] = []
    offset = 0
    while True:
        take = min(PAGE_SIZE, limit - len(laws)) if limit else PAGE_SIZE
        if take <= 0:
            break
        payload = request_json(api_url(
            base, "laws", repeal_status="None", mission="New", limit=take,
            offset=offset, order="+law_info.law_id", omit_current_revision_info="true",
            response_format="json",
        ))
        page = payload.get("laws", [])
        laws.extend(page)
        offset += len(page)
        total = int(payload.get("total_count", len(laws)))
        print(f"一覧: {len(laws):,}/{min(total, limit) if limit else total:,}", file=sys.stderr)
        if not page or len(laws) >= total or (limit and len(laws) >= limit):
            break
    return laws


def text_content(node: Any) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    return "".join(text_content(child) for child in node.get("children", []))


def clean_text(value: str) -> str:
    return " ".join(value.replace("\u3000", " ").split())


def xml_to_node(element: ET.Element) -> dict[str, Any]:
    """Convert mixed-content e-Gov XML to the API v2 JSON-shaped tree."""
    children: list[Any] = []
    if element.text:
        children.append(element.text)
    for child in element:
        children.append(xml_to_node(child))
        if child.tail:
            children.append(child.tail)
    return {"tag": element.tag.rsplit("}", 1)[-1], "attr": dict(element.attrib), "children": children}


ERA_BASE = {"Meiji": 1867, "Taisho": 1911, "Showa": 1925, "Heisei": 1988, "Reiwa": 2018}


def xml_date(attr: dict[str, str]) -> str | None:
    try:
        year = ERA_BASE[attr["Era"]] + int(attr["Year"])
        return f"{year:04d}-{int(attr['PromulgateMonth']):02d}-{int(attr['PromulgateDay']):02d}"
    except (KeyError, TypeError, ValueError):
        return None


def payload_from_bulk_xml(data: bytes, revision_id: str) -> dict[str, Any]:
    root = ET.fromstring(data)
    law_id = revision_id.split("_", 1)[0]
    law_num = clean_text(root.findtext("LawNum", default=""))
    title_element = root.find("./LawBody/LawTitle")
    title = clean_text("".join(title_element.itertext())) if title_element is not None else law_num
    enforcement = None
    parts = revision_id.split("_")
    if len(parts) > 1 and len(parts[1]) == 8 and parts[1].isdigit():
        enforcement = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:]}"
    return {
        "law_info": {
            "law_id": law_id, "law_num": law_num,
            "law_type": root.attrib.get("LawType", "Misc"),
            "promulgation_date": xml_date(root.attrib),
        },
        "revision_info": {
            "law_revision_id": revision_id, "law_title": title,
            "amendment_enforcement_date": enforcement,
        },
        "law_full_text": xml_to_node(root),
    }


def current_xml_members(bundle: zipfile.ZipFile) -> list[str]:
    """Select the newest effective (not future) revision of each law via the official CSV."""
    csv_name = next((name for name in bundle.namelist() if name.lower().endswith(".csv")), None)
    if not csv_name:
        raise RuntimeError("一括データに法令一覧CSVがありません")
    with bundle.open(csv_name) as raw:
        rows = list(csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig")))
    selected: dict[str, tuple[str, str]] = {}
    names = set(bundle.namelist())
    for row in rows:
        if row.get("未施行"):
            continue
        url = row.get("本文URL", "")
        revision_suffix = url.rstrip("/").rsplit("/", 1)[-1]
        law_id = row.get("法令ID", "")
        if not law_id or not revision_suffix:
            continue
        revision_id = f"{law_id}_{revision_suffix}"
        member = f"{revision_id}/{revision_id}.xml"
        if member not in names:
            continue
        # Rarely, two already-effective versions are listed. The later enforcement
        # date is the current text.
        if law_id not in selected or revision_suffix > selected[law_id][0]:
            selected[law_id] = (revision_suffix, member)
    return [selected[law_id][1] for law_id in sorted(selected)]


def render_node(node: Any, lines: list[str]) -> None:
    if isinstance(node, str) or not isinstance(node, dict):
        return
    tag = node.get("tag", "")
    if tag in SKIP_TAGS or tag == "LawTitle":
        return
    text = clean_text(text_content(node))
    if tag in BLOCK_TITLES and text:
        lines.extend([f"{BLOCK_TITLES[tag]} {text}", ""])
        return
    if tag == "Sentence" and text:
        lines.extend([text, ""])
        return
    if tag in INLINE_NUMBER_TAGS and text:
        lines.append(f"**{text}** ")
        return
    if tag in {"Table", "Fig", "Style", "Format"}:
        if text:
            lines.extend([f"> {text}", ""])
        return
    for child in node.get("children", []):
        render_node(child, lines)


def compact_blank_lines(lines: Iterable[str]) -> str:
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip() + "\n"


def law_to_markdown(payload: dict[str, Any]) -> str:
    info = payload["law_info"]
    rev = payload["revision_info"]
    root = payload["law_full_text"]
    title = rev.get("law_title") or info["law_num"]
    source = f"https://laws.e-gov.go.jp/law/{info['law_id']}"
    lines = [
        "---", f"law_id: {json.dumps(info['law_id'], ensure_ascii=False)}",
        f"law_revision_id: {json.dumps(rev['law_revision_id'], ensure_ascii=False)}",
        f"law_num: {json.dumps(info['law_num'], ensure_ascii=False)}",
        f"law_type: {json.dumps(info['law_type'], ensure_ascii=False)}",
        f"promulgation_date: {json.dumps(info.get('promulgation_date'), ensure_ascii=False)}",
        f"enforcement_date: {json.dumps(rev.get('amendment_enforcement_date'), ensure_ascii=False)}",
        f"source: {source}", "---", "", f"# {title}", "", f"**法令番号:** {info['law_num']}  ",
        f"**公布日:** {info.get('promulgation_date') or '不明'}  ",
        f"**e-Gov:** [{source}]({source})", "",
    ]
    render_node(root, lines)
    return compact_blank_lines(lines)


def manifest_entry(item: dict[str, Any]) -> dict[str, Any]:
    info, rev = item["law_info"], item["revision_info"]
    return {
        "revision_id": rev["law_revision_id"], "title": rev.get("law_title") or info["law_num"],
        "law_num": info["law_num"], "law_type": info["law_type"],
        "promulgation_date": info.get("promulgation_date"),
    }


def initial_bulk_sync(args: argparse.Namespace, root: Path) -> None:
    """Build the initial mirror from e-Gov's single official bulk archive."""
    with tempfile.TemporaryDirectory(prefix="laws-bulk-") as tmp_name:
        tmp = Path(tmp_name)
        archive = Path(args.bulk_archive).resolve() if args.bulk_archive else tmp / "all_xml.zip"
        if not args.bulk_archive:
            print("初回同期: e-Gov一括データを取得します", file=sys.stderr)
            download_file(args.bulk_url, archive)
        stage_laws = tmp / "laws"
        entries: dict[str, dict[str, Any]] = {}
        with zipfile.ZipFile(archive) as bundle:
            members = current_xml_members(bundle)
            if args.limit:
                members = members[:args.limit]
            total = len(members)
            for count, member in enumerate(members, 1):
                revision_id = Path(member).stem
                payload = payload_from_bulk_xml(bundle.read(member), revision_id)
                law_id = payload["law_info"]["law_id"]
                entry = manifest_entry(payload)
                target = stage_laws / entry["law_type"] / f"{law_id}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(law_to_markdown(payload), encoding="utf-8")
                entries[law_id] = entry
                if count % 100 == 0 or count == total:
                    print(f"一括変換: {count:,}/{total:,}", file=sys.stderr)
        if not entries:
            raise RuntimeError("一括データに法令XMLがありません")

        laws_dir = root / "laws"
        if laws_dir.exists():
            shutil.rmtree(laws_dir)
        shutil.move(str(stage_laws), laws_dir)
        checked = (dt.datetime.now(ZoneInfo("Asia/Tokyo")).date() - dt.timedelta(days=1)).isoformat()
        write_json(root / "manifest.json", {
            "schema_version": 2, "source": args.api_base,
            "bulk_checked_through": checked, "laws": entries,
        })
        (root / "INDEX.md").write_text(build_index(entries), encoding="utf-8")
        print(f"完了: 現行法令 {len(entries):,}件", file=sys.stderr)


def apply_update_archive(archive: Path, root: Path, entries: dict[str, dict[str, Any]]) -> int:
    changed = 0
    with zipfile.ZipFile(archive) as bundle:
        for member in current_xml_members(bundle):
            revision_id = Path(member).stem
            payload = payload_from_bulk_xml(bundle.read(member), revision_id)
            law_id = payload["law_info"]["law_id"]
            entry = manifest_entry(payload)
            if entries.get(law_id, {}).get("revision_id") == entry["revision_id"]:
                continue
            target = root / "laws" / entry["law_type"] / f"{law_id}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(law_to_markdown(payload), encoding="utf-8")
            old = entries.get(law_id)
            if old and old.get("law_type") != entry["law_type"]:
                (root / "laws" / old["law_type"] / f"{law_id}.md").unlink(missing_ok=True)
            entries[law_id] = entry
            changed += 1
    return changed


def incremental_bulk_sync(args: argparse.Namespace, root: Path, manifest: dict[str, Any]) -> None:
    """Use one official update archive per unprocessed day; no per-law requests."""
    yesterday = dt.datetime.now(ZoneInfo("Asia/Tokyo")).date() - dt.timedelta(days=1)
    checked_text = manifest.get("bulk_checked_through")
    if checked_text:
        start = dt.date.fromisoformat(checked_text) + dt.timedelta(days=1)
    else:
        start = yesterday
    if start > yesterday:
        print(f"更新済み: {checked_text}", file=sys.stderr)
        return
    if (yesterday - start).days > 89:
        raise RuntimeError("最終同期から90日を超えています。--full で一括再同期してください")
    entries = manifest["laws"]
    changed = 0
    with tempfile.TemporaryDirectory(prefix="laws-update-") as tmp_name:
        day = start
        while day <= yesterday:
            archive = Path(tmp_name) / f"update-{day:%Y%m%d}.zip"
            url = args.update_url.format(date=f"{day:%Y%m%d}")
            print(f"更新データ取得: {day.isoformat()}（1リクエスト）", file=sys.stderr)
            download_file(url, archive)
            changed += apply_update_archive(archive, root, entries)
            manifest["bulk_checked_through"] = day.isoformat()
            day += dt.timedelta(days=1)
    manifest["schema_version"] = 2
    write_json(root / "manifest.json", manifest)
    (root / "INDEX.md").write_text(build_index(entries), encoding="utf-8")
    print(f"完了: 更新 {changed:,}件、現行法令 {len(entries):,}件", file=sys.stderr)


def read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "source": DEFAULT_API_BASE, "laws": {}}
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_index(entries: dict[str, dict[str, Any]]) -> str:
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for law_id, entry in entries.items():
        groups.setdefault(entry["law_type"], []).append((law_id, entry))
    lines = ["# 日本の現行法令一覧", "", f"収録法令数: **{len(entries):,}件**", ""]
    for law_type in sorted(groups, key=lambda t: (TYPE_NAMES.get(t, t), t)):
        rows = sorted(groups[law_type], key=lambda pair: (pair[1]["title"], pair[0]))
        lines.extend([f"## {TYPE_NAMES.get(law_type, law_type)} ({len(rows):,}件)", ""])
        for law_id, entry in rows:
            lines.append(f"- [{entry['title']}](laws/{law_type}/{law_id}.md) — {entry['law_num']}")
        lines.append("")
    return "\n".join(lines)


def sync(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    laws_dir, manifest_path = root / "laws", root / "manifest.json"
    old = read_manifest(manifest_path)
    if args.full:
        initial_bulk_sync(args, root)
        return
    if not old.get("laws") and (args.limit is None or args.bulk_archive):
        initial_bulk_sync(args, root)
        return
    if old.get("laws") and args.limit is None:
        incremental_bulk_sync(args, root, old)
        return
    listing = fetch_current_laws(args.api_base, args.limit)
    new_entries = {item["law_info"]["law_id"]: manifest_entry(item) for item in listing}
    old_entries = old.get("laws", {})
    changed = [law_id for law_id, entry in new_entries.items()
               if args.force or old_entries.get(law_id, {}).get("revision_id") != entry["revision_id"]
               or not (laws_dir / entry["law_type"] / f"{law_id}.md").exists()]
    print(f"本文取得対象: {len(changed):,}件", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="laws-sync-") as tmp_name:
        tmp = Path(tmp_name)

        def fetch_one(law_id: str) -> tuple[str, Path]:
            payload = request_json(api_url(args.api_base, f"law_data/{law_id}", response_format="json"))
            target = tmp / new_entries[law_id]["law_type"] / f"{law_id}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(law_to_markdown(payload), encoding="utf-8")
            return law_id, target

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(fetch_one, law_id): law_id for law_id in changed}
            for future in concurrent.futures.as_completed(futures):
                law_id, source = future.result()
                completed += 1
                if completed % 25 == 0 or completed == len(changed):
                    print(f"本文: {completed:,}/{len(changed):,}", file=sys.stderr)

        laws_dir.mkdir(parents=True, exist_ok=True)
        for law_id in changed:
            entry = new_entries[law_id]
            target = laws_dir / entry["law_type"] / f"{law_id}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp / entry["law_type"] / f"{law_id}.md", target)

    complete_run = args.limit is None
    if args.prune and complete_run:
        removed = set(old_entries) - set(new_entries)
        for law_id in removed:
            entry = old_entries[law_id]
            (laws_dir / entry["law_type"] / f"{law_id}.md").unlink(missing_ok=True)
        for directory in laws_dir.iterdir():
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        print(f"削除対象: {len(removed):,}件", file=sys.stderr)
    elif not complete_run:
        # A limited run augments the existing manifest; it never claims completeness.
        new_entries = {**old_entries, **new_entries}

    manifest = {"schema_version": 1, "source": args.api_base, "laws": new_entries}
    write_json(manifest_path, manifest)
    (root / "INDEX.md").write_text(build_index(new_entries), encoding="utf-8")
    print(f"完了: 現行法令 {len(new_entries):,}件", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--bulk-url", default=BULK_URL)
    parser.add_argument("--update-url", default=UPDATE_URL)
    parser.add_argument("--bulk-archive", help="ダウンロード済みのe-Gov一括ZIPを使う")
    parser.add_argument("--full", action="store_true", help="公式一括データで全件を再構築する")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, help="テスト用。先頭N件だけ同期する")
    parser.add_argument("--force", action="store_true", help="版IDが同じでも本文を再取得する")
    parser.add_argument("--prune", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        parser.error("--workers は1〜16で指定してください")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit は1以上で指定してください")
    return args


if __name__ == "__main__":
    sync(parse_args())
