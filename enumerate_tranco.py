"""Enumerate Wayback Machine snapshots of ToS / privacy policies for Tranco top sites.

Stage 1 of the broad layer (see proposal.md). Three steps per domain:

  1. Discover: fetch the archived homepage (mid-2022 and latest) in raw `id_`
     mode and pick the best "terms" and "privacy" links from it.
  2. Fallback: if a homepage yields no link for a doc type, probe common paths
     (/terms, /privacy-policy, ...) against the CDX index.
  3. Enumerate: for every discovered policy URL, list HTTP-200 captures since
     2022 from the CDX API, collapsed to at most one per month.

Wildcard / domain-wide CDX queries are not used: the CDX server rejects them
(403) or times out for large sites, so discovery is link- and path-based.

Outputs (all under --out-dir, default data/tranco/), written incrementally so
the script can be stopped and re-run; finished domains / URLs are skipped:

  tranco_list.csv     rank,domain                    (the pinned list slice)
  list_meta.json      Tranco list id + parameters used
  discovery_log.csv   one row per domain: status + notes (coverage stats)
  policy_urls.csv     one row per (domain, doc_type, url) discovered
  snapshots.csv       one row per (url, monthly capture): timestamp, digest, ...
  enumerated_urls.csv one row per URL whose CDX enumeration finished

Usage:
  python enumerate_tranco.py --top 1500
  python enumerate_tranco.py --top 20 --out-dir /tmp/tranco_test   # quick test
"""

import argparse
import csv
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK = "https://web.archive.org/web"
TRANCO_API = "https://tranco-list.eu/api/lists/date/{date}"
TRANCO_DOWNLOAD = "https://tranco-list.eu/download/{list_id}/{n}"
DEFAULT_LIST_ID = "V354N"  # Tranco list created 2026-11-01; pinned for reproducibility
USER_AGENT = "COSC545-policy-archaeology/0.1 (academic research project)"

DATA_DIR = Path(__file__).parent / "data" / "tranco"

# Homepages to discover links from. Two time points catch URL moves.
DISCOVERY_TIMESTAMPS = {"2022": "20220701", "latest": "2026"}
FROM_YEAR = "2022"

DOC_TYPES = ("terms", "privacy")

# (pattern, score) on anchor text and on URL path. Higher = more confident.
TEXT_PATTERNS = {
    "terms": [(r"\bterms of (service|use)\b", 5), (r"\bterms (and|&) conditions\b", 5),
              (r"\buser agreement\b", 5), (r"\bconditions of use\b", 5),
              (r"\bterms\b", 3), (r"\blegal\b", 1)],
    "privacy": [(r"\bprivacy (policy|notice|statement)\b", 5), (r"\bprivacy\b", 3)],
}
PATH_PATTERNS = {
    "terms": [(r"terms[-_]?of[-_]?(service|use)|user[-_]?agreement|conditions[-_]?of[-_]?use", 4),
              (r"(^|/)(terms|tos)([-_.]|/|$)", 3), (r"/legal(/|$)", 1)],
    "privacy": [(r"privacy[-_]?(policy|notice|statement)", 4), (r"privacy", 3)],
}
# Links that mention privacy/terms but are not the policy itself.
EXCLUDE = re.compile(
    r"cookie|choices|do[-_ ]?not[-_ ]?sell|opt[-_ ]?out|settings|preferences|"
    r"california|ccpa|adchoices|ad[-_ ]choices|help|support|faq|accessibility|"
    r"sitemap|login|signin|sign[-_]in|careers|investor|press|blog|"
    r"seller|merchant|developer|api[-_ ]terms|partner|supplier|vendor|"
    r"mailto:|javascript:|tel:", re.I)

# Fallback paths, most common first. CDX canonicalization ignores www./scheme.
PROBE_PATHS = {
    "terms": ["terms", "terms-of-service", "terms-of-use", "tos", "legal/terms",
              "terms-and-conditions", "legal/terms-of-service", "policies/terms", "legal"],
    "privacy": ["privacy", "privacy-policy", "legal/privacy", "privacy-notice",
                "legal/privacy-policy", "policies/privacy", "privacy-statement"],
}

WAYBACK_URL_RE = re.compile(r"/web/(\d{14})id_/(.+)$")


# --------------------------------------------------------------------------- HTTP

class RateLimiter:
    """Global minimum interval between requests, shared by all worker threads."""

    def __init__(self, per_second):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.next_time - now
            self.next_time = max(now, self.next_time) + self.interval
        if delay > 0:
            time.sleep(delay)

    def pause(self, seconds):
        """Pause *all* workers (IA throttles per IP, so one 429 means everyone waits)."""
        with self.lock:
            resume = time.monotonic() + seconds
            if resume > self.next_time:
                self.next_time = resume
                print(f"  .. throttled by archive; pausing all workers {seconds:.0f}s", flush=True)


class Client:
    def __init__(self, rate, retries=4, timeout=60):
        self.limiter = RateLimiter(rate)
        self.retries = retries
        self.timeout = timeout
        self.local = threading.local()

    @property
    def session(self):
        if not hasattr(self.local, "session"):
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            self.local.session = s
        return self.local.session

    def get(self, url, params=None):
        """GET with retries/backoff on 429, 5xx and network errors.

        Returns the Response (including 4xx other than 429), or None if all
        retries failed. A 429 or a refused connection means the Internet Archive
        is throttling this IP (it escalates to a temporary firewall block if
        ignored), so those pause every worker, not just this one.
        """
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.ConnectionError:
                self.limiter.pause(60 * (2 ** attempt))
                continue
            except requests.RequestException:  # read timeout etc.
                resp = None
            if resp is not None and resp.status_code != 429 and resp.status_code < 500:
                return resp
            if resp is not None and resp.status_code == 429:
                self.limiter.pause(60 * (2 ** attempt))
            elif attempt < self.retries:
                time.sleep(5 * (2 ** attempt))
        return None


# --------------------------------------------------------------------------- Tranco

def load_tranco(client, list_id, top, out_dir):
    path = out_dir / "tranco_list.csv"
    if path.exists():
        with open(path, newline="") as f:
            rows = [(int(r["rank"]), r["domain"]) for r in csv.DictReader(f)]
        if len(rows) >= top:
            return rows[:top]
    resp = client.get(TRANCO_DOWNLOAD.format(list_id=list_id, n=top))
    if resp is None or resp.status_code != 200:
        raise SystemExit(f"Could not download Tranco list {list_id}")
    rows = []
    for line in resp.text.splitlines():
        rank, domain = line.strip().split(",", 1)
        rows.append((int(rank), domain.strip().lower()))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "domain"])
        w.writerows(rows)
    (out_dir / "list_meta.json").write_text(json.dumps(
        {"list_id": list_id, "top": top, "download": TRANCO_DOWNLOAD.format(list_id=list_id, n=top),
         "retrieved": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2))
    return rows


# --------------------------------------------------------------------------- discovery

def fetch_archived(client, url, when):
    """Fetch the capture of `url` nearest to `when` in raw mode.

    Wayback follows archived redirects, so the returned original URL may be on a
    different host (e.g. zoom.us -> www.zoom.com). Returns
    (timestamp, original_url, html) or None.
    """
    resp = client.get(f"{WAYBACK}/{when}id_/{url}")
    if resp is None or resp.status_code != 200:
        return None
    m = WAYBACK_URL_RE.search(resp.url)
    if not m:
        return None
    if "html" not in resp.headers.get("Content-Type", "html"):
        return None
    return m.group(1), m.group(2), resp.text


def score_link(doc_type, text, href):
    if EXCLUDE.search(text) or EXCLUDE.search(href):
        return 0
    path = urlparse(href).path.lower()
    score = 0
    for pattern, s in TEXT_PATTERNS[doc_type]:
        if re.search(pattern, text, re.I):
            score += s
            break
    for pattern, s in PATH_PATTERNS[doc_type]:
        if re.search(pattern, path):
            score += s
            break
    return score


def pick_links(html, base_url):
    """Return {doc_type: (url, anchor_text, score)} for the best link of each type."""
    soup = BeautifulSoup(html, "html.parser")
    best = {}
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(base_url, a["href"].strip()))[0]
        if urlparse(href).scheme not in ("http", "https"):
            continue
        text = " ".join(a.get_text(" ").split())[:120]
        for doc_type in DOC_TYPES:
            score = score_link(doc_type, text, href)
            if score < 3:
                continue
            # Prefer higher score, then the shorter (more canonical) URL.
            key = (score, -len(href))
            if doc_type not in best or key > best[doc_type][3]:
                best[doc_type] = (href, text, score, key)
    return {k: v[:3] for k, v in best.items()}


def cdx_has_capture(client, url):
    params = {"url": url, "from": FROM_YEAR, "filter": "statuscode:200",
              "fl": "timestamp,original", "limit": "1", "output": "json"}
    resp = client.get(CDX, params)
    if resp is None or resp.status_code != 200:
        return None
    try:
        rows = resp.json()
    except ValueError:
        return None
    return rows[1][1] if len(rows) > 1 else None


def resolve(client, url):
    """Follow archived redirects for a discovered link; return the final original URL."""
    got = fetch_archived(client, url, DISCOVERY_TIMESTAMPS["latest"])
    return got[1] if got else url


def canonicalize(client, doc_type, url):
    """Drop tracking query strings (?ref=footer, ?icid=...) that split one document's
    capture history across URLs. Only done when the bare path itself looks like a
    policy page and has captures, so redirectors like /fwlink?LinkID=... are kept."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    if not any(re.search(p, parsed.path.lower()) and s >= 3 for p, s in PATH_PATTERNS[doc_type]):
        return url
    bare = parsed._replace(query="").geturl()
    return bare if cdx_has_capture(client, bare) else url


def discover(client, rank, domain):
    """Return (log_row, [policy_url_rows]) for one domain."""
    found = {}  # (doc_type, url) -> row
    notes = []
    homepage_ok = False
    resolved = {}  # link -> canonical url; 2022 and latest homepages often share links

    for label, when in DISCOVERY_TIMESTAMPS.items():
        got = fetch_archived(client, f"https://{domain}/", when)
        if not got:
            notes.append(f"no_homepage_{label}")
            continue
        homepage_ok = True
        ts, original, html = got
        for doc_type, (href, text, score) in pick_links(html, original).items():
            if href not in resolved:
                resolved[href] = canonicalize(client, doc_type, resolve(client, href))
            final = resolved[href]
            found.setdefault((doc_type, final), {
                "rank": rank, "domain": domain, "doc_type": doc_type, "url": final,
                "link_url": href, "anchor_text": text, "score": score,
                "source": f"homepage_{label}", "source_timestamp": ts,
            })

    for doc_type in DOC_TYPES:
        if any(d == doc_type for d, _ in found):
            continue
        for path in PROBE_PATHS[doc_type]:
            hit = cdx_has_capture(client, f"{domain}/{path}")
            if hit:
                found[(doc_type, hit)] = {
                    "rank": rank, "domain": domain, "doc_type": doc_type, "url": hit,
                    "link_url": "", "anchor_text": "", "score": "",
                    "source": "path_probe", "source_timestamp": "",
                }
                break
        else:
            notes.append(f"no_{doc_type}")

    types = {d for d, _ in found}
    if not homepage_ok and not found:
        status = "no_homepage"
    elif types == set(DOC_TYPES):
        status = "both"
    elif types:
        status = "partial"
    else:
        status = "none_found"
    log = {"rank": rank, "domain": domain, "status": status,
           "n_urls": len(found), "notes": ";".join(notes)}
    return log, list(found.values())


# --------------------------------------------------------------------------- enumeration

def enumerate_url(client, url, collapse):
    """List HTTP-200 captures of `url` since FROM_YEAR. Returns rows or None on failure."""
    params = {"url": url, "from": FROM_YEAR, "filter": "statuscode:200",
              "fl": "timestamp,original,mimetype,statuscode,digest,length",
              "collapse": collapse, "output": "json"}
    resp = client.get(CDX, params)
    if resp is None or resp.status_code != 200:
        return None
    try:
        rows = resp.json()
    except ValueError:
        return None
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, r)) for r in rows[1:]]


# --------------------------------------------------------------------------- I/O

class CsvAppender:
    """Thread-safe append-only CSV writer that writes a header for new files."""

    def __init__(self, path, fields):
        self.path, self.fields = path, fields
        self.lock = threading.Lock()
        new = not path.exists() or path.stat().st_size == 0
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.DictWriter(self.f, fieldnames=fields, extrasaction="ignore")
        if new:
            self.w.writeheader()
            self.f.flush()

    def write(self, rows):
        with self.lock:
            self.w.writerows(rows)
            self.f.flush()

    def close(self):
        self.f.close()


def read_column(path, column):
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {r[column] for r in csv.DictReader(f)}


def read_rows(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------- main

def run_parallel(fn, items, workers, label):
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fn, item) for item in items]
        for fut in as_completed(futures):
            fut.result()
            done += 1
            if done % 25 == 0 or done == len(items):
                print(f"  {label}: {done}/{len(items)}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=1500, help="number of Tranco domains")
    ap.add_argument("--list-id", default=DEFAULT_LIST_ID, help="Tranco list id")
    ap.add_argument("--out-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rate", type=float, default=0.8,
                    help="max requests/second across all workers. A run at ~2/s with 8 "
                         "workers got our IP temporarily refused by IA; a run averaging "
                         "~0.4/s was fine. Lower this if you see 'throttled' messages")
    ap.add_argument("--collapse", default="timestamp:6",
                    help="CDX collapse: timestamp:6 = monthly (default), digest = every change")
    ap.add_argument("--skip-discovery", action="store_true")
    ap.add_argument("--skip-enumeration", action="store_true")
    args = ap.parse_args()

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    client = Client(args.rate)

    domains = load_tranco(client, args.list_id, args.top, out)
    print(f"Tranco {args.list_id}: {len(domains)} domains -> {out}")

    # ---- Stage 1+2: discovery
    log_path, urls_path = out / "discovery_log.csv", out / "policy_urls.csv"
    if not args.skip_discovery:
        done = read_column(log_path, "domain")
        todo = [d for d in domains if d[1] not in done]
        print(f"Discovery: {len(todo)} domains to do ({len(done)} already done)")
        log_w = CsvAppender(log_path, ["rank", "domain", "status", "n_urls", "notes"])
        url_w = CsvAppender(urls_path, ["rank", "domain", "doc_type", "url", "link_url",
                                        "anchor_text", "score", "source", "source_timestamp"])

        def do_discover(item):
            rank, domain = item
            try:
                log, rows = discover(client, rank, domain)
            except Exception as e:  # keep going; record the failure for re-run
                print(f"  ! {domain}: {e!r}")
                return
            url_w.write(rows)   # urls first, so a logged domain always has its urls
            log_w.write([log])

        run_parallel(do_discover, todo, args.workers, "discovered")
        log_w.close()
        url_w.close()

    # ---- Stage 3: enumeration
    if not args.skip_enumeration:
        policy_rows = read_rows(urls_path)
        done = read_column(out / "enumerated_urls.csv", "url")
        todo = list({r["url"]: r for r in policy_rows if r["url"] not in done}.values())
        print(f"Enumeration: {len(todo)} URLs to do ({len(done)} already done)")
        snap_w = CsvAppender(out / "snapshots.csv",
                             ["rank", "domain", "doc_type", "url", "timestamp", "original",
                              "mimetype", "statuscode", "digest", "length"])
        done_w = CsvAppender(out / "enumerated_urls.csv", ["url", "n_snapshots"])

        def do_enumerate(row):
            snaps = enumerate_url(client, row["url"], args.collapse)
            if snaps is None:
                print(f"  ! CDX failed for {row['url']} (will retry on re-run)")
                return
            meta = {k: row[k] for k in ("rank", "domain", "doc_type", "url")}
            snap_w.write([{**meta, **s} for s in snaps])
            done_w.write([{"url": row["url"], "n_snapshots": len(snaps)}])

        run_parallel(do_enumerate, todo, args.workers, "enumerated")
        snap_w.close()
        done_w.close()

    # ---- Summary
    logs = read_rows(log_path)
    counts = {}
    for r in logs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    n_snaps = sum(int(r["n_snapshots"]) for r in read_rows(out / "enumerated_urls.csv"))
    print("\nDiscovery status:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"Policy URLs: {len(read_rows(urls_path))}   Snapshots listed: {n_snaps}")


if __name__ == "__main__":
    main()
