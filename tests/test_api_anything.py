"""Исполнитель операций API Anything: слоты, JSON по пути, HTML-записи, #id и @атрибут."""

from __future__ import annotations

import pytest

from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.api_anything import (
    extract_html,
    extract_html_items,
    extract_json,
    extract_json_object,
    operation_request,
)


def op(response: dict, url: str = "https://example.org/list?page=%7Bpage%7D&x=1", **extra):
    return {
        "name": "testOp",
        "_site": "Тест",
        "request": {
            "method": "GET",
            "url": url,
            "headers": {"accept": "*/*", "user-agent": "Chrome", "sec-fetch-mode": "cors",
                        "agentid": ""},
        },
        "response": response,
        **extra,
    }  # fmt: skip


def test_none_drops_query_slot_and_value_fills_it():
    o = op({"format": "json", "extract": "a", "pick": []})
    assert operation_request(o, {"page": None})[0] == "https://example.org/list?x=1"
    assert operation_request(o, {"page": 2})[0] == "https://example.org/list?page=2&x=1"
    only = op({"format": "json"}, url="https://example.org/task?page=%7Bpage%7D")
    assert operation_request(only, {"page": None})[0] == "https://example.org/task"


def test_header_slot_comes_from_params_and_browser_headers_are_dropped():
    o = op(
        {"format": "json", "extract": "a", "pick": []},
        url="https://example.org/feed",
        slots=[{"ref": "session:testOp/WzAgentId", "at": ["header:agentid"]}],
    )
    _, headers = operation_request(o, {"WzAgentId": "agent-1"})
    assert headers == {"accept": "*/*", "agentid": "agent-1"}
    with pytest.raises(SourceFormatError, match="WzAgentId") as err:
        operation_request(o, {})
    assert "agent-1" not in str(err.value)


def test_json_path_through_dots_list_and_object():
    text = '{"data": {"flag": true, "n": 3, "items": [{"id": 1, "s": "a", "x": 0}]}}'
    rows = op({"format": "json", "extract": "data.items", "pick": ["id", "title=s"]})
    assert extract_json(rows, text) == [{"id": 1, "title": "a"}]
    status = op({"format": "json", "extract": "data", "pick": ["flag", "missing"]})
    assert extract_json_object(status, text) == {"flag": True, "missing": None}
    with pytest.raises(SourceFormatError, match="не объект"):
        extract_json_object(rows, text)
    with pytest.raises(SourceFormatError, match="не список"):
        extract_json(status, text)
    with pytest.raises(SourceFormatError, match="Тест: testOp"):
        extract_json(rows, '{"data": null}')


PAGE = """<html><body><div id="feed" class="list">
<article class="card"><a class="card__link" href="/task/view/1" title="Первый">Первый</a>
<span class="foot" title="24.10.2026 19:30">2 часа назад</span><span class="foot">Москва</span>
<p class="desc">Строка один<br />строка&nbsp;два</p></article>
<article class="card hot"><a class="card__link" href="/task/view/2">Второй</a>
<span class="foot">без даты</span></article>
</div><article class="card"><a class="card__link" href="/x">вне ленты</a></article>
<div id="other"><h1>не тот</h1></div><div id="task"><section class="hero"><h1>Заголовок</h1>
Текст<img src="a.png"></section></div></body></html>"""


def test_html_items_with_attributes_and_scope():
    o = op({"format": "html", "html": {"items": "#feed article.card", "fields": {
        "title": ".card__link", "url": ".card__link@href", "date": ".foot@title",
        "desc": ".desc"}}})  # fmt: skip
    assert extract_html_items(o, PAGE) == [
        {"title": "Первый", "url": "/task/view/1", "date": "24.10.2026 19:30",
         "desc": "Строка один\nстрока два"},
        {"title": "Второй", "url": "/task/view/2", "date": None, "desc": None},
    ]  # fmt: skip


def test_html_id_selector_and_single_card():
    o = op({"format": "html", "html": {"items": "body", "fields": {
        "title": "#task .hero h1", "content": "#task .hero", "image": "#task img@src",
        "none": "#nothing"}}})  # fmt: skip
    assert extract_html(o, PAGE) == {
        "title": "Заголовок", "content": "Заголовок\nТекст", "image": "a.png", "none": None,
    }  # fmt: skip
    empty = op({"format": "html", "html": {"items": "#absent", "fields": {"t": "h1"}}})
    assert extract_html(empty, PAGE) == {"t": None}


@pytest.mark.parametrize("selector", ["div > p", "a[href]", "#a#b", ".x@", "p@a b"])
def test_unsupported_selectors_are_format_errors(selector):
    o = op({"format": "html", "html": {"items": "body", "fields": {"f": selector}}})
    with pytest.raises(SourceFormatError):
        extract_html(o, PAGE)
