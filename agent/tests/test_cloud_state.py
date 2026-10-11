import io
import json
import zipfile

import pytest
from cryptography.fernet import Fernet, InvalidToken

from jobagent.cloud_state import ARCHIVE, prepare_state, restore_state, save_state


def test_private_state_roundtrip_excludes_credentials(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir(); target.mkdir()
    (source / "research").mkdir()
    (source / "research/contact.json").write_text('{"private":"contact"}')
    (source / "gmail_report_drafts.json").write_text('{"sent":true}')
    (source / "token.json").write_text("oauth-secret")
    key = Fernet.generate_key()
    save_state(source, key)
    encrypted = (source / ARCHIVE).read_bytes()
    assert b"contact" not in encrypted and b"oauth-secret" not in encrypted
    (target / ARCHIVE).write_bytes(encrypted)
    assert restore_state(target, key)
    assert (target / "research/contact.json").read_text() == '{"private":"contact"}'
    assert not (target / "token.json").exists()
    with pytest.raises(InvalidToken):
        restore_state(target, Fernet.generate_key())


@pytest.mark.parametrize("name", ["../token.json", "research/../../token.json", "token.json", "/etc/passwd", "research\\..\\token.json"])
def test_restore_rejects_unexpected_paths_before_writing(tmp_path, name):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("research/valid.json", "{}")
        archive.writestr(name, "bad")
    key = Fernet.generate_key()
    (tmp_path / ARCHIVE).write_bytes(Fernet(key).encrypt(data.getvalue()))
    with pytest.raises(ValueError):
        restore_state(tmp_path, key)
    assert not (tmp_path / "research/valid.json").exists()


def test_bootstrap_preserves_newer_drive_and_sent_state(tmp_path):
    (tmp_path / "config.yaml").write_text("morning:\n  reviewed_contacts_file: research/contacts.json\n")
    boot = {"version":"review-1", "drive_output_state":{"files":{"sheet":"reviewed"}},
            "sent_outreach":{"company":{"state":"sent", "sent_at":"2026-09-10"}}}
    prepare_state(tmp_path, boot, {"example":{"email":"public@example.com"}})
    drive = tmp_path / "drive_output_state.json"
    ledger = tmp_path / "gmail_report_drafts.json"
    assert json.loads(ledger.read_text())["company"]["state"] == "sent"
    drive.write_text('{"files":{"sheet":"newer"}}')
    ledger.write_text('{"company":{"state":"sent","sent_at":"2026-09-11"}}')
    prepare_state(tmp_path, boot, {})
    assert json.loads(drive.read_text())["files"]["sheet"] == "newer"
    boot["version"] = "review-2"
    prepare_state(tmp_path, boot, {})
    assert json.loads(ledger.read_text())["company"]["sent_at"] == "2026-09-11"


def test_configured_resume_is_validated_and_added_to_manifest(tmp_path):
    from docx import Document
    (tmp_path / "research/report").mkdir(parents=True)
    resume = tmp_path / "research/report/resume.docx"
    Document().save(resume)
    (tmp_path / "config.yaml").write_text("morning:\n  resume_attachment: research/report/resume.docx\n")
    prepare_state(tmp_path, {}, {})
    assert json.loads((resume.parent / "deliverables.json").read_text())["files"] == ["resume.docx"]
    resume.write_bytes(b"invalid")
    with pytest.raises(ValueError):
        prepare_state(tmp_path, {}, {})
