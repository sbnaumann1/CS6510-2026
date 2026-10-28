"""The event envelope, the schemas and the topic manifest agree (spec 008 FR-004, FR-003)."""

from __future__ import annotations

import json
import pathlib

import jsonschema
import orjson
import pytest
import yaml

from checkout_web import events as ev

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "events" / "schemas"

SAMPLES = {
    ev.ITEM_PUBLISHED: {"sku": "SKU-00042", "name": "Oat Milk 1L", "priceCents": 429},
    ev.CATALOG_PUBLISHED: {"publishId": "b7a1", "itemCount": 2000},
    ev.SCAN_RECORDED: {"scanId": 120431, "transactionId": "tx-9017", "sku": "SKU-00042"},
    ev.SCAN_LOG_RESET: {"logId": "5d1e"},
    ev.STOCK_RESERVATION_REQUESTED: {
        "transactionId": "tx-9017",
        "epoch": 1,
        "lines": [{"sku": "SKU-00042", "quantity": 2}],
    },
    ev.STOCK_RELEASE_REQUESTED: {"transactionId": "tx-9017", "epoch": 1},
    ev.STOCK_RESERVED: {"transactionId": "tx-9017", "epoch": 1, "inReplyTo": "e1", "replayed": False},
    ev.STOCK_REJECTED: {"transactionId": "tx-9017", "epoch": 1, "inReplyTo": "e1", "shortSkus": ["SKU-1"]},
    ev.STOCK_RELEASED: {
        "transactionId": "tx-9017", "epoch": 1, "inReplyTo": "e1", "unitsRestored": 3, "alreadyReleased": False,
    },
    ev.RESERVATION_REFUSED: {"transactionId": "tx-9017", "epoch": 1, "inReplyTo": "e1"},
    ev.DEAD_LETTER: {
        "sourceTopic": "checkout.completions.v1", "partition": 3, "offset": 7, "consumer": "inventory",
        "attempts": 5, "error": "boom", "event": {"x": 1},
    },
}


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.json").read_text())


def test_every_event_type_has_a_schema_and_a_sample():
    assert set(SAMPLES) == set(ev.EVENT_TYPES)
    for t in ev.EVENT_TYPES:
        assert (SCHEMAS / f"{t}.json").exists(), t
    assert (SCHEMAS / "envelope.json").exists()


@pytest.mark.parametrize("etype", ev.EVENT_TYPES)
def test_envelope_and_payload_validate(etype):
    event = ev.make_event(etype, "tester", SAMPLES[etype], correlation_id="req-1")
    body = orjson.loads(ev.encode(event))
    jsonschema.validate(body, _schema("envelope"))
    jsonschema.validate(body["data"], _schema(etype))


@pytest.mark.parametrize("etype", ev.EVENT_TYPES)
def test_unknown_extra_fields_are_accepted(etype):
    jsonschema.validate({**SAMPLES[etype], "addedLater": True}, _schema(etype))


def test_round_trip():
    e = ev.make_event(ev.SCAN_RECORDED, "checkout", SAMPLES[ev.SCAN_RECORDED], correlation_id="req-9")
    assert ev.decode(ev.encode(e)) == e


def test_correlation_id_defaults_to_the_current_request_id():
    from checkout_web.request import _request_id

    token = _request_id.set("req-abc")
    try:
        e = ev.make_event(ev.SCAN_RECORDED, "checkout", SAMPLES[ev.SCAN_RECORDED])
    finally:
        _request_id.reset(token)
    assert e.correlation_id == "req-abc"
    assert ev.headers(e)["x-request-id"] == "req-abc"
    assert ev.headers(e)["x-event-type"] == ev.SCAN_RECORDED


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[]", b'{"version": 1}', b'{"version": 2, "type": "x", "producer": "p", "data": {}, "eventId": "e", "occurredAt": "t"}'],
)
def test_malformed_or_unknown_major_version_raises(raw):
    with pytest.raises(ev.EventDecodeError):
        ev.decode(raw)


def test_topics_constants_match_the_manifest():
    manifest = yaml.safe_load((ROOT / "events" / "topics.yaml").read_text())["topics"]
    assert set(manifest) == set(ev.TOPICS)
    for name, spec in ev.TOPICS.items():
        assert manifest[name]["partitions"] == spec.partitions, name
        assert bool(manifest[name]["compact"]) == spec.compact, name


def test_create_topics_script_is_generated_from_the_manifest():
    import importlib.util

    spec = importlib.util.spec_from_file_location("create_topics", ROOT / "scripts" / "create_topics.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (ROOT / "events" / "create-topics.sh").read_text() == mod.render(), (
        "events/create-topics.sh is stale: run `uv run python scripts/create_topics.py --write`"
    )
