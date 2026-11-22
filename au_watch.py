#!/usr/bin/env python3
"""
AU LLB Notification Watcher (laptop edition)

Watches the Andhra University Directorate of Admissions page (and the APLAWCET
portal) for new or changed notifications and alerts you by:
  - phone push (ntfy app)        - every alert, louder for law/spot items
  - WhatsApp (CallMeBot, free)    - law and spot/vacancy items only
  - email (Gmail)                 - every new notice; law PDFs attached
  - opening the notice in your browser on the laptop (law items)

Standard library only. Windows Task Scheduler runs it every few minutes.

  python au_watch.py            normal check (what the scheduler runs)
  python au_watch.py --test     send a test message on every channel
  python au_watch.py --status   print what the watcher knows
  python au_watch.py --reset    forget seen links (next run re-baselines)
"""
import hashlib, json, logging, os, re, smtplib, socket, ssl, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from html.parser import HTMLParser
from logging.handlers import RotatingFileHandler
from pathlib import Path

HOME = Path(os.environ.get("AUWATCH_HOME") or Path(__file__).resolve().parent)
CFG_FILE, STATE_FILE = HOME / "config.json", HOME / "state.json"
LOCK_FILE, LOG_FILE = HOME / "run.lock", HOME / "watch.log"

DEFAULTS = {
    "pages": [
        "https://audoa.andhrauniversity.edu.in/",
        "https://cets.apsche.ap.gov.in/LAWCET/LAWCET/LAWCET_HomePage",
    ],
    # Tier 1 (URGENT): anything about law admissions.
    "law_keywords": [r"LL\.?\s?B", r"LL\.?\s?M", r"LAWCET", r"\bLaw\b", r"Ambedkar College"],
    # Tier 2 (IMPORTANT): leftover-seat style notices for any course.
    "spot_keywords": [r"spot", r"left[\s-]?over", r"vacan", r"remaining seats", r"final phase"],
    "gmail_user": "", "gmail_app_password": "", "mail_to": "",
    "email_every_new_notice": True,
    "ntfy_topic": "", "ntfy_server": "https://ntfy.sh",
    "whatsapp_phone": "", "callmebot_apikey": "",
    "open_browser_on_law_alert": True,
    "save_pdfs_to": str(Path.home() / "Downloads" / "AU_LLB_Notices"),
    "heartbeat_hour": 9,            # daily "still alive" message at this local hour (-1 = off)
    "site_down_alert_hours": 3,     # warn if the AU site can't be read for this long
    "doc_recheck_minutes": 30,      # how often to re-check law PDFs for silent re-uploads
    "flood_threshold": 25,          # more new links than this at once = page redesign
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AU-notice-watcher/2.0"
log = logging.getLogger("auwatch")


# ----------------------------------------------------------------- utilities
def setup_logging():
    if log.handlers:
        return
    HOME.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    if sys.stdout and (sys.stdout.isatty() or os.environ.get("AUWATCH_CLOUD") == "1"):
        log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)


def load_json(path, default):
    for p in (path, path.with_suffix(path.suffix + ".bak")):
        try:
            with open(p, encoding="utf-8-sig") as f:   # -sig: tolerate PowerShell's BOM
                return json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            log.error("Could not read %s (%s); trying backup", p.name, e)
    return default


def save_json(path, data):
    """Atomic write + keep the previous good copy as .bak (survives crashes/power loss)."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    if path.exists():
        try:
            os.replace(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass
    os.replace(tmp, path)


CLOUD = os.environ.get("AUWATCH_CLOUD") == "1"     # running on GitHub Actions
ENV_KEYS = {"NTFY_TOPIC": "ntfy_topic", "MAIL_TO": "mail_to", "GMAIL_USER": "gmail_user",
            "GMAIL_APP_PASS": "gmail_app_password", "WHATSAPP_PHONE": "whatsapp_phone",
            "CALLMEBOT_APIKEY": "callmebot_apikey", "HEARTBEAT_HOUR": "heartbeat_hour"}


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in load_json(CFG_FILE, {}).items() if v is not None})
    for env, key in ENV_KEYS.items():                 # GitHub Secrets override the file
        val = (os.environ.get(env) or "").strip()
        if val:
            cfg[key] = int(val) if key == "heartbeat_hour" else val
    if CLOUD:
        cfg["open_browser_on_law_alert"] = False
        cfg["save_pdfs_to"] = str(HOME / "pdfs")      # temporary, only for email attachments
    cfg["gmail_app_password"] = (cfg.get("gmail_app_password") or "").replace(" ", "")
    cfg["mail_to"] = cfg.get("mail_to") or cfg.get("gmail_user")
    return cfg


def acquire_lock():
    """One run at a time. A lock older than 15 min is from a crashed run; take it over."""
    try:
        if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < 900:
            return False
        LOCK_FILE.write_text(str(os.getpid()))
        return True
    except OSError:
        return False


def online():
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53), ("ntfy.sh", 443)):
        try:
            socket.create_connection((host, port), timeout=5).close()
            return True
        except OSError:
            continue
    return False


def http_get(url, max_bytes=25_000_000, tries=3):
    """GET with retries, backoff and a fallback for broken certificate chains."""
    last = None
    for attempt in range(tries):
        for verify in (True, False):
            ctx = None if verify else ssl._create_unverified_context()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=45, context=ctx) as r:
                    data = r.read(max_bytes + 1)
                    if len(data) > max_bytes:
                        raise ValueError("file too large")
                    return data, r.headers.get_content_charset() or "utf-8"
            except (ssl.SSLError, urllib.error.URLError) as e:
                last = e
                if verify and "CERTIFICATE" in str(e).upper():
                    log.warning("Certificate problem on %s; retrying without verification", url)
                    continue          # try unverified straight away
                break
            except Exception as e:
                last = e
                break
        if attempt < tries - 1:
            time.sleep(5 if attempt == 0 else 15)
    raise last or RuntimeError("download failed")


class LinkParser(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.links, self._href, self._buf = base, {}, None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._buf = dict(attrs).get("href"), []

    def handle_data(self, d):
        if self._href is not None:
            self._buf.append(d)

    def handle_endtag(self, tag):
        if tag != "a" or self._href is None:
            return
        href = (self._href or "").strip()
        self._href = None
        href = re.sub(r"^#+(?=https?://)", "", href)       # AU has hrefs like "#https://..."
        if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            return
        url = urllib.parse.urljoin(self.base, href).split("#")[0]
        text = " ".join(" ".join(self._buf).split())
        text = re.sub(r"^(Click here to View|Click here)\s*(Notification|Notofocation)?\s*(for)?\s*", "", text, flags=re.I)
        name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        if url not in self.links or len(text) > len(self.links[url]):
            self.links[url] = text or name


def is_pdf(url):
    return urllib.parse.urlparse(url).path.lower().endswith(".pdf")


# ------------------------------------------------------------- notifications
class Notifier:
    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state
        self.state.setdefault("outbox", [])

    def channels(self):
        c = self.cfg
        return {"push": bool(c["ntfy_topic"]),
                "email": bool(c["mail_to"] and (c["gmail_app_password"] or c["ntfy_topic"])),
                "whatsapp": bool(c["whatsapp_phone"] and c["callmebot_apikey"])}

    def send(self, title, body, tier="normal", url=None, attachments=None, channels=None):
        """tier: urgent | important | normal | info. Failed sends are queued and retried."""
        active = self.channels()
        want = channels or ["push", "email"] + (["whatsapp"] if tier in ("urgent", "important") else [])
        results = {}
        for ch in want:
            if not active.get(ch):
                continue
            msg = {"ch": ch, "title": title, "body": body, "tier": tier, "url": url,
                   "attachments": attachments or [], "created": time.time(), "tries": 0}
            ok = self._deliver(msg)
            results[ch] = ok
            if not ok:
                self.state["outbox"].append(msg)
        return results

    def flush_outbox(self):
        keep = []
        for msg in self.state.get("outbox", []):
            if time.time() - msg["created"] > 48 * 3600 or msg["tries"] >= 60:
                log.error("Giving up on %s message: %s", msg["ch"], msg["title"])
                continue
            if not self._deliver(msg):
                keep.append(msg)
        self.state["outbox"] = keep

    def _deliver(self, m):
        m["tries"] = m.get("tries", 0) + 1
        try:
            getattr(self, "_" + m["ch"])(m)
            log.info("Sent %s: %s", m["ch"], m["title"])
            return True
        except Exception as e:
            log.error("Failed %s (%s): %s", m["ch"], m["title"], e)
            if m["ch"] == "email" and self.cfg.get("ntfy_topic") and self.cfg.get("mail_to"):
                try:
                    self._email_relay(m)
                    log.info("Sent email via backup relay (no attachment): %s", m["title"])
                    return True
                except Exception as e2:
                    log.error("Backup email relay also failed: %s", e2)
            return False

    def _push(self, m):
        prio = {"urgent": 5, "important": 4, "normal": 3, "info": 2}[m["tier"]]
        tags = {"urgent": ["rotating_light", "scales"], "important": ["warning"],
                "normal": ["bell"], "info": ["white_check_mark"]}[m["tier"]]
        payload = {"topic": self.cfg["ntfy_topic"], "title": m["title"][:250],
                   "message": m["body"][:3800], "priority": prio, "tags": tags}
        if m.get("url"):
            payload["click"] = m["url"]
            payload["actions"] = [{"action": "view", "label": "Open notice", "url": m["url"]}]
        req = urllib.request.Request(self.cfg["ntfy_server"].rstrip("/") + "/",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=30).read()

    def _whatsapp(self, m):
        text = f"*{m['title']}*\n\n{m['body']}"
        if m.get("url"):
            text += f"\n\nOpen: {m['url']}"
        q = urllib.parse.urlencode({"phone": self.cfg["whatsapp_phone"], "text": text[:1200],
                                    "apikey": self.cfg["callmebot_apikey"]})
        req = urllib.request.Request("https://api.callmebot.com/whatsapp.php?" + q, headers={"User-Agent": UA})
        body = urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "ignore").lower()
        # Only treat clear key problems as failures; a false "failed" would cause repeat messages.
        if "apikey" in body and ("invalid" in body or "not valid" in body or "not activated" in body):
            raise RuntimeError("CallMeBot said: " + re.sub("<[^>]+>", " ", body)[:200])

    def _email(self, m):
        c = self.cfg
        if not (c["gmail_user"] and c["gmail_app_password"]):
            return self._email_relay(m)        # no Gmail set up: use the ntfy email route
        msg = EmailMessage()
        msg["Subject"] = ("🚨 " if m["tier"] == "urgent" else "") + m["title"]
        msg["From"], msg["To"] = c["gmail_user"], c["mail_to"]
        body = m["body"] + (f"\n\nOpen: {m['url']}" if m.get("url") else "")
        body += "\n\n-- AU LLB watcher on your laptop. Source: https://audoa.andhrauniversity.edu.in/"
        msg.set_content(body)
        total = 0
        for p in m.get("attachments", []):
            p = Path(p)
            if p.exists() and total + p.stat().st_size < 18_000_000:
                total += p.stat().st_size
                msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
        errors = []
        for port in (465, 587):          # 465 is blocked on some networks; 587 is the backup
            try:
                if port == 465:
                    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=40)
                else:
                    s = smtplib.SMTP("smtp.gmail.com", 587, timeout=40)
                    s.starttls(context=ssl.create_default_context())
                with s:
                    s.login(c["gmail_user"], c["gmail_app_password"])
                    s.send_message(msg)
                return
            except smtplib.SMTPAuthenticationError as e:
                raise RuntimeError("Gmail rejected the app password (re-run Install.bat and paste it again): "
                                   + str(e)[:120])
            except (OSError, smtplib.SMTPException) as e:
                errors.append(f"port {port}: {e}")
        raise RuntimeError("Gmail unreachable (antivirus/firewall?) - " + " | ".join(errors))

    def _email_relay(self, m):
        """Backup email route through ntfy when Gmail is blocked. Text only, no attachment.
        Uses a hidden second topic so your phone doesn't get the push twice."""
        to = self.cfg["mail_to"] or self.cfg["gmail_user"]
        body = m["body"] + (f"\n\nOpen: {m['url']}" if m.get("url") else "")
        if m.get("attachments"):
            body += "\n\n(PDF saved on your laptop in Downloads\\AU_LLB_Notices.)"
        payload = {"topic": self.cfg["ntfy_topic"] + "-mail", "title": m["title"][:250],
                   "message": body[:3800], "email": to}
        req = urllib.request.Request(self.cfg["ntfy_server"].rstrip("/") + "/",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=30).read()


# ------------------------------------------------------------------ the check
def classify(text, url, cfg):
    hay = f"{text} {urllib.parse.unquote(url)}"
    if any(re.search(k, hay, re.I) for k in cfg["law_keywords"]):
        return "urgent"
    if any(re.search(k, hay, re.I) for k in cfg["spot_keywords"]):
        return "important"
    return "normal"


def safe_name(text, url):
    base = urllib.parse.unquote(url.rsplit("/", 1)[-1]) or "notice.pdf"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)[:120]
    return base if base.lower().endswith(".pdf") else base + ".pdf"


def save_pdf(cfg, url, text, data):
    try:
        folder = Path(cfg["save_pdfs_to"])
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / (datetime.now().strftime("%Y-%m-%d_%H%M_") + safe_name(text, url))
        p.write_bytes(data)
        return str(p)
    except OSError as e:
        log.error("Could not save PDF: %s", e)
        return None


def open_in_browser(url):
    try:
        if os.name == "nt":
            os.startfile(url)        # noqa: pops the notice open on the laptop screen
        else:
            import webbrowser
            webbrowser.open(url)
    except Exception as e:
        log.warning("Could not open browser: %s", e)


def check(cfg, state, notifier):
    now = time.time()
    seen = state.setdefault("seen", {})            # url -> {"text", "first_seen", "page"}
    counts = state.setdefault("link_counts", {})
    docs = state.setdefault("docs", {})            # url -> {"sha", "checked"}
    baselined = state.setdefault("baselined", [])  # pages whose existing links are recorded
    new_items, changed_items, au_ok, fresh_pages = [], [], False, []

    for page in cfg["pages"]:
        try:
            raw, charset = http_get(page, max_bytes=5_000_000)
            parser = LinkParser(page)
            parser.feed(raw.decode(charset, "ignore"))
            links = parser.links
        except Exception as e:
            log.warning("Could not read %s: %s", page, e)
            state.setdefault("page_errors", {})[page] = str(e)[:200]
            continue

        last = counts.get(page, 0)
        if last >= 10 and len(links) < max(5, 0.4 * last):
            # Maintenance page / half-loaded page. Don't treat as "everything removed".
            log.warning("%s returned only %d links (usually %d); ignoring this read", page, len(links), last)
            continue
        if page == cfg["pages"][0]:
            au_ok = True
        counts[page] = max(len(links), int(last * 0.9))
        state.get("page_errors", {}).pop(page, None)
        page_is_new = page not in baselined     # first successful read: record, don't alert
        if page_is_new:
            baselined.append(page)
            fresh_pages.append((page, len(links)))

        for url, text in links.items():
            if url not in seen:
                seen[url] = {"text": text, "first_seen": now, "page": page}
                if not page_is_new:
                    new_items.append((url, text, classify(text, url, cfg)))
            elif text and text != seen[url]["text"] and classify(text, url, cfg) != "normal":
                # Same link, new wording (e.g. "last date extended up to ...")
                changed_items.append((url, text, seen[url]["text"]))
                seen[url]["text"] = text

    # ---- site up/down bookkeeping (the AU page is the one that matters)
    if au_ok:
        if state.get("down_alerted"):
            notifier.send("AU site is reachable again", "Watcher has resumed normal checks.", "info")
        # In the cloud, round to the hour so the saved history only changes when something matters
        state.update(last_ok=(int(now // 3600 * 3600) if CLOUD else now), fail_since=None, down_alerted=False)
    else:
        state["fail_since"] = state.get("fail_since") or now
        hrs = (now - state["fail_since"]) / 3600
        if hrs >= cfg["site_down_alert_hours"] and not state.get("down_alerted"):
            notifier.send("Can't reach the AU admissions site",
                          f"The watcher has not been able to read the AU page for {hrs:.0f} hours "
                          "(site down or blocking). It keeps trying every few minutes. "
                          "Check the site yourself today in case a notice went up.",
                          "important", url=cfg["pages"][0])
            state["down_alerted"] = True

    if fresh_pages:
        summary = ", ".join(f"{urllib.parse.urlparse(p).netloc} ({n} links)" for p, n in fresh_pages)
        log.info("Baseline recorded: %s", summary)
        notifier.send("AU watcher is live",
                      f"Now tracking {summary}. You'll be alerted the moment anything new appears.", "info")

    # ---- flood guard: page redesign or mass re-upload
    if len(new_items) > cfg["flood_threshold"]:
        hot = [i for i in new_items if i[2] != "normal"]
        body = (f"{len(new_items)} links changed at once - the site was probably redesigned or "
                "re-uploaded. Law/spot items among them:\n\n" +
                ("\n".join(f"- {t}\n  {u}" for u, t, _ in hot[:15]) or "(none)") +
                "\n\nPlease open the site and look yourself.")
        notifier.send(f"AU page changed a lot ({len(new_items)} new links)", body,
                      "urgent" if hot else "important", url=cfg["pages"][0])
        return

    # ---- new notices
    tabs_opened = 0
    for url, text, tier in sorted(new_items, key=lambda i: ["urgent", "important", "normal"].index(i[2])):
        attachments = []
        if tier == "urgent" and is_pdf(url):
            try:
                data, _ = http_get(url)
                docs[url] = {"sha": hashlib.sha256(data).hexdigest(), "checked": now}
                p = save_pdf(cfg, url, text, data)
                if p:
                    attachments.append(p)
            except Exception as e:
                log.warning("Could not download %s: %s", url, e)
        if tier == "normal" and not cfg["email_every_new_notice"]:
            channels = ["push"]
        else:
            channels = None
        label = {"urgent": "LAW NOTICE", "important": "Spot/vacancy notice", "normal": "New AU notice"}[tier]
        body = text + ("\n\n(PDF saved on your laptop and attached to the email.)" if attachments else "")
        notifier.send(f"{label}: {text[:90]}", body, tier, url=url, attachments=attachments, channels=channels)
        if tier == "urgent" and cfg["open_browser_on_law_alert"] and tabs_opened < 3:
            open_in_browser(url)
            tabs_opened += 1

    for url, text, old in changed_items:
        notifier.send(f"Notice wording changed: {text[:80]}", f"Now: {text}\n\nBefore: {old}",
                      classify(text, url, cfg), url=url)

    # ---- silent re-uploads: same PDF name, new content (AU does this for vacancy lists)
    law_pdfs = [u for u, v in seen.items() if is_pdf(u) and classify(v["text"], u, cfg) == "urgent"]
    due = [u for u in law_pdfs if now - docs.get(u, {}).get("checked", 0) > cfg["doc_recheck_minutes"] * 60]
    for url in due[:25]:
        try:
            data, _ = http_get(url, tries=1)
        except Exception:
            continue
        sha = hashlib.sha256(data).hexdigest()
        old = docs.get(url, {}).get("sha")
        docs[url] = {"sha": sha, "checked": now}
        if old and old != sha:
            text = seen[url]["text"]
            p = save_pdf(cfg, url, text, data)
            notifier.send(f"LAW PDF UPDATED: {text[:80]}",
                          f"The file behind this notice was replaced with new content:\n{text}",
                          "urgent", url=url, attachments=[p] if p else [])
            if cfg["open_browser_on_law_alert"]:
                open_in_browser(url)


def heartbeat(cfg, state, notifier):
    h = cfg["heartbeat_hour"]
    today = datetime.now().strftime("%Y-%m-%d")
    if h < 0 or datetime.now().hour < h or state.get("heartbeat_day") == today:
        return
    state["heartbeat_day"] = today
    last_ok = state.get("last_ok")
    ago = (time.time() - last_ok) / 3600 if last_ok else None
    if ago is None or ago > 6:
        body = ("WARNING: no successful check in the last "
                f"{'??' if ago is None else f'{ago:.0f}'} hours. Is the laptop asleep, off or offline?")
        tier = "important"
    else:
        when = "within the last hour" if CLOUD else f"{ago*60:.0f} min ago"
        body = (f"Still watching. Last successful check {when}. "
                f"{len(state.get('seen', {}))} links tracked, nothing missed.")
        tier = "info"
    notifier.send("AU watcher daily check-in", body, tier, channels=["push", "email"])


def main():
    setup_logging()
    args = set(sys.argv[1:])
    cfg = load_config()
    state = load_json(STATE_FILE, {})
    notifier = Notifier(cfg, state)

    if "--status" in args:
        print(json.dumps({k: state.get(k) for k in ("last_ok", "fail_since", "link_counts", "page_errors")},
                         indent=1, default=str))
        print("links tracked:", len(state.get("seen", {})), "| queued messages:", len(state.get("outbox", [])))
        print("channels:", notifier.channels())
        print("last_ok:", datetime.fromtimestamp(state["last_ok"]) if state.get("last_ok") else "never")
        return 0
    if "--reset" in args:
        for k in ("seen", "docs", "link_counts", "baselined"):
            state.pop(k, None)
        save_json(STATE_FILE, state)
        print("Seen list cleared. Next run records a new baseline.")
        return 0
    if "--test" in args:
        res = notifier.send("AU watcher test message",
                            "If you can read this, alerts work on this channel.", "urgent",
                            url=cfg["pages"][0], channels=["push", "email", "whatsapp"])
        state["outbox"] = []           # don't keep retrying test messages
        save_json(STATE_FILE, state)
        print("Channel results:", res or "no channels configured")
        for ch, on in notifier.channels().items():
            if not on:
                print(f"  {ch}: not set up (skipped)")
        return 0 if res and all(res.values()) else 1

    if not acquire_lock():
        log.info("Another check is running; skipping.")
        return 0
    try:
        if not online():
            log.info("Laptop is offline; skipping this round.")
            return 0
        notifier.flush_outbox()
        check(cfg, state, notifier)
        heartbeat(cfg, state, notifier)
        if not CLOUD:
            state["last_run"] = time.time()
        save_json(STATE_FILE, state)
    except Exception:
        log.exception("Unexpected error")
        save_json(STATE_FILE, state)
    finally:
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
