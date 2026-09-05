"""Render the analysis as terminal text or a self-contained HTML page."""

import html
import statistics

from . import analyze


def _fmt(value, spec="%.1f", dash="-"):
    return dash if value is None else spec % value


def text(conn):
    cover = analyze.coverage(conn)
    routes = analyze.route_summary(conn)
    adher, unmatched = analyze.adherence(conn)

    lines = []
    add = lines.append
    add("Athens bus service, observed")
    add("=" * 72)
    if not cover["from"]:
        add("No positions collected yet. Run: python run.py poll")
        return "\n".join(lines)

    add("window      %s -> %s  (%.2f h, %d poll cycles)" % (
        cover["from"].strftime("%Y-%m-%d %H:%M"), cover["to"].strftime("%H:%M"),
        cover["hours"], cover["cycles"]))
    add("positions   %d stored from %d fetched (%.0f%% were repeat GPS fixes)" % (
        cover["positions"], cover["fetched"],
        100.0 * (1 - cover["positions"] / cover["fetched"]) if cover["fetched"] else 0.0))
    add("derived     %d stop arrivals (%d observed, %d interpolated), %d witnessed departures" % (
        cover["events"], cover["exact"] or 0,
        (cover["events"] or 0) - (cover["exact"] or 0), cover["departures"]))
    add("vehicles    %d distinct, against %d scheduled trips" % (
        cover["vehicles"], cover["scheduled"]))
    if cover["errors"]:
        add("errors      %d failed route fetches" % cover["errors"])
    add("")

    add("Headway regularity (what a passenger waiting at a stop experiences)")
    add("-" * 72)
    add("%-5s %-5s %4s %5s %6s %6s %5s %6s %6s %8s" % (
        "line", "dir", "veh", "gaps", "sched", "obs", "CV", "wait", "even", "penalty"))
    for route in routes:
        add("%-5s %-5s %4d %5d %6s %6s %5s %6s %6s %8s" % (
            route["line_id"], route["direction"], route["vehicles"], route["headways"],
            _fmt(route["scheduled_headway"], "%.0f"), _fmt(route["observed_headway"], "%.1f"),
            _fmt(route["cv"], "%.2f"), _fmt(route["wait"], "%.1f"),
            _fmt(route["even_wait"], "%.1f"), _fmt(route["penalty"], "%+.1f")))
    add("")
    add("All times in minutes. sched/obs are the planned and observed gaps between")
    add("buses. wait is what the average passenger actually waits; even is what they")
    add("would wait if the same buses were evenly spaced; penalty is the difference,")
    add("the cost of irregularity alone. CV is the spread of gaps: below ~0.5 is even,")
    add("above ~1.0 means buses are arriving in clumps.")
    lapped = [r for r in routes if r["same_vehicle_gaps"]]
    if lapped:
        add("")
        add("Excluded as lap times rather than headways (the same bus coming round")
        add("again, not a gap between two services): %s." % ", ".join(
            "%s %s %d" % (r["line_id"], r["direction"], r["same_vehicle_gaps"])
            for r in lapped))
    add("")

    add("Schedule adherence at the terminal")
    add("-" * 72)
    if adher:
        add("%-5s %-5s %5s %8s %8s %7s" % ("line", "dir", "n", "median", "on-time", "worst"))
        for row in adher:
            add("%-5s %-5s %5d %+7.1fm %7.0f%% %+6.0fm" % (
                row["line_id"], row["direction"], row["departures"],
                row["median_delay"], row["on_time_pct"], row["worst"]))
    else:
        add("No departures witnessed yet. A departure is only counted when a bus is")
        add("seen at a terminal and then seen leaving it, so this fills in as the")
        add("poller runs across a turnaround.")
    if unmatched:
        add("")
        add("unmatched departures (no scheduled trip within %d min): %s" % (
            analyze.MATCH_WINDOW_MIN,
            ", ".join("%s=%d" % kv for kv in sorted(unmatched.items()))))
    return "\n".join(lines)


_CSS = """
:root { color-scheme: light dark;
  --bg:#fbfbfa; --fg:#1c1b19; --muted:#6b6862; --line:#e2ded7; --card:#fff;
  --good:#2f7d4f; --warn:#b06f1a; --bad:#a83232; --bar:#3c6e9e; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#17181a; --fg:#e8e6e3; --muted:#9a968f; --line:#2e3033; --card:#1f2124;
  --good:#6cc08b; --warn:#e0a95c; --bad:#e07a7a; --bar:#6ea8d8; } }
* { box-sizing:border-box; }
body { margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
main { max-width:60rem; margin:0 auto; }
h1 { font-size:1.5rem; margin:0 0 .25rem; letter-spacing:-.01em; }
h2 { font-size:1.05rem; margin:2.5rem 0 .5rem; letter-spacing:-.005em; }
.sub { color:var(--muted); margin:0 0 1.5rem; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(9rem,1fr)); gap:.75rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:.5rem; padding:.75rem .9rem; }
.card .n { font-size:1.35rem; font-weight:600; font-variant-numeric:tabular-nums; }
.card .k { color:var(--muted); font-size:.8rem; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; font-size:.9rem; }
th,td { text-align:right; padding:.4rem .55rem; border-bottom:1px solid var(--line); white-space:nowrap; }
th { text-align:right; color:var(--muted); font-weight:500; font-size:.8rem; }
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2) { text-align:left; }
td.name { color:var(--muted); font-size:.82rem; max-width:18rem; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.good{color:var(--good)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.note { color:var(--muted); font-size:.85rem; border-left:2px solid var(--line);
  padding-left:.85rem; margin:1rem 0; }
.note b { color:var(--fg); font-weight:600; }
"""


def _bar_chart(routes):
    """Waiting penalty per route as an inline SVG, no libraries.

    The penalty cannot be negative, so the baseline sits flush against the
    labels rather than leaving room for bars that will never be drawn.
    """
    data = [r for r in routes if r["penalty"] is not None]
    if not data:
        return ""
    data.sort(key=lambda r: r["penalty"], reverse=True)
    row_h, baseline, width, right_gutter = 26, 76, 640, 64
    height = row_h * len(data) + 28
    span = max(0.5, max(r["penalty"] for r in data))
    scale = (width - baseline - right_gutter) / span

    parts = ['<svg viewBox="0 0 %d %d" width="100%%" role="img" '
             'aria-label="Extra waiting time caused by uneven gaps, by route">' % (width, height)]
    parts.append('<line x1="%.1f" y1="4" x2="%.1f" y2="%d" stroke="var(--line)"/>' % (
        baseline, baseline, height - 22))
    for index, route in enumerate(data):
        y = index * row_h + 6
        value = route["penalty"]
        length = max(1.0, value * scale)
        parts.append('<text x="0" y="%.1f" font-size="12" fill="var(--fg)">%s %s</text>'
                     % (y + 13, html.escape(route["line_id"]), route["direction"]))
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="14" rx="2" '
                     'fill="var(--bar)"/>' % (baseline, y + 3, length))
        parts.append('<text x="%.1f" y="%.1f" font-size="11.5" fill="var(--muted)">'
                     '+%.1f min</text>' % (baseline + length + 6, y + 14, value))
    parts.append('<text x="%.1f" y="%d" font-size="11" fill="var(--muted)">'
                 'extra waiting caused by uneven gaps</text>' % (baseline, height - 6))
    parts.append("</svg>")
    return "".join(parts)


def _cls(value, warn, bad):
    if value is None:
        return ""
    if value >= bad:
        return ' class="bad"'
    if value >= warn:
        return ' class="warn"'
    return ' class="good"'


def html_page(conn):
    cover = analyze.coverage(conn)
    routes = analyze.route_summary(conn)
    adher, unmatched = analyze.adherence(conn)
    esc = html.escape

    if cover["from"]:
        window = "%s to %s Athens time, %.1f hours" % (
            cover["from"].strftime("%d %b %Y %H:%M"), cover["to"].strftime("%H:%M"), cover["hours"])
    else:
        window = "no data collected yet"

    dup = 100.0 * (1 - cover["positions"] / cover["fetched"]) if cover["fetched"] else 0.0
    cards = [
        ("%.1f h" % cover["hours"], "observed"),
        ("%d" % cover["positions"], "GPS fixes stored"),
        ("%.0f%%" % dup, "fetches already seen"),
        ("%d" % cover["vehicles"], "vehicles"),
        ("%d" % cover["events"], "stop arrivals"),
        ("%d" % cover["departures"], "witnessed departures"),
    ]

    out = ["<title>Athens bus service, observed</title>", "<style>%s</style>" % _CSS, "<main>"]
    out.append("<h1>Athens bus service, observed</h1>")
    out.append('<p class="sub">%s &middot; source: OASA telematics API</p>' % esc(window))
    out.append('<div class="cards">')
    for number, label in cards:
        out.append('<div class="card"><div class="n">%s</div><div class="k">%s</div></div>'
                   % (esc(number), esc(label)))
    out.append("</div>")

    out.append("<h2>How evenly buses actually arrive</h2>")
    out.append('<p class="note">On a line running every ten minutes, punctuality against the '
               'timetable is not what a passenger feels &mdash; the gap between buses is. '
               '<b>Penalty</b> is how much longer the average passenger waits than they '
               'would if the same buses were spaced evenly &mdash; the cost of irregularity '
               'on its own, with frequency held constant. <b>CV</b> is the spread of those '
               'gaps: under 0.5 is even, over 1.0 means buses are arriving in clumps.</p>')
    out.append(_bar_chart(routes))
    out.append('<div class="scroll"><table><thead><tr>'
               "<th>line</th><th>direction</th><th>veh</th><th>gaps</th><th>sched</th>"
               "<th>observed</th><th>CV</th><th>wait</th><th>even</th><th>penalty</th>"
               "</tr></thead><tbody>")
    for route in routes:
        out.append("<tr><td>%s</td><td class='name'>%s</td><td>%d</td><td>%d</td>"
                   "<td>%s</td><td>%s</td><td%s>%s</td><td>%s</td><td>%s</td>"
                   "<td%s>%s</td></tr>" % (
                       esc(route["line_id"]), esc(route["direction"]),
                       route["vehicles"], route["headways"],
                       _fmt(route["scheduled_headway"], "%.0f"),
                       _fmt(route["observed_headway"], "%.1f"),
                       _cls(route["cv"], 0.5, 1.0), _fmt(route["cv"], "%.2f"),
                       _fmt(route["wait"], "%.1f"), _fmt(route["even_wait"], "%.1f"),
                       _cls(route["penalty"], 1.0, 3.0), _fmt(route["penalty"], "%+.1f")))
    out.append("</tbody></table></div>")

    out.append("<h2>Departures against the timetable</h2>")
    if adher:
        out.append('<div class="scroll"><table><thead><tr><th>line</th><th>direction</th>'
                   "<th>departures</th><th>median delay</th><th>on time</th><th>worst</th>"
                   "</tr></thead><tbody>")
        for row in adher:
            out.append("<tr><td>%s</td><td class='name'>%s</td><td>%d</td>"
                       "<td%s>%+.1f min</td><td>%.0f%%</td><td>%+.0f min</td></tr>" % (
                           esc(row["line_id"]), esc(row["direction"]), row["departures"],
                           _cls(row["median_delay"], 3.0, 8.0), row["median_delay"],
                           row["on_time_pct"], row["worst"]))
        out.append("</tbody></table></div>")
    else:
        out.append('<p class="note">No departures witnessed yet. A departure counts only when a '
                   'bus is seen at a terminal and then seen leaving it, so this table fills in '
                   'once the poller has run across a turnaround.</p>')

    lapped = [r for r in routes if r["same_vehicle_gaps"]]
    if lapped:
        out.append('<p class="note">A headway is a gap between two <i>different</i> buses. '
                   'Gaps where the same vehicle came round again are lap times, not headways, '
                   'and are excluded: %s. A circular line worked by a single bus therefore '
                   'reports no regularity figure at all rather than a flattering one.</p>'
                   % esc(", ".join("%s %s (%d)" % (r["line_id"], r["direction"],
                                                   r["same_vehicle_gaps"]) for r in lapped)))

    out.append("<h2>What these numbers do not know</h2>")
    out.append('<p class="note">Arrivals are inferred from GPS fixes sampled every 30&ndash;60 '
               'seconds, so a stop passed between two fixes gets an interpolated time '
               '(%d of %d here were observed directly). Routes are grouped into directions by '
               'the terminal they leave from, but which group is called <i>go</i> is only a '
               'label &mdash; the feed never says. The timetable is today&rsquo;s service '
               'pattern only, and a departure counts only when a bus was seen leaving a '
               'terminal, so adherence covers the trips that began inside the window rather '
               'than all of them. A short window over a few lines describes those hours on '
               'those lines and nothing wider.</p>' % (
                   cover["exact"] or 0, cover["events"] or 0))
    if unmatched:
        out.append('<p class="note">Departures with no scheduled trip within %d minutes: %s.</p>'
                   % (analyze.MATCH_WINDOW_MIN,
                      esc(", ".join("%s (%d)" % kv for kv in sorted(unmatched.items())))))
    out.append("</main>")
    return "\n".join(out)
