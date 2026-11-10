"""Fetch archived policy pages, extract their text, and deduplicate versions by text.

Why: clean_dedup.py groups captures by the Wayback content digest, but the digest
is a hash of the raw HTML, which changes whenever *anything* on the page changes
(scripts, CSRF tokens, ads, "© 2025"). Most documents show a "new version" every
month even when the policy is unchanged. Comparing the extracted, normalized
policy text instead gives the real version history.

Two stages:

  fetch   Download each page in data/clean/fetch_list.csv (one per unique digest,
          Tranco layer) and data/clean/provider_snapshot_index.csv (AI-provider
          layer) in raw `id_` mode, extract the main text, normalize it with
          clean_dedup.clean_text, and save it gzipped. Resumable and shardable.

  dedup   Offline. Join the fetched texts back onto every capture and collapse
          consecutive captures with identical text into versions. Works on
          partial data, so it can be run while fetching is still going.

Fetch outputs (data/pages/, git-ignored, large):
  text/<k[:2]>/<key>.txt.gz        extracted text, one file per unique page
  fetch_log[_<i>of<n>].csv         key, status, http_status, n_words, text_hash, ...

Dedup outputs (data/clean/):
  text_versions.csv     one row per (document, text version)
  text_report.json      fetch coverage + digest-vs-text version counts

Usage:
  python fetch_text.py fetch                    # everything, from one machine
  python fetch_text.py fetch --shard 1/4        # teammate 1 of 4 (own IP each)
  python fetch_text.py fetch --limit 50         # quick test
  python fetch_text.py dedup                    # rebuild text_versions.csv

Sharding splits pages by a hash of their key, so every machine gets a fixed,
disjoint slice. Copy everyone's data/pages/ folder together before `dedup`.
"""

import argparse
import csv
import gzip
import hashlib
import json
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

from clean_dedup import clean_text
from enumerate_tranco import Client, CsvAppender

HERE = Path(__file__).parent
CLEAN_DIR = HERE / "data" / "clean"
PAGES_DIR = HERE / "data" / "pages"

LOG_FIELDS = ["key", "layer", "doc_ids", "timestamp", "original", "status",
              "http_status", "content_type", "n_words", "text_hash", "note"]

# Elements that are page chrome, not policy text.
DROP_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "form",
             "nav", "header", "footer", "aside", "button"]
# Banner/widget containers only. Deliberately not bare "cookie": privacy policies
# often have a real <section id="cookies"> that must be kept.
CHROME_ATTR = re.compile(r"cookie[-_]?(banner|consent|notice|bar|popup)|consent[-_]?(banner|manager)|"
                         r"onetrust|truste-|gdpr-banner|newsletter|skip-link|breadcrumb|"
                         r"site-header|site-footer|navbar", re.I)

MIN_WORDS = 80  # below this the "policy" is almost certainly a stub/error page
# Pages that are archive/CDN/bot-wall artifacts rather than the policy itself.
SUSPECT_RE = re.compile(
    r"just a moment|checking your browser|enable javascript|javascript is (disabled|required)|"
    r"access denied|attention required|are you a robot|captcha|page not found|"
    r"404 not found|this page (isn't|is not) available|temporarily unavailable|"
    r"you need to enable javascript", re.I)


# --------------------------------------------------------------------------- extraction

def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")  # bytes in: bs4 sniffs the encoding
    for tag in soup(DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed:  # removed as a child of an earlier match
            continue
        attrs = " ".join([tag.get("id") or "", *(tag.get("class") or [])])
        if (attrs.strip() and CHROME_ATTR.search(attrs)) or tag.get("aria-hidden") == "true" \
                or tag.get("role") in ("navigation", "banner", "contentinfo", "dialog"):
            tag.decompose()
    candidates = [soup.find("main"), soup.find("article"), soup.find(attrs={"role": "main"})]
    root = max((c for c in candidates if c is not None),
               key=lambda c: len(c.get_text(" ", strip=True)), default=None)
    # Some sites wrap only a hero section in <main>; fall back to body if it is tiny.
    body = soup.body or soup
    if root is None or len(root.get_text(" ", strip=True)) < 0.3 * len(body.get_text(" ", strip=True)):
        root = body
    return clean_text(root.get_text("\n"))


def text_hash(text):
    """Hash of the text with case and whitespace normalized away."""
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def classify(text):
    n = len(text.split())
    if n == 0:
        return "empty", n
    if SUSPECT_RE.search(text[:2000]) and n < 400:
        return "suspect", n
    if n < MIN_WORDS:
        return "too_short", n
    return "ok", n


# --------------------------------------------------------------------------- tasks

def provider_key(row):
    h = hashlib.sha1(row["original"].encode()).hexdigest()[:8]
    return f"p_{row['provider']}_{row['timestamp']}_{h}"


def load_tasks(clean_dir):
    tasks = []
    with open(clean_dir / "fetch_list.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            tasks.append({"key": r["digest"], "layer": "tranco", "doc_ids": r["doc_ids"],
                          "timestamp": r["timestamp"], "original": r["original"],
                          "mimetype": r["mimetype"]})
    path = clean_dir / "provider_snapshot_index.csv"
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                tasks.append({"key": provider_key(r), "layer": "provider",
                              "doc_ids": r["canonical_url"], "timestamp": r["timestamp"],
                              "original": r["original"], "mimetype": "text/html"})
    return tasks


def in_shard(key, shard):
    i, n = shard
    return int(hashlib.md5(key.encode()).hexdigest(), 16) % n == i - 1


def text_path(pages_dir, key):
    return pages_dir / "text" / key[:2] / f"{key}.txt.gz"


def log_paths(pages_dir):
    return sorted(pages_dir.glob("fetch_log*.csv"))


def read_logs(pages_dir):
    """Latest log row per key across all shard logs (a re-run can retry failures)."""
    rows = {}
    for path in log_paths(pages_dir):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("key") and r.get("status"):
                    rows[r["key"]] = r
    return rows


# --------------------------------------------------------------------------- fetch

RETRYABLE = {"failed", "http_error"}


def fetch_one(client, pages_dir, task):
    row = {k: task[k] for k in ("key", "layer", "doc_ids", "timestamp", "original")}
    if task["mimetype"] == "application/pdf":
        return {**row, "status": "non_html", "note": "pdf skipped"}
    url = f"https://web.archive.org/web/{task['timestamp']}id_/{task['original']}"
    resp = client.get(url)
    if resp is None:
        return {**row, "status": "failed", "note": "no response after retries"}
    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    row.update(http_status=resp.status_code, content_type=ctype)
    if resp.status_code != 200:
        return {**row, "status": "http_error"}
    if ctype == "application/pdf" or resp.content[:5] == b"%PDF-":
        return {**row, "status": "non_html", "note": "pdf"}
    if ctype == "text/plain":
        text = clean_text(resp.content.decode(resp.encoding or "utf-8", errors="replace"))
    else:
        try:
            text = extract_text(resp.content)
        except Exception as e:  # malformed markup; keep going
            return {**row, "status": "parse_error", "note": repr(e)[:200]}
    status, n_words = classify(text)
    path = text_path(pages_dir, task["key"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)
    return {**row, "status": status, "n_words": n_words, "text_hash": text_hash(text)}


def run_fetch(args):
    pages_dir = args.pages_dir
    pages_dir.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks(args.clean_dir)
    if args.layer != "all":
        tasks = [t for t in tasks if t["layer"] == args.layer]
    if args.shard:
        tasks = [t for t in tasks if in_shard(t["key"], args.shard)]
    done = read_logs(pages_dir)
    todo = [t for t in tasks
            if t["key"] not in done or (args.retry_failed and done[t["key"]]["status"] in RETRYABLE)]
    if args.limit:
        todo = todo[:args.limit]
    shard_name = f"_{args.shard[0]}of{args.shard[1]}" if args.shard else ""
    log_path = pages_dir / f"fetch_log{shard_name}.csv"
    hours = len(todo) / args.rate / 3600
    print(f"{len(tasks)} pages in scope, {len(tasks) - len(todo)} already done, "
          f"{len(todo)} to fetch (~{hours:.1f} h at {args.rate} req/s) -> {log_path}")

    client = Client(args.rate)
    log = CsvAppender(log_path, LOG_FIELDS)
    counts, lock = Counter(), threading.Lock()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_one, client, pages_dir, t) for t in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            row = fut.result()
            log.write([row])
            with lock:
                counts[row["status"]] += 1
            if i % 100 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}  " + ", ".join(f"{k}={v}" for k, v in counts.most_common()),
                      flush=True)
    log.close()


# --------------------------------------------------------------------------- dedup

def run_dedup(args):
    logs = read_logs(args.pages_dir)
    clean = args.clean_dir

    # Every capture, in time order per document, with the key of its fetched page.
    captures = defaultdict(list)  # doc_id -> [(timestamp, key, layer, label)]
    with open(clean / "snapshots.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            captures[r["doc_id"]].append((r["timestamp"], r["digest"], "tranco", r["doc_type"]))
    path = clean / "provider_snapshot_index.csv"
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                captures[r["canonical_url"]].append(
                    (r["timestamp"], provider_key(r), "provider", r["provider"]))

    versions, stats = [], Counter()
    digest_versions = Counter()  # per layer, for comparison with the text versions
    for doc_id, caps in captures.items():
        caps.sort()
        prev_digest = None
        current = None
        seen_hashes = set()
        n = 0
        for ts, key, layer, label in caps:
            stats[f"{layer}_captures"] += 1
            if key != prev_digest:
                digest_versions[layer] += 1
                prev_digest = key
            log = logs.get(key)
            if log is None:
                stats[f"{layer}_not_fetched"] += 1
                continue
            if log["status"] != "ok":
                # Error/stub pages are gaps, not versions: skipping them stops an
                # A -> error -> A sequence from counting as two policy changes.
                stats[f"{layer}_unusable_{log['status']}"] += 1
                continue
            stats[f"{layer}_usable"] += 1
            h = log["text_hash"]
            if current and current["text_hash"] == h:
                current["last_seen"] = ts
                current["n_captures"] += 1
                continue
            n += 1
            current = {"doc_id": doc_id, "layer": layer, "label": label, "version": n,
                       "text_hash": h, "first_seen": ts, "last_seen": ts, "n_captures": 1,
                       "text_key": key, "n_words": log["n_words"],
                       "is_revert": int(h in seen_hashes)}
            seen_hashes.add(h)
            versions.append(current)

    report = {"fetch_status": dict(Counter(r["status"] for r in logs.values())),
              "captures": {k: v for k, v in sorted(stats.items())}}
    for layer in ("tranco", "provider"):
        lv = [v for v in versions if v["layer"] == layer]
        report[f"{layer}_versions"] = {
            "documents_with_text": len({v["doc_id"] for v in lv}),
            "digest_versions_all_captures": digest_versions[layer],
            "text_versions": len(lv),
            "text_changes": len(lv) - len({v["doc_id"] for v in lv}),
            "reverts": sum(v["is_revert"] for v in lv),
        }
    fields = ["doc_id", "layer", "label", "version", "text_hash", "first_seen", "last_seen",
              "n_captures", "text_key", "n_words", "is_revert"]
    with open(clean / "text_versions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(versions)
    (clean / "text_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


# --------------------------------------------------------------------------- main

def parse_shard(s):
    i, n = (int(x) for x in s.split("/"))
    if not 1 <= i <= n:
        raise argparse.ArgumentTypeError("shard must look like 2/4")
    return i, n


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["fetch", "dedup"])
    ap.add_argument("--clean-dir", type=Path, default=CLEAN_DIR)
    ap.add_argument("--pages-dir", type=Path, default=PAGES_DIR)
    ap.add_argument("--shard", type=parse_shard, help="i/n: fetch only slice i of n")
    ap.add_argument("--layer", choices=["all", "tranco", "provider"], default="all")
    ap.add_argument("--limit", type=int, help="fetch at most this many pages (testing)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-fetch pages whose last attempt failed or got an HTTP error")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rate", type=float, default=0.8,
                    help="max requests/second across all workers (see enumerate_tranco.py)")
    args = ap.parse_args()
    if args.stage == "fetch":
        run_fetch(args)
    else:
        run_dedup(args)


if __name__ == "__main__":
    main()
