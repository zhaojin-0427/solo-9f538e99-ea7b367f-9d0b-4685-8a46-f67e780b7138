"""Quarantine intake (待核收件) and revalidation flows."""
from __future__ import annotations

import threading

from conftest import create_package


def _with_unknown_concept(samples: dict) -> bytes:
    """feasible instance + one numeric fact for a concept the XSD lacks."""
    return samples["feasible"].replace(
        b'<acct:Cash contextRef="ctx1" unitRef="usd" decimals="0">100</acct:Cash>',
        b'<acct:Cash contextRef="ctx1" unitRef="usd" decimals="0">100</acct:Cash>\n'
        b'  <acct:CryptoAssets contextRef="ctx1" unitRef="usd" '
        b'decimals="0">25</acct:CryptoAssets>',
    )


def _xsd_v2(samples: dict) -> bytes:
    return samples["xsd"].replace(
        b'</xs:schema>',
        b'  <xs:element name="CryptoAssets" id="acct_CryptoAssets" '
        b'type="xs:decimal" abstract="false"/>\n</xs:schema>',
    )


def _revise_package(client, pkg, samples, xsd):
    r = client.post(
        f"/packages/{pkg['package_id']}/revisions",
        headers={"Expected-Revision": str(pkg["latest_revision"])},
        files={
            "xsd": ("acct.xsd", xsd, "application/xml"),
            "calculation": ("acct-calc.xml", samples["calc"], "application/xml"),
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_intake(client, pkg, xml):
    return client.post(
        f"/packages/{pkg['package_id']}/intakes",
        files={"instance": ("i.xml", xml, "application/xml")},
    )


def test_unknown_concept_is_quarantined_and_visible(client, samples):
    pkg = create_package(client, samples)
    xml = _with_unknown_concept(samples)

    # the normal endpoint still rejects it outright
    r = client.post(
        f"/packages/{pkg['package_id']}/instances",
        files={"instance": ("i.xml", xml, "application/xml")},
    )
    assert r.status_code == 422

    # the intake endpoint keeps the original bytes in the quarantine area
    r = _create_intake(client, pkg, xml)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["package_revision"] == 1
    assert body["unknown_concepts"] == ["CryptoAssets"]
    assert body["instance_id"] is None
    assert body["last_revalidation"] is None
    # all structural info was still parsed
    assert body["entity"] == "E-001"
    assert body["currency"] == "USD"
    assert len(body["facts"]) == 10
    intake_id = body["intake_id"]

    got = client.get(f"/intakes/{intake_id}").json()
    assert got["status"] == "pending"
    assert got["unknown_concepts"] == ["CryptoAssets"]

    # original bytes are downloadable and byte-identical
    raw = client.get(f"/intakes/{intake_id}/raw").content
    assert raw == xml

    # list endpoints / filters
    listing = client.get("/intakes").json()
    assert [i["intake_id"] for i in listing] == [intake_id]
    pending = client.get(
        "/intakes", params={"package_id": pkg["package_id"], "status": "pending"}
    ).json()
    assert len(pending) == 1
    assert client.get("/intakes", params={"status": "accepted"}).json() == []
    assert client.get("/intakes/nope").status_code == 404


def test_intake_still_rejects_other_structural_errors(client, samples):
    pkg = create_package(client, samples)

    # malformed XML
    r = _create_intake(client, pkg, b"<not-closed")
    assert r.status_code == 422

    # two entities
    extra_ctx = (
        b'<xbrli:context id="ctx2">'
        b'<xbrli:entity><xbrli:identifier scheme="s">E-002</xbrli:identifier></xbrli:entity>'
        b'<xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate>'
        b'<xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>'
        b'</xbrli:context>'
    )
    multi = _with_unknown_concept(samples).replace(
        b'</xbrli:context>', b'</xbrli:context>' + extra_ctx, 1
    )
    r = _create_intake(client, pkg, multi)
    assert r.status_code == 422
    assert "entity" in r.json()["error"]["message"]

    # dimensional context
    dim = _with_unknown_concept(samples).replace(
        b'<xbrli:context id="ctx1">',
        b'<xbrli:context id="ctx1"><xbrli:scenario>'
        b'<xbrldi:explicitMember xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        b'dimension="acct:Region">acct:US</xbrldi:explicitMember>'
        b'</xbrli:scenario>',
    )
    r = _create_intake(client, pkg, dim)
    assert r.status_code == 422
    assert "dimension" in r.json()["error"]["message"]

    # missing decimals on the unknown concept itself
    no_dec = samples["feasible"].replace(
        b'<acct:Cash contextRef="ctx1" unitRef="usd" decimals="0">100</acct:Cash>',
        b'<acct:Cash contextRef="ctx1" unitRef="usd" decimals="0">100</acct:Cash>\n'
        b'  <acct:CryptoAssets contextRef="ctx1" unitRef="usd">25</acct:CryptoAssets>',
    )
    r = _create_intake(client, pkg, no_dec)
    assert r.status_code == 422
    assert "decimals" in r.json()["error"]["message"]

    # nothing was quarantined
    assert client.get("/intakes").json() == []


def test_intake_rejects_instance_that_current_package_already_accepts(client, samples):
    pkg = create_package(client, samples)
    r = _create_intake(client, pkg, samples["feasible"])
    assert r.status_code == 422
    assert "normal instance endpoint" in r.json()["error"]["message"]


def test_revalidation_failure_keeps_bytes_and_records_reason(client, samples):
    pkg = create_package(client, samples)
    xml = _with_unknown_concept(samples)
    intake = _create_intake(client, pkg, xml).json()

    # revalidation against an unchanged package still fails
    r = client.post(
        f"/intakes/{intake['intake_id']}/revalidate",
        headers={"Expected-Package-Revision": "1"},
    )
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack["instance"] is None
    rev = ack["intake"]["last_revalidation"]
    assert rev["result"] == "failed"
    assert rev["package_revision"] == 1
    assert rev["error_code"] == "bad_payload"
    assert "CryptoAssets" in rev["message"]

    # still pending, original bytes retained
    stored = client.get(f"/intakes/{intake['intake_id']}").json()
    assert stored["status"] == "pending"
    assert client.get(f"/intakes/{intake['intake_id']}/raw").content == xml
    # no instance was created
    assert client.get("/instances").json() == []


def test_revalidation_against_stale_revision_is_refused(client, samples):
    pkg = create_package(client, samples)
    xml = _with_unknown_concept(samples)
    intake = _create_intake(client, pkg, xml).json()

    _revise_package(client, pkg, samples, _xsd_v2(samples))  # package now at rev 2

    # client still claims rev 1 -> publication refused, intake untouched
    r = client.post(
        f"/intakes/{intake['intake_id']}/revalidate",
        headers={"Expected-Package-Revision": "1"},
    )
    assert r.status_code == 412
    assert r.json()["error"]["code"] == "stale_revision"

    stored = client.get(f"/intakes/{intake['intake_id']}").json()
    assert stored["status"] == "pending"
    assert stored["last_revalidation"] is None


def test_successful_revalidation_publishes_original_bytes_as_instance(client, samples):
    pkg = create_package(client, samples)
    xml = _with_unknown_concept(samples)
    intake = _create_intake(client, pkg, xml).json()
    _revise_package(client, pkg, samples, _xsd_v2(samples))  # package rev 2

    r = client.post(
        f"/intakes/{intake['intake_id']}/revalidate",
        headers={"Expected-Package-Revision": "2"},
    )
    assert r.status_code == 200, r.text
    ack = r.json()
    assert ack["instance"] is not None
    instance = ack["instance"]
    assert instance["package_id"] == pkg["package_id"]
    assert instance["latest_revision"] == 1
    concepts = {f["concept"] for f in instance["latest"]["facts"]}
    assert "acct:CryptoAssets" in concepts

    # intake now points at the formal instance and records the acceptance
    got = client.get(f"/intakes/{intake['intake_id']}").json()
    assert got["status"] == "accepted"
    assert got["instance_id"] == instance["instance_id"]
    assert got["last_revalidation"]["result"] == "accepted"
    assert got["last_revalidation"]["package_revision"] == 2
    assert got["last_revalidation"]["instance_id"] == instance["instance_id"]

    # the formal instance stores the exact original intake bytes
    detail = client.get(f"/instances/{instance['instance_id']}").json()
    assert detail["instance_id"] == instance["instance_id"]

    # it is a fully working instance: normal checks/reports still function
    r = client.post(
        f"/instances/{instance['instance_id']}/checks",
        headers={
            "Expected-Instance-Revision": "1",
            "Expected-Package-Revision": "2",
        },
    )
    assert r.status_code == 200, r.text
    report = r.json()["report"]
    assert report["status"] in ("feasible", "infeasible", "pending")
    # report snapshot carries the original bytes too
    snap = client.get(f"/reports/{report['report_id']}/snapshots/instance").content
    assert snap == xml

    # raw intake bytes remain available and unchanged
    assert client.get(f"/intakes/{intake['intake_id']}/raw").content == xml


def test_repeated_revalidation_is_idempotent(client, samples):
    pkg = create_package(client, samples)
    intake = _create_intake(client, pkg, _with_unknown_concept(samples)).json()
    _revise_package(client, pkg, samples, _xsd_v2(samples))

    first = client.post(
        f"/intakes/{intake['intake_id']}/revalidate",
        headers={"Expected-Package-Revision": "2"},
    ).json()
    second = client.post(
        f"/intakes/{intake['intake_id']}/revalidate",
        headers={"Expected-Package-Revision": "2"},
    ).json()

    assert second["instance"]["instance_id"] == first["instance"]["instance_id"]
    # still exactly one instance and one accepted intake
    assert len(client.get("/instances").json()) == 1
    accepted = client.get("/intakes", params={"status": "accepted"}).json()
    assert len(accepted) == 1


def test_concurrent_revalidations_create_at_most_one_instance(client, samples):
    from app import storage

    pkg = create_package(client, samples)
    intake = _create_intake(client, pkg, _with_unknown_concept(samples)).json()
    _revise_package(client, pkg, samples, _xsd_v2(samples))

    errors: list[Exception] = []
    results: list[dict] = []

    def worker():
        try:
            results.append(
                storage.revalidate_intake(intake["intake_id"], 2)
            )
        except Exception as exc:  # noqa: BLE001 - surfaced via assertions below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # one winner published; the rest were either collapsed onto it or refused
    # with a 409 while it was in flight.
    accepted = [r for r in results if r["instance"] is not None]
    instance_ids = {r["instance"]["instance_id"] for r in accepted}
    assert len(instance_ids) <= 1
    assert all(
        getattr(e, "code", None) == "revalidation_in_progress" for e in errors
    )
    assert len(instance_ids) == 1  # exactly one publication happened
    assert len(client.get("/instances").json()) == 1
    got = client.get(f"/intakes/{intake['intake_id']}").json()
    assert got["status"] == "accepted"
    assert got["instance_id"] == next(iter(instance_ids))


def test_failed_revalidation_leaves_no_half_instance_or_accepted_state(
    client, samples, monkeypatch
):
    from app import storage as storage_mod

    pkg = create_package(client, samples)
    intake = _create_intake(client, pkg, _with_unknown_concept(samples)).json()
    _revise_package(client, pkg, samples, _xsd_v2(samples))

    # Fail inside the publication transaction, after the instance rows were
    # inserted but before COMMIT.
    def boom():
        raise RuntimeError("simulated in-transaction failure")

    monkeypatch.setattr(storage_mod, "_before_intake_commit", boom)
    import pytest
    with pytest.raises(RuntimeError):
        storage_mod.revalidate_intake(intake["intake_id"], 2)
    monkeypatch.undo()

    # transaction rolled back: no instance rows, intake still pending
    assert client.get("/instances").json() == []
    got = client.get(f"/intakes/{intake['intake_id']}").json()
    assert got["status"] == "pending"
    assert got["instance_id"] is None

    # a later, healthy revalidation still succeeds exactly once
    ack = client.post(
        f"/intakes/{intake['intake_id']}/revalidate",
        headers={"Expected-Package-Revision": "2"},
    ).json()
    assert ack["instance"] is not None
    assert len(client.get("/instances").json()) == 1


def test_intakes_survive_restart(client, samples, tmp_path, monkeypatch):
    from app import db

    pkg = create_package(client, samples)
    xml = _with_unknown_concept(samples)
    intake = _create_intake(client, pkg, xml).json()

    db.reset_conn()
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c2:
        got = c2.get(f"/intakes/{intake['intake_id']}")
        assert got.status_code == 200
        body = got.json()
        assert body["status"] == "pending"
        assert body["package_revision"] == 1
        assert c2.get(f"/intakes/{intake['intake_id']}/raw").content == xml
        # existing endpoints keep working
        assert c2.get("/packages").json()
        assert c2.get("/instances").json() == []


def test_intake_records_the_package_revision_seen_at_upload(client, samples):
    pkg = create_package(client, samples)
    xml = _with_unknown_concept(samples)
    first = _create_intake(client, pkg, xml).json()
    assert first["package_revision"] == 1

    _revise_package(client, pkg, samples, _xsd_v2(samples))
    # a second upload against the new package, still carrying a different
    # undeclared concept, records the new revision
    xml2 = samples["feasible"].replace(
        b'<acct:Cash contextRef="ctx1" unitRef="usd" decimals="0">100</acct:Cash>',
        b'<acct:Cash contextRef="ctx1" unitRef="usd" decimals="0">100</acct:Cash>\n'
        b'  <acct:DigitalTokens contextRef="ctx1" unitRef="usd" '
        b'decimals="0">7</acct:DigitalTokens>',
    )
    second = _create_intake(client, pkg, xml2).json()
    assert second["package_revision"] == 2
    assert second["unknown_concepts"] == ["DigitalTokens"]
    ids = [i["intake_id"] for i in client.get("/intakes").json()]
    assert ids == [first["intake_id"], second["intake_id"]]
