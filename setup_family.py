"""Put this family's projects in the pool and register its health checks. Run by the Setup workflow (GitHub Actions: the hub
trusts this repo, s1d-labs/<family>, through its OIDC token). Safe to run again: queue items that exist are kept.
SEED_LIMIT=N (the workflow's seed_limit input) seeds only the first N items of each report: a first pool run to watch.
RESET_FAILED=true (reset_failed input) puts failed items back in the queue, e.g. after a fix in crawl.py."""
import os

from scrape_hub_client import register_project, seed_queue, set_pool, set_probes

from family import FAMILY, PROBES, PROJECTS

LIMIT = int(os.environ.get("SEED_LIMIT") or 0)
RESET = os.environ.get("RESET_FAILED", "").lower() == "true"

print("health checks:", set_probes(FAMILY, PROBES))
for p in PROJECTS:
    register_project(p["name"], title=p["title"], unit_label=p["unit_label"], record_label=p["record_label"],
                     expected_units=len(p["items"]), expected_records=sum(i["payload"].get("expected", 0) for i in p["items"]))
    set_pool(p["name"], FAMILY, p["task"], target=p["target"], env=p["env"], probe=p["probe"], **p["pool"])
    print(p["name"], "queue:", seed_queue(p["name"], p["items"][:LIMIT] if LIMIT else p["items"], reset_failed=RESET))
