#!/usr/bin/env python3
"""Build RSS 2.0, Atom, and iCalendar feeds from the public MyRec event list.

HTML parsing is intentionally defensive: MyRec's public page is not a documented
API. Review generated output after initial setup and after source-page changes.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://maldenma.myrec.com/info/calendar/list.aspx"
SITE_URL = "https://maldenma.myrec.com/info/default.aspx"
TIMEZONE = ZoneInfo("America/New_York")
USER_AGENT = "MaldenMyRecFeed/1.0 (public event feed; respectful periodic fetch)"

# Tune these after reviewing the source list and initial output.
# These patterns only examine event titles; they cannot infer every reservation.
EXCLUDE_TITLE_PATTERNS = [
    r"\bpark renovations?\b",
]
# Leave empty to include all entries except the patterns above.
INCLUDE_TITLE_PATTERNS: list[str] = []

DATE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}$",
    re.I,
)
TIME_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}\s*(?:AM|PM))\s*[-–]\s*"
    r"(?P<end>\d{1,2}:\d{2}\s*(?:AM|PM))",
    re.I,
)
MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"]
) if m}


@dataclass(frozen=True)
class Event:
    uid: str
    title: str
    start: datetime
    end: datetime
    url: str
    description: str = ""


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_clock(value: str) -> time:
    return datetime.strptime(re.sub(r"\s+", " ", value.strip().upper()), "%I:%M %p").time()


def parse_date_header(value: str) -> date | None:
    value = clean_text(value)
    if not DATE_RE.match(value):
        return None
    try:
        return datetime.strptime(value, "%A %B %d, %Y").date()
    except ValueError:
        return None


def allowed_title(title: str) -> bool:
    if INCLUDE_TITLE_PATTERNS and not any(re.search(p, title, re.I) for p in INCLUDE_TITLE_PATTERNS):
        return False
    return not any(re.search(p, title, re.I) for p in EXCLUDE_TITLE_PATTERNS)


def extract_lines(soup: BeautifulSoup) -> list[str]:
    # Separators preserve headings and individual list/table cells better than
    # a single flattened string. Deduplicate adjacent identical fragments.
    raw = [clean_text(s) for s in soup.get_text("\n").splitlines()]
    lines: list[str] = []
    for line in raw:
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return lines


def parse_events(page_html: str) -> list[Event]:
    soup = BeautifulSoup(page_html, "html.parser")
    lines = extract_lines(soup)
    events: list[Event] = []
    current_date: date | None = None
    i = 0

    while i < len(lines):
        possible_date = parse_date_header(lines[i])
        if possible_date:
            current_date = possible_date
            i += 1
            continue

        if current_date is None:
            i += 1
            continue

        # MyRec's list page has shown times and titles in the same visible
        # line in some renderings and adjacent fragments in others.
        match = TIME_RE.search(lines[i])
        if not match:
            i += 1
            continue

        line = lines[i]
        title_part = clean_text(line[:match.start()] + " " + line[match.end():])
        # If the title is in the next text fragment, use it.
        if not title_part and i + 1 < len(lines):
            title_part = lines[i + 1]
        # Remove decorative separator/punctuation fragments.
        title_part = title_part.strip(" |:-–\t")
        if not title_part and i + 1 < len(lines):
            title_part = lines[i + 1].strip(" |:-–\t")

        # In the source list, an event row may be split into time and title.
        # Avoid interpreting navigation/footer text as events.
        if not title_part or len(title_part) > 240 or DATE_RE.match(title_part):
            i += 1
            continue

        try:
            start_clock = parse_clock(match.group("start"))
            end_clock = parse_clock(match.group("end"))
            start = datetime.combine(current_date, start_clock, tzinfo=TIMEZONE)
            end = datetime.combine(current_date, end_clock, tzinfo=TIMEZONE)
            if end <= start:
                end += timedelta(days=1)
        except ValueError:
            i += 1
            continue

        # Keep the original MyRec list page as the source URL. A direct event
        # link is not consistently available in the list markup.
        if allowed_title(title_part):
            identity = f"{current_date.isoformat()}|{start.isoformat()}|{end.isoformat()}|{title_part.lower()}"
            uid = hashlib.sha256(identity.encode("utf-8")).hexdigest() + "@malden-myrec-feed"
            events.append(Event(uid, title_part, start, end, SOURCE_URL))

        i += 1

    # De-duplicate identical items while preserving the same-time events if
    # their titles differ. Publish upcoming entries only.
    now = datetime.now(TIMEZONE)
    unique: dict[str, Event] = {}
    for event in events:
        if event.end >= now:
            unique[event.uid] = event
    return sorted(unique.values(), key=lambda e: (e.start, e.title.lower()))


def xml_escape(value: str) -> str:
    return html.escape(value, quote=True)


def write_rss(events: list[Event], output: Path, generated: datetime) -> None:
    rss = ET.Element("rss", {"version": "2.0", "xmlns:atom": "http://www.w3.org/2005/Atom"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Malden Recreation — MyRec events"
    ET.SubElement(channel, "link").text = SITE_URL
    ET.SubElement(channel, "description").text = "Upcoming entries from the public Malden Recreation MyRec calendar. Independently generated feed; verify details at the source."
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(generated)
    ET.SubElement(channel, "{http://www.w3.org/2005/Atom}link", {
        "href": "rss.xml", "rel": "self", "type": "application/rss+xml"
    })
    for event in events:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = event.title
        ET.SubElement(item, "link").text = event.url
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = event.uid
        ET.SubElement(item, "pubDate").text = format_datetime(event.start)
        ET.SubElement(item, "description").text = (
            f"{event.start.strftime('%A, %B %-d, %Y')} "
            f"{event.start.strftime('%I:%M %p').lstrip('0')}–"
            f"{event.end.strftime('%I:%M %p').lstrip('0')} (Eastern Time). "
            f"Check the source page for details: {event.url}"
        )
    ET.ElementTree(rss).write(output / "rss.xml", encoding="utf-8", xml_declaration=True)


def write_atom(events: list[Event], output: Path, generated: datetime) -> None:
    ns = "http://www.w3.org/2005/Atom"
    ET.register_namespace("", ns)
    feed = ET.Element(f"{{{ns}}}feed")
    ET.SubElement(feed, f"{{{ns}}}id").text = SITE_URL
    ET.SubElement(feed, f"{{{ns}}}title").text = "Malden Recreation — MyRec events"
    ET.SubElement(feed, f"{{{ns}}}updated").text = generated.isoformat()
    ET.SubElement(feed, f"{{{ns}}}link", {"href": SITE_URL, "rel": "alternate"})
    ET.SubElement(feed, f"{{{ns}}}link", {"href": "atom.xml", "rel": "self"})
    ET.SubElement(feed, f"{{{ns}}}subtitle").text = "Upcoming entries from the public Malden Recreation MyRec calendar."
    for event in events:
        entry = ET.SubElement(feed, f"{{{ns}}}entry")
        ET.SubElement(entry, f"{{{ns}}}id").text = "urn:sha256:" + event.uid.split("@")[0]
        ET.SubElement(entry, f"{{{ns}}}title").text = event.title
        ET.SubElement(entry, f"{{{ns}}}updated").text = generated.isoformat()
        ET.SubElement(entry, f"{{{ns}}}published").text = event.start.isoformat()
        ET.SubElement(entry, f"{{{ns}}}link", {"href": event.url})
        ET.SubElement(entry, f"{{{ns}}}summary").text = (
            f"{event.start.strftime('%A, %B %d, %Y')} "
            f"{event.start.strftime('%I:%M %p')}–{event.end.strftime('%I:%M %p')} Eastern Time. "
            f"Verify details at the MyRec source page."
        )
    ET.ElementTree(feed).write(output / "atom.xml", encoding="utf-8", xml_declaration=True)


def ical_escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def ical_datetime(value: datetime) -> str:
    return value.astimezone(TIMEZONE).strftime("%Y%m%dT%H%M%S")


def fold_ical_line(line: str) -> str:
    # RFC 5545 lines should be folded at 75 octets; this simple implementation
    # handles the common UTF-8 case by counting encoded bytes.
    out, current, count = [], "", 0
    for char in line:
        size = len(char.encode("utf-8"))
        if count + size > 73:
            out.append(current)
            current = " " + char
            count = 1 + size
        else:
            current += char
            count += size
    out.append(current)
    return "\r\n".join(out)


def write_ics(events: list[Event], output: Path, generated: datetime) -> None:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Independent Malden MyRec Feed//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Malden Recreation MyRec Events",
        "X-WR-TIMEZONE:America/New_York",
        f"X-PUBLISHED-TTL:PT6H",
        f"LAST-MODIFIED:{generated.astimezone(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ')}",
    ]
    for event in events:
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{event.uid}",
            f"DTSTAMP:{generated.astimezone(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{event.start.astimezone(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{event.end.astimezone(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{ical_escape(event.title)}",
            f"URL:{event.url}",
            f"DESCRIPTION:{ical_escape('Source: ' + event.url + '. Verify current details at MyRec.')}",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    content = "\r\n".join(fold_ical_line(line) for line in lines) + "\r\n"
    (output / "events.ics").write_text(content, encoding="utf-8", newline="")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="public", help="Output directory (default: public)")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    try:
        response = requests.get(
            SOURCE_URL, timeout=30,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"ERROR: Could not fetch MyRec event list: {exc}", file=sys.stderr)
        return 1

    events = parse_events(response.text)
    if not events:
        print(
            "ERROR: No events could be parsed from MyRec. The page may have changed. "
            "No feeds were written; inspect the source HTML and update the parser.",
            file=sys.stderr,
        )
        return 2

    generated = datetime.now(TIMEZONE)
    write_rss(events, output, generated)
    write_atom(events, output, generated)
    write_ics(events, output, generated)

    # A small landing page makes the published URLs discoverable.
    base = "./"
    (output / "index.html").write_text(f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Malden MyRec Event Feeds</title></head>
<body>
<h1>Malden MyRec Event Feeds</h1>
<p>Independent feeds generated from the public <a href="{SOURCE_URL}">MyRec event list</a>.</p>
<ul>
<li><a href="{base}rss.xml">RSS 2.0 feed</a></li>
<li><a href="{base}atom.xml">Atom feed</a></li>
<li><a href="{base}events.ics">iCalendar subscription (.ics)</a></li>
</ul>
<p>Last generated: {generated.isoformat()}</p>
<p>This is not an official City of Malden or MyRec service. Confirm details and cancellations at the source.</p>
</body></html>""", encoding="utf-8")

    print(f"Generated {len(events)} events into {output.resolve()}")
    for event in events[:10]:
        print(f"  {event.start.isoformat()} | {event.title}")
    if len(events) > 10:
        print(f"  ... and {len(events) - 10} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
