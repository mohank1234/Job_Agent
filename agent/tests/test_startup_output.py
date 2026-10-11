import json

import pytest

from jobagent.drive_output import Publisher, DriveOutputError
from jobagent.startup_output import load_research, enrich_rows
from tests.test_output import good_row, FakeDrive, bundle
from jobagent.runtime import now_iso


def entry():
    row = good_row()
    return {"Company": "Example", "Job Link": row["Job Link"],
            "Research Checked At": now_iso(), "Draft JD SHA256": row["JD SHA256"],
            "Investment Source": "https://example.com/funding", "Manager Source": "https://example.com/team",
            "Cold Email": "A grounded draft", "LinkedIn Note": "A short note"}


@pytest.mark.parametrize("changes", [{"JD SHA256": "changed"}, {"JD Status": "not_currently_listed"},
                                    {"Fit Status": "out_of_scope"}, {"JD Verified At": "2000-01-01T00:00:00Z"}])
def test_draft_withheld_when_job_changes_closes_or_falls_outside_scope(changes):
    rows = enrich_rows([good_row(**changes)], {"roles": [entry()]})
    assert not rows[0]["Cold Email"] and not rows[0]["LinkedIn Note"]
    assert rows[0]["Draft Status"].startswith("Withheld")


def test_old_contact_research_does_not_get_new_date_on_jd_refresh():
    old = {**entry(), "Research Checked At": "2000-01-01T00:00:00Z"}
    row = enrich_rows([good_row()], {"roles": [old]})[0]
    assert row["Research Checked At"] == old["Research Checked At"]
    assert not row["Cold Email"]


def test_unmatched_old_exports_cannot_leak_old_drafts():
    row = good_row(**{"Cold Email": "outdated", "Manager Name": "outdated"})
    assert enrich_rows([row], {"roles": []}) == []
    assert "Cold Email" not in row and "Manager Name" not in row


@pytest.mark.parametrize("changes", [{"LinkedIn Note": "x" * 301}, {"Public Work Email": "person@example.com"}])
def test_catalog_rejects_overlength_notes_and_emails_without_evidence(tmp_path, changes):
    (tmp_path / "startup-research.json").write_text(json.dumps({"version": 1, "roles": [{**entry(), **changes}]}))
    with pytest.raises(ValueError):
        load_research(tmp_path)


def test_stretch_requirement_is_not_automatically_ready():
    research = {**entry(), "Requires Fit Review": True, "Requirements To Confirm": "Advanced TypeScript not established"}
    row = enrich_rows([good_row()], {"roles": [research]})[0]
    assert row["Fit Status"] == "needs_review"
    assert row["LinkedIn Note Characters"] == str(len(row["LinkedIn Note"]))


@pytest.mark.parametrize("name", ["../token.json", "C:/token.json", "verification.json", "missing.md"])
def test_bad_manifest_stops_before_remote_writes(tmp_path, name):
    bundle(tmp_path)
    (tmp_path / "deliverables.json").write_text(json.dumps({"version": 1, "files": [name]}))
    drive = FakeDrive()
    with pytest.raises(DriveOutputError):
        Publisher(drive, tmp_path / "state.json").publish(tmp_path)
    assert not drive.items


def test_all_manifest_outputs_and_receipt_are_in_single_folder(tmp_path):
    bundle(tmp_path)
    (tmp_path / "Outreach.md").write_text("draft only")
    (tmp_path / "deliverables.json").write_text(json.dumps({"version": 1, "files": ["Outreach.md"]}))
    drive = FakeDrive()
    publisher = Publisher(drive, tmp_path / "state.json")
    result = publisher.publish(tmp_path)
    assert len(drive.items) == 10
    for item in drive.items.values():
        if item["id"] != result["folder_id"]:
            assert item["parents"] == [result["folder_id"]]
    receipt = next(v for v in drive.items.values() if v["name"] == "publication.json")
    assert receipt["data"] == (tmp_path / "publication.json").read_bytes()
    names = {v["name"] for v in drive.items.values()}
    assert {"Outreach.md", "JobAgent Report.xlsx", "deliverables.json"} <= names


def test_trashed_optional_tracker_is_not_restored_or_recreated(tmp_path):
    bundle(tmp_path)
    drive = FakeDrive()
    publisher = Publisher(drive, tmp_path / "state.json")
    first = publisher.publish(tmp_path)
    tracker = first["files"]["application_tracker"]
    drive.items[tracker]["trashed"] = True
    size = len(drive.items)
    result = publisher.publish(tmp_path)
    assert result["tracker_url"] is None and "trashed" in result["tracker_status"]
    assert len(drive.items) == size and drive.items[tracker]["trashed"]
    assert "application_tracker" not in result["files"]
