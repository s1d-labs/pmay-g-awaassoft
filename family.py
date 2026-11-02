"""What this family scrapes: the PMAY-G AwaasSoft MIS reports on report.pmayg.dord.gov.in (Ministry of Rural Development).

FAMILY must equal the repo name (s1d-labs/<FAMILY>, case aside) and the family on scrape.s1d.me/runners.
One project per report (reports.json, made by discover.py): each report has its own columns, so each gets its own table in the
team database (scrape_hub.projects.<project>). One queue item per combination of the report's filters (financial year x scheme x
...); the task walks the states inside an item. The site only answers from India: proxy="IN" runs the work on GitHub runners
through the hub's Indian proxies.
"""
import itertools
import json
from pathlib import Path

HERE = Path(__file__).parent
FAMILY = json.loads((HERE / "scrape.json").read_text())["family"]
TARGET = "report.pmayg.dord.gov.in"
REPORTS = json.loads((HERE / "reports.json").read_text())


def items(rep):
    """One queue item per combination of the report's filters (no filters: one item, "all")."""
    dims = [{**d, "options": d["options"][::-1] if d["key"] == "fin_year" else d["options"]} for d in rep["dims"]]   # newest year first
    out = []
    for combo in itertools.product(*[d["options"] for d in dims]):
        out.append({"key": "~".join(v for v, _ in combo) or "all",
                    "payload": {"sel": {d["field"]: v for d, (v, _) in zip(dims, combo)},
                                "labels": {d["field"]: t for d, (_, t) in zip(dims, combo)}}})
    return out


PROJECTS = [
    {
        "name": f"{FAMILY}-{r['slug']}",            # 2-64 chars of a-z 0-9 - _
        "title": f"PMAY-G {r['code']} {r['title']}".replace("  ", " ")[:200],
        "task": "crawl",
        "target": TARGET,
        "env": {"SITE": f"https://{TARGET}", "REPORT": r["slug"]},
        "probe": f"{FAMILY}:{TARGET}",
        "unit_label": "query", "record_label": "row",
        # gentle: a government site, one Indian proxy per worker; the hub's back-off lowers it further if the site pushes back
        "pool": {"proxy": "IN", "threads": 2, "delay_ms": 500, "max_workers": 4},
        "items": items(r),
    }
    for r in sorted(REPORTS, key=lambda r: (not r["code"], r["code"], r["slug"]))
]

# health check the hub runs every minute (through an Indian proxy: the host is .gov.in); work holds while it fails
PROBES = [{"name": f"{FAMILY}:{TARGET}", "url": f"https://{TARGET}/netiay/newreport.aspx", "expect": "Physical Progress Reports"}]
