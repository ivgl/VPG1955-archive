#!/usr/bin/env python3
"""Read-only VPI1955 turn intake/release gate. Never changes state or phases.

Run with BOTH private repositories checked out locally:
  python3 scripts/verify_turn_readiness.py --state ../VPG1955-state --archive . --gate intake
  python3 scripts/verify_turn_readiness.py --state ../VPG1955-state --archive . --gate lock
  python3 scripts/verify_turn_readiness.py --state ../VPG1955-state --archive . --gate final

Intake PASS is NOT a confirmation that every Discord post was submitted.
LOCK/FINAL require separate administrator-approved checkpoint documents.
"""
import argparse
import hashlib
import json
from pathlib import Path


COUNTRIES = ("USA", "URS", "GBR", "FRA", "FRG", "JPN", "CHN", "EGY", "IND", "ISR", "ITA", "YUG")
PHASES = (
    "PREPARE", "RECEIVING", "LOCKED", "IN_REVIEW", "ADMIN_APPROVED",
    "VERDICTS_PUBLISHED", "APPEALS_OPEN", "APPEALS_REVIEW",
    "FINAL_APPROVED", "COMMITTED",
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def document(path, failures):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        failures.append(f"Missing/invalid JSON {path}: {exc}")
        return {}


def local_file(root, name, failures):
    if not isinstance(name, str) or not name:
        failures.append(f"Invalid source path: {name!r}")
        return None
    file = (root / name).resolve()
    if not file.is_relative_to(root.resolve()) or not file.is_file():
        failures.append(f"Missing/unsafe source path: {name}")
        return None
    return file


def source_matches(root, name, entry, failures):
    path = local_file(root, name, failures)
    if path is None:
        return False
    expected_hash = entry.get("sha256")
    expected_bytes = entry.get("bytes")
    if not isinstance(expected_hash, str) or not isinstance(expected_bytes, int):
        failures.append(f"Missing source SHA-256/bytes: {name}")
        return False
    if sha256(path) != expected_hash or path.stat().st_size != expected_bytes:
        failures.append(f"Source checksum/size mismatch: {name}")
        return False
    return True


def same_ids(found, expected, key, failures):
    if not isinstance(found, list) or len(set(found)) != len(found):
        failures.append(f"{key}: not a unique ID list")
    elif set(found) != set(expected):
        failures.append(f"{key}: mismatches current archived IDs")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", required=True, type=Path, help="Local private VPG1955-state checkout")
    ap.add_argument("--archive", required=True, type=Path, help="Local private VPG1955-archive checkout")
    ap.add_argument("--gate", choices=("intake", "lock", "final"), default="intake")
    args = ap.parse_args()
    state, archive = args.state.resolve(), args.archive.resolve()
    failures, warnings = [], []

    head = document(state / "HEAD.json", failures)
    if head.get("campaign_id") != "VPI1955_MAIN" or head.get("phase") not in PHASES:
        failures.append("Unexpected canonical campaign or HEAD phase")
    year = head.get("year")
    if not isinstance(year, int):
        failures.append("No integer year in HEAD")
        year = -1
    manifest_name = head.get("manifest_path")
    if not isinstance(manifest_name, str):
        failures.append("HEAD has no manifest path")
        manifest_name = ""
    mf = local_file(state, manifest_name, failures) if manifest_name else None
    if mf and sha256(mf) != head.get("manifest_sha256"):
        failures.append("HEAD/manifest SHA-256 mismatch")
    manifest = document(mf, failures) if mf else {}
    if manifest.get("year") != year or manifest.get("rules_version") != head.get("rules_version"):
        failures.append("HEAD/manifest year or rules version mismatch")
    if manifest.get("campaign_id") != head.get("campaign_id"):
        failures.append("HEAD/manifest campaign mismatch")
    for entry in manifest.get("files", []):
        if isinstance(entry, dict):
            source_matches(state, entry.get("path"), entry, failures)
    if not manifest.get("files"):
        failures.append("Manifest has no payload inventory")

    refs = manifest.get("country_refs", {})
    if set(refs.keys()) != set(COUNTRIES):
        failures.append("Manifest does not reference exactly 12 countries")
    for country in COUNTRIES:
        if country not in refs:
            continue
        fp = local_file(state, refs[country], failures)
        if fp is None:
            continue
        obj = document(fp, failures)
        if obj.get("id") != country:
            failures.append(f"Country ID mismatch: {country}")
        s = obj.get("economy", {}).get("stats", {})
        try:
            if abs(s["fiscal_revenue_total_billion"] - s["fiscal_spending_total_billion"] - s["fiscal_budget_balance_billion"]) > .001:
                failures.append(f"{country}: budget balance broken")
            func = sum(s[k] for k in (
                "fiscal_spend_social_billion", "fiscal_spend_economy_billion",
                "fiscal_spend_defense_billion", "fiscal_spend_admin_billion",
                "fiscal_spend_other_billion"))
            service = s["fiscal_debt_total_billion"] * s["fiscal_interest_rate_pct"] / 100
            if abs(func + service - s["fiscal_spending_total_billion"]) > .001:
                failures.append(f"{country}: spending/debt-service identity broken")
        except (KeyError, TypeError, ValueError) as exc:
            failures.append(f"{country}: unavailable fiscal fields {exc}")

    turn = archive / "turns" / str(year)
    posts = document(turn / "posts" / "index.json", failures)
    if posts.get("year") != year or posts.get("campaign_id") != head.get("campaign_id"):
        failures.append("Post register campaign/year mismatch")
    pentries = posts.get("entries", [])
    postids, postpaths, discordids = [], set(), set()
    if not isinstance(pentries, list):
        failures.append("Post index entries must be a list")
        pentries = []
    for post in pentries:
        pid, name, mid = post.get("intake_id"), post.get("content_path"), post.get("discord_message_id")
        if not pid or pid in postids:
            failures.append(f"Missing/duplicate intake ID {pid!r}")
        postids.append(pid)
        if post.get("country_id") not in COUNTRIES:
            failures.append(f"Unknown country on post {pid}")
        if name in postpaths:
            failures.append(f"Duplicate source path {name}")
        postpaths.add(name)
        if mid:
            if mid in discordids:
                failures.append(f"Duplicate Discord message ID {mid}")
            discordids.add(mid)
        source_matches(archive, name, post, failures)
        if not post.get("game_original_accepted_by_administrator"):
            warnings.append(f"Post {pid}: admin-original acceptance not recorded")
        if not post.get("actor_id"):
            warnings.append(f"Post {pid}: registered actor ID not resolved (not an intake blocker)")
    originals_dir = turn / "posts" / "originals"
    if originals_dir.is_dir():
        orphans = {str(f.relative_to(archive)) for f in originals_dir.rglob("*") if f.is_file()} - postpaths
        if orphans:
            failures.append(f"Unindexed original post files: {len(orphans)}")
    if not pentries:
        warnings.append("No accepted posts in archive; no claim of Discord completeness")

    events = document(turn / "events" / "index.json", failures)
    if events.get("year") != year or events.get("campaign_id") != head.get("campaign_id"):
        failures.append("Event register campaign/year mismatch")
    evlist, eventids = events.get("events", []), []
    if not isinstance(evlist, list):
        failures.append("Event index must list events")
        evlist = []
    for event in evlist:
        eid, rel = event.get("id"), event.get("path")
        if not eid or eid in eventids:
            failures.append(f"Missing/duplicate event ID: {eid}")
        eventids.append(eid)
        path = local_file(turn, rel, failures)
        if path is None:
            continue
        if event.get("sha256") is not None:
            source_matches(turn, rel, event, failures)
        if event.get("status") in ("PUBLISHED", "PUBLISHED_ADMIN_CONFIRMED"):
            if event.get("sha256") is None:
                failures.append(f"Published event without checksum: {eid}")
    for missing in events.get("pending_sources", []):
        warnings.append(f"Unresolved event sources: {missing.get('set', 'UNKNOWN')}")
    for row in events.get("archived_sets", []):
        if row.get("archived_count") != row.get("expected_count"):
            failures.append(f"Incomplete declared event set: {row.get('id')}")

    if args.gate in ("lock", "final"):
        if head.get("phase") not in PHASES[2:]:
            failures.append("LOCK gate requested before administrative phase change")
        frozen = document(turn / "checkpoints" / "locked.json", failures)
        if frozen.get("year") != year or frozen.get("admin_confirmed") is not True:
            failures.append("LOCK checkpoint has not been confirmed by administrator")
        same_ids(frozen.get("post_intake_ids"), postids, "Frozen post IDs", failures)
        same_ids(frozen.get("published_event_ids"),
                 [e["id"] for e in evlist if e.get("status") in ("PUBLISHED", "PUBLISHED_ADMIN_CONFIRMED")],
                 "Frozen published event IDs", failures)
    if args.gate == "final":
        if head.get("phase") not in PHASES[8:]:
            failures.append("FINAL gate requested before official final approval")
        approved = document(turn / "checkpoints" / "final_approved.json", failures)
        if approved.get("year") != year or approved.get("admin_confirmed") is not True:
            failures.append("FINAL checkpoint has not been approved by administrator")
        same_ids(approved.get("reviewed_post_intake_ids"), postids, "Adjudicated post IDs", failures)
        if approved.get("appeals_resolved") is not True or approved.get("treaties_and_processes_reviewed") is not True:
            failures.append("Appeals or treaty/process obligations not confirmed")
        effects = approved.get("approved_effect_ids")
        if not isinstance(effects, list) or len(effects) != len(set(effects)):
            failures.append("Missing or duplicate approved effect IDs")
        warnings.append("Human review of verdicts, approval and public sync is still required")

    print(f"GATE={args.gate} YEAR={year} HEAD_PHASE={head.get('phase')}")
    print(f"INDEXED: {len(postids)} posts; {len(evlist)} events; 12 canonical countries expected")
    for message in warnings:
        print("WARN:", message)
    for message in failures:
        print("BLOCK:", message)
    if failures:
        print("BLOCKED: Read-only gate failed; do not advance the turn.")
        return 1
    print(f"PASS: {args.gate} mechanical checks. This does NOT approve gameplay or verify Discord completeness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
