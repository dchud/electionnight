import json

import httpx
import pytest

from monitor.ref import wiki
from monitor.ref.wiki import WikiClient, article_url, parse_tables

HTML = """
<h2 id="Top">Top <span>heading</span></h2>
<style>.x { color: red }</style>
<table>
<tr><th colspan="2">Group</th><th rowspan="2">Solo</th></tr>
<tr><th>A</th><th>B</th></tr>
<tr>
  <td rowspan="2"><a href="/wiki/One" title="One">One</a>
    <sup class="reference"><a href="#cite">[1]</a></sup></td>
  <td><span style="display: none">hidden</span>shown&#160;text</td>
  <td><ul>
    <li><a href="/wiki/Pat_Doe" title="Pat Doe (politician)">Pat Doe</a>
      (Republican)<sup class="reference">[2]</sup></li>
    <li>Sam Roe (Green)</li></ul>
    <table><tr><td>inner</td></tr></table></td>
</tr>
<tr><td>second</td><td>third</td></tr>
</table>
"""


def test_parse_tables_expands_spans_and_drops_hidden_text():
    tables = parse_tables(HTML)
    assert len(tables) == 2
    assert tables[0].rows[0][0].text == "inner"
    outer = tables[1]
    assert outer.heading == "Top heading"
    assert [cell.text for cell in outer.rows[0]] == ["Group", "Group", "Solo"]
    assert [cell.text for cell in outer.rows[1]] == ["A", "B", "Solo"]
    assert outer.rows[2][0].text == "One"
    assert outer.rows[2][1].text == "shown text"
    assert outer.rows[3][0].text == "One"
    assert outer.rows[3][1].text == "second"


def test_parse_tables_keeps_list_items_and_links():
    cell = parse_tables(HTML)[1].rows[2][2]
    assert [item.text for item in cell.items] == [
        "Pat Doe (Republican)",
        "Sam Roe (Green)",
    ]
    assert cell.items[0].links[0].title == "Pat Doe (politician)"
    assert cell.items[0].links[0].label == "Pat Doe"
    assert cell.items[1].links == []


def test_article_url():
    assert (
        article_url("2026 Foo elections", "Top")
        == "https://en.wikipedia.org/wiki/2026_Foo_elections#Top"
    )


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(wiki.time, "sleep", slept.append)
    return slept


def client_for(handler):
    transport = httpx.MockTransport(handler)
    return WikiClient(httpx.Client(transport=transport, timeout=5.0))


def test_client_identifies_itself_and_retries_on_429(no_sleep):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(
            200, json={"parse": {"text": "<p>hi</p>", "revid": 11}}
        )

    client = WikiClient(
        httpx.Client(
            transport=httpx.MockTransport(handler),
            headers={"User-Agent": wiki.USER_AGENT},
        )
    )
    assert client.page("Some page") == ("<p>hi</p>", 11)
    assert len(seen) == 2
    assert seen[0].headers["user-agent"] == wiki.USER_AGENT
    assert "@" not in wiki.USER_AGENT
    assert 7.0 in no_sleep
    assert seen[0].url.params["action"] == "parse"
    assert seen[0].url.params["page"] == "Some page"


def test_client_raises_on_api_error(no_sleep):
    client = client_for(
        lambda request: httpx.Response(
            200, json={"error": {"code": "missingtitle"}}
        )
    )
    with pytest.raises(RuntimeError, match="missingtitle"):
        client.page("No such page")


def test_client_waits_out_maxlag(no_sleep):
    replies = [
        httpx.Response(200, json={"error": {"code": "maxlag"}}),
        httpx.Response(200, json={"parse": {"text": "x", "revid": 1}}),
    ]
    client = client_for(lambda request: replies.pop(0))
    assert client.page("Busy")[1] == 1


def test_wikidata_ids_follow_redirects_but_not_into_sections(no_sleep):
    def handler(request):
        titles = request.url.params["titles"].split("|")
        assert titles == ["Jane_Doe", "Mike Smith", "Pat Roe"]
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "query": {
                        "normalized": [{"from": "Jane_Doe", "to": "Jane Doe"}],
                        "redirects": [
                            {"from": "Mike Smith", "to": "Michael Smith"},
                            {
                                "from": "Pat Roe",
                                "to": "2026 elections",
                                "tofragment": "District 9",
                            },
                        ],
                        "pages": [
                            {
                                "title": "Jane Doe",
                                "pageprops": {"wikibase_item": "Q1"},
                            },
                            {
                                "title": "Michael Smith",
                                "pageprops": {"wikibase_item": "Q2"},
                            },
                            {
                                "title": "2026 elections",
                                "pageprops": {"wikibase_item": "Q3"},
                            },
                        ],
                    }
                }
            ),
        )

    client = client_for(handler)
    found = client.wikidata_ids(["Mike Smith", "Jane_Doe", "Pat Roe", ""])
    assert found == {"Jane_Doe": "Q1", "Mike Smith": "Q2"}
