"""Clean and deduplicate the already-collected Wayback data. Offline only.

This script makes no network requests. It reads the raw files written by
enumerate_tranco.py and extract_policies.py and writes cleaned copies under
data/clean/. The raw files are never modified, so it is safe to re-run at any
time (e.g. again after collection finishes); every output is rebuilt from
scratch on each run.

What it does
------------
Tranco broad layer (data/tranco/):
  * Drops malformed rows (e.g. a half-written last line if the collector was
    killed mid-write), exact duplicates, and non-200 captures.
  * Canonicalizes policy URLs so variants of the same document are merged:
    http/https, www./no-www, host case, trailing slash, #fragments, default
    ports. Query strings are kept (they can select different pages, e.g.
    go.microsoft.com/fwlink/?LinkID=...).
  * Restores the many-to-many URL <-> domain mapping. enumerate_tranco.py
    enumerates each URL once and credits its snapshots to only one domain
    (e.g. google.ca and google.com share one privacy URL).
  * Merges captures of the same document across URL variants, then
    deduplicates by (document, month) and by (document, timestamp).
  * Collapses runs of consecutive identical content digests into *versions*
    (first_seen / last_seen / n_captures). A version is a distinct text to
    fetch, diff and classify downstream.
  * Builds a fetch list with one capture per unique digest, the minimum set of
    pages the next stage needs to download.
  * Flags, but does not drop, suspect captures: non-HTML mimetypes (PDF,
    plain text) and tiny payloads (< 1 KB compressed, often redirect stubs).

AI-provider core layer (data/snapshot_index.csv, data/text/*.txt):
  * Deduplicates and sorts the snapshot index.
  * Normalizes extracted text: Unicode NFKC, removes zero-width/invisible
    characters, normalizes whitespace, rejoins lines broken at inline links
    (lines starting with punctuation), drops consecutive duplicate lines and
    a small list of UI boilerplate lines.

Outputs (data/clean/)
---------------------
  policy_urls.csv       domain, rank, doc_type, url, doc_id (deduped discovery rows)
  documents.csv         one row per canonical document: doc_id, doc_type(s), domains, URL variants, counts
  snapshots.csv         cleaned captures with doc_id, all domains and flags
  versions.csv          consecutive-digest runs per document (distinct versions)
  fetch_list.csv        one representative capture per unique digest
  provider_snapshot_index.csv   deduped AI-provider index
  text/<name>.txt       normalized provider texts
  report.json           counts for every cleaning step

Usage:
  python clean_dedup.py
  python clean_dedup.py --data-dir data --out-dir data/clean
"""

import argparse
import csv
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).parent

HTML_MIMETYPES = {"text/html", "application/xhtml+xml"}
SMALL_PAYLOAD = 1000  # CDX `length` is the compressed record size, in bytes
TIMESTAMP_RE = re.compile(r"^\d{14}$")
DIGEST_RE = re.compile(r"^[A-Z2-7]{32}$")  # base32 SHA-1, as the CDX API reports it

# Zero-width / invisible characters that survive get_text() (U+2060 appears in the OpenAI page).
INVISIBLE_RE = re.compile("[­​‌‍‎‏⁠⁡⁢⁣﻿]")
# Lines that are UI chrome, not policy text. Matched against the whole stripped line.
BOILERPLATE_LINES = {
    "select language", "skip to main content", "skip to content", "back to top",
    "menu", "close", "search", "share", "copy link", "print", "…", "...",
}
# A line starting with these continues the previous line (text split at an inline <a>/<b>).
CONTINUATION_RE = re.compile(r"^[.,;:!?)\]}%’”]")


# --------------------------------------------------------------------------- I/O

def read_csv(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def is_complete(row):
    """True if a DictReader row has every column and no extras (no truncated/garbled line)."""
    return None not in row and all(v is not None for v in row.values())


# --------------------------------------------------------------------------- URLs

def canonical_url(url):
    """Identity key for a policy document's URL.

    Lowercases scheme/host, drops `www.`, default ports, fragments and trailing
    slashes, and treats http/https as one. Keeps the query string. The Wayback
    CDX index already treats scheme and `www.` as equivalent, so captures
    enumerated under any of these variants are of the same resource.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit(("", host, path, parts.query, "")).lstrip("/")


# --------------------------------------------------------------------------- Tranco layer

def clean_tranco(data_dir, out_dir, report):
    tdir = data_dir / "tranco"
    ranks = {r["domain"]: int(r["rank"]) for r in read_csv(tdir / "tranco_list.csv")}

    # ---- policy URLs: (domain, doc_type, canonical url) is one discovery fact
    raw_policy = read_csv(tdir / "policy_urls.csv")
    policy, seen = [], set()
    for r in raw_policy:
        if not is_complete(r) or not r["url"]:
            continue
        doc_id = canonical_url(r["url"])
        key = (r["domain"], r["doc_type"], doc_id)
        if key in seen:
            continue
        seen.add(key)
        policy.append({**r, "doc_id": doc_id})
    report["policy_urls"] = {"raw_rows": len(raw_policy), "clean_rows": len(policy),
                             "dropped_duplicate_or_variant": len(raw_policy) - len(policy)}

    doc_domains = defaultdict(set)
    doc_types = defaultdict(set)
    doc_variants = defaultdict(set)
    for r in policy:
        doc_domains[r["doc_id"]].add(r["domain"])
        doc_types[r["doc_id"]].add(r["doc_type"])
        doc_variants[r["doc_id"]].add(r["url"])

    enumerated = {r["url"] for r in read_csv(tdir / "enumerated_urls.csv") if is_complete(r)}

    # ---- snapshots
    raw_snaps = read_csv(tdir / "snapshots.csv")
    counts = Counter()
    by_key = {}
    for r in raw_snaps:
        if not is_complete(r) or not TIMESTAMP_RE.match(r["timestamp"]) \
                or not DIGEST_RE.match(r["digest"]):
            counts["dropped_malformed"] += 1
            continue
        if r["statuscode"] != "200":
            counts["dropped_non_200"] += 1
            continue
        doc_id = canonical_url(r["url"])
        key = (doc_id, r["timestamp"])
        if key in by_key:
            counts["dropped_duplicate_capture"] += 1
            continue
        doc_variants[doc_id].add(r["url"])
        doc_types[doc_id].add(r["doc_type"])
        doc_domains[doc_id].add(r["domain"])
        by_key[key] = r

    # Collection is monthly (collapse=timestamp:6); merging URL variants can put
    # two captures in one month. Keep the earliest per (doc, month).
    per_month = {}
    for (doc_id, ts), r in sorted(by_key.items()):
        per_month.setdefault((doc_id, ts[:6]), (ts, r))
    counts["dropped_same_month_after_merge"] = len(by_key) - len(per_month)

    snaps = []
    for (doc_id, _), (ts, r) in sorted(per_month.items()):
        doms = sorted(doc_domains[doc_id], key=lambda d: (ranks.get(d, 10**9), d))
        mime = r["mimetype"]
        length = int(r["length"]) if r["length"].isdigit() else None
        flags = []
        if mime not in HTML_MIMETYPES:
            flags.append(f"non_html:{mime}")
        if length is not None and length < SMALL_PAYLOAD:
            flags.append("small_payload")
        snaps.append({
            "doc_id": doc_id,
            "doc_type": "|".join(sorted(doc_types[doc_id])),
            "domains": "|".join(doms),
            "best_rank": ranks.get(doms[0], "") if doms else "",
            "url": r["url"], "timestamp": ts, "month": f"{ts[:4]}-{ts[4:6]}",
            "original": r["original"], "mimetype": mime, "digest": r["digest"],
            "length": r["length"], "flags": ";".join(flags),
        })
    counts["raw_rows"] = len(raw_snaps)
    counts["clean_rows"] = len(snaps)
    counts["flagged_non_html"] = sum("non_html" in s["flags"] for s in snaps)
    counts["flagged_small_payload"] = sum("small_payload" in s["flags"] for s in snaps)
    report["snapshots"] = dict(counts)

    # ---- versions: runs of identical consecutive digests within a document
    versions = []
    by_doc = defaultdict(list)
    for s in snaps:
        by_doc[s["doc_id"]].append(s)
    for doc_id, caps in by_doc.items():
        n = 0
        for s in caps:  # already sorted by timestamp
            if versions and versions[-1]["doc_id"] == doc_id and versions[-1]["digest"] == s["digest"]:
                v = versions[-1]
                v["last_seen"] = s["timestamp"]
                v["n_captures"] += 1
                continue
            n += 1
            versions.append({
                "doc_id": doc_id, "version": n, "doc_type": s["doc_type"], "domains": s["domains"],
                "digest": s["digest"], "first_seen": s["timestamp"], "last_seen": s["timestamp"],
                "n_captures": 1, "fetch_timestamp": s["timestamp"], "fetch_original": s["original"],
                "mimetype": s["mimetype"], "flags": s["flags"],
            })
    # A digest that comes back after a different one (A -> B -> A) is a revert;
    # it stays a separate version for diffing but needs no new fetch.
    seen_digest = set()
    for v in versions:
        v["is_revert"] = int((v["doc_id"], v["digest"]) in seen_digest)
        seen_digest.add((v["doc_id"], v["digest"]))

    # ---- fetch list: one capture per unique digest (content is identical)
    fetch = {}
    for v in versions:
        f = fetch.setdefault(v["digest"], {
            "digest": v["digest"], "timestamp": v["fetch_timestamp"], "original": v["fetch_original"],
            "wayback_url": f"https://web.archive.org/web/{v['fetch_timestamp']}id_/{v['fetch_original']}",
            "mimetype": v["mimetype"], "doc_ids": set(), "flags": v["flags"],
        })
        f["doc_ids"].add(v["doc_id"])
    fetch_rows = [{**f, "doc_ids": "|".join(sorted(f["doc_ids"]))}
                  for f in sorted(fetch.values(), key=lambda f: (f["timestamp"], f["digest"]))]

    # ---- documents
    snap_count = Counter(s["doc_id"] for s in snaps)
    ver_count = Counter(v["doc_id"] for v in versions)
    first = {}
    last = {}
    for s in snaps:
        first.setdefault(s["doc_id"], s["timestamp"])
        last[s["doc_id"]] = s["timestamp"]
    docs = []
    for doc_id in sorted(doc_domains, key=lambda d: (min(ranks.get(x, 10**9) for x in doc_domains[d]), d)):
        doms = sorted(doc_domains[doc_id], key=lambda d: (ranks.get(d, 10**9), d))
        variants = sorted(doc_variants[doc_id])
        docs.append({
            "doc_id": doc_id, "doc_type": "|".join(sorted(doc_types[doc_id])),
            "domains": "|".join(doms), "n_domains": len(doms), "best_rank": ranks.get(doms[0], ""),
            "url_variants": "|".join(variants), "n_url_variants": len(variants),
            "enumerated": int(any(u in enumerated for u in variants)),
            "n_snapshots": snap_count[doc_id], "n_versions": ver_count[doc_id],
            "first_seen": first.get(doc_id, ""), "last_seen": last.get(doc_id, ""),
        })

    report["documents"] = {
        "canonical_documents": len(docs),
        "enumerated": sum(d["enumerated"] for d in docs),
        "not_yet_enumerated": sum(1 for d in docs if not d["enumerated"]),
        "enumerated_with_zero_captures": sum(1 for d in docs if d["enumerated"] and not d["n_snapshots"]),
        "shared_by_multiple_domains": sum(1 for d in docs if d["n_domains"] > 1),
        "merged_url_variants": sum(1 for d in docs if d["n_url_variants"] > 1),
        "ambiguous_doc_type": sum(1 for d in docs if "|" in d["doc_type"]),
    }
    report["versions"] = {
        "distinct_versions": len(versions),
        "reverts": sum(v["is_revert"] for v in versions),
        "unique_digests_to_fetch": len(fetch_rows),
        "captures_saved_by_dedup": len(snaps) - len(fetch_rows),
    }

    out = out_dir
    write_csv(out / "policy_urls.csv",
              ["rank", "domain", "doc_type", "url", "doc_id", "link_url", "anchor_text",
               "score", "source", "source_timestamp"],
              sorted(policy, key=lambda r: (int(r["rank"]), r["doc_type"], r["url"])))
    write_csv(out / "documents.csv", list(docs[0].keys()) if docs else ["doc_id"], docs)
    write_csv(out / "snapshots.csv", list(snaps[0].keys()) if snaps else ["doc_id"], snaps)
    write_csv(out / "versions.csv", list(versions[0].keys()) if versions else ["doc_id"], versions)
    write_csv(out / "fetch_list.csv",
              ["digest", "timestamp", "original", "wayback_url", "mimetype", "doc_ids", "flags"],
              fetch_rows)


# --------------------------------------------------------------------------- provider layer

def clean_text(text):
    text = unicodedata.normalize("NFKC", text)
    text = INVISIBLE_RE.sub("", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line.lower() in BOILERPLATE_LINES:
            continue
        if lines and CONTINUATION_RE.match(line):
            lines[-1] += line
            continue
        if lines and line == lines[-1]:
            continue
        lines.append(line)
    # Rejoining can leave "word .Next"; restore the space after sentence punctuation.
    return "\n".join(re.sub(r"([.!?;:])(?=[A-Z])", r"\1 ", l) for l in lines) + "\n"


def clean_providers(data_dir, out_dir, report):
    raw = read_csv(data_dir / "snapshot_index.csv")
    rows = {}
    for r in raw:
        if not is_complete(r) or not TIMESTAMP_RE.match(r["timestamp"]):
            continue
        rows.setdefault((r["provider"], r["timestamp"], r["original"].strip()), r)
    clean = [{"provider": p, "timestamp": t, "original": o, "canonical_url": canonical_url(o)}
             for p, t, o in sorted(rows)]
    write_csv(out_dir / "provider_snapshot_index.csv",
              ["provider", "timestamp", "original", "canonical_url"], clean)
    report["provider_snapshot_index"] = {"raw_rows": len(raw), "clean_rows": len(clean),
                                         "by_provider": dict(Counter(r["provider"] for r in clean))}

    text_report = {}
    for path in sorted((data_dir / "text").glob("*.txt")):
        before = path.read_text(encoding="utf-8")
        after = clean_text(before)
        (out_dir / "text").mkdir(parents=True, exist_ok=True)
        (out_dir / "text" / path.name).write_text(after, encoding="utf-8")
        text_report[path.name] = {"lines_before": len(before.splitlines()),
                                  "lines_after": len(after.splitlines()),
                                  "words": len(after.split())}
    report["provider_texts"] = text_report


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=HERE / "data")
    ap.add_argument("--out-dir", type=Path, default=HERE / "data" / "clean")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    report = {}
    clean_tranco(args.data_dir, args.out_dir, report)
    clean_providers(args.data_dir, args.out_dir, report)
    (args.out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"\nWrote cleaned data to {args.out_dir}")


if __name__ == "__main__":
    main()
