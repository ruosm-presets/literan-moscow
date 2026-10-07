#!/usr/bin/env python3
"""Sanity checks for JOSM tagging preset file(s), beyond XSD validation.

Checks:
  1. values <-> display_values <-> ru.display_values element counts match
     (comma-separated lists, backslash-escaped commas count as one element)
  2. <reference ref="..."> points to an existing <chunk id="..."> (error);
     unreferenced chunks (warning)
  3. no duplicate item names within the same group
  4. <link href="..."> is a well-formed http(s) URL or OSM wiki page name (error)
  5. icon="..." points to this repo served via GitHub Pages and the file
     exists under pics/ (error) — no external icon hosting
  6. link targets are reachable via GET (warning by default, error with
     --strict-links); for every http:// link the https:// twin is probed and
     suggested when available

Usage:
  python3 scripts/check_presets.py [--no-http] [--strict-links] [file.xml ...]
Exit code 1 if any error was reported.
"""

import argparse
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NS = "http://josm.openstreetmap.de/tagging-preset-1.0"
COMBO_TAGS = (f"{{{NS}}}combo", f"{{{NS}}}multiselect")
URL_RE = re.compile(r"^https?://\S+$")
# JOSM <link href> also accepts wiki page names, e.g. "Tag:amenity=post_office"
WIKI_PAGE_RE = re.compile(r"^[A-Za-z0-9_%:.#=/-]+$")
OSM_WIKI_URL = "https://wiki.openstreetmap.org/wiki/"
PAGES_BASE = "https://ruosm-presets.github.io/literan-moscow/"
ICON_NAME_RE = re.compile(r"^[a-z0-9_]+\.(svg|png)$")
LIST_SPLIT_RE = re.compile(r"(?<!\\),")
UA = {"User-Agent": "literan-moscow-preset-checks/1.0 (JOSM preset CI)"}


class Report:
    def __init__(self, path: str):
        self.path = path
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


def split_keep(value: str) -> list[str]:
    return LIST_SPLIT_RE.split(value)


def idn_url(url: str) -> str:
    """Re-encode a URL with an IDN (non-ASCII) hostname for urllib."""
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    try:
        host = parts.hostname.encode("idna").decode("ascii") if parts.hostname else ""
    except UnicodeError:
        return url
    netloc = host if not parts.port else f"{host}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def check_structure(root: ET.Element, rep: Report, xml_path: Path) -> list[str]:
    """Structural checks; returns link URLs to probe."""
    ctx: dict[ET.Element, str] = {}

    def label(e: ET.Element) -> str:
        return e.get("name") or e.get("id") or "?"

    def walk(e: ET.Element, path: list[str]) -> None:
        for c in e:
            tag = c.tag.split("}")[-1]
            if tag in ("item", "chunk", "group"):
                ctx[c] = "/".join(path + [label(c)])
                new_path = path if tag == "chunk" else path + [label(c)]
                walk(c, new_path)

    walk(root, [])

    # 1. values <-> display_values counts
    for e in root.iter():
        if e.tag not in COMBO_TAGS:
            continue
        values = e.get("values")
        if not values or not values.strip():
            continue
        n_values = len(split_keep(values))
        for attr in ("display_values", "ru.display_values"):
            dv = e.get(attr)
            if dv is None:
                continue
            n_dv = len(split_keep(dv))
            if n_dv != n_values:
                rep.error(
                    f"{where(e, ctx)}: <{e.tag.split('}')[-1]} key={e.get('key')!r}>: "
                    f"values has {n_values} entries but {attr} has {n_dv}"
                )

    # 2. chunk references
    parent: dict[ET.Element, ET.Element] = {}
    for e in root.iter():
        for c in e:
            parent[c] = e
    chunk_ids = {c.get("id") for c in root.iter(f"{{{NS}}}chunk")}
    used: Counter[str] = Counter()
    for r in root.iter(f"{{{NS}}}reference"):
        ref = r.get("ref")
        used[ref] += 1
        if ref not in chunk_ids:
            rep.error(f"{where(parent.get(r, root), ctx)}: reference to undefined chunk {ref!r}")
    for cid in sorted(chunk_ids - set(used)):
        rep.warn(f"unused chunk {cid!r}")

    # 3. duplicate item names within a group
    def items_with_group(e: ET.Element, gpath: tuple[str, ...]):
        for c in e:
            tag = c.tag.split("}")[-1]
            if tag == "item":
                yield c, gpath
            elif tag == "group":
                yield from items_with_group(c, gpath + (c.get("name") or "?",))

    names: Counter[tuple[str, ...]] = Counter()
    for item, gpath in items_with_group(root, ()):
        names[(item.get("name") or "?", gpath)] += 1
    for (name, gpath), n in names.items():
        if n > 1:
            rep.error(f"duplicate item name {name!r} x{n} in group /{'/'.join(gpath)}")

    # 4+5. icons: must be Pages URLs of files that exist in this repo
    base_dir = xml_path.parent
    for e in root.iter():
        icon = e.get("icon")
        if not icon:
            continue
        if not URL_RE.match(icon):
            rep.error(f"{where(e, ctx)}: icon is not a valid URL: {icon!r}")
        elif not icon.startswith(PAGES_BASE):
            rep.error(
                f"{where(e, ctx)}: icon must be hosted in this repo under {PAGES_BASE}"
                f" (put the file into pics/icons/), got: {icon!r}"
            )
        elif not (base_dir / icon[len(PAGES_BASE):]).is_file():
            rep.error(f"{where(e, ctx)}: icon file missing in repo: {icon[len(PAGES_BASE):]!r}")
        elif not ICON_NAME_RE.match(icon.rsplit("/", 1)[-1]):
            rep.error(f"{where(e, ctx)}: icon filename must be lowercase snake_case"
                      f" (e.g. shopping_cart.svg), got: {icon.rsplit('/', 1)[-1]!r}")

    # 4. links: collect URLs for reachability probing
    links: list[str] = []
    for e in root.iter():
        if e.tag != f"{{{NS}}}link":
            continue
        href = e.get("href") or e.get("wiki") or ""
        if not href:
            continue
        if URL_RE.match(href):
            links.append(href.split("#", 1)[0])
        elif WIKI_PAGE_RE.match(href):
            # wiki page name -> absolute URL (fragment is not sent to servers)
            links.append(OSM_WIKI_URL + href.split("#", 1)[0])
        else:
            rep.error(f"{where(e, ctx)}: link href is not a valid URL or wiki page: {href!r}")
    return links


def where(elem: ET.Element, ctx: dict[ET.Element, str]) -> str:
    return ctx.get(elem, "<root>")


def _probe(url: str, retries: int = 1, timeout: int = 10) -> int | None:
    """GET the url, return final HTTP status; None on network failure after retries."""
    url = idn_url(url)
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA | {"Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "code", None)
            if status and 400 <= status < 500 and status != 429:
                return status  # definitive answer, no retry
    return None


def check_links(links: list[str], rep: Report, workers: int = 16, strict: bool = False) -> None:
    """Probe link targets; report problems. Bot-protected sites may block probes,
    so failures are warnings unless --strict-links."""
    tasks = sorted(set(links))
    twins = {u: u.replace("http://", "https://", 1) for u in tasks if u.startswith("http://")}

    def probe_pair(item: tuple[str, str]) -> tuple[str, int | None]:
        return item[0], _probe(item[1])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_probe, tasks))
        twin_results = list(pool.map(probe_pair, twins.items()))
    twins_ok = {orig for orig, status in twin_results if status is not None and 200 <= status < 400}

    report = rep.error if strict else rep.warn
    for url, status in zip(tasks, results):
        if status is None:
            report(f"link unreachable (network or bot protection?): {url}")
        elif not (200 <= status < 400):
            note = ""
            if url in twins_ok:
                note = f"; https works, prefer {twins[url]}"
                report(f"insecure http link, https is available: {url} -> {twins[url]}")
            elif url.startswith("http://") and url in twins:
                pass  # twin result already covered above when probed
            report(f"link returned HTTP {status}: {url}{note}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", help="preset XML files")
    ap.add_argument("--no-http", action="store_true", help="skip remote link reachability checks")
    ap.add_argument("--strict-links", action="store_true",
                    help="treat unreachable/erroring links as errors instead of warnings")
    args = ap.parse_args()

    had_errors = False
    for path in args.files:
        rep = Report(path)
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            print(f"::error file={path}::XML parse error: {exc}")
            return 1
        links = check_structure(root, rep, Path(path))
        if not args.no_http and links:
            check_links(links, rep, strict=args.strict_links)
        for w in rep.warnings:
            print(f"::warning file={path}::{w}")
        for e in rep.errors:
            print(f"::error file={path}::{e}")
        status = "OK" if rep.ok else "FAILED"
        print(f"{path}: {status} ({len(rep.errors)} errors, {len(rep.warnings)} warnings)")
        had_errors |= not rep.ok
    return 1 if had_errors else 0


if __name__ == "__main__":
    sys.exit(main())
