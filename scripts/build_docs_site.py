"""Render the public guides into a new directory for the gh-pages branch.

From a publication checkout with Pandoc 3 installed, run
python3 scripts/build_docs_site.py /path/to/new-site
"""
from __future__ import annotations

import argparse
import html
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
from urllib.parse import quote, unquote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/04-zain/MIFflow"
STYLES = ROOT / "docs/site"
LINK_ATTRIBUTE = re.compile(r'\b(href|src)="([^"]+)"')
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".pdf"}


def page_path(source: Path) -> Path:
    return Path("index.html") if source == Path("README.md") else source.with_suffix(".html")


# Within each section the directories follow the order of the desktop workflow.
SECTIONS = (
    ("Getting started", (
        ("Installation", "docs/stages/installation-and-build", "software"),
        ("Analysis setup", "docs/stages/setup", "analysis"),
        ("Running an analysis", "docs/stages/run", "index"),
    )),
    ("Analysis stages", (
        ("Images and channels", "docs/stages/images", "index"),
        ("Cyclic fluorescence", "docs/stages/cyclic-if-methods", "index"),
        ("Spectral unmixing", "docs/stages/unmixing", "index"),
        ("Tissue detection", "docs/stages/tissue", "index"),
        ("Paired-image alignment", "docs/stages/alignment", "index"),
        ("Tissue classification", "docs/stages/tissue-classification", "index"),
        ("Cell segmentation", "docs/stages/cells", "index"),
        ("Cell measurements", "docs/stages/cell-measurements", "index"),
        ("Phenotyping", "docs/stages/phenotyping", "index"),
        ("Secondary structures", "docs/stages/secondary-structures", "index"),
        ("Results and export", "docs/stages/results", "index"),
    )),
    ("Reference", (
        ("Classifier methods", "docs/stages/classifier-methods", "index"),
        ("Data formats", "docs/reference", "data-formats"),
        ("Example images", "docs/data", "public-image-sources"),
        ("Licences", "docs/third-party", ""),
    )),
)


def navigation(source: Path, titles: dict[Path, str], prefix: str) -> tuple[str, str]:
    home = Path("README.md")
    troubleshooting = Path("docs/troubleshooting.md")
    listed = {home, troubleshooting}
    location = "Documentation"
    def link(path: Path, label: str) -> str:
        current = ' aria-current="page"' if path == source else ""
        href = prefix + page_path(path).as_posix()
        return f'<a href="{href}"{current}>{html.escape(label)}</a>'

    parts = [link(home, "Overview")]
    for section, groups in SECTIONS:
        parts.append(f'<h2>{section}</h2>')
        for group, directory, first in groups:
            members = sorted((p for p in titles if p.parent == Path(directory)),
                             key=lambda p: (p.stem != first, titles[p].casefold()))
            if not members:
                continue
            listed.update(members)
            active = source in members
            if active:
                location = f"{section} / {group}"
            if len(members) == 1:
                parts.append(link(members[0], group))
                continue
            parts.append(f'<details{" open" if active else ""}><summary>{group}</summary><div>')
            for path in members:
                label = titles[path] if directory != "docs/third-party" else path.stem.replace("-license", "").upper()
                parts.append(link(path, label))
            parts.append("</div></details>")
        if section == "Reference":
            parts.append(link(troubleshooting, "Troubleshooting"))
            if source == troubleshooting:
                location = section
    if listed != set(titles):
        raise ValueError(f"Missing navigation entries: {set(titles) - listed}")
    return "\n".join(parts), html.escape(location)


def page_outline(article: str) -> str:
    links = []
    for anchor, heading in re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', article, re.S):
        label = re.sub(r"<[^>]+>", "", heading)
        links.append(f'<a href="#{anchor}">{label}</a>')
    if not links:
        return ""
    return '<details class="outline"><summary>On this page</summary><nav aria-label="On this page">' + "".join(links) + "</nav></details>"


def build(output: Path, pandoc: str) -> None:
    tracked = {
        Path(os.fsdecode(name)) for name in
        subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0") if name
    }
    pages = {path for path in tracked if path.suffix == ".md"
             and (path in {Path("README.md"), Path("docs/troubleshooting.md")}
                  or path.parts[:2] in {("docs", "stages"), ("docs", "reference"),
                                        ("docs", "data"), ("docs", "third-party")})}
    assets: set[Path] = set()
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(STYLES / "guide.css", output / "guide.css")
    (output / ".nojekyll").touch()
    shutil.copyfile(STYLES / "guide.js", output / "guide.js")
    titles = {}
    for source in sorted(pages):
        lines = (ROOT / source).read_text().splitlines()
        titles[source] = next((line[2:].strip() for line in lines if line.startswith("# ")), lines[0])
    for source, title in titles.items():
        page = page_path(source)
        prefix = "../" * (len(page.parts) - 1)
        rendered = subprocess.check_output([
            pandoc, "--from=gfm+tex_math_dollars", "--to=html5", "--mathml",
            "--standalone", "--wrap=none", "--no-highlight", "--template", str(STYLES / "guide.html"),
            "--metadata", "pagetitle=" + (title if source == Path("README.md") else f"{title} · MIFflow"), "--variable", f"site-root={prefix}",
            "--variable", f"source-url={REPO}/blob/main/{source.as_posix()}", str(ROOT / source),
        ], text=True)

        def resolve_link(match: re.Match[str]) -> str:
            attr, raw = match.groups()
            url = urlsplit(html.unescape(raw))
            if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
                return match.group()
            target = Path(posixpath.normpath((source.parent / unquote(url.path)).as_posix()))
            if target in pages:
                dest = page_path(target)
            elif target in tracked and target.suffix.lower() in ASSET_SUFFIXES:
                assets.add(target)
                dest = target
            elif target in tracked or (ROOT / target).is_dir():
                kind = "blob" if target in tracked else "tree"
                ref = f"{REPO}/{kind}/main/{quote(target.as_posix())}"
                return f'{attr}="{html.escape(urlunsplit(("", "", ref, url.query, url.fragment)), quote=True)}"'
            else:
                return match.group()
            path = quote(os.path.relpath(dest, page.parent))
            new_url = urlunsplit(("", "", path, url.query, url.fragment))
            return f'{attr}="{html.escape(new_url, quote=True)}"'

        # Only article links are relative to the original Markdown file.
        main = '<main id="guide" class="site-width" tabindex="-1">'
        start, article = rendered.split(main, 1)
        article, end = article.split("</main>", 1)
        article = LINK_ATTRIBUTE.sub(resolve_link, article)
        article = re.sub(r"(<table\b[^>]*>)", r'<div class="table-scroll">\1', article)
        article = article.replace("</table>", "</table></div>")
        destination = output / page
        destination.parent.mkdir(parents=True, exist_ok=True)
        nav, location = navigation(source, titles, prefix)
        start = start.replace("<!--guide-navigation-->", nav).replace("<!--guide-location-->", location)
        start = start.replace("<!--guide-outline-->", page_outline(article))
        destination.write_text(start + main + article + "</main>" + end)
    for asset in sorted(assets):
        (output / asset).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / asset, output / asset)
    print(f"Rendered {len(pages)} guides and copied {len(assets)} linked assets.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new directory for the generated site")
    parser.add_argument("--pandoc", default="pandoc", help="path to Pandoc 3")
    args = parser.parse_args()
    build(args.output.resolve(), args.pandoc)
