"""The family's `crawl` task (see scrape.json): PMAY-G AwaasSoft MIS reports (report.pmayg.dord.gov.in/netiay/newreport.aspx).

Try it without the hub:  python crawl.py --local <report-page> [field=value ...]
    e.g. python crawl.py --local PhysicalProgresReport_SPPMAYG.aspx ddlFinYear=ALLPMAYG     (needs an Indian IP: myproxy run ...)

Every report is an ASP.NET form: filter dropdowns (financial year, scheme, ...), a state dropdown and a Submit button; the answer
is a GridView with one row per state ("All States") or per district (one state). One queue item = one combination of the filters
(payload["sel"]). For it the task submits "All States", stores the India total and the state rows, then submits each state that
has non-zero numbers and stores its district rows. Every page it gets back goes to the bucket (gzipped HTML, signed by the hub).

Records are flat: the filters (code and label), level (india / state / district / row), state_code, state, district, as_on, and
the report's own columns as snake_case keys with numbers as int / float. Record id = item key + state + row name: re-running an
item replaces its rows.
"""
import gzip
import html
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import requests

PROJECT = os.environ.get("SCRAPE_POOL_PROJECT", "")
SITE = os.environ.get("SITE", "https://report.pmayg.dord.gov.in")
DEADLINE = time.time() + float(os.environ.get("SCRAPE_MAX_HOURS", "1")) * 3600 - 120
UA = "Mozilla/5.0 (X11; Linux x86_64) s1d-labs scraper (https://github.com/s1d-labs)"
REPORTS = json.loads((Path(__file__).parent / "reports.json").read_text())
STATE_FIELD = "ctl00$ContentPlaceHolder1$ddlState"
STATES = {}                                   # normalised state name -> state code, from every report's state dropdown
for _r in REPORTS:
    for _c, _n in _r.get("states") or []:
        STATES.setdefault(re.sub(r"[^A-Z0-9]", "", _n.upper()), _c)
NUM = re.compile(r"^-?\(?[\d,]*\.?\d+\)?%?$")
SNO = re.compile(r"(^|_)(s_?no|sl_?no|sr_?no|serial_no)($|_)")


# ---------------------------------------------------------------- parsing (pure)

class _Tables(HTMLParser):
    """Every <table> in a page as {"attrs", "rows": [[(text, colspan, rowspan, is_th)]]}; nested tables are kept apart."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.stack, self.cell, self.skip = [], [], None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "table":
            self.stack.append({"attrs": a, "rows": []})
        elif not self.stack:
            return
        elif tag == "tr":
            self.stack[-1]["rows"].append([])
        elif tag in ("td", "th"):
            if not self.stack[-1]["rows"]:
                self.stack[-1]["rows"].append([])
            self.cell = {"text": [], "colspan": _int(a.get("colspan")), "rowspan": _int(a.get("rowspan")), "th": tag == "th",
                         "depth": len(self.stack)}
        elif tag == "br" and self.cell is not None:
            self.cell["text"].append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag == "table" and self.stack:
            self.tables.append(self.stack.pop())
        elif tag in ("td", "th") and self.cell is not None and self.cell["depth"] == len(self.stack):
            c = self.cell
            self.stack[-1]["rows"][-1].append((" ".join("".join(c["text"]).split()), c["colspan"], c["rowspan"], c["th"]))
            self.cell = None

    def handle_data(self, data):
        if self.cell is not None and not self.skip and self.cell["depth"] == len(self.stack):
            self.cell["text"].append(data)


def _int(v):
    try:
        return max(1, min(int(v), 100))
    except (TypeError, ValueError):
        return 1


def _grid(rows):
    """Expand colspan / rowspan into a rectangular list of text rows."""
    out, carry = [], {}                       # carry[col] = (text, rows left)
    for row in rows:
        line, col, cells = [], 0, list(row)
        while cells or col in carry:
            if col in carry:
                text, left = carry[col]
                line.append(text)
                if left > 1:
                    carry[col] = (text, left - 1)
                else:
                    del carry[col]
                col += 1
                continue
            text, colspan, rowspan, _ = cells.pop(0)
            for _ in range(colspan):
                line.append(text)
                if rowspan > 1:
                    carry[col] = (text, rowspan - 1)
                col += 1
        out.append(line)
    return out


def number(text):
    """'1,23,456' -> 123456, '12.50' -> 12.5, '' / '-' / 'NA' -> None, anything else stays text."""
    t = text.strip()
    if t in ("", "-", "--", "NA", "N/A", "na", "."):
        return None
    if NUM.match(t):
        neg = t.startswith("-") or t.startswith("(")
        t = t.strip("-()%").replace(",", "")
        v = float(t)
        v = -v if neg else v
        return int(v) if v.is_integer() and "." not in t else v
    return t


def snake(text):
    s = re.sub(r"[^0-9a-z]+", "_", text.lower().replace("%", " pct ").replace("&", " and ").replace("#", " ")).strip("_")
    return ("c_" + s if s[:1].isdigit() else s)[:60] or "col"


def _is_data(line):
    return sum(1 for c in line[1:] if c and NUM.match(c)) >= 1 and not all(not c or not NUM.match(c) for c in line[2:])


def parse_table(page):
    """The report's result grid as (columns, rows): columns are snake_case names, rows are lists of typed values.
    Picks the table with the most data rows (a data row has numbers after its first cells); its leading rows are the header."""
    p = _Tables()
    p.feed(page)
    best = None
    for t in p.tables:
        g = _grid(t["rows"])
        n = sum(1 for line in g if _is_data(line))
        if n and (best is None or n > best[0]):
            best = (n, g)
    if not best:
        return [], []
    g = best[1]
    first = next(i for i, line in enumerate(g) if _is_data(line))
    head, body = g[:first], g[first:]
    width = max(len(line) for line in body)
    cols = []
    for c in range(width):
        parts = []
        for line in head:
            t = line[c] if c < len(line) else ""
            if t and t not in (".", "*") and t not in parts:
                parts.append(t)
        cols.append(snake(" ".join(parts)) if parts else f"col_{c}")
    seen = {}
    for i, c in enumerate(cols):                # make names unique: total, total_2, ...
        seen[c] = seen.get(c, 0) + 1
        if seen[c] > 1:
            cols[i] = f"{c}_{seen[c]}"
    rows = [[number(line[c]) if c < len(line) else None for c in range(width)] for line in body if any(x.strip() for x in line)]
    return cols, rows


def as_on(page):
    """The report's 'As On: 02/11/2026 21:41:08' as ISO 8601, or None."""
    m = re.search(r"As\s*On\s*:?\s*(?:<[^>]*>\s*)*:?\s*(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", page, re.I)
    if not m:
        return None
    d, mo, y, hh, mm, ss = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0)).isoformat()
    except ValueError:
        return None


def rows_to_records(cols, rows, level):
    """[(name, level, data, texts)] for one grid. The name column is the first text column after the serial number; a row named
    'Total' (or 'Grand Total') becomes level 'total'. Metric columns keep their snake_case header names; texts are the row's text
    cells in order (they identify a row in detail grids, where the name repeats)."""
    if not rows:
        return []
    name_col = next((c for c in range(len(cols)) if any(isinstance(r[c], str) for r in rows if c < len(r))), None)
    out = []
    for r in rows:
        name = r[name_col] if name_col is not None else None
        name = str(name).strip() if name is not None else ""
        lvl = "total" if re.fullmatch(r"(grand\s*)?total|all\s*india|india", name, re.I) else level
        data = {}
        for c, v in enumerate(r):
            if c == name_col or SNO.search(cols[c]):
                continue
            data[cols[c]] = v
        if not name and not any(isinstance(v, (int, float)) for v in data.values()):
            continue
        if lvl == "total" and any(o[1] == "total" and o[2] == data for o in out):
            continue                                        # the grid repeats its Total row as a footer
        out.append((name, lvl, data, [str(v) for v in r if isinstance(v, str) and v]))
    return out


def has_numbers(data):
    return any(isinstance(v, (int, float)) and v != 0 for v in data.values())


# ---------------------------------------------------------------- the form

def form_fields(page):
    """What a browser would send for this page's form: hidden + text inputs, checked radios, enabled selects (selected option)."""
    out = {}
    for m in re.finditer(r"<input\b[^>]*>", page, re.I):
        s = m.group(0)
        n, v, ty = (re.search(rf'\b{a}="([^"]*)"', s) for a in ("name", "value", "type"))
        ty = (ty.group(1) if ty else "text").lower()
        if not n or "disabled" in s:
            continue
        if ty in ("hidden", "text") or (ty == "radio" and "checked" in s):
            out[html.unescape(n.group(1))] = html.unescape(v.group(1)) if v else ""
    for m in re.finditer(r"<select\b([^>]*)>(.*?)</select>", page, re.S | re.I):
        n = re.search(r'name="([^"]+)"', m.group(1))
        if not n or "disabled" in m.group(1):
            continue
        opt = re.search(r'<option[^>]*selected[^>]*value="([^"]*)"', m.group(2)) or re.search(r'<option[^>]*value="([^"]*)"', m.group(2))
        out[html.unescape(n.group(1))] = html.unescape(opt.group(1)) if opt else ""
    return out


def select_options(page, name):
    m = re.search(r'<select\b[^>]*name="' + re.escape(name) + r'"[^>]*>(.*?)</select>', page, re.S)
    if not m:
        return []
    return [(html.unescape(v), " ".join(html.unescape(t).split())) for v, t in re.findall(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)', m.group(1))]


def norm(name):
    return re.sub(r"[^A-Z0-9]", "", name.upper())


# ---------------------------------------------------------------- fetching

class Fetcher:
    """One requests.Session plus the last page of each report (the next submit posts its __VIEWSTATE)."""

    def __init__(self, hub):
        self.hub, self.s, self.last = hub, requests.Session(), {}
        self.s.headers["User-Agent"] = UA

    def _do(self, method, url, data=None):
        hub = self.hub
        delay = hub.settings.get("delay_ms", 0) / 1000
        if delay:
            time.sleep(delay)
        t = time.time()
        try:
            r = self.s.request(method, url, data=data, timeout=120)
        except requests.RequestException:
            hub.metric("http_error")
            hub.error()
            return None, None
        hub.metric("n_http")
        hub.metric("t_http_s", time.time() - t)
        if r.history and urlsplit(r.url).path.lower() != urlsplit(url).path.lower():
            hub.metric("redirected")
            return 302, r.url                  # the site sent this choice to another report (crawled as its own project)
        if r.status_code != 200:
            hub.metric(f"http_{r.status_code}")
            if r.status_code in (403, 429, 503):
                hub.block()
            return r.status_code, r.text
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        return 200, r.text

    def _post(self, url, page, changes, submit=None):
        data = form_fields(page)
        data.update(changes)
        data["__EVENTTARGET"] = "" if submit or not changes else next(iter(changes))
        data["__EVENTARGUMENT"] = ""
        if submit:
            data[submit] = "Submit"
        code, out = self._do("POST", url, data)
        if code == 200 and "<form" not in out:
            code = 502                         # a proxy or error page instead of the report
        return code, out

    def submit(self, url, sel, submit, firsts=None):
        """Set the filters like a person would, then submit; returns (status, page), or (None, None) when an option is not offered.

        Each dropdown's server-side change handler runs on the next postback and resets the dropdowns after it (a new year reloads
        the schemes, a new scheme resets the state), so every filter that differs from the page is posted on its own, in page
        order, before the Submit that also carries the state. A dropdown the page hides at that point (F.13 hides its level once the
        year changes) is left alone when the item asks for its first option (firsts), and makes the item "not offered" otherwise."""
        page = self.last.get(url)
        if page is None:
            code, page = self._do("GET", url)
            if code != 200:
                return code, page
        for field, want in sel.items():
            if field == STATE_FIELD:
                continue
            if form_fields(page).get(field) == want:
                continue
            opts = [v for v, _ in select_options(page, field)]
            if not opts and (firsts or {}).get(field) == want:
                continue
            if want not in opts:
                self.last[url] = page
                return None, None              # this year has no such scheme (the dropdown was reloaded without it)
            code, page = self._post(url, page, {field: want})
            if code != 200:
                self.last.pop(url, None)
                return code, page
        state = {STATE_FIELD: sel[STATE_FIELD]} if STATE_FIELD in sel else {}
        if state and state[STATE_FIELD] not in (v for v, _ in select_options(page, STATE_FIELD)):
            self.last[url] = page
            return None, None
        code, out = self._post(url, page, state, submit)
        if code != 200:
            self.last.pop(url, None)          # stale view state or an error page: the next call starts from a GET
            return code, out
        self.last[url] = out
        echo = form_fields(out)
        wrong = [f.rsplit("$", 1)[-1] for f, v in sel.items() if f in echo and echo[f] != v]
        if wrong:                             # the page answered for other filters than asked: never store that under this key
            self.last.pop(url, None)
            return 409, "selection not kept: " + ", ".join(wrong)
        return code, out


def run_item(f, rep, item):
    """Fetch one queue item. Returns (records [(id, data)], files [(path, bytes, type)]), or raises RuntimeError."""
    p = item["payload"]
    url = SITE + "/netiay/" + rep["path"]
    base = {"report_code": rep["code"], "report": rep["title"]}
    for d in rep["dims"]:
        v = p["sel"][d["field"]]
        base[d["key"] + "_code"] = v
        base[d["key"]] = p["labels"].get(d["field"], v)
    firsts = {d["field"]: d["options"][0][0] for d in rep["dims"]}
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", item["key"])[:150]
    records, files, ids = [], [], set()

    def get(sel, tag):
        code, page = f.submit(url, sel, rep.get("submit"), firsts)
        if code == 409:                                    # now and then the site drops a filter under load: once more from a GET
            code, page = f.submit(url, sel, rep.get("submit"), firsts)
        if code in (None, 302) or code == 500 and page and re.search(r"Invalid postback or callback argument", page):
            return None                                    # not offered here (or moved to another report): nothing to store
        if code == 409:
            raise RuntimeError(f"{page} ({tag})")
        if code != 200:
            raise RuntimeError(f"HTTP {code} for {tag}")
        files.append((f"pages/{safe}/{tag}.html.gz", gzip.compress(page.encode("utf-8")), "application/gzip"))
        return page

    def add(page, level, state_code, state):
        cols, rows = parse_table(page)
        stamp = as_on(page)
        out = []
        detail = "state" in cols or "district" in cols     # a detail grid (one row per FTO file, ...) names both itself
        for name, lvl, data, texts in rows_to_records(cols, rows, level):
            if lvl == "total" and state_code:
                continue                                    # a state's total row repeats its row in the All States grid
            if detail and lvl != "total":
                lvl = "row"
            rec = {**base, "level": lvl, "state_code": state_code, "state": state, "district": None, "name": name, "as_on": stamp}
            if lvl == "row" and norm(name) in STATES:       # rows that are states (a report without a state filter, a detail grid)
                rec.update(state=name, state_code=state_code or STATES[norm(name)])
                if not detail:
                    rec["level"] = "state"
            elif lvl == "state":
                rec.update(state=name, state_code=STATES.get(norm(name)))
            elif lvl == "district":
                rec["district"] = name
            for k, v in data.items():
                if k in ("state", "district"):
                    if isinstance(v, str):
                        rec[k] = v
                elif k in rec:
                    rec["c_" + k] = v                       # a report column called level, state_code, ...
                else:
                    rec[k] = v
            rid = "~".join([item["key"], state_code or "all"] + (texts or [lvl]))[:400]
            n = 2
            while rid in ids:
                rid = "~".join([item["key"], state_code or "all"] + (texts or [lvl]))[:396] + f"~{n}"
                n += 1
            ids.add(rid)
            records.append((rid, rec))
            out.append(rec)
        return out

    sel = dict(p["sel"])
    states = rep.get("states")                              # [[code, name], ...] or None when the report has no state filter
    if not states:
        page = get(sel, "report")
        if page:
            add(page, "row", None, None)
        return records, files
    all_code = rep.get("all_states")
    todo = states
    if all_code is not None:
        page = get({**sel, STATE_FIELD: all_code}, "all")
        if page is None:
            return records, files
        by_name = {norm(n): c for c, n in states}
        todo = []
        for rec in add(page, "state", None, None):
            if rec["level"] == "state" and has_numbers({k: v for k, v in rec.items() if k not in base}):
                code = by_name.get(norm(rec["name"]))
                rec["state_code"] = code or rec["state_code"]
                if code:
                    todo.append((code, rec["name"]))
    for code, name in todo:
        page = get({**sel, STATE_FIELD: code}, f"state-{code}")
        if page:
            add(page, "district", code, name)
    return records, files


# ---------------------------------------------------------------- the task

def worker(hub, rep, stop, idx):
    f = Fetcher(hub)
    while time.time() < DEADLINE and not stop.is_set():
        if idx >= max(1, int(hub.settings.get("threads", 1))):
            time.sleep(10)                                  # the hub lowered threads: this one waits
            continue
        if hub.settings.get("paused"):
            time.sleep(15)
            continue
        items = hub.claim(1, steal=True, retry_failed=True)
        if not items:
            if hub.settings.get("paused"):
                continue
            return                                          # queue empty
        item = items[0]
        try:
            records, files = run_item(f, rep, item)
        except RuntimeError as e:
            hub.complete([item["key"]], "failed", error=str(e)[:200])
            continue
        if files:
            hub.upload_many(files)
        for rid, data in records:
            hub.record(rid, data)
        hub.complete([item["key"]], "done", fetched=len(records))


def main():
    from scrape_hub_client import HubClient      # imported here so --local works without it
    hub = HubClient(PROJECT, job_index=int(os.environ.get("SCRAPE_JOB_INDEX", "0")), total_jobs=int(os.environ.get("SCRAPE_TOTAL_JOBS", "1")))
    slug = os.environ.get("REPORT") or PROJECT.split("-", 3)[-1]      # the pool entry's env names the report
    rep = next((r for r in REPORTS if r["slug"] == slug), None)
    stop = threading.Event()
    errors = []

    def run(i):
        try:
            worker(hub, rep, stop, i)
        except BaseException as e:      # noqa: BLE001 - one thread's crash ends the run as failed
            errors.append(e)
            stop.set()

    try:
        if rep is None:
            raise SystemExit(f"no report in reports.json for project {PROJECT!r}")
        n = max(1, min(int(hub.settings.get("threads", 1)), 8))
        threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(1)
        if errors:
            raise errors[0]
        hub.close()
    except BaseException as e:
        stop.set()
        hub.close("failed", f"{type(e).__name__}: {e}"[:200])
        raise


# ---------------------------------------------------------------- local try-out

class LocalHub:
    """Stand-in for HubClient so `--local` runs without the hub: counters go to stderr, records to stdout."""
    settings = {"threads": 1, "delay_ms": 0, "paused": False}

    def __init__(self):
        self.counts = {}

    def metric(self, name, value=1):
        self.counts[name] = self.counts.get(name, 0) + value

    def error(self, n=1):
        self.metric("errors", n)

    def block(self, n=1):
        self.metric("blocked", n)


def local(page, args):
    rep = next(r for r in REPORTS if r["path"].endswith(page) or r["slug"] == page)
    sel = {d["field"]: d["options"][0][0] for d in rep["dims"]}
    for a in args:
        k, v = a.split("=", 1)
        sel[next(d["field"] for d in rep["dims"] if d["field"].endswith("$" + k) or d["key"] == k)] = v
    labels = {d["field"]: dict(map(tuple, d["options"])).get(sel[d["field"]], sel[d["field"]]) for d in rep["dims"]}
    item = {"key": "~".join(sel.values()) or "all", "payload": {"sel": sel, "labels": labels}}
    hub = LocalHub()
    records, files = run_item(Fetcher(hub), rep, item)
    for rid, data in records:
        print(json.dumps({"record_id": rid, "data": data}, ensure_ascii=False))
    print(f"{len(records)} records, {len(files)} pages ({sum(len(b) for _, b, _ in files)} bytes gz), counters {hub.counts}", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--local"]:
        local(sys.argv[2] if len(sys.argv) > 2 else "PhysicalProgresReport_SPPMAYG.aspx", sys.argv[3:])
    else:
        main()
