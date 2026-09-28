from types import SimpleNamespace

from jobagent.outreach import website_contacts as w

ABOUT = """<html><body>
<h2>Our team</h2><div>Caleb Peffer<br>Co-Founder &amp; CEO</div><div>Nicolas Camara<br>Co-Founder &amp; CTO</div>
<h2>Backed by</h2><div>Tobias Lutke<br>CEO of Shopify</div>
</body></html>"""
HOME = """<html><body><p>"Great product" - Jane Quote, CEO</p>
<a href="mailto:hello@acme.dev">Say hello</a> Reach our CEO: caleb [at] acme [dot] dev
<p>Support: support@acme.dev, careers@acme.dev, someone@gmail.com</p></body></html>"""


def site(pages):
    return w.read_site("https://acme.dev", client=SimpleNamespace(close=lambda: None),
                       get=lambda client, url: SimpleNamespace(text=pages[url]) if url in pages else (_ for _ in ()).throw(OSError()))


def test_leaders_come_from_team_pages_not_testimonials_or_investors():
    found = site({"https://acme.dev": HOME, "https://acme.dev/about": ABOUT})
    assert [(p["name"], p["title"]) for p in found["leaders"]] == [
        ("Caleb Peffer", "Co-Founder & CEO"), ("Nicolas Camara", "Co-Founder & CTO")]


def test_published_leader_address_is_used_and_shared_inboxes_are_not():
    found = site({"https://acme.dev": HOME, "https://acme.dev/about": ABOUT})
    assert {e["email"] for e in found["emails"]} == {"hello@acme.dev", "caleb@acme.dev", "support@acme.dev",
                                                     "careers@acme.dev"}
    contact = w.published_contact(found, [], "acme.dev")
    assert contact["Public Work Email"] == "caleb@acme.dev" and contact["Email Contact Name"] == "Caleb Peffer"
    assert contact["Email Provider"] == "COMPANY_WEBSITE" and contact["Email Verification Status"] == "published"
    from jobagent.outreach.founders import verified_leadership_contact
    from jobagent.outreach.report_drafts import draft_recipient
    assert verified_leadership_contact(contact, "acme.dev") and draft_recipient(contact) == "caleb@acme.dev"


def test_founders_inbox_counts_but_nothing_is_guessed():
    only_inbox = site({"https://acme.dev/contact": '<a href="mailto:founders@acme.dev">Founders</a>'})
    contact = w.published_contact(only_inbox, [], "acme.dev")
    assert contact["Public Work Email"] == "founders@acme.dev" and contact["Email Contact Role"] == "Founders"
    nothing = site({"https://acme.dev/about": ABOUT, "https://acme.dev/contact": "<a href='mailto:hello@acme.dev'>x</a>"})
    # Leaders are known, but no address of theirs is published: never construct one.
    assert w.published_contact(nothing, [], "acme.dev") == {}
