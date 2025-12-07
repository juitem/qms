#!/usr/bin/env python3
"""
Generate wget URL lists for Tizen image regular and debuginfo RPMs.

Usage example:

  python3 gen_tizen_wget_links.py \
      --url=https://download.tizen.org/snapshots/TIZEN/Tizen/Tizen-Unified-X-ASAN/reference/images/standard/tizen-headed-aarch64/ \
      --outdir=./download

Outputs (in outdir):

  regular_packages_urls.txt   # wget URL list for normal packages
  debuginfo_packages_urls.txt # wget URL list for debuginfo packages

You can then run:

  wget -c -i regular_packages_urls.txt
  wget -c -i debuginfo_packages_urls.txt
"""

import argparse
import os
import sys
import re
import urllib.request
import urllib.parse
from html.parser import HTMLParser


class IndexParser(HTMLParser):
    """Very small HTML index parser to collect href attributes."""
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        attr_dict = dict(attrs)
        href = attr_dict.get("href")
        if href:
            self.hrefs.append(href)


def fetch_url(url):
    """Download URL and return bytes."""
    with urllib.request.urlopen(url) as resp:
        return resp.read()


def download_file(base_url, href, outdir):
    """
    Download a file referenced by href (relative or absolute)
    from a directory index into outdir. Returns local path.
    """
    full_url = urllib.parse.urljoin(base_url, href)
    filename = os.path.basename(urllib.parse.urlparse(full_url).path)
    if not filename:
        # Fallback: use href as name
        filename = href.strip("/")

    local_path = os.path.join(outdir, filename)
    data = fetch_url(full_url)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


def extract_baseurls_from_ks(ks_path):
    """
    Parse .ks file and extract repo baseurl=... values.

    Returns: list of baseurl strings (may contain duplicates).
    """
    baseurls = []
    pattern = re.compile(r'\brepo\b.*?baseurl=([^\s]+)')
    with open(ks_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "repo" not in line or "baseurl=" not in line:
                continue
            m = pattern.search(line)
            if not m:
                continue
            url = m.group(1).strip()
            # Strip possible trailing escape/backslash
            if url.endswith("\\"):
                url = url[:-1]
            baseurls.append(url)
    return baseurls

def read_package_names(packages_path):
    """
    Read package names from .packages file.

    We assume that each non-empty, non-comment line
    has the package identifier as the first whitespace-separated field.

    The token may or may not contain the '.rpm' suffix, so we normalize it
    to a proper RPM filename using normalize_rpm_filename().
    """
    names = []
    with open(packages_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            token = line.split()[0]
            rpm_name = normalize_rpm_filename(token)
            if rpm_name is None:
                continue
            names.append(rpm_name)
    return names


def make_debug_baseurl(baseurl):
    """
    Convert a packages baseurl -> debug baseurl.
    Ex: .../repos/standard/packages/ -> .../repos/standard/debug/
    """
    if "/packages/" not in baseurl:
        return None
    return baseurl.replace("/packages/", "/debug/")


def make_debuginfo_name(pkg_filename):
    """
    Convert a regular RPM filename into its debuginfo variant.

    Assumes format:
        name-version-release.arch.rpm

    Output:
        name-debuginfo-version-release.arch.rpm

    This is a heuristic but matches normal RPM naming rules.
    """
    if not pkg_filename.endswith(".rpm"):
        return None

    body = pkg_filename[:-4]  # drop ".rpm"
    if "." not in body:
        # No arch separator, give up
        return None

    nvra, arch = body.rsplit(".", 1)  # "name-version-release", "arch"

    # We need at least two '-' to split into name / version / release
    last_dash = nvra.rfind("-")
    if last_dash == -1:
        return None
    second_last_dash = nvra.rfind("-", 0, last_dash)
    if second_last_dash == -1:
        return None

    name = nvra[:second_last_dash]
    vr = nvra[second_last_dash + 1:]

    # Avoid double debuginfo if name already ends with "-debuginfo"
    if name.endswith("-debuginfo"):
        debug_nvra = nvra
    else:
        debug_nvra = f"{name}-debuginfo-{vr}"

    return f"{debug_nvra}.{arch}.rpm"


def main():
    parser = argparse.ArgumentParser(
        description="Generate wget link lists for Tizen regular and debuginfo RPMs."
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Base URL of Tizen image directory (index of .../tizen-headed-aarch64/).",
    )
    parser.add_argument(
        "--outdir",
        required=True,
        help="Output directory for downloaded ks/packages and URL lists.",
    )

    args = parser.parse_args()
    base_url = args.url
    if not base_url.endswith("/"):
        base_url += "/"

    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)

    # 1) Fetch directory index HTML and parse hrefs
    try:
        html = fetch_url(base_url).decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"Error: failed to fetch index from {base_url}: {e}", file=sys.stderr)
        sys.exit(1)

    parser_html = IndexParser()
    parser_html.feed(html)

    ks_hrefs = [h for h in parser_html.hrefs if h.endswith(".ks")]
    packages_hrefs = [h for h in parser_html.hrefs if ".packages" in h]

    if not ks_hrefs:
        print("Warning: no .ks files found in directory index.", file=sys.stderr)
    if not packages_hrefs:
        print("Warning: no .packages files found in directory index.", file=sys.stderr)

    # 2) Download ks and packages files
    ks_paths = []
    for href in ks_hrefs:
        try:
            path = download_file(base_url, href, outdir)
            ks_paths.append(path)
            print(f"Downloaded ks: {path}")
        except Exception as e:
            print(f"Warning: failed to download ks {href}: {e}", file=sys.stderr)

    packages_paths = []
    for href in packages_hrefs:
        try:
            path = download_file(base_url, href, outdir)
            packages_paths.append(path)
            print(f"Downloaded packages: {path}")
        except Exception as e:
            print(f"Warning: failed to download packages {href}: {e}", file=sys.stderr)

    # 3) Extract baseurls from all ks files
    baseurls = []
    seen = set()
    for ks_path in ks_paths:
        urls = extract_baseurls_from_ks(ks_path)
        for u in urls:
            if u not in seen:
                seen.add(u)
                baseurls.append(u)

    if not baseurls:
        print("Warning: no repo baseurl= entries found in ks files.", file=sys.stderr)

    debug_baseurls = []
    debug_seen = set()
    for u in baseurls:
        dbg = make_debug_baseurl(u)
        if dbg and dbg not in debug_seen:
            debug_seen.add(dbg)
            debug_baseurls.append(dbg)

    # 4) Read all package names from packages files
    package_names = []
    pkg_seen = set()
    for pkg_path in packages_paths:
        names = read_package_names(pkg_path)
        for n in names:
            if n not in pkg_seen:
                pkg_seen.add(n)
                package_names.append(n)

    if not package_names:
        print("Warning: no package names found in .packages files.", file=sys.stderr)

    # 5) Build URL sets
    regular_urls = set()
    debuginfo_urls = set()

    def join_url(base, filename):
        if not base.endswith("/"):
            base = base + "/"
        return urllib.parse.urljoin(base, filename)

    for pkg in package_names:
        for bu in baseurls:
            regular_urls.add(join_url(bu, pkg))

        debug_name = make_debuginfo_name(pkg)
        if not debug_name:
            continue
        for dbu in debug_baseurls:
            debuginfo_urls.add(join_url(dbu, debug_name))

    # 6) Write outputs
    regular_out = os.path.join(outdir, "regular_packages_urls.txt")
    debug_out = os.path.join(outdir, "debuginfo_packages_urls.txt")

    with open(regular_out, "w", encoding="utf-8") as f:
        for u in sorted(regular_urls):
            f.write(u + "\n")

    with open(debug_out, "w", encoding="utf-8") as f:
        for u in sorted(debuginfo_urls):
            f.write(u + "\n")

    print(f"\n[RESULT]")
    print(f"  Regular package URLs : {regular_out} ({len(regular_urls)} entries)")
    print(f"  Debuginfo package URLs: {debug_out} ({len(debuginfo_urls)} entries)")


if __name__ == "__main__":
    main()
