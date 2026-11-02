"""Build reports.json: the catalogue of PMAY-G MIS reports this family scrapes, with each report's filters and their options.

The site only answers from India, and setup_family.py runs on GitHub (US), so the catalogue is made here and committed:
    myproxy run python discover.py            # ~120 requests, one at a time

For every report linked from newreport.aspx it keeps the ones without a captcha that have a filter form, reads their dropdowns
(the state dropdown is iterated by crawl.py inside each queue item; every other enabled dropdown is a queue dimension), then submits
the form once (latest year, All States) to check that a result grid comes back.
"""
import json
import re
import sys
import time
from pathlib import Path

import crawl

INDEX = crawl.SITE + "/netiay/newreport.aspx"
# Power BI embeds, the index, the map of inspection photos; F.2 debit accounts (officials' phone numbers, no grid); F.8 (its form
# has no Submit button on the site, every query comes back empty)
SKIP = re.compile(r"/pbi/|newreport\.aspx|GISReport/|DebitAccountDetailsReport|FTO_Pending_At_Bank\.aspx", re.I)
# pages the index doesn't link: A.2's "Special Projects" years redirect to them
EXTRA = ["AwaasPlus2024Reports/AwaasPlus2024_PhyProgReport_SpecialProject.aspx", "PhysicalProgressReport/PhysicalProgressReport_SpecialProject.aspx"]
KEYS = {"ddlFinYear": "fin_year", "ddlScheme": "scheme"}


RESERVED = {"report_code", "report", "level", "state_code", "state", "district", "name", "as_on"}


def key_for(field):
    short = field.rsplit("$", 1)[-1]
    if short in KEYS:
        return KEYS[short]
    short = re.sub(r"^(ddl|drp|rbl|rdbl)_?", "", short)
    key = crawl.snake(re.sub(r"(?<=[a-z])(?=[A-Z])", "_", short))
    return "filter_" + key if key in RESERVED else key


def title_of(page):
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    m = re.search(r"\b([A-Z]{1,2})\.\s?(\d{1,2})\.?\s+([A-Za-z][^<]{4,160})", body)
    if not m:
        t = re.search(r"<title>([^<]*)</title>", page)
        return "", " ".join((t.group(1) if t else "").split())
    return f"{m.group(1)}.{m.group(2)}", " ".join(crawl.html.unescape(m.group(3)).split()).strip(" |")


def main():
    hub = crawl.LocalHub()
    f = crawl.Fetcher(hub)
    code, index = f._do("GET", INDEX)
    if code != 200:
        sys.exit(f"index: HTTP {code} (needs an Indian IP: myproxy run python discover.py)")
    links = {}                                      # path -> the link's text on the index (the title when the page has none)
    for path, text in re.findall(r'href="https://report\.pmayg\.dord\.gov\.in/netiay/([A-Za-z0-9_/]+\.aspx)"[^>]*>(.*?)</a>', index, re.S):
        links.setdefault(path, " ".join(crawl.html.unescape(re.sub(r"<[^>]+>", " ", text)).split()))
    paths = sorted(set(re.findall(r'https://report\.pmayg\.dord\.gov\.in/netiay/([A-Za-z0-9_/]+\.aspx)', index)) | set(EXTRA))
    out, slugs = [], set()
    for path in paths:
        if SKIP.search(path):
            continue
        url = crawl.SITE + "/netiay/" + path
        code, page = f._do("GET", url)
        time.sleep(0.5)
        if code != 200 or "cannot be found" in page:
            print(f"skip {path}: HTTP {code}", file=sys.stderr)
            continue
        if re.search(r"captcha", page, re.I):
            print(f"skip {path}: captcha", file=sys.stderr)
            continue
        selects = re.findall(r"<select\b([^>]*)>", page)
        if not selects:
            print(f"skip {path}: no filters", file=sys.stderr)
            continue
        rcode, title = title_of(page)
        if not rcode and links.get(path):
            title = links[path]
        title = re.sub(r"\s*\|\s*(Pradhan Mantri|MIS Report).*$", "", title)
        dims, states, all_states = [], None, None
        for attrs in selects:
            field = crawl.html.unescape(re.search(r'name="([^"]+)"', attrs).group(1))
            if "disabled" in attrs:
                continue
            opts = [(v, t) for v, t in crawl.select_options(page, field) if v != "" and not re.search(r"select", t, re.I)]
            opts = [o for i, o in enumerate(opts) if o[0] not in {v for v, _ in opts[:i]}]     # some lists repeat a code
            if field == crawl.STATE_FIELD:
                all_states = next((v for v, t in opts if re.match(r"\W*all\s*states?\b|\W*all\W*$", t, re.I)), None)
                states = [[v, t] for v, t in opts if v != all_states]
            elif opts:
                dims.append({"field": field, "key": key_for(field), "options": [[v, t] for v, t in opts]})
        submit = next(iter(re.findall(r'type="submit"[^>]*name="([^"]*btnSubmit[^"]*)"|name="([^"]*btnSubmit[^"]*)"[^>]*type="submit"', page)), None)
        submit = crawl.html.unescape(next((s for s in submit if s), "")) if submit else None
        stem = re.sub(r"\.aspx$", "", path.rsplit("/", 1)[-1])
        slug = re.sub(r"[^a-z0-9]+", "-", (rcode.replace(".", "") + "-" + stem).lower()).strip("-")[:47].strip("-")
        while slug in slugs:
            slug = slug[:45] + "-2"
        slugs.add(slug)
        rep = {"path": path, "slug": slug, "code": rcode, "title": title, "submit": submit, "dims": dims,
               "states": states, "all_states": all_states}
        # one check: the latest year (last option), everything else first option, All States (or the first state)
        sel = {d["field"]: (d["options"][-1] if d["key"] == "fin_year" else d["options"][0])[0] for d in dims}
        if states:
            sel[crawl.STATE_FIELD] = all_states if all_states is not None else states[0][0]
        code, res = f.submit(url, sel, submit)
        time.sleep(0.5)
        cols, rows = crawl.parse_table(res) if code == 200 else ([], [])
        rep["check"] = {"http": code, "rows": len(rows), "columns": cols}
        items = 1
        for d in dims:
            items *= len(d["options"])
        print(f"{rcode:5} {slug:48} items={items:5} states={len(states or [])} rows={len(rows)} cols={len(cols)}", file=sys.stderr)
        out.append(rep)
    Path(__file__).with_name("reports.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(out)} reports -> reports.json; counters {hub.counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
