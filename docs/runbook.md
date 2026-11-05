# Runbook

What to do when something breaks. The pipeline is designed to degrade gracefully: a
failing source is recorded and shown as *missing* or *unavailable*, and everything
else keeps running. Most incidents therefore need a check and a decision, not an
emergency fix.

**Where to look first**

- The **status bar** on the dashboard: freshness of each source; stale, missing and
  unavailable sources are marked.
- The **job summary** of the latest "Ingest and analyse" run (Actions tab): the
  Markdown quality report with an "Issues" list.
- The full JSON report, kept for 7 days in the run's `ingest-<run id>` artifact
  (`reports/date=YYYY-MM-DD/<run id>.json`).
- The weekly **evaluation report**, reliability section: uptime, freshness and gaps
  per source, and success rate per workflow.

## A data feed goes down

Symptoms: a status chip shows *Missing*, *Stale* or *Degraded*; the quality report
lists `outage`, `stale`, `invalid` or `empty` for a source.

1. **Check if it is upstream.** Open the feed URL in a browser (URLs are in
   `src/transit_ripple/config.py`) or check the provider's status page or social
   channels. MTA subway feeds and Citi Bike rarely fail for long. The NYC DOT traffic
   speed feed is often hours behind; "stale" there is common and expected.
2. **Nothing to do for a short outage.** Missing minutes are recorded as missing,
   events survive gaps of up to 10 minutes, and longer gaps end events with
   `end_reason = data_gap`.
3. **If one subway feed is down for hours**, its lines are reported missing and
   cannot have events. Note the period in the next evaluation review; the reliability
   section counts the gaps.
4. **If the format changed** (status `invalid` with a parse error), reproduce locally:
   `uv run ripple ingest --sources <source> --polls 1 --save-raw` and inspect the
   saved payload. Fix the parser with a test built from the new payload; never commit
   raw payloads that contain personal data.
5. **The camera API** fails per request; failed cameras are stored as `fetch_error`
   (NULL counts, never 0). The events report lists them under `cameras.errors`.

## A workflow fails

| Workflow | Common causes | What to do |
|---|---|---|
| Ingest and analyse | an analysis step raised; a dependency or upstream change | Data is still published when ingestion succeeded. Open the failed step's log, reproduce locally with the same `--now`, fix with a test. |
| Ingest and analyse, publish step | `stale info` / lease rejected: another run pushed the data branch first | Harmless; the next run publishes. If it repeats, check the concurrency group `ingest-subway` and that no one pushes to `data` by hand. |
| Deploy dashboard | Pages not enabled; export error | Settings -> Pages -> Source: GitHub Actions. For export errors, run `uv run ripple export --data-dir <data checkout>` locally. |
| Evaluate | explanation check failed | The report is still committed: open `docs/evaluation/latest.md`, section "Explanation consistency", fix the template, and add the case to `tests/test_evaluation.py`. |
| Evaluate, push step | branch protection forbids the bot pushing to the default branch | Allow `github-actions[bot]` to push `docs/evaluation/` or change the workflow to open a pull request instead. |
| CI | lint or tests | Run `uv run ruff check . && uv run ruff format --check . && uv run pytest` locally. |

To re-run: Actions tab -> the run -> "Re-run failed jobs" (after a fix, prefer a
fresh run from the default branch via "Run workflow").

## The bus key expires or is rejected

Symptoms: "MTA buses: Unavailable" on the dashboard; the quality report shows
`bus: unavailable (API key rejected ...)` or `MTA_BUS_API_KEY is not set`. Bus speeds
stop, the impact score is renormalized over the other signals, and bus options use
usual or assumed speeds with low confidence.

1. Request a new free key from MTA Bus Time (Bus Time developer resources; the key is
   e-mailed). Bus Time keys are usually 36-character UUIDs; a key for the MTA
   developer portal (api.mta.info) is a different key and does not work here.
2. Store it: Settings -> Secrets and variables -> Actions -> **Repository secrets** ->
   `MTA_BUS_API_KEY` (update the value). Do not put it in an environment secret, a
   variable, the code, or a `.env` file that is committed.
3. Verify: run "Ingest and analyse" manually. The job summary should show
   `bus | ok` and the next deploy should show "MTA buses: OK". A quick check that
   prints only status codes is to call
   `https://bustime.mta.info/api/siri/vehicle-monitoring.json?key=...&LineRef=MTA%20NYCT_B63`
   from a trusted machine; never paste the key into issues, logs or chat.
4. Bus Time asks clients not to poll more than once every 30 seconds; the pipeline
   enforces this (`RIPPLE_BUS_MIN_REQUEST_INTERVAL_S`). Do not lower it.

**Occasional `bus | outage (ChunkedEncodingError)`.** The all-bus response is 3-5 MB
at daytime and is sometimes cut off mid-transfer. `fetch_url` retries such broken
transfers twice after a 2-second pause, so isolated failures are expected and
harmless. Investigate only if most polls in an hour fail (check `quality_source`
on the data branch). Low bus record counts between about 1 and 5 am New York time
are normal: few buses run then.

## Scheduled workflows are paused or never start

**Paused for inactivity.** GitHub disables scheduled workflows in public
repositories after 60 days without repository activity, and notifies the owner by
e-mail. To recover: Actions tab -> "Ingest and analyse" (and "Evaluate") -> **Enable
workflow**. Regular commits to the default branch (for example the weekly evaluation
report) make this unlikely, but check the Actions tab if the dashboard banner says the
data is out of date.

**Never started or very irregular.** Scheduled runs are best-effort: GitHub may
delay or skip them under load, and a new repository can take hours before the first
scheduled run. In the meantime:

1. Check the workflow is enabled and on the default branch (schedules only run there).
2. Trigger runs manually (Actions -> "Ingest and analyse" -> Run workflow); every
   successful run also redeploys the dashboard.
3. Gaps are recorded, not hidden: the reliability section reports the median and
   longest time between runs, and events end with `data_gap` across long gaps.

Each ingest run keeps collecting for `RIPPLE_COLLECT_MINUTES` (default 330) and
publishes every few minutes, so the data stays nearly continuous even when GitHub
starts runs hours apart; the dashboard itself is redeployed only when a run ends.
If regular 5-minute redeploys become essential, an external scheduler can call the
`workflow_dispatch` API with a fine-grained token limited to Actions on this
repository; store that token outside the repository.

## The data branch grows too large

Each run fetches the data branch. If runs get slow, check sizes with
`git ls-tree -r -l origin/data | sort -k4 -n | tail`. Retention is
`RIPPLE_BIKE_RETENTION_DAYS` and `RIPPLE_BUS_RETENTION_DAYS` (35 days), and past days
are compacted to one file per day. Long-term archives belong in release assets or
object storage (see the design document).

## Something looks wrong on the site

- **"Data may be out of date" banner:** no ingest run for more than 20 minutes; see
  "Scheduled workflows" above.
- **An implausible event or recommendation:** open the event's JSON
  (`data/events/<id>.json` on the site). Every recommendation logs its inputs, and the
  explanation lists the metrics behind each sentence. Reproduce with
  `uv run ripple recommend --now <time>` on a data checkout.
- **Map tiles missing:** OpenStreetMap tile servers rate-limit heavy use; for high
  traffic switch to another tile provider in `site/app.js`.
