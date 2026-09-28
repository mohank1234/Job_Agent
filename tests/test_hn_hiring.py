from jobagent.sources import hn_hiring

POST = {"id": 101, "text": (
    "Acme Robotics | Senior QA Automation Engineer | Remote (India OK) | Full-time<p>"
    "We build warehouse robots. Apply: "
    '<a href="https://jobs.ashbyhq.com/acme-robotics/123">https://jobs.ashbyhq.com/acme-robotics/123</a>'
    "<p>Email the founder: jane [at] acmerobotics [dot] com, or privacy@acmerobotics.com for data requests.")}


def test_parse_post_reads_company_boards_and_published_email():
    post = hn_hiring.parse_post(POST)
    assert post["company"] == "Acme Robotics"
    assert post["boards"] == ["https://jobs.ashbyhq.com/acme-robotics/123"]
    assert post["emails"] == ["jane@acmerobotics.com"]


def test_contact_matches_by_board_slug_or_name_only():
    posts = [{**hn_hiring.parse_post(POST), "thread": "September 2026"}]
    by_board = hn_hiring.contact_for("acme-robotics", ["acme-robotics"], posts)
    assert by_board["Public Work Email"] == "jane@acmerobotics.com"
    assert by_board["Email Source"] == "https://news.ycombinator.com/item?id=101"
    from jobagent.outreach.report_drafts import draft_recipient
    assert draft_recipient(by_board) == "jane@acmerobotics.com"
    assert hn_hiring.contact_for("Acme Robotics", [], posts)["Public Work Email"] == "jane@acmerobotics.com"
    assert hn_hiring.contact_for("Other Co", ["other"], posts) == {}


def test_webmail_and_role_inboxes_are_not_contacts():
    post = hn_hiring.parse_post({"id": 1, "text": "X | QA<p>me@gmail.com sales@x.io noreply@x.io"})
    assert post["emails"] == []
