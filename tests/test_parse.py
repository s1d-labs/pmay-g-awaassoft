"""Parsing tests on saved report pages (gzipped): fast, no network. Add a saved page for every grid layout the site has."""
import gzip
from pathlib import Path

import crawl

PAGES = Path(__file__).parent / "pages"
P = "ctl00$ContentPlaceHolder1$"


def page(name):
    return gzip.open(PAGES / f"{name}.html.gz", "rt", encoding="utf-8").read()


def test_district_grid_with_grouped_header():
    cols, rows = crawl.parse_table(page("a2-bihar-districts"))
    assert cols[:4] == ["sno", "district_name", "sanctions_out_of_geo_tagged", "sanctions_with_verified_accounts"]
    assert "installment_1st" in cols and "installment_4th" in cols       # "Installment" spans 1st..4th
    recs = crawl.rows_to_records(cols, rows, "district")
    assert recs[0][:2] == ("Total", "total") and recs[0][2]["completed"] == 4321030
    name, level, data, texts = recs[1]
    assert (name, level, texts) == ("ARARIA", "district", ["ARARIA"])
    assert data["sanctions_out_of_geo_tagged"] == 300063 and isinstance(data["installment_4th"], int)
    assert "sno" not in data and len(recs) == 39                          # Total + 38 districts of Bihar
    assert crawl.as_on(page("a2-bihar-districts")) == "2026-11-02T21:41:08"


def test_two_row_header_with_serial_number_inside_a_group():
    cols, rows = crawl.parse_table(page("f10-all-states"))
    assert cols[2] == "c_1st_installment_no_of_files_files"
    recs = crawl.rows_to_records(cols, rows, "state")
    assert [r[0] for r in recs[:2]] == ["Total", "ARUNACHAL PRADESH"]
    assert not any(crawl.SNO.search(k) for k in recs[1][2])              # "S.No. & State Name details / SNo" dropped
    assert all(isinstance(v, int) for v in recs[1][2].values())


def test_detail_grid_rows_are_identified_by_their_text_cells():
    cols, rows = crawl.parse_table(page("f13-arunachal"))
    assert cols == ["s_no", "state", "district", "block", "fto_file_name", "no_of_vendors_employees", "amount_rs"]
    recs = crawl.rows_to_records(cols, rows, "district")
    files = [r for r in recs if r[1] != "total"]
    assert len({tuple(r[3]) for r in files}) == len(files)                # name repeats, (name, file name, ...) doesn't


def test_numbers_and_names():
    assert crawl.number("1,23,456") == 123456 and crawl.number("12.50") == 12.5 and crawl.number("-") is None
    assert crawl.number("(42)") == -42 and crawl.number("ASSAM") == "ASSAM" and crawl.number("0") == 0
    assert crawl.snake("Completed & verified") == "completed_and_verified" and crawl.snake("1st") == "c_1st"


def test_form_fields_send_what_a_browser_sends():
    t = page("c1-all-states")
    f = crawl.form_fields(t)
    assert "__VIEWSTATE" in f and f[P + "ddlFinYear"] == "ALLPMAYG" and f[P + "ddlState"] == "0"
    assert ("05", "BIHAR") in crawl.select_options(t, P + "ddlState")


class FakeFetcher:
    """Answers the All States query with the saved F.10 page and Bihar with the saved district page; other states: not offered."""

    def __init__(self):
        self.calls = []

    def submit(self, url, sel, submit, firsts=None):
        self.calls.append(sel[crawl.STATE_FIELD])
        state = sel[crawl.STATE_FIELD]
        if state == "0":
            return 200, page("f10-all-states")
        if state == "05":
            return 200, page("a2-bihar-districts")
        return None, None


def test_an_item_walks_all_states_then_each_state_with_numbers():
    rep = {"path": "x.aspx", "code": "F.10", "title": "t", "submit": P + "btnSubmit", "all_states": "0",
           "states": [["03", "ARUNACHAL PRADESH"], ["05", "BIHAR"], ["99", "NOWHERE"]],
           "dims": [{"field": P + "ddlFinYear", "key": "fin_year", "options": [["2024-2025", "2024-2025"]]}]}
    item = {"key": "2024-2025", "payload": {"sel": {P + "ddlFinYear": "2024-2025"}, "labels": {P + "ddlFinYear": "2024-2025"}}}
    f = FakeFetcher()
    records, files = crawl.run_item(f, rep, item)
    assert f.calls[0] == "0" and set(f.calls[1:]) == {"03", "05"}       # only states in the dropdown with non-zero rows
    by_id = dict(records)
    assert len(by_id) == len(records)                                    # ids are unique
    total = by_id["2024-2025~all~Total"]
    assert total["level"] == "total" and total["fin_year_code"] == "2024-2025" and total["report_code"] == "F.10"
    bihar = by_id["2024-2025~all~BIHAR"]
    assert bihar["level"] == "state" and bihar["state_code"] == "05"
    araria = by_id["2024-2025~05~ARARIA"]
    assert araria["level"] == "district" and araria["state"] == "BIHAR" and araria["district"] == "ARARIA"
    assert araria["installment_1st"] == 298868
    assert not any(r["level"] == "total" and r["state_code"] for _, r in records)
    assert [p for p, _, _ in files] == ["pages/2024-2025/all.html.gz", "pages/2024-2025/state-05.html.gz"]
    for _, data in records:                                              # flat: no nested values
        assert all(v is None or isinstance(v, (str, int, float)) for v in data.values())


def test_filters_are_set_in_page_order_whatever_the_payload_order():
    order = []

    class Recorder(FakeFetcher):
        def submit(self, url, sel, submit, firsts=None):
            order.append([f for f in sel if f != crawl.STATE_FIELD])
            return None, None

    rep = {"path": "x.aspx", "code": "B.1", "title": "t", "submit": P + "btnSubmit", "all_states": None, "states": None,
           "dims": [{"field": P + "ddlFinYear", "key": "fin_year", "options": [["2010-2011", "2010-2011"]]},
                    {"field": P + "ddlScheme", "key": "scheme", "options": [["ICF", "ICF"]]}]}
    sel = {P + "ddlScheme": "ICF", P + "ddlFinYear": "2010-2011"}        # as the hub's jsonb gives it back: shorter keys first
    crawl.run_item(Recorder(), rep, {"key": "2010-2011~ICF", "payload": {"sel": sel, "labels": {}}})
    assert order == [[P + "ddlFinYear", P + "ddlScheme"]]                # the year first: changing it resets the scheme


def test_the_catalogue_is_complete():
    import family
    assert len(family.PROJECTS) >= 40 and len({p["name"] for p in family.PROJECTS}) == len(family.PROJECTS)
    for p in family.PROJECTS:
        assert len(p["name"]) <= 64 and p["name"].startswith(family.FAMILY + "-") and p["items"]
        assert len({i["key"] for i in p["items"]}) == len(p["items"])
        assert "IN" in (p["pool"]["proxy"], p["pool"]["geo"])
