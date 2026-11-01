# When the subway breaks down, does the street break down too?

*A case study in building a near-zero-cost, honest, real-time transit analysis system
on free public data and GitHub Actions.*

## The problem

Every New Yorker knows the moment. The train stops between stations, the conductor
mentions a signal problem, and the platform empties onto the street. Everyone pulls
out a phone, and every phone gives the same advice: take the bus on the avenue, walk
to the other line, grab a Citi Bike. Within minutes that obvious alternative is
overloaded too. The bus crawls, the docks are empty, the other platform is packed.

Two questions follow. The first is analytical: when the subway is disrupted, how
large, how long and how localized is the effect on the street? The second is
practical: given what is happening right now, should a rider switch lines, take a
bike or a bus, or simply wait? And can the system avoid sending everyone to the same
place?

I wanted to answer both with data anyone can access, at essentially zero cost, and
without hiding how uncertain the answers are.

## Constraints that shaped the design

Three constraints shaped almost every decision.

1. **No paid services.** The data had to come from free public feeds: MTA subway
   GTFS-Realtime, MTA service alerts, MTA Bus Time, NYC DOT traffic speeds, NYC
   traffic cameras and Citi Bike's GBFS feed. Compute had to be free GitHub Actions
   minutes. Hosting had to be GitHub Pages.
2. **No language models.** Plain-English explanations are tempting to generate with
   an LLM. I chose fixed templates instead. They cost nothing, behave the same every
   time, and can be checked by a machine: every number in a sentence must appear in
   the metrics stored with it.
3. **Honesty over polish.** A stale feed is "missing", not "normal". A baseline with
   two days of history is "low confidence" and excluded, not quietly used. Every
   evaluation metric shows its sample size.

## Design

### A shared timeline from feeds that do not agree on time

The hardest engineering problem was mundane: the sources run at very different
rates. Subway feeds update every 30 seconds. Citi Bike updates every minute. DOT
traffic speeds arrive every five minutes, and sometimes hours late. Camera counts
exist only when the system asks for them. Bus positions arrive every 30 seconds
but only make sense as speeds across two snapshots. GitHub's scheduler promises
"every five minutes" and delivers whenever it can.

Everything is bucketed into one-minute UTC slots that keep both the source's
observation time and the fetch time. When a minute has no fresh value, the last one
is carried forward, but only up to a per-source maximum age, and its age is recorded.
Past that limit the value is missing. This one rule removed a whole class of bugs
where hours-old data looked current.

### Events, baselines and an impact score

A delay event is a line whose median train delay, in its worst direction, stays at
least five minutes late for five consecutive minutes, with at least two of those
minutes actually observed rather than filled in. Events are linked to MTA alerts and
to the stations where trains were measured late.

For the three most affected stations, the system compares four street signals within
500 meters with their usual level for the same weekday and half-hour slot: road
speed, bus speed, Citi Bike availability, and vehicle and person counts from traffic
cameras. Each comparison becomes a 0-100 sub-score. The sub-scores are combined with
configurable weights, renormalized over whichever signals have data and a reliable
baseline. The score is "not enough data", never zero, when nothing qualifies.

Cameras are the expensive signal. Running an object detector on hundreds of cameras
every five minutes would blow any free budget. So cameras are counted only while an
event is ongoing, near its worst stations, with a hard cap of 30 cameras per run. The
detector, YOLOX (Apache-2.0), runs on CPU in GitHub Actions and keeps counts only,
never images.

### Recommendations without a destination

The recommendation engine ranks four kinds of options for each affected station.
"Walk to another line" uses walking time plus the next predicted train on lines
that are not disrupted. "Citi Bike" uses the nearest dock with bikes and whether
availability is falling. "Bus" uses the nearest stop and current speed against the
usual. "Wait" uses how long similar past disruptions lasted, by line, time of day
and alert cause, falling back to broader groups when history is thin.

There is no destination input yet, so options are compared on a deliberately simple
proxy: the time to get four kilometers away from the station. That is a real
limitation, and the site says so.

The "avoid the crowd" rule is the part I find most interesting. The option that would
be fastest on a normal day is the *obvious* one, where most riders will go. If it
already shows crowding, it gets a penalty and a visible flag, so the second-best,
less crowded option can rise to the top. Crowding here means buses on that route much
slower than usual, bikes nearby disappearing fast, or cameras counting far more
people than usual.

### A static site with a replay mode

The dashboard is plain HTML, CSS and JavaScript: Leaflet with OpenStreetMap tiles,
and Chart.js. It reads compact JSON exported at deploy time, so there is no server and
no build step. A replay slider plays any past event back minute by minute. Station
delays on the map, the timelines and the suggested options all move together, so the
site is worth visiting even when the subway is running fine.

## Results, including the uncomfortable ones

The evaluation runs weekly and writes a report with sample sizes and date ranges. The
first report covers only a few hours, because GitHub's five-minute schedule had not
yet started firing for the new repository. Everything in it is preliminary, and I
would rather show that than wait for nicer numbers.

- **Detection vs. MTA alerts:** 2 of 6 detected events matched an unplanned alert
  (precision 33%), and 2 of 4 alert-lines had a matching event (recall 50%). That
  precision is low, and it is not obviously wrong. Many short delays never get an
  alert, so some "false positives" are real delays without one. The report lists every
  unmatched event for manual review. The first version of this metric also counted
  alerts issued while the pipeline was not running as misses, which made recall look
  far worse (5%). Fixing that was an early lesson in reading one's own metrics
  skeptically.
- **Surface impact:** the placebo test compares stations near events with the same
  stations at the same time on earlier days, and with distant control stations at
  the same moment. It has no usable sample yet, and the report says "no conclusion
  can be drawn" rather than presenting the raw impact scores as evidence.
- **Recommendations:** no recommendation had been logged long enough before the first
  report to be replayed. The replay machinery is tested on synthetic events: it
  measures real recovery times, the first train observed at the suggested station,
  and realized bus speeds.
- **Explanations:** zero sentences with an uncited number. More usefully, the
  randomized test that feeds every template with random inputs found a crash in one
  template variant before it ever reached production.

## Lessons learned

**Live data finds the bugs that unit tests do not.** The first live run of the
recommendation engine at midnight suggested switching to lines that do not run at
night. The static schedule says those lines serve the station, but nothing was
coming. The fix was to require live arrival predictions before suggesting a line.
Running against real feeds also surfaced a database view bound before the first
camera counts existed, and an unrecovered event that would have rewritten itself
on every run.

**Free infrastructure has free-tier behavior.** GitHub's scheduler is best-effort,
and in this project it took hours to start at all. Public APIs reject keys or lag by
hours. The system records these failures instead of papering over
them, and the [runbook](runbook.md) documents what to do about each.

**Templates scale better than expected.** Each new signal or option meant a new
template branch. Because every number is cited, a single property test covers them
all, and the explanations can never state something the data does not.

**Write the evaluation before you need it.** Building the placebo test and the replay
harness early made it obvious which data had to be logged: every recommendation, with
its inputs, every run. That data cannot be reconstructed later.

## What is next

Destination-aware routing, so a rider going one stop is not treated like one crossing
the city. Months of history, so baselines, recovery estimates and the placebo test
have real samples. And better ground truth than MTA alerts, such as hand-labeled
incidents or official delay statistics, so that precision and recall mean what they
claim to mean.
