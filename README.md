# pmay-g-awaassoft

s1d-labs scrape-pool family for the **PMAY-G AwaasSoft MIS reports** (Pradhan Mantri Awaas Yojana – Gramin, Ministry of Rural
Development) at [report.pmayg.dord.gov.in/netiay/newreport.aspx](https://report.pmayg.dord.gov.in/netiay/newreport.aspx).

## What it collects

Every report on the index that has no captcha (42 of them: physical, financial and social progress, E-FMS / FTO, SECC,
AwaasPlus 2018 and 2024, PM-Janman, special projects), for every combination of its filters (financial year × scheme × …), at
every level the site offers: India total, states, districts (and one row per FTO file for F.13).

- **One project per report** (`pmay-g-awaassoft-<code>-<page>`), so each report has its own table: `scrape_hub.projects.<project>`
  in the team database and Metabase ("Team data").
- **Rows** are flat: `report_code`, `report`, each filter as `<filter>_code` + `<filter>` (label), `level`
  (`total` / `state` / `district` / `row`), `state_code`, `state`, `district`, `name`, `as_on` (the report's own timestamp), then
  the report's columns in snake_case with numbers as int / float. Record id = `<item key>~<state code or all>~<row name ...>`, so
  re-running an item replaces its rows.
- **Files**: every page the site returned, gzipped, in the owner's bucket at `<project>/pages/<item key>/{all,state-NN,report}.html.gz`.

Not collected: the 16 reports behind an image captcha (most IAY-era ones and the beneficiary drill-downs), Power BI dashboards, the
inspection-photo map, F.2 (officials' phone numbers) and F.8 (its form has no Submit button on the site).

## Files

| File | What |
|---|---|
| `discover.py` | builds `reports.json` from the live site (reports, filters, options). Re-run when the site adds reports or years: `myproxy run python discover.py` |
| `reports.json` | the catalogue (committed: the setup workflow runs on GitHub, which can't reach the site) |
| `family.py` | one project per report, one queue item per filter combination (newest year first), `proxy="IN"` |
| `crawl.py` | the task: sets the filters one postback at a time (the site resets later dropdowns when an earlier one changes), submits All States, then each state with numbers |
| `tests/` | parsing tests on saved pages (`python -m pytest tests`) |

## Trying it

The site only answers from India; from a box use `myproxy`:

```
myproxy run python crawl.py --local PhysicalProgresReport_SPPMAYG.aspx ddlFinYear=ALLPMAYG      # one item, records to stdout
myproxy run scrape-check --items 2                                                               # setup + task against a stand-in hub
```

The pool runs it on GitHub runners through the hub's Indian proxies (one per worker, 2 threads, 500 ms between requests).
The CLAUDE.md in this repo is the full scraper reference.
