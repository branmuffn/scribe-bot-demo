"""Coverage sweep (2026-11-25, Brian: "every button ... on every page"): on the made-up demo data, open every page the
app links to and press every button: each form is sent once per kind (as the page fills it in), and every link and
htmx call is followed. Nothing may answer with a server error, and no page link may lead nowhere."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


from tests.unit.test_public_demo import demo  # noqa: F401  (the demo fixture)

SKIP = re.compile(r"^/(audio|static|sw\.js|manifest|health|ping|progress|status/(ring|machine))|export\.|\.txt$|^/notifications/\d+/open")
START = ["/", "/inbox", "/inbox?show=older", "/conversations", "/conversations?trash=1", "/people", "/tasks", "/tasks?tab=waiting",
         "/tasks?tab=decisions", "/tasks?tab=unsure", "/tasks?tab=done", "/review", "/search?q=budget", "/search/advanced",
         "/notifications", "/notifications?tab=all", "/capture", "/status", "/costs", "/help", "/more", "/settings", "/processing",
         "/speaking", "/cleanup", "/decisions"]


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.forms, self.hx, self.form, self.by_id = set(), [], set(), None, {}

    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == "a" and a.get("href") and "download" not in a:
            self.links.add(a["href"])
        for k in ("hx-get", "hx-post", "hx-delete"):
            if a.get(k):
                self.hx.add((k[3:].upper(), a[k]))
        if tag == "form":
            self.form = {"method": (a.get("method") or "get").upper(), "action": a.get("action") or "", "fields": {}, "id": a.get("id"),
                         "required": set(), "form_id": a.get("id")}
            self.forms.append(self.form)
            if a.get("id"):
                self.by_id[a["id"]] = self.form
        elif a.get("name") and (self.form is not None or a.get("form") in self.by_id):
            form = self.by_id.get(a.get("form")) or self.form
            t = a.get("type", "")
            if tag == "input" and t in ("checkbox", "radio") and "checked" not in a:
                return
            if "required" in a:
                form["required"].add(a["name"])
            if tag in ("input", "textarea", "select", "button") and a["name"] not in form["fields"]:
                form["fields"][a["name"]] = a.get("value", "")
        if tag == "option" and self.form is not None and self.form["fields"] and a.get("value"):
            last = list(self.form["fields"])[-1]
            if self.form["fields"][last] == "":
                self.form["fields"][last] = a["value"]

    def handle_endtag(self, tag):
        if tag == "form":
            self.form = None


def kind(url):
    return re.sub(r"\d+", "N", urlsplit(url).path)


def test_every_page_link_and_button_answers(demo):  # noqa: F811
    seen, queue, problems, pressed, refused = set(), list(START), [], set(), []
    while queue:
        url = queue.pop(0)
        if url in seen or len(seen) > 400:
            continue
        seen.add(url)
        r = demo.get(url)
        if r.status_code >= 500 or (r.status_code == 404 and not url.startswith("/gone")):
            problems.append(f"GET {url} -> {r.status_code}")
            continue
        if "text/html" not in r.headers.get("content-type", ""):
            continue
        p = Page()
        p.feed(r.text)
        # F43: Inbox and Today showed a bare "Scribe Bot" in the browser tab and history; every page names itself
        t = re.search(r"<title>([^<]*)</title>", r.text)
        if t and t.group(1).strip() == "Scribe Bot":
            problems.append(f"GET {url}: the tab title doesn't say which page this is")
        for h in p.links:
            u = urljoin(url, h)
            if u.startswith("http") or u.startswith("#") or SKIP.search(u) or u in seen:
                continue
            u = u.split("#")[0]
            if kind(u) not in {kind(s) for s in seen} or u in START:
                queue.append(u)
        for f in p.forms:
            act = urljoin(url, f["action"] or url)
            if f["method"] != "POST" or SKIP.search(act) or (kind(act), tuple(sorted(f["fields"]))) in pressed:
                continue
            pressed.add((kind(act), tuple(sorted(f["fields"]))))
            r2 = demo.post(act, data=f["fields"], follow_redirects=True)
            if r2.status_code >= 500:
                problems.append(f"POST {act} {f['fields']} (from {url}) -> {r2.status_code}")
            elif r2.status_code == 422 and "__" not in act:
                blank = [k for k, v in f["fields"].items() if v == "" and k not in f["required"]]
                missing = [e["loc"][1] for e in r2.json().get("detail", []) if isinstance(e, dict) and len(e["loc"]) > 1]
                # the tag box is novalidate on purpose: its script ignores an empty Add (ui.js, the "#tags" submit handler)
                unguarded = [k for k in missing if k not in f["required"] and not act.endswith("/tags")]
                if unguarded:
                    problems.append(f"POST {act} (from {url}): empty {unguarded} is refused but not marked required {blank}")
            elif r2.status_code >= 400:
                refused.append(f"POST {act} {f['fields']} (from {url}) -> {r2.status_code} {r2.text[:80]!r}")
        for m, h in p.hx:
            u = urljoin(url, h)
            if SKIP.search(u) or ("hx", m, kind(u)) in pressed:
                continue
            pressed.add(("hx", m, kind(u)))
            r3 = demo.request(m, u)
            if r3.status_code >= 500:
                problems.append(f"{m} {u} (from {url}) -> {r3.status_code}")
    print("pages", len(seen), "presses", len(pressed), *refused, sep="\n")
    assert not problems, "\n".join(problems)
    assert len(seen) > 60 and len(pressed) > 80, (len(seen), len(pressed))
