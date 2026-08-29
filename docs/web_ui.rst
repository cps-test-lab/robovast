.. _web_ui:

======
Web UI
======

RoboVAST ships a small **web frontend** — a browser client of the
``robovast-service`` (see :ref:`architecture`). It is a thin client of the same
:class:`robovast.service.interface.RobovastInterface` contract the CLI and MCP
server use, so it works identically against a local ``vast serve`` or an
in-cluster service.

It provides four views:

* **Monitor** — lists campaigns and shows each one's live progress (phase, per-batch
  run progress, budget/stopping criteria), with a **Stop** action and a collapsible
  **live log** panel. A campaign that is **over** is listed **folded**: one row carrying
  its phase, id, description, start time and a **compact run meter**, with the jobs list,
  the Details panel and the log not rendered at all — so a page of finished campaigns is a
  page of rows rather than metres of scroll. Clicking the row (or its chevron) unfolds the
  full card, unchanged. A **running** campaign starts unfolded, and can be folded by hand
  like any other; a campaign that finishes while you are watching it stays open. The
  compact meter is the same bar the open card draws full width — same segments, same
  ``done/total`` — with the counts inside the track and the rest (passed, failed, no
  result, start and finish times) on hover. A **search** campaign's folded row adds a
  small **rounds ring** with its round count in the hole, because a search's run counters
  describe its *current batch* rather than the campaign; hovering the ring gives the round
  bound, the best objective and the **objective-over-rounds chart**. A search whose rounds
  nothing bounds draws the bare ring: there is no denominator, and none is invented. The campaign list itself is **streamed** over Server-Sent Events
  (``GET /campaigns/events``), not polled: a launched campaign appears in the list
  immediately — with its true live phase and **how long ago it started** — and every phase
  change is pushed within a second. That age is deliberately relative rather than a wall
  clock: a campaign id already ends in a ``-YYYY-MM-DD-HHMMSS`` stamp, so an absolute start
  time beside it says the same thing twice. Hovering it gives the exact start and finish in
  your browser's locale and timezone, and how long the campaign took — and the exact time is
  worth having, because the id's stamp comes from the *service host's* clock while this one
  is the viewer's, which on a cluster are not the same reading.
  Leaving the tab and coming back does not leave it behind; see `Staying up to date`_.
  Hovering a campaign's **name** says **where it came from** — the workspace and the ``.vast``
  it was launched from; see `Where a campaign came from`_.
  The phase reflects the whole lifecycle, including its two pre-run steps:
  ``building`` (the campaign is **waiting for its experiment image** —
  builds are content-addressed and shared, so it may be waiting on one another campaign
  triggered) and ``variation`` (the campaign's configurations are being expanded), then
  ``running`` → ``finishing`` → ``postprocessing`` → ``finished`` (or ``failed`` /
  ``stopped``). A build that fails is shown as a ``failed`` campaign in the list rather
  than vanishing, and its builder output is in that campaign's own log under a ``BUILD``
  divider. The per-batch run bar also distinguishes **finished** runs (the
  solid fill) from those **currently running** (a lighter segment on top), with the
  exact ``running N · pending M`` counts beside it. While a fixed-size campaign runs,
  an **ETA** (``~12m left (≈ 14:35)``, the estimated finish time in your locale)
  appears next to that count once at least one run has
  finished, extrapolating from the average time per completed run. A collapsible **Jobs** list shows
  each execution unit of the current batch — a *run* locally, a Kubernetes *Job* on the
  cluster — with its status; expanding a running one streams that **job's own live log**:
  every container it runs, merged into one stream, each line tagged ``[<container>]``
  and colored per container when the job has more than one. That matters in the ROS
  shape, where the simulator and the system under test have their own containers and a
  failure is only legible when their output is read against the scenario's.
  A **running** job's row also carries a red **Stop** button, which kills *that job alone*
  and lets the rest of the campaign carry on — the intervention for a job that is visibly
  wedged and will not exit by itself. It is offered on running jobs only: a queued one has
  not started, and a blocked one has a cause (no quota, an unpullable image) that deleting
  it does not fix. Confirming asks for an optional reason, and the reason is worth giving —
  it is stored with the run and is what explains the kill to whoever reads the results
  later. The kill is permanent: the runs it cuts short are recorded as ``killed`` (see
  :ref:`stopping-one-job`), counted as neither passes nor failures. The campaign **live log** panel below is the
  campaign's unified *infrastructure* log — the variation (config generation), run
  (controller) and postprocessing phases assembled into one stream with
  ``===== PHASE =====`` dividers — streamed live and shown in full once it finishes.
  Both tails **follow the newest line only while you are at the newest line**, the same
  contract as the :ref:`run log <run-view>`: scroll back and the arriving lines are
  appended without moving the view, so a passage stays where you are reading it, and a
  button in the corner jumps back to the end and resumes following. A selection also
  pauses the follow while you hold it — auto-scrolling out from under a drag is what
  made a live log impossible to copy from — and dropping it resumes the tail by itself.
  **Stop** cooperatively ends the campaign *and* terminates its in-flight jobs, so
  running work halts promptly (not only after the current batch). That is the whole
  campaign; to end one job and keep the rest, use the per-job **Stop** on its row above.
  A card's controls are two buttons: the **actions menu** (☰) and the fold. The menu is
  ordered by what a reader came for — open something, take something away, re-run something,
  destroy something — and every entry is conditional, so a campaign with nothing to act on
  yet is offered no menu at all. Its middle group is **Download**, which streams the
  campaign's ``tar.gz`` straight from the service — from the object store on a cluster, from
  disk on a local one; the lane is not something a viewer should have to know — and, when
  that campaign also has a copy on the share, **Copy share link** (omitted for a share
  provider that has no link a browser could open — SFTP has none). Below those,
  **Retrigger campaign** starts a **new** campaign from
  what this one recorded — its frozen ``_config/`` and the image its runs actually
  used — rather than from the workspace it was launched from, which may be gone or
  may have moved on. The source campaign is untouched, so this works whatever state
  it ended in, and the new campaign appears at the top of the list with a description
  naming the one it came from. It replays the recorded launch
  (``_execution/launch.yaml``), so re-running a one-config pilot stays a one-config
  pilot. A campaign that never recorded a usable image is refused rather than rebuilt
  from a guess: a campaign's build context is not archived in its results, so the
  refusal names the container and points back at the workspace.
  A finished campaign also carries a collapsed **Details** box — what it cost, how it
  behaved, and what the next one should reserve; see `The Details panel`_.
  The same menu offers **Retrigger postprocessing**, which opens a dialog to *adapt
  the* ``results_processing.postprocessing`` *block* (in a Monaco YAML editor) and
  re-run the analysis against the preserved raw rosbags — to compute different metrics
  after the fact without a new run. Because a campaign is self-contained (it carries
  the ``.vast`` that ran), the edit is written **into that file in place**: the
  ``results_processing.postprocessing`` block of the campaign's own
  ``_config/<name>.vast``, with no override file and no revision history. It is the one
  narrow exception to the snapshot being a record of what ran, and it is why the
  read-only config view calls that snapshot *frozen* rather than *immutable*. The
  browser equivalent of ``vast cluster monitor``.
  Its **Export to share** entry names the variant it will write — *(raw)* or
  *(postprocessed)*. That is not a setting: which one a campaign yields is read off the
  campaign itself, and once postprocessing has written into its tree the raw campaign no
  longer exists to export. A campaign ends up on the share as *both* by being uploaded at
  campaign end (before postprocessing, hence raw) and exported again afterwards.
* **Launcher** — starts a campaign from a workspace (which ``.vast``, config filter,
  runs per configuration, *Postprocess when done* and *Upload to share when done*
  toggles) and watches its live status. The browser equivalent of ``vast workspace run``. *Upload to share when done* streams a raw, pre-postprocessing
  ``tar.gz`` to the configured external share the moment the runs finish (off by
  default; the share destination comes from the service's ``.env``).
* **Config** — a workspace-based ``.vast`` editor with live validation, a
  generated-configuration preview, and a per-configuration view the ``.vast`` declares. It
  also serves, read-only, the configuration a campaign already ran — see
  :ref:`web-ui-campaign-config`.
* **Results** — browse a campaign's data: an Explorer over its analysis notebooks, a
  panel-based replay of one run, and read-only SQL with charts.
* **Admin** — the *service* rather than the work: how loaded the lane has been, which
  version is running, and what the service has been doing. Pinned to the foot of the
  sidebar beside the usage meters, which report the same service. See `The Admin page`_.

.. _web-ui-admin:

The Admin page
--------------

Every other page is about a campaign. This one is about the service running them, and it
answers three questions no other page does.

**How loaded has the lane been.** The sidebar meters say *now*; "is the cluster busy?" is a
question about a period. The service samples its own ``/usage`` every 30 seconds and keeps
24 hours of readings, plotted as CPU and memory against capacity over the last hour or day.

One chart for the whole lane — every node summed, one colour per resource — and **two
readings per resource**: a filled area for what is actually being **consumed**, under a
dashed line for what has been **reserved**. The gap between them is the number that sizes
the next sweep: a campaign reserving nine cores per pod and using two draws a chart that
would otherwise read as a full cluster while it idled. The caption names the encoding, so a
reader never has to infer which is which.

On a **cluster** the dashed line is the sum of pod requests, and the fill comes from
metrics-server. Where metrics-server is not installed — or where the service's RBAC predates
the ``metrics.k8s.io`` grant, which ``vast service upgrade`` reconciles — there is **no fill
and the caption says why**, rather than a zero line that would read as an idle cluster. A
single sample can be missing its fill too (a node that joined seconds ago is not in metrics
yet); that is drawn as a gap. Locally the opposite holds: the Docker lane measures and
reserves nothing at all — it sets no container limits and runs one scenario at a time — so
the chart shows the fill alone, and that is the whole truth about that lane.

The recording is **in memory only**, and the caption under the chart says what it actually
covers: a service started ten minutes ago has ten minutes of history, and an empty 24-hour
view is the beginning of the record rather than an idle cluster. A restart clears it —
including the restart the Upgrade button below causes. Capacity is stored with every
reading rather than once, so a node joining or being drained does not redraw the past
against today's size.

**What version is running, and is there a newer one.** The page reports the version, the
git revision where the deployment can tell (blank means it cannot, which is not a
mismatch), and the image digest the kubelet actually resolved — the only thing that
distinguishes two builds of a floating tag. Against that it reports what the tag points at
in the registry now. A registry that does not answer is reported as *unknown*, never as "up
to date": that would tell you a fix you have just published is not there.

**Upgrade rolls the pod, and reconciles nothing else.** It stamps the Deployment's restart
annotation; with ``imagePullPolicy: Always`` the new pod pulls the tag afresh. RBAC, the
registry ingress route, the credential Secrets and the build daemon are untouched, so a
version needing a permission the last one did not will deploy and then fail at runtime with
a 403. ``vast service upgrade`` is the command that reconciles all of
it, and the credential Secrets in particular can *only* be done there — they are rebuilt
from the operator's environment, which the pod does not have.

So the button is for "new bytes are published and nothing else changed". It refuses while
campaigns are live, naming them, because the controller driving them runs in the pod being
replaced; confirming the dialog is what overrides that. Kubernetes starts the new pod
before stopping the old, so the API stays up, and the page waits for the running digest to
change rather than trusting the request it just made.

Where a deployment cannot roll itself — a local ``vast serve``, or a service driving the
cluster from outside it — there is no button, just the reason. The chart and the log work
on both lanes unchanged.

**What the service has been doing.** A service writes to stderr, and stderr is not readable
back, which is why several failures in RoboVAST are diagnosable only from a log nobody
could reach. The service now keeps its last few hundred kilobytes in memory and tails them
here live, over the same stream the campaign logs use.

Two limits, both stated on the page: it holds what *this process* logged, so a container
that has already died is only in ``kubectl logs -p deploy/robovast-service``; and a busy
multi-campaign run fills the buffer quickly, since every campaign's records are interleaved
in it. ``vast service log [-f]`` prints the same thing from a terminal, against whichever
service the CLI resolves.

.. _web-ui-freshness:

Staying up to date
------------------

A monitor you have to remember to refresh is a monitor that lies. The page keeps itself
current across a tab switch, a suspended laptop and a dropped ``kubectl port-forward``,
and it does so on two mechanisms, because the page has two kinds of live data.

**The streams** — the campaign list and every log panel — are Server-Sent Events, and the
browser's own ``EventSource`` recovery covers only the easy failure. Two others are handled
here. It **gives up**: after enough consecutive failures the browser parks the stream in
``readyState CLOSED`` and never reopens it. And it **cannot see a zombie**: when a laptop
suspends or a tunnel is torn down, the socket raises no error, reports ``OPEN`` forever, and
delivers nothing more — indistinguishable from a campaign that simply has not changed. So
the service heartbeats every quiet second with a *visible* event (:doc:`http_api`), and the
UI watches that clock: a stream that is closed, or silent for 15 s, is replaced with a fresh
connection whenever the tab becomes visible, the network returns, or the check next runs.
The **Refresh** button beside the campaign list's heading does the same thing on demand — it
is not the only way to get there, which is why it is an icon rather than a labelled
button: it sits next to the heading it acts on, the way the Explorer's and the run view's do.
Anything other than a healthy stream is labeled ``reconnecting…`` next to it, so a stale list
always says that it might be.

**The polled readings** — a campaign's phase and its per-job listing, and the sidebar's
resource meters — deliberately stop polling while the tab is hidden, so a monitor left open
in a background tab costs the service nothing. That is what makes coming back the moment they
must be re-read: without it a card would show the phase from before you switched away for as
long as its timer takes to restart. Those queries, and the Results tab's campaign listing,
therefore fetch once on return.

**The app itself** can go stale too, and it is the one thing here that no amount of
polling helps with. Each view is a separate chunk fetched on first visit, named by a
hash of its contents; restarting the service onto a new build rehashes all of them. A tab
opened before the restart is still holding the previous ``index.html``, so the next view it
opens asks for a file that is no longer there. Nothing is wrong with the service and nothing
is wrong with the tab — they are simply one build apart.

So the failure is caught where it happens, at the view's own boundary, and it offers both
clicks: **Try again**, which re-attempts the download and is what a dropped tunnel needs,
and **Reload**, which fetches the new document and is what a restart needs. The message
names both causes rather than guessing between them. A reload is cheap here because the
navigation lives in the URL hash — it comes back to the view you were on. The service serves
``index.html`` as ``no-cache`` so that reload is guaranteed to see the new build, and
everything under ``assets/`` as immutable, since a content hash in the name is a promise the
bytes never change.

What deliberately does *not* move is a result you are reading. The Results views take a
snapshot of the campaign list and adopt a new one only when you ask (see
`Results viewer`_) — a finished campaign is immutable, so reshuffling its tree under
someone mid-read would cost attention and return nothing. Freshness applies to what is
still changing.

.. _web-ui-import:

Bringing a campaign in
----------------------

The **import menu** at the right-hand end of the campaign list offers the two ways a
campaign gets here. *Import archive…* takes a campaign archive (``.tar.gz``) off your
machine — a colleague's results, a published dataset, or a campaign from a service you have
since torn down. It is the other direction of a campaign card's **Download**, and it is the
same operation as ``vast campaign import`` and the ``import_campaign`` MCP tool: the browser
is only how the bytes get there (:doc:`http_api`). The other entry, *Import from Share*,
takes one off the configured share instead and is described under
:ref:`web-ui-share-import`; there the service does the fetching and no bytes pass through
your browser at all.

**Extracting is not importing.** Listings, this page and every query answer from the
campaign's ``campaign.db``, not from its results tree, so an archive that was merely unpacked
would appear blank — which is why the archive is registered, and why ``vast results
download`` alone does not make a campaign appear here.

**It is also not instant.** The import is a tracked operation like any other: the campaign
appears in the list straight away at phase ``importing`` — its id is read from the archive
before a byte is extracted — and, when the archive is a raw one, rolls on into
``postprocessing`` rather than finishing without the tables anybody would query. So the
dialog closes as soon as you start it and you watch the row, exactly as for a run. A failed
import removes itself; there is no half-campaign left to tidy up.

**An older archive is brought forward on the way in.** The ``.vast``'s version ladder is
applied in memory (the archived file is never rewritten), the campaign store migrates when it
is opened, and a store that is missing entirely is reconstructed from the results tree — the
normal case for a raw, pre-postprocess archive.

That is four independent things that can each be fine, old, missing or broken, so the outcome
is reported **per stage** rather than as one word, and every stage that is not ``ok`` says what
recovers it. The report is written to the campaign's ``_execution/import.json``, and this page
shows it under the heading once the campaign settles — the import answers with the campaign's
id, not with a verdict, so there is nothing to show until there is.

The panel is **collapsed** by default, because an import is usually unremarkable and its
headline is the whole story; the headline still names any caveat, so what folds away is the
detail behind one, never its existence. Two problems are offered as a button rather than as
advice: an archive whose campaign id is **already here** is refused before anything moves
rather than silently replacing evidence, and *Replace existing* re-imports over it without
re-uploading the bytes; a **corrupt store** offers *Rebuild store*, which reconstructs it from
the results tree. A refusal of either kind never creates a campaign row, which is why it is
reported here rather than on a card.

A **degraded** import is not a failure. A campaign that lists but under-reports — its runs
were archived without their run directories, say — is still a campaign somebody has, and
discarding it to keep a verdict clean would throw away the data. Only a stage that genuinely
blocks fails the import, and the one thing nothing here can fix is an archive from a *newer*
robovast: a schema cannot be migrated downwards, so that one asks you to upgrade.

.. _web-ui-share-import:

Importing from the share
------------------------

*Import from Share*, the second entry in the campaign list's import menu, opens what the
configured share holds — read by the service with the service's own credentials, since a
browser has none of its own. Pressing **Import** on a row has the service fetch that
archive, so the bytes never come through your browser, and the dialog closes: the campaign
appears in the list at phase ``importing`` and its own row is the progress from then on.
One import per visit; a refusal instead keeps the dialog open, because a refused import
creates no row to be reported on.

It is a menu entry rather than a panel below the campaign list: on a deployment with a
hundred campaigns such a panel is off the bottom of the page, collapsed, which is a poor
place for the only feature that brings somebody else's work in — hence a menu entry, a
search box, and newest first.

**Newest campaign first**, ordered by the timestamp inside each campaign id. Not by when the
archive was written: no share provider reports a modification time, and when the campaign
*ran* is the more useful fact anyway. The id carries it in plain sight, which is why there is
no separate date column.

**One row per campaign, and the row says which archive it will fetch.** A campaign can be on
the share *twice* — the campaign-end upload writes a ``raw`` archive before postprocessing,
a later export adds a ``postprocessed`` one, and nothing removes the first. The dialog takes
the postprocessed archive where there is one, because it arrives complete; a ``raw`` one is
postprocessed after it lands, so that campaign runs on into a second phase. To name a
specific variant, use the CLI: ``vast share import <campaign-id>.raw.tar.gz``.

**Campaigns already here are listed too**, marked *already here* with their Import disabled.
They are not filtered out, so that a link to a campaign you already have still explains
itself.

**Copy link** on a row copies a URL that opens this dialog with that campaign in the search
box — hand it to a colleague and they only have to press *Import*. The same string works in a
terminal: ``vast share import '<link>'`` reads it. The link carries **this** deployment's
address, which is right for colleagues on the same one and wrong for anybody else; a bare
campaign id is the portable form.

The share is deliberately not treated as a subset of what this deployment has — the two
overlap, and an archive with no campaign here is the ordinary case this dialog exists for,
not an anomaly to be filtered out. Nothing about the share is cached on a campaign either: it
is another system's state, and a copy of it would be wrong the first time somebody deleted an
archive out of band. When this deployment has no share at all, the menu entry is greyed out —
but only then. A listing that is still loading, or one that failed because the share is
unreachable, leaves it enabled and the dialog says which happened; greying it out would report
a network problem as a deployment without a share.

.. _web-ui-origin:

Where a campaign came from
--------------------------

**Hover a campaign's name** on its card and it says which workspace and which ``.vast`` the
campaign was launched from, with the workspace id and the file's full, workspace-relative
path — the campaign's frozen ``_config/`` keeps only the basename, so a project holding
several ``.vast`` files in subdirectories would otherwise be ambiguous.

It is on the name rather than in a line of its own because it answers a question *about* a
campaign the reader has already found by its name: a card is read for what the campaign is
doing, and a permanent second line spends that space on something wanted occasionally.

**The name itself is unchanged** — same type, same weight, no underline, no cursor change,
nothing that moves by a pixel. So the hover is not advertised: it is a detail for whoever
goes looking, and it is documented here rather than marked on a card that is read at a
glance for something else.

**It is a record, not a link, and it is deliberately not clickable.** A campaign is
workspace-independent (:ref:`architecture`): the workspace named may since have been edited,
renamed or deleted, and for an ingested campaign it may never have existed on this deployment
at all. Nothing resolves it — **Retrigger campaign** still relaunches from the campaign's own
``_config/``, which is the honest thing to re-run. A link would promise the workspace is still
there, and that is the one thing this cannot promise.

A **re-run** adds a ``Rerun of`` row naming the campaign it came from, above the workspace the
configuration originally came from — copied forward at launch rather than looked up, so it
survives the parent campaign being deleted and a re-run of a re-run still names the root. The
campaign listing is paged, so a hover that had to find its parent in the list would answer
differently depending on where you had scrolled.

A campaign that ran **before this was recorded** has no hover at all. Its ``.vast`` basename
could be read out of its snapshot, but that says nothing about which workspace, and filling in
half the answer would read as though it knew the rest.

The same record reaches an agent through the MCP ``get_campaign_summary`` tool, beside the
image and code provenance it already reports. It is deliberately **not** on
``list_campaigns``, which is a triage listing.

.. _web-ui-details:

The Details panel
-----------------

A finished campaign's card carries a collapsed **Details** box: what the campaign cost, how
it behaved, and — the reason it exists — what the next one should reserve. It answers "did
this run well, and what did it cost?"; the Results explorer keeps answering "what did it
find?", which is why there is no per-configuration breakdown here.

It is **closed by default and queries nothing until opened**. Its four SQL statements include
one that scans every 1 Hz resource sample of every run, so a page of twenty campaigns would
otherwise pay for twenty campaigns nobody asked about. Opening it reads once; the answer is
re-read when the campaign's metric tables appear, since a campaign is *finished* some minutes
before it is *postprocessed*.

Columns, each answering something the others cannot:

* **Overview** — CPU-hours consumed, simulated time (summed run durations; the simulator runs
  at realtime pacing, so one simulated second is one wall second) and completed runs per
  minute of wall clock. Counted in runs, which is what the data records — a job with
  ``execution.runs_per_job > 1`` carries several.
* **CPU** and **Memory** — a ring of MEAN usage per container, showing which of them the pod's
  demand is actually made of, beside one bar per container: the box is the p25–p75 of per-tick
  demand, the whiskers p05–p95, the tick inside it the median, the amber tick the peak, and the
  faint backdrop what the ``.vast`` reserved. An over-reservation is the backdrop the box does
  not fill. The row ends with the measured median; the **suggestion is in the column header's
  hover**, with the p95 and peak it comes from.
* **Ended in** — which scenario action each run's trial was in when it ended, top five, split
  pass/fail. Derived from the ``behaviors`` table that ``scenario_execution`` writes for every
  campaign, and campaign-independent by construction: it names no action, topic or plugin. A
  run that timed out is attributed to the goal it was pursuing, not to the scenario's own
  terminal marker.
* **Duration** — the distribution of run durations. Binned over the data's range rather than
  from zero, with a ten-second floor so a campaign whose runs agreed to within milliseconds
  does not draw that noise as if it were a spread.
* **Objective** — a search's best objective per round, with its stopping criteria beneath.
  Rate-shaped objectives are drawn against 0..1, since ``best = 1`` then means "as good as it
  can get"; anything on another scale keeps its own range, and both axes are labeled either
  way.

.. _web-ui-sizing:

Sizing advice
~~~~~~~~~~~~~

The CPU and Memory hovers give, per container, what was declared, what was measured, and what
to reserve — the value to write into ``execution.containers.<name>.resources.cpu`` /
``.memory``, plus the pod total, which is the figure that divides into the cluster quota.

The two rules differ, and the difference is not cosmetic:

* **CPU is sized on sustained use** (p95) plus 25% headroom, rounded up to a quarter core.
  Exceeding a cpu reservation costs CFS throttling for that scheduling period — slower, still
  correct — which is the right price for not reserving a brief peak permanently. A container
  whose peak is far above its sustained use is marked ⚡: it will be throttled during those
  bursts, and that is the trade rather than an oversight.
* **Memory is sized on the PEAK** plus 25% headroom, rounded up to 128Mi. Exceeding a memory
  limit is an OOM kill: the run dies and the campaign loses that cell. Sizing memory on a
  percentile would be choosing how often a run survives. Its figures are upper bounds —
  ``memory_rss_bytes`` is summed per process name, so pages shared with a fork count twice.

A suggestion is withheld, with the reason shown, when the container produced fewer than 30
samples: a campaign of sub-second runs yields single digits of in-window ticks across every
run put together, and a p95 over seven points is the maximum wearing a percentile's name.

The advice works on a campaign that declares no ``resources`` block at all — which is the
campaign that most needs it, since nothing else says what to set. The container *names* are
read from the config either way, so the measured main container (recorded under the role name
``robovast``) still resolves to a name that appears in the reader's file.

**The same advice reaches an agent.** ``get_campaign_summary`` in the MCP server returns an
``advice`` list computed by :mod:`robovast.results_processing.advice`, which is the authority
for these rules; ``tests/results_processing/test_advice.py`` pins the web UI's constants
against it, so the two cannot drift into telling a human and an agent to reserve different
amounts for the same campaign. Each item carries a plain-text ``title`` and ``detail`` beside
its ``kind``, so a consumer that has never heard of a particular kind can still show it.

.. _visualization-key-map:

What a campaign declares, and where it lands
--------------------------------------------

Everything the UI draws is declared in the ``.vast``'s ``visualization:`` block, which is
shaped like this page: one key per view.

.. code-block:: yaml

   visualization:
     config:
       panels: [...]               # Config tab, third column
     results:
       run_view:
         panels: [...]             # Results -> Run view
         timeline: {...}           # Results -> Run view, playback time base
       explorer:
         notebooks: [...]          # Results -> Explorer
       data_browser:
         plots: [...]              # Results -> Data browser

.. _visualization-old-keys:

**Reading an older campaign.** A campaign archived before these keys moved carries them in
two unrelated top-level blocks. The mapping, for anyone opening a campaign's archived
``_config/*.vast``:

=================================  ============================================
Old key                            Now
=================================  ============================================
``visualization.panels``           ``visualization.results.run_view.panels``
``visualization.timeline``         ``visualization.results.run_view.timeline``
``evaluation.visualization``       ``visualization.results.explorer.notebooks``
``evaluation.plots``               ``visualization.results.data_browser.plots``
=================================  ============================================

This table documents a **historical format; it is not a compatibility promise**. Nothing in
RoboVAST reads the old keys: a ``.vast`` using them is refused by name, and a campaign that
*ran* with them keeps them in its frozen snapshot — so its Explorer tabs, declared plots and
run-view panels do not appear, and it can neither be retriggered nor reconstructed into a
workspace. Its data is unaffected: SQL, logs, the run listing and the download all work. To
bring such a campaign forward, migrate its configuration by hand:

.. code-block:: bash

   vast files cat /results/<campaign_id>/_config/<name>.vast > old.vast
   # move the keys per the table above, then:
   vast workspace init <dir> --name recovered

.. _web-ui-config:

Config editor
-------------

The **Config** tab is where a ``.vast`` is authored. Because a browser has no working
directory, the project lives in a server-side **workspace** (see
:ref:`architecture`): select or create one, **upload** the scenario file and any
run files, and author the ``.vast`` in the Monaco editor.

.. tip::

   To seed a workspace from an existing project directory in one command (instead of
   uploading files by hand), use the CLI — it mirrors the MCP workspace tools and
   drives the same service:

   .. code-block:: bash

      vast workspace init configs/examples/ros2_basic --name ros2demo
      vast workspace list        # confirm it
      # then open the Config tab and pick "ros2demo" from the workspace dropdown

   ``vast workspace init`` writes ``.vast``/``.osc`` inline and uploads the rest
   (preserving sub-directories and the executable bit). Re-running it on the same
   directory creates a *new* workspace each time; a name that already exists gets
   an incrementing ``-2``/``-3`` suffix (printed in the command's output) so the
   copies stay distinguishable in the dropdown.

   To push a fresh version of a project **into the workspace that already exists**
   (instead of making another copy), use ``vast workspace update`` with the
   workspace's ``ws-…`` id or name:

   .. code-block:: bash

      vast workspace update ros2demo configs/examples/ros2_basic          # add + overwrite
      vast workspace update ros2demo configs/examples/ros2_basic --prune  # also delete removed files

   To work on individual files rather than sync a whole directory, a workspace's
   contents are addressable as ``/sources/<workspace_id>/<path>`` — the writable
   half of the same address space a campaign's outputs use (see
   :ref:`reading-result-files`):

   .. code-block:: bash

      vast files ls  /sources/ros2demo/                   # what the workspace holds
      vast files cat /sources/ros2demo/demo.vast
      vast files put /sources/ros2demo/files/run.sh ./run.sh
      vast files rm  /sources/ros2demo/files/old.osc

   ``put`` writes ``.vast``/``.osc`` directly and streams everything else through
   the upload side channel, preserving the executable bit — the same two paths the
   Config tab's drag-a-folder upload uses. A workspace pinned with
   ``vast serve --workspace-dir`` takes these writes like any other — they land on
   the real files.

   ``update`` re-uploads every file (overwriting in place) with the same inline /
   side-channel split and skip rules as ``init``. By default it only adds and
   overwrites; ``--prune`` also deletes workspace files that no longer exist under
   the directory, so the workspace mirrors it exactly. Read-only pinned workspaces
   (``--workspace-dir``) refuse the update — edit their files on disk instead. In the
   browser, dragging a project folder onto the **Config → Files** tab performs the
   same add/overwrite sync. An agent has no third route: the MCP interface reaches the
   service, not the caller's disk, so a whole directory goes through this command.

.. tip::

   To skip the upload entirely and always have a project available — even across
   restarts — pin its directory at launch (local backend only):

   .. code-block:: bash

      vast serve --workspace-dir configs/examples/ros2_basic

   The directory is used **in place**: it appears in the dropdown the moment the
   service starts, with a path-stable id so its UI link keeps working after a
   restart, and **edits land on the real files**. That is what lets the Config tab
   author a project that lives in a git working tree — the browser has no working
   directory of its own, so without it the only route was to copy the project into
   the store, edit the copy and copy it back. (Campaign outputs still land in the
   shared results store, never under the pinned dir.)

   Two things are refused, both because the directory is *yours* rather than the
   store's: **deleting the workspace** (unpin it by dropping the flag) and a
   **whole-directory sync** into it (``vast workspace update``), which would
   overwrite every file at once and, with ``--prune``, delete the ones the source
   does not have. Editing files one at a time is the point; mirroring a different
   tree over someone's checkout is not.

   One directory may be pinned, named after itself; it may hold any number of
   ``.vast`` files, chosen per campaign, so pin the collection rather than each
   project. Hidden files and ``results/`` are skipped, exactly like
   ``workspace init``.

   **It lands wherever the UI is — with no flag at all.** A workspace lives in the
   store of whichever service you talk to, and ``vast workspace`` follows the same
   one the browser is on: a service answering on the local port, otherwise the one
   ``vast login`` recorded.

   .. code-block:: bash

      # (a) a service running on this machine → this follows it:
      vast workspace init configs/examples/growth_sim
      #> Target: service (http://127.0.0.1:8800) [detected]

      # (b) logged in to the deployed one → this goes there:
      vast workspace init configs/examples/growth_sim
      #> Target: service (https://robovast.example.org) [detected]

      # (c) neither → this machine, in-process:
      vast workspace init configs/examples/growth_sim
      #> Target: this machine, in-process (store: …)

   There is no flag to pick between them: with the service published and
   authenticated, an operator uses the same path as everybody else. Every command
   prints the ``Target:`` it
   resolved — including ``[detected]`` — so the choice is never silent. To reach a
   service that is neither local nor in this cluster (a remote VM), bring up your
   own tunnel to the conventional port ``127.0.0.1:8800``
   (``ssh -N -L 8800:127.0.0.1:8800 <vm>``) — the same auto-detect then follows it,
   no flag needed.

The ``.vast`` JSON Schema
(from the service) drives completion and inline validation as you type, and the
service validates the whole project (schema + scenario references + plugin refs)
after each edit — problems appear in the panel below the editor. **Generate** expands
the config and lists the resolved configurations with their parameters, without
running anything. Variation plugins declared in the ``.vast`` ``plugins:`` list are
installed server-side automatically, so validation and preview resolve them.

.. _config-view:

The config view
~~~~~~~~~~~~~~~

The Config tab is three columns: the **editor**, a narrow list of the
**configurations** ``Generate`` expanded to, and — beside them — a **view of the selected
configuration** that the ``.vast`` itself declares.

.. code-block:: yaml

   visualization:
     config:
       panels:
       - parameters: {title: Scenario parameters, height: "35%"}
       - world:      {title: World configuration, height: "15%"}
       - scene3d:    {title: Scene}          # no height -> takes what is left

Panels stack top to bottom in declaration order. A panel's only layout field is its
``height`` — pixels, or a percentage of the column; the last one may omit it and take
whatever the others left. (Declaring two heightless panels is refused when the campaign is
validated, naming the panel, rather than laying one over the other.) Everything else on a
panel entry is that panel's own **data binding**, exactly as in the run view.

A ``.vast`` that declares no ``config:`` block gets ``parameters`` and ``world``, so the
column is never empty for want of a block nobody wrote.

The built-in panels:

**Scenario parameters** (``parameters``) — what the trial is given, as YAML, and nothing
else. Not the ``_``-prefixed keys a variation writes for other readers (``_map_file``,
``_path``, ``_goal_parameter_name``), and not a preview of each factor's value list with this
configuration's value marked: both would restate in a second notation what the ``.vast`` in
the next column already says, and the column is too narrow to spend on that.

**World configuration** (``world``) — the resolved ``sim`` block: the world this
configuration runs in and the plugin overrides on it. A different question from the
parameters — one says what the trial does, the other what it runs in — and it is where a
campaign that varies its environment shows that. **Describe world** asks the simulator, in
the campaign's own image, which plugin keys it actually offers; behind a button because it
runs a container.

.. _config-scene3d:

**Scene** (``scene3d``) — the world in 3D, with what this configuration's variations placed
drawn on it. Same renderer and same navigation as the :ref:`run view's <scene3d-panel>`.

*The geometry is the campaign's* **base world**. It is keyed on the ``.vast``'s world and
its campaign-level overrides, not on the selected configuration, so clicking through
configurations swaps markers rather than running a container each time — and the entry is
the **same cache** a campaign uses, so compiling it here leaves the run view warm, and the
reverse. A configuration naming a different world *file* (a floorplan baked per cell) is a
different identity and does get its own geometry.

What that costs is stated in the panel: an override that changes geometry through a plugin
is **not** in the mesh, only what the variation contributes as markers. This is a view of
the base world plus placements, not a compiled preview of the exact model the run will load.

**Map** (``map2d``, ships with ``robovast_nav``) — the occupancy map a nav campaign plans
on, with the same markers drawn top-down. It exists beside the 3D scene because it is the
*planning* view: a path is searched over these cells and an obstacle is placed relative to
that path, so "why did the path go there" is a question about this picture. Drag to pan,
wheel to zoom, double-click to fit; the scale bar reads in meters.

Which map it draws: the one a variation contributed as its ``map`` role, or the one bound on
the panel, which is how a campaign points at a map it holds itself. A binding wins, being the
author naming a file outright:

.. code-block:: yaml

   - map2d:
       title: Map
       map: files/depot.yaml            # or {param: map_file} / {internal: _map_file} / {role: map}
       markers:
       - {kind: pose, pos: [0.0, 0.0], yaw: 0.0, label: start, color: "#60a5fa"}
       - {kind: pose, param: goal_pose, label: goal, color: "#4ade80"}

Its markers are in the **map** frame — the panel *is* the map — so a map-frame parameter
needs no ``offset:`` here, where the world-frame 3D scene needs one. The map itself must be
a nav ``map.yaml`` (``image``, ``resolution``, ``origin``) beside an 8-bit binary **PGM**,
which is what ``map_saver`` writes; the panel reports anything it cannot read rather than
drawing a map at a guessed origin.

What a variation contributes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The markers are not the panels' knowledge. A variation type answers
:meth:`~robovast.common.variation.base_variation.Variation.config_view_data` with **neutral
geometry** — a box at a pose, a polyline, a pose marker — and every panel draws whatever it
is handed. So a panel renders a variation it has never heard of, and a new variation needs
no change in any panel. See :ref:`variation-config-view` for writing one.

Markers can also be declared in the ``.vast``, on either geometry panel and with the same
grammar, which is how a campaign whose factor is a plain parameter list shows its endpoints —
nothing about ``ParameterVariationList`` knows about placement, so it contributes nothing:

.. code-block:: yaml

   - scene3d:
       markers:
       # The robot spawns at world (-8, 0), per the world's spawn_robot plugin.
       - {kind: pose, pos: [-8.0, 0.0], yaw: 0.0, label: start, color: "#60a5fa"}
       # goal_pose is a MAP-frame pose and map = world + (8, 0) for this world, so the
       # marker is shifted back by it.
       - {kind: pose, param: goal_pose, offset: [-8.0, 0.0, 0.0], label: goal}

.. _panel-bindings:

Every field of every panel is bound the same way, and a panel that declares its fields has
them **checked**: a misspelled binding is a validation error naming the valid fields, rather
than validating cleanly and leaving the panel silently empty. The four sources are

``map: files/depot.yaml``
    a literal — written out, the common case;
``map: {param: map_file}``
    a resolved scenario parameter, so the field follows the selected configuration;
``path: {internal: _path}``
    an ``_``-prefixed key a variation left on the configuration, named exactly as the
    configuration carries it;
``map: {role: map}``
    a named entry of what the variations contributed.

A source naming something a configuration does not have resolves to nothing, and the panel
draws nothing for it — the same rule, and the same reason, as a ``param:`` that misses.
Which fields a panel takes is in ``get_plugin_details("robovast.panel_types", "<type>")`` and
in the served config schema, so the editor completes them.

``param:`` reads a resolved scenario parameter, so the marker follows the selection; one
parameter holding a list of poses yields one numbered marker each. ``internal:`` reads a
variation's own datum the same way — and for ``kind: path`` it reads the polyline itself, which
is how a campaign draws a route a variation planned (``_path``). ``offset:`` is a literal
translation applied afterwards — the way a map-frame parameter is placed in a world-frame
scene, declared in the file because nothing in a panel can know a campaign's frames. A
``param:`` the configuration does not have draws **nothing**, rather than a marker at the
origin: a pose silently at (0, 0) is a wrong answer, an absent one is a visible question.
``label`` is drawn beside the marker by ``map2d``.

Declared and contributed markers are concatenated, not one overriding the other: a campaign
can have both.

See :repo_link:`configs/examples/basic_nav/basic_nav_roqsim.vast` for the declared form. The
contributed form needs no declaration at all: a ``map2d`` panel with no ``markers:`` draws
whatever the variations contributed.

Starting it
-----------

**The service serves the UI itself**, so the web frontend comes up together with
``robovast-service`` — there is no separate web server to run or port to expose.
Build the UI once, then start the service:

.. code-block:: bash

   cd frontend/ui && npm install && npm run build     # emits frontend/ui/dist (served by the service)
   vast serve                                # serves the UI + REST API on one port

Open the service URL in a browser and you get the UI; the REST API is served
same-origin under the same URL (OpenAPI at ``/docs``). The in-cluster service
ships the same build in its image, so the cluster service needs no extra step.

Accessing it — ``vast ui``
--------------------------

``vast ui`` is a thin shortcut: it opens a browser at the service on the
conventional local port and does nothing else. Something must already be serving
there — you make the service reachable with ``vast serve``, and ``vast ui`` opens
it:

.. code-block:: bash

   vast serve            # this machine: local service on :8800
   vast login <url>      # the deployed one, published over its Ingress
   vast ui               # open a browser at whichever of those answers

* **This machine** — run ``vast serve`` (local backend, serves the UI itself),
  then ``vast ui`` to open it. If nothing answers, ``vast ui`` says so and exits
  rather than starting anything — ``vast serve`` is the one command that owns the
  service lifecycle.
* **Cluster** — deploy and publish it with ``vast cluster setup
  --ingress-host``, then open ``https://robovast.<domain>`` and log in. No kubectl,
  no kubeconfig, nothing held open. ``vast login <url>`` points the CLI and MCP at
  the same place.
* **Remote VM** — the service binds ``127.0.0.1`` there, so reach it with your
  own SSH tunnel (``ssh -N -L 8800:127.0.0.1:8800 <vm>``) and open
  ``http://127.0.0.1:8800``. Because that is the conventional port, ``vast ui``
  and every other command auto-detect the tunnel — nothing to export.

Because the service serves the **web UI and the REST API on the same port**,
whatever ``vast ui`` opens is all a browser, the ``vast`` CLI, and the MCP server
need. Other service-touching commands (``vast workspace …``) resolve the same
target the same way, so nothing needs to be exported.

A connection indicator in the **sidebar footer** reads ``disconnected`` until the
service answers, then becomes a stack of labelled meters showing the backend's
live **resource usage**, each with the compact ``used/total`` in the bar and a
hover tooltip spelling the numbers out:

* **CPU** and **Mem** — always shown. The numbers are backend-appropriate: the
  host machine's utilization for a local ``vast serve``, and the cluster's node
  capacity vs. the summed requests of the pods scheduled onto those nodes for an
  in-cluster service (runs still queued for a node show up in the jobs meter as
  pending, not as CPU in use). So on a cluster these meters are what is **reserved**;
  what is actually being consumed is on the Admin page's chart, beside it (see
  `The Admin page`_). ``get_resource_usage`` reports both readings as
  ``cpu_reserved`` / ``cpu_measured``, either of which is null on a lane that has no
  such reading.
* **Jobs** — only while there is scenario work, since a permanent ``0/0`` on an
  empty track was indistinguishable from a dead widget. Its total is the
  outstanding work (running + pending), not a capacity, so a full bar means the
  queue has drained.
* **Disk** — the filesystem the backend's runs write into: the campaign results
  root's filesystem locally, and on a cluster the filesystem of **the node carrying
  the service pod** — the disk its workspaces are a ``hostPath`` on, named in the
  tooltip. Deliberately not a cluster-wide sum, which would report tens of terabytes
  free while the one disk that decides whether a campaign can be written filled up.
  Unlike CPU and memory on a cluster, this is **measured** usage rather than a sum of
  pod requests — nothing reserves disk, so a request sum would read near-empty on
  a full disk.
* **Store** — the campaign results store, where the backend can measure one. Its
  denominator is the store's own volume, not the **Disk** figure; the two can sit on
  different nodes.

The last two appear only where the backend can actually report them, and are
absent rather than zero when it cannot: a service older than the fields, a
cluster whose kubelet could not be read (the service needs ``nodes/proxy``; see
:ref:`deployment`), or a provider backed by a cloud bucket, which has no capacity
to fill and so nothing to draw. ``get_resource_usage`` reports the reason when the
backend tried and failed. The reading is sampled at most once every few seconds
and shared across browser tabs, so it never loads the backend.

.. note::

   The service is **unauthenticated in v1** and must stay behind the
   localhost / SSH-tunnel / ``kubectl port-forward`` boundary — do not expose it
   directly. Public access (Ingress + token/TLS) is a deferred, whole-surface
   decision (see :ref:`deployment`).

Results viewer
--------------

The **Results** tab explores a finished campaign's data through three sub-views: an
**Explorer** (a campaign → config → run tree with per-node notebook reports — with a
**batch** level between campaign and config for a search campaign), a
**Run view** (a time-driven, panel-based replay of one run — see below), and a
**Data browser** (ad-hoc SQL + charts). Each carries a small icon, used in the sidebar
and again on the campaign cards.

**The selected result is the URL**, down to the run::

   #/results/explorer/<campaign>                            the campaign
   #/results/explorer/<campaign>?batch=<i>                   one search round
   #/results/explorer/<campaign>/<config>                    one configuration
   #/results/explorer/<campaign>/<config>/<run>              one run
   #/results/explorer/<campaign>/<config>/<run>?tab=<name>   ...and which of its tabs
   #/results/run/<campaign>/<config>/<run>                   the same run, replayed

So a link addresses a *result*, not just the campaign that produced it: "look at this run's
log" is a URL you can paste to someone, and a reload comes back to the same node with the
tree already opened on it. The path is the same ``<campaign>/<config>/<run>`` address a run
has on disk and under :ref:`/results <file-address-space>`, so there is one spelling to
learn rather than two.

**Both views use it, and share it.** Pick a run in the Explorer and switch to the Run view and
that run is what plays; each has an icon button that hands its node to the other — at the right
of the Explorer's tab row, and left of the Run view's gear, both carrying the icon of where they
lead. The Data browser is campaign-scoped, so it carries no node: stepping through it and back
returns to the campaign.

Two things stay out of the path deliberately. The **notebook tab** is a lens on a node rather
than part of its address, so it is a query parameter (``?tab=``; the built-in Log tab is
``?tab=log``, which is why a workload may not be *named* ``log``). And a **batch** is a grouping
recorded in the store rather than a directory — a run is identified by its config and index alone
— so it appears only when a batch node is itself the thing selected. For a config or run it is
looked up, never carried: a second copy of a derivable fact is one that can disagree.

A URL naming a config or run the campaign does not have falls back to the campaign node. A
finished campaign's structure is fixed, so that is a wrong link rather than a stale one, and
nothing keeps re-checking it.

Each campaign card in **Campaigns** also offers shortcuts — in its **actions menu** (the ☰
button) — that jump straight into the Explorer or the Run view *for that campaign*. A card only
offers what it can deliver: **Open in results Explorer** once the campaign is finished **and**
postprocessed (the same gate the Results tab itself applies), and **Replay runs in the Run
view** only if the campaign also recorded runs to replay. They are named lines in a menu rather
than a row of icon buttons: a list whose rows are meant to be scanned cannot also carry five
same-sized glyphs per row that have to be learnt before they can be used. Changing the selection inside a view updates the URL without adding a browser-history
step, so **Back** always returns to where you came from in one press; a jump *between* views is a
real step, so Back returns to the view you left.

.. _web-ui-campaign-config:

Reading the configuration a campaign ran
----------------------------------------

**Open configuration** in a campaign card's actions menu opens **Config** on that campaign's
frozen ``_config/`` — the configuration it was actually staged with — at
``#/config/campaign/<campaign_id>``. It appears once the campaign has staged that snapshot
(after variation expansion) and stays for the rest of its life, so the configuration of a
campaign that is still running can be read while it runs.

**This is not a workspace, and it is deliberately not in the workspace picker.** It is
served from the read-only results tree (``/results/<campaign_id>/_config/``, which has no
write route at all), and the card's link is the only way to it: clicking **Config** in the
sidebar always returns to your workspaces. Where a workspace has its picker, the header
names what is on screen — ``<campaign_id>/<file>.vast (read-only)`` — and the editor takes
no cursor at all: a caret blinking in a YAML buffer is an invitation to type an edit that
would then be refused.

Two things are unavailable in that mode, and both for the same reason: live validation and
**Generate** are workspace operations, and validating the snapshot in place would be wrong
anyway — ``_config/`` archives the scenario at its *basename* while a ``.vast`` may declare
``scenarios/foo.osc``, so it would report a scenario that is plainly there as missing.
**Create workspace from this** is the way on: it reconstructs the snapshot into a new,
ordinary workspace — placing the scenario where the ``.vast`` declares it, the same
reconstruction **Retrigger campaign** performs — and refuses, creating
nothing, if the snapshot is short a file its own run used. From there everything works
normally, and launching it starts a campaign of your own rather than editing a record of
one that already ran.

**Explorer.** The tree shows each campaign's configs and runs with a pass/fail status
dot; selecting a node opens its details on the right. When a campaign declares
:ref:`evaluation notebooks <evaluation-notebooks>` under
``visualization.results.explorer.notebooks``, each workload appears as a **tab**, and the notebook for
the selected node's level (campaign / batch / config / run) is executed server-side and
shown as a rendered HTML page. The notebook's ``DATA_DIR`` is set to the selected node's
directory (see :ref:`evaluation-notebooks`). Output is cached, so re-selecting a
node is instant. The selected node is URL-carried and shared with the Run view, so selecting a
run here is the run that replays there (see above); the Data browser takes only the campaign.

**Search campaigns group by batch.** A search proposes its configurations one round at a
time, so a flat config list buries the very thing that says whether the search worked. For
a campaign whose ``mode`` is ``search`` the tree therefore inserts a **batch** node per
ask/tell round: each carries the round's pass tally and its best objective, and each config
below it is labeled with its own objective. A batch-mode campaign has exactly one round,
so it is left flat — grouping it would add a node that says nothing.

A batch is a grouping recorded in the store, **not** a directory: a search's configs sit
flat under the campaign root whichever round proposed them. A ``batch`` notebook is
therefore given the campaign root as its ``DATA_DIR`` and told *which* round through an
injected ``BATCH`` index. See :ref:`evaluation notebooks <evaluation-notebooks>` for how to
declare one.

After the declared workloads comes a built-in **Log** tab, which needs nothing in the ``.vast``.
It shows the same merged log as the run-view panel — same filters, same colors — over whatever
the selected node scopes to, and it is where the *cross-run* question lives, because a run view
can only ever show one run:

* a **run** node — that run's lines;
* a **config** node — every run of it, with a ``run_id`` column;
* the **campaign** — a search across every run at once, reporting hits per run joined to each
  run's verdict, so "which runs logged this, and did they fail?" is one query. Click a row to
  read that run's log, and the trail back to the search stays.

There is no playback clock here, so the view drops the graying and the jump button rather than
implying a position it does not have.

**Data browser.** The left panel lists the tables in the campaign's
``_execution/data.db`` — one per metric CSV, plus the ``runs`` **dimension table**
(per-run ``status``/``duration_s`` and each scenario parameter as a ``param_*``
column), with ``campaign.db`` attached as schema ``campaign``. Write **read-only SQL**
in the editor and **Run** it; the result shows as a table and, via the chart builder,
as a chart — pick *x* / *y* / *color* columns and a mark. Join ``runs`` to any metric
table on ``(config_name, run_id)`` to answer "how does *<param>* affect *<metric>*".

**The first query on a cluster campaign may pause.** Its databases live in the object
store, and the service copies them into a local cache inside that first request; every
query afterwards reads the cache. Rather than an unexplained spinner, the Explorer's
loading row and the Data browser's toolbar say what is happening and how much is moving
("First query — fetching campaign data (39.9 MiB) from the object store over a
port-forward…"). Nothing is shown for a local service, which transfers nothing, or for a
campaign already cached — so the message appears exactly when there is something to
explain. It comes from ``GET /campaigns/{id}/data-status``, which is cheap enough to ask
alongside the query itself.

.. note::

   A campaign only becomes queryable once **analysis postprocessing** has run — it
   builds ``data.db`` from the raw rosbags. Launching with **Postprocess when done**
   (the default) runs it automatically on both backends; otherwise the Results tab
   offers a **Run postprocessing** button, and the CLI equivalent is
   ``vast campaign postprocess <id>``. To *change* the postprocessing
   parameters and re-run, use **Retrigger postprocessing** in the Monitor view's
   campaign actions menu (see above). The rosbag→CSV step always runs in
   the campaign's own execution image (locally in a container, in a cluster as a Job),
   because rosbags only deserialize where the system-under-test's ROS2 message types
   are defined.

.. _declared-plots:

**Declared plots.** A campaign can carry its own saved plots, authored in the
``.vast`` under ``visualization.results.data_browser.plots`` (analogous to referencing analysis notebooks).
Each plot is a SQL query plus a `Vega-Lite <https://vega.github.io/vega-lite/>`_
encoding; the viewer runs the query and binds the result rows into the spec as
``data.values`` — so the query's column aliases are the Vega-Lite ``field`` names and
no ``data`` block is written:

.. code-block:: yaml

   visualization:
     results:
       data_browser:
         plots:
           - title: "Landing error vs wind speed"
             query: >
               SELECT r.param_wind_speed AS wind_speed,
                      m.value            AS landing_error,
                      r.config_name      AS config
               FROM runs r
               JOIN landing_error m ON (m.config_name = r.config_name AND m.run_id = r.run_id)
             vega_lite:
               mark: point
               encoding:
                 x:     { field: wind_speed,    type: quantitative }
                 y:     { field: landing_error, type: quantitative }
                 color: { field: config,        type: nominal }

Declared plots render automatically in the Results tab for that campaign, and are
schema-validated with the rest of the ``.vast`` in the Config editor.

These are **campaign-scoped**: one query across every run, rendered in the Data browser. For the
same Vega-Lite authoring against a **single run**, on the replay timeline and with a playback cursor,
see the :ref:`vega run-view panel <vega-panel>`.

The same SQL surface is available to an LLM through the MCP ``describe_campaign_data``
/ ``query_campaign_data_sql`` tools, which resolve locally or delegate to a configured
service — so CLI, MCP, and the web UI read results the same way.

.. _run-view:

Run view
--------

The **Run view** replays a *single run* of a postprocessed campaign over its **rosbag
timeline**. You pick a campaign and a run; the view then lays out a set of **panels**
that a shared **playback clock** drives — dragging the timeline moves every panel to
the same instant. All panels read only the run's recorded results — its postprocessed
``data.db``, plus per-run artifact files such as the 3D scene descriptor (there is no
live connection to the system-under-test).

The run picker lists only campaigns that actually **recorded runs** (``num_runs > 0``,
tallied from ``campaign.db``). A campaign that never started, or that ended before its
store was written, has nothing to replay, so it is not offered here at all — rather
than being selectable and then answering with an empty view.

.. _shutdown-toggle:

**The run ends at its scenario's verdict.** The gear at the right of the header opens the run
view's settings, and its **Include shutdown phase** entry decides whether "this run" means the
trial or the whole recording. It is **off by default** — everything after the verdict is teardown:
nodes being killed, lifecycle transitions failing because their peer is already gone, TF errors
from a publisher that has stopped. It is minutes of wall time, it colors the log red, and it
describes nothing that happened during the run.

It governs the view rather than a panel, which is why it sits in the header and neither the
playback bar nor the log panel carries a toggle of its own: with the shutdown hidden, the
timeline stops at the verdict (so the duration readout is the trial's) and the log stops there
too. Tick the entry and the full recording returns, with a divider on the playback bar marking
where the trial ended.

A menu rather than a bare icon because it is a *view-wide* setting, the same shape the campaign
row's gear uses: the header is otherwise a row of labeled controls, and each further such
setting would add another icon to decode. The entries are named in words, and the menu grows
without widening the header.

.. _reset-3d-view:

**Reset 3D view** is the same menu's second entry, below a divider because it is an action rather
than a setting: it puts the :ref:`3D scene <scene3d-panel>` panel's camera back where the scene
opened — the world's own baked view — and is over. It is the way back from a lost view, and it
exists because there is no way back by hand: the wheel *flies*, so a few notches aimed at a wall
can leave the camera inside geometry or far enough out that the world is a dot. It is enabled while
such a view is mounted and disabled, with that reason on hover, in a run view that has no 3D panel.

It sits in the header rather than on the panel for the mirror image of the reason above: the camera
belongs to one panel, but that panel is the full-bleed base layer and carries no header of its own,
and a button floating over the world would sit in front of the very thing it acts on.

The moment itself is read from ``scenario_timestamps``, written once by postprocessing —
the same row ``search_run_logs`` cuts on, so the web UI and the MCP tools cannot disagree
about where a run ended. A run that reached no verdict, and a campaign postprocessed before
the verdict was recorded, leave the control disabled with that reason on hover: nothing is
trimmed, rather than trimmed to a guess.

Which panels appear, where they sit, and where each gets its data are declared in the
``.vast`` under a top-level ``visualization.results.run_view.panels`` list — the campaign author defines
the view once and every run of the campaign replays through it:

.. code-block:: yaml

   visualization:
     results:
       run_view:
         panels:
           - costmap:
               title: Nav2 costmaps
               position: { anchor: top-right, width: 440, height: 440 }
               minimizable: true
               layers:
                 map:    { topic: /map }
                 global: { topic: /global_costmap/costmap }
                 local:  { topic: /local_costmap/costmap }
                 poses:  { table: poses }     # TF source for placing/moving layers
           - scenario_tree:
               position: { anchor: left, width: 320 }
               source: { table: behaviors }
           - camera:
               title: Camera
               position: { fill: true }       # everything the docked panels leave over
               source: { topic: /camera/image_raw }

Each panel entry is a single-key mapping — the key is the panel **type** (selecting the
panel plugin) and its value holds that panel's fields: an optional ``title``, a
``position`` (an ``anchor`` — ``bottom``/``top``/``left``/``right``, a corner,
``top-center``/``bottom-center``/``left-center``/``right-center``, or ``center`` — plus
``width``/``height`` in pixels or a ``"40%"`` string; or ``fill: true`` *instead of* an
anchor), the toggles ``minimizable``/``minimized``/``hidden``/``fixed``, and panel-specific
**data bindings** (which ``data.db`` table or recorded topic each piece of data comes from).
Any field you omit falls back to the panel type's built-in default, so a bare ``log:``
on its own is a complete panel.

**Two panels are there without being declared.** The ``playback`` transport bar always: a run
view without it has no clock to scrub and every other panel nothing to follow, so it is not a
decision a ``.vast`` gets to make or a line it should have to copy. And a ``scene3d`` for a
simulator that records a run capture (roqsim does; Gazebo has no scene export and so offers
none) — the panel that replays what those runs always write. Declare either one explicitly only
to change it: an entry of your own keeps its position and its fields, exactly as an authored
``execution.env`` wins over a backend's. A campaign with no ``visualization`` block at all still
opens on a working transport bar.

The transport does not count as something to look at — it is the clock, not content — so the view
offers help authoring panels only when it is genuinely all there is. A simulator that contributes a
``scene3d`` has a run view worth watching and is not asked to declare one; declaring only
``playback`` yourself, to move the bar, still leaves nothing to watch and still gets the notice.

**Docked panels reserve the space they occupy, and everything else is placed in what is
left.** A ``top``/``bottom`` bar reserves its height and spans the full width; a
``left``/``right`` column reserves its width and lives in the band between the bars. Both
count in pixels or percent. Combinations that could not be honored are rejected when the
campaign is validated rather than quietly ignored — a ``width`` on a full-width ``top`` bar,
for instance.

**Docked panels always stack vertically**, but the two kinds of side differ in what they
reserve. A bar owns the full width, so bars can only stack one below the other and the side
reserves the **sum** of their heights. A column owns the full height, so several
``anchor: left`` panels tile *down one gutter* rather than sitting beside each other, and the
side reserves the **widest** of them, once — which is how a sidebar of two panels leaves a
single 320 px margin, not two.

A dock reserves a few pixels more than its own size, so neighboring panels never touch: the
gap sits between a column and what is beside it, between a bar and what is above it, and
between the members of one column. Panels still sit flush against the view's outer edges —
it is a gap between panels, not a margin around the page.

Size a column's members by ratio, by pixels, or not at all:

.. code-block:: yaml

   - nav2_behavior_tree: {position: {anchor: left, width: 320, height: "50%"}}
   - scenario_tree:      {position: {anchor: left, width: 320}}   # takes the rest

A **percentage on a column member is a fraction of the column**, not of the view — two
members at ``"50%"`` come out the same height and tile it exactly, gap included, with no
arithmetic to subtract the playback bar. This
is the only place a percentage is measured against something other than the whole view: a
bar's ``height: "8%"`` and a column's ``width: "25%"`` are both of the view, which is their
natural reading. A member with **no** ``height`` takes whatever the members above it left, so
only the last member of a side may omit it; declaring another panel after it is refused at
validation rather than silently stacking one on top of the other.

The four ``-center`` anchors differ from the matching dock in one way that matters:
``bottom`` **docks** at the very edge and reserves its height, which is what the playback bar
does. ``bottom-center`` *floats* above that reserved band, centered on the free width, and
reserves nothing itself — so it shares the bottom edge with the playback bar instead of
covering it. It needs a declared size (a full-width one is just ``bottom``). The pinned edge
is the one the anchor is named after, so ``minimized`` collapses the panel towards that edge
and expands it back away from it. The corners behave the same way, and both float 12 px
inside the reserved bands where a docked column sits flush against the edge.

``fill: true`` is used **instead of** an anchor: the panel takes the whole rectangle the
docked panels leave over, and needs no ``width``/``height`` — those come from the docks
around it. It sits beneath the floating panels, so corners and ``-center`` panels overlay it.
Only one panel per view may declare it; a second would occupy the same rectangle invisibly,
and is refused at validation.

**Some panels are contributed by the simulator backend and need no entry at all.** A backend
whose runs always record the capture the ``scene3d`` panel replays supplies that panel the same
way it supplies the environment that produces the capture — there is nothing to decide, so
there is nothing to declare. Declaring it yourself still wins, which is how you place it
somewhere other than the base layer.

The declared layout is where the panels **start**, not where they are stuck:

* **Move** — press the mouse on a panel's **title bar**, drag, and release to drop it there.
  A moved panel is raised above the others, and always keeps an edge inside the view so it
  can be grabbed back.
* **Resize** — drag **any edge or corner** of a panel. The edge you grab is the one that moves
  and the opposite one stays put, so a ``left`` column can be widened from its right edge or
  its left, and a ``bottom-right`` panel from any of its four sides. A ``left``/``right``
  column with no declared ``height`` spans the view; dragging its bottom edge is what gives it
  one. An axis a panel gets no size along has no handle — a full-width ``top`` bar's width is
  ignored by the layout, so it is resized by height alone.

Panels that show no title bar cannot be moved — the docked ``playback`` transport and a base
layer such as ``scene3d`` stay put. A ``fill: true`` panel is sized by the docks around it, so
it has no free edge to drag either. Everything else is movable and resizable unless the
``.vast`` says otherwise: ``fixed: true`` locks a panel's geometry outright, and
``resizable: false`` is the narrower opt-out for one that may be moved but not resized.

These are **view-local** adjustments: they last as long as the view is open, and reloading it
(or switching runs) restores the layout the ``.vast`` declares. The space a dock reserves is
the one the ``.vast`` declares too, so resizing or minimizing a docked bar or column in the
view does not re-flow the panels around it. To change the layout for good, edit it under
**Edit visualization**.

The built-in panels:

**Playback** (``playback``) — a transport bar spanning the bottom: a click-to-seek
progress bar, an icon play/pause, a **2×** fast-forward toggle, and a ``current / total``
time label. It owns the clock; every other panel follows it, which is why it is contributed
to every campaign rather than declared — see above. The timeline range comes from the run
capture's own time base when a ``scene3d`` panel declares one (the run's ground truth, and available
before any postprocessing), else from an explicit ``visualization.results.run_view.timeline``, else from the union of the
postprocessed ``poses`` / ``behaviors`` / ``scenario_timestamps`` timestamps.

That range is the whole **recording**; where the trial ended is a separate figure, so the
:ref:`shutdown toggle <shutdown-toggle>` can shorten the timeline and restore it without
re-querying anything. While the shutdown phase is shown, the bar draws a full-height divider at
the verdict — with it hidden the verdict *is* the end of the bar, and a line there would mark
nothing.

**Costmaps** (``costmap``) — an rviz-style top-down view of what nav2 saw: the static
map, the global and local costmaps, the **actual path the robot drove**, and the robot
marker, all at the current time (scroll to zoom, drag to pan). Each ``layers`` entry
binds a name to a costmap **topic**; ``poses`` (the TF table) both places the layers into
the map frame and provides the driven-path trail + robot pose. It requires the
:ref:`costmap postprocessing step <costmap-delivery>` — if the ``costmaps`` data is
missing the panel says so rather than drawing nothing.

A layer is left out, and named in the top-left corner (*"local: nearest frame 4.3 s away"*),
when the recording genuinely has no frame near the cursor — before nav2 starts publishing,
after it stops, or across a gap mid-run — rather than showing the closest frame it could find
as though it were current. Each layer is judged against **its own** publish rate, so a static
map, published once and never again, is never affected.

While scrubbing, layers keep showing their last frame instead of blanking: a replacement is
already being fetched, and the layers differ enough in weight (a full-map global costmap is
~15 KB against a local costmap's sub-KB) that blanking during the catch-up would make the
heaviest one flicker. Note also that a costmap is placed at the time it was published, so at
speed the robot marker can sit slightly ahead of its window — that offset is the recording's
own resolution, not drift. *This panel ships with the*
``robovast_nav`` *package* (not the core UI) as a package-provided panel — see below — so
it is available whenever ``robovast_nav`` is installed; the ``.vast`` still references it
as plain ``- costmap:``.

.. _camera-panel:

**Camera** (``camera``) — a camera that was **recorded during the run**, played on the
playback clock. This is what a simulator with no 3D scene has instead of one: Gazebo writes
no run capture and has no scene exporter, so a :ref:`scene3d <scene3d-panel>` panel has
nothing to replay there, while a monitor camera spawned into the world gives that run view a
picture of the trial.

It needs no bindings — a bare ``- camera:`` is a complete panel whenever the run registered
exactly one video, the same promise ``scene3d`` makes. ``source: { topic: … }`` picks one when
a run recorded several; ``source: { path: …, t0: … }`` is the escape hatch for a video no
producer registered, and needs ``t0`` because a file with no entry in the ``videos`` table
carries nothing that says where it sits on the timeline.

The panel is a **reader** of the clock and never a writer, so it shows no controls of its own:
the :ref:`playback <run-view>` bar owns time and this follows it, including at 2×. Seeking
happens only when the element drifts more than about one frame from the cursor, so ordinary
playback is not a seek storm. Outside the recording — a camera that came up late, a trial that
ran past the last frame — it dims and says *"No frames at this time"* rather than showing
frame 0 as though it were the current moment.

Where the video comes from: a producer writes it into the run directory and registers it in
the ``videos`` table. ``rosbags_to_webm`` is the first such producer (see the
:ref:`worked example <videos-table>`), but the table is a contract any of them may write.

Two properties worth knowing. The encode is **constant-rate** — ``fps`` is derived so the
first and last frames land exactly on their recorded moments, so only mid-run jitter drifts,
which is sub-second at a monitor camera's 1 Hz. And **seeking is efficient on the local lane**:
the file is served with ``FileResponse``, so the browser ranges into it. A cluster campaign
fetches the one object behind the address first, then serves it the same way.

**Scenario tree** (``scenario_tree``) — an rviz-scenario-execution-style behaviour tree
that colors each node by its status (running / success / failure) at the current time.
It reads the ``behaviors`` table, written by ``scenario_execution`` on every run (no ROS
required). Where the data supports it, each node
also shows its kind (sequence / selector / parallel / decorator), its feedback message at
the current time, and — on hover — its class and the ``.osc`` file and line it came from;
when a tree ends in failure the panel names the action responsible, via ``tip_id``.

**Double-clicking a node seeks playback to the moment that node next changes status** — so on an
inactive ``wait_for_pick`` the first double-click lands exactly where it starts running, the next
on where it succeeded or failed, and a further one wraps back to its first change. Playback pauses
on the jump, and only real status transitions count: a node re-reporting ``RUNNING`` with a new
feedback message is not a stop.

The panel renders *any* table in the ``behaviors`` schema — point it at another with
``source: { table: <name> }``. Columns a table does not have are simply not shown, so an
older or differently-produced table still renders.

**Run log** (``log``) — everything the run said, following the playback cursor. One row per log
event from every container, joined with ``/rosout`` and placed on the run's clock (see
:ref:`merged-run-log`). Lines not yet logged at the cursor are grayed out with a divider marking
"now", so the log's whole shape stays visible while the position in it is unambiguous.

Filtering is instant and client-side, over the whole loaded log: a text box (substring, or a
regular expression with the ``.*`` toggle), two severity chips that cycle
*off → highlight → only these*, and one dropdown listing every ``container``, ROS ``node`` and
``source`` **this run actually produced**, with counts. Clicking a line seeks playback to it, and
the ▲▼ buttons jump to the previous/next warning or error — which is what turns a filter into
navigation. Scrolling away stops the follow and raises a button showing the cursor's time; click
it (or press ``Escape``) to jump back and resume following.

The log also stops at the scenario's verdict. In the run view that is the header gear's
:ref:`Include shutdown phase <shutdown-toggle>` entry and this panel shows no control of its own
— one question, one place to answer it. The Explorer's **Log** tab has no run view around it, so it
carries a power icon for the same setting in its own filter bar. Either way the cut is on the
verdict's **wall** time and not its sim time: the clock map does not extrapolate, so lines after
``/clock`` stopped have no sim time at all, and a sim-time cut is blind to exactly the lines
the toggle exists to remove.

Wall time is also what the log is **ordered** by, which is why those lines sit at the end where
they were logged — and not sim time with the rows lacking one placed first, which would read a
missing sim time as "logged before the simulator's clock started". It does not mean that: the
clock map is silent at *both* ends of its range, so such an order lands a run's shutdown at the
very top of the log, next to the boot lines.

A line with no sim time is **dimmed** in the time column, and that is the only marking it gets:
the figure is the seconds since the run's first log line, measured on the wall clock, and hovering
it says so. Nothing is prefixed to it, so a column of monospace figures keeps its alignment.

A log covering more than one run (a config's, or the Explorer's whole campaign) is ordered by run
first, so it reads as one run after another instead of interleaving runs that each start at zero.
There is no cursor in that view: every run has its own moment ``12.5 s``, so a single position
cannot point into all of them, and the log is shown plain rather than divided at an arbitrary row.

The footer never stays silent about what is missing: no ``run_log`` table (postprocessing predates
it), a run with no clock map (``wall time only``), how many shutdown lines were hidden and how the
scenario ended, how many lines the filter hid, and whether the load hit its ceiling.

The playback bar itself gains tick marks for every warning and error — full height for errors,
half for warnings — so the log's shape is visible *before* you scrub into it.

**Nav2 behavior tree** (``nav2_behavior_tree``) — the same tree view for **nav2's own**
behavior tree, reading the ``nav2_behaviors`` table produced by the :ref:`nav2 BT
postprocessing <configuration>` (``rosbags_nav2bt_to_csv`` + ``nav2_bt_tree``): node status
over time from nav2's ``/behavior_tree_log``, tree structure from the BT XML nav2 ran.
Declare it as ``- nav2_behavior_tree:`` and it brings its own table, title and — when the
table is absent — the nav2 postprocessing steps to add, rather than the scenario's
``bt_log``.

*This type ships with the* ``robovast_nav`` *package*, so it is available whenever that
package is installed, but it is not a second implementation: it renders the built-in panel
above with different defaults, and so gains its behaviour automatically. Both trees can be
shown at once — the scenario's says what the trial did, nav2's says why the navigator
recovered. See :repo_link:`configs/examples/basic_nav` for a complete campaign.

.. _scene3d-panel:

**3D scene** (``scene3d``) — the 3D world view, typically the run view's **base layer**
(``position: { fill: true }``): the simulated world's actual geometry rendered in the browser, with
**everything that moved** replayed — including *articulation*, so an arm bends rather than swinging as
one rigid piece.

The mouse bindings:

- **wheel** — fly toward or away from whatever is under the pointer. You steer by aiming: point at a
  far shelf and scroll, and you arrive at that shelf. The approach slows as you close on it, so a
  notch crosses open ground quickly and never crosses the surface you are aiming at — and a notch out
  undoes a notch in exactly.
- **left-drag** — turn about the surface under the pointer. Aimed at a corridor wall a metre away it
  reads as looking around; aimed at a building across the world it orbits the building.
- **right-drag** — pan sideways and vertically, at roughly the speed of the surface being dragged.
- **double-click** — travel to frame whatever you clicked, without turning the camera.

All three drags are scaled by one number, the distance to the **pivot**, and the panel re-measures it
against the actual geometry whenever a gesture starts. That is what lets a single set of bindings fit
a 3 m tabletop and a 65 m building without a mode or a speed setting: a pivot fixed at the distance
the world was authored with is right in the opening frame and wrong everywhere you travel to, which
is felt as a drag that flings the camera through walls and a wheel that cannot approach one.

The wheel deliberately does not shrink an orbit radius toward a *fixed* center, which is the usual
default and is what makes such a view freeze a short way from that center; here the pivot is
re-chosen every gesture and can be flown through. Because it flies without bound, the way back is
the header gear's :ref:`Reset 3D view <reset-3d-view>` entry, which re-frames the camera at the view
the world was authored with.

It needs no bindings at all — ``- scene3d:`` on its own is a complete panel — because the run's
**capture** names the world it used and the service builds the matching **geometry** on demand. Both
artifacts are specified in :ref:`run-capture`.

*Geometry is compiled when somebody looks, not when a campaign runs.* A descriptor is 13–31 MB and takes
5–9 s to compile, for an artifact whose only consumer is this panel — so a campaign does not ship one.
On the first view the service compiles it **inside that campaign's own pinned image** (the world is
generally installed there from a wheel, and a host that merely happens to have the tooling could be a
different version, which renders plausible but wrong geometry) and caches it keyed by *world identity*:
image digest + world reference + ``world_overrides``. Every later view is a read from disk — and so is
the first view of **every other run, and every other campaign that used the same world**. A 25-run sweep
compiles once, not 25 times.

Because a build is seconds (≈8 s on a warm cluster node) and can be a couple of minutes when the node
must first pull the image, the panel **names what it is waiting for** rather than spinning: *Fetching the
simulation image onto the node*, *Compiling the world geometry*, *Copying the scene back from the
container*. The rest of the run view stays usable meanwhile — the capture, the timeline and the
table-fed panels need no geometry, so playback and the costmap keep working — and a failure stops polling
and shows its reason.

Nothing is listed under the panel. A capture's tracks name the joints and bodies they drive exactly as
the descriptor spells them, so what gets animated is discovered from the artifact pair; a track matching
nothing is reported, with the capture's own ``world`` and ``producer``, rather than leaving a silently
static world.

.. note::

   The panel does not animate from the postprocessed ``poses`` table (``rosbags_tf_to_csv``): that
   would need a rosbag before anything moved, impose a naming contract on the simulator plus a
   ``bind`` list for its exceptions, and could only place bodies parented to the world — so an
   articulated robot would replay rigid. Nor is there a ``scene.scope``/``capture.scope`` to declare:
   geometry is resolved by content key, so there is nothing to declare, and nothing to declare
   *wrongly* (a campaign-scope descriptor aimed at a world that varies per configuration renders
   confidently wrong geometry, and no validation could catch it). The ``poses`` table itself serves
   the costmap panel and ``timeseries``.

   ``execution.generate`` is supported for a campaign that wants its descriptor *frozen into its
   results* — an archive that must replay even without the image — but it is not how the run view
   obtains geometry.

**2D scene** (``scene``) — a top-down/side 2D plot of "where the thing is right now": one
column against another (e.g. a quadrotor's ``x`` vs altitude ``z``) from any table with a
time column (``source``, ``x``, ``y``; ``trail: false`` disables the driven path).

**Time series** (``timeseries``) — a chart of one or more numeric columns over the run's
timeline with a cursor at the current time (``source`` + a ``series`` list of
``{ column, label }``).

Every panel binding a ``source`` takes ``decimate_hz`` (and ``key``) with it, on the same terms as
the :ref:`vega panel <vega-panel>` below: a run longer than the row cap is cut at the head unless it
is thinned.

**State** (``state``) — the current numeric values of selected columns as labeled
read-outs (``source`` + ``fields`` of ``{ column, label, unit }``).

.. _vega-panel:

**Vega chart** (``vega``) — any diagram, declared as a `Vega-Lite
<https://vega.github.io/vega-lite/>`_ spec over one of the run's ``data.db`` tables. Where
``timeseries`` plots columns that *already exist*, a spec's ``transform`` can **derive** what the
run never recorded, and any Vega-Lite mark is available. It binds a ``source`` (the same
``{ table, time_column, filter, decimate_hz, key }`` as ``timeseries``, so the run scope, the frame
filter and the thinning all happen in SQL) plus a ``vega_lite`` spec, and optionally ``max_rows``
(default 5000, which is also the ceiling — see below).

*Which of the two to pick:* ``timeseries`` is a hand-rolled canvas chart — the cheap path for
numeric columns at high sample rates. ``vega`` costs a full Vega render but expresses everything
else.

The spec is bound to two **named datasets** and so declares no ``data`` block of its own:

* ``table`` — the run's rows for that table;
* ``cursor`` — a single row ``{t}`` at the current playback time.

The playback cursor is layered in **automatically**, into the top-level spec and into each child of a
``vconcat``/``hconcat``/``concat`` — but only where it means something: the spec must be layerable
(``mark`` or ``layer``) and must bind the time column to ``x`` or ``y``. A boxplot by frame therefore
gets no cursor, and ``facet``/``repeat`` specs are left alone. Reference the ``cursor`` dataset
yourself to place it anywhere else.

Three things to know when charting a ``poses`` table, because every such spec hits them:

* **Dotted column names.** ``rosbags_tf_to_csv`` writes ``position.x`` / ``orientation.yaw``, and a
  Vega-Lite ``field`` reads a dot as a nested path. Either escape it (``position\.x``) or — usually
  clearer — hoist it to a flat name in a ``calculate`` transform: ``datum['position.x']``.
* **Every ``data.db`` column is TEXT.** The panel coerces each column whose values all parse as
  finite numbers, so ``type: quantitative`` works without a ``format.parse`` block.
* **A long run needs ``decimate_hz``, not a bigger ``max_rows``.** The row cap is a ``LIMIT`` applied
  *after* ``ORDER BY`` time, so a run that outgrows it is cut at the **head**: the chart ends
  mid-run while looking complete. Raising ``max_rows`` cannot fix that — the data query clamps at
  5000 rows whatever a panel asks for, which is why ``vast config validate`` rejects a larger one.
  ``source: {decimate_hz: 5}`` instead keeps one sample per 1/hz second across the *whole* run, in
  SQL. Rule of thumb: ``hz ≈ 4000 / run seconds``; a 460×380 panel resolves nothing past a few
  hundred points anyway. The panel says so itself when a query is truncated.

  On a multi-keyed table (``poses`` is keyed by ``frame``) either ``filter`` down to one series, as
  the example below does, or name the key — ``source: {decimate_hz: 5, key: frame}``. Without one of
  those, each time bucket keeps a single row from a *single* frame and the other frames vanish
  entirely rather than being thinned. Thinning also assumes a numeric time column: an ISO-8601
  ``timestamp`` casts to 0 and lands the whole run in one bucket.

A worked example over a ``poses`` table — derived speed above the raw pose, sharing one time axis, so
both charts get a cursor:

.. code-block:: yaml

   - vega:
       title: base_link
       position: {anchor: bottom-right, width: 460, height: 380}
       source: {table: poses, filter: {frame: base_link}, decimate_hz: 5}
       vega_lite:
         resolve: {scale: {x: shared}}
         transform:
         - {calculate: "datum['position.x']", as: px}
         - {calculate: "datum['position.y']", as: py}
         - window:                                  # previous sample, to difference against
           - {op: lag, field: px, as: px0}
           - {op: lag, field: py, as: py0}
           - {op: lag, field: timestamp, as: t0}
           sort: [{field: timestamp}]
         - filter: "isValid(datum.t0)"              # the first sample has no predecessor
         - calculate: >
             sqrt(pow(datum.px - datum.px0, 2) + pow(datum.py - datum.py0, 2))
             / max(datum.timestamp - datum.t0, 1e-6)
           as: speed
         - window: [{op: mean, field: speed, as: speed_avg}]   # differencing TF is noisy
           frame: [-9, 0]
           sort: [{field: timestamp}]
         vconcat:
         - height: 150
           encoding:
             x: {field: timestamp, type: quantitative, axis: null}
             y: {field: speed_avg, type: quantitative, title: "speed [m/s]"}
           mark: {type: line, strokeWidth: 1.5}
         - height: 120
           transform: [{fold: [px, py], as: [series, value]}]
           mark: {type: line, strokeWidth: 1.5}
           encoding:
             x: {field: timestamp, type: quantitative, title: "t [s]"}
             y: {field: value, type: quantitative, title: pose}
             color: {field: series, type: nominal}

This is the same authoring language as the campaign-scoped :ref:`declared plots
<declared-plots>` above; the difference is scope and binding — ``visualization.results.data_browser.plots`` runs a SQL query
across the whole campaign and renders in the Data browser, while a ``vega`` panel binds one table of
one run and renders in the Run view.

Custom and package-provided panels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The panel types above are the ones bundled into the core UI, but the run view is not
limited to them. A panel can also be loaded **at runtime** from outside the core UI as a
`Module-Federation <https://module-federation.io/>`_ remote — a small pre-built JavaScript
bundle. This is how the ``costmap`` panel ships from ``robovast_nav`` rather than the core
UI, and it is also how *you* can add a bespoke visualization to a run view. Either way, in
the ``.vast`` a panel is always referenced by a **type name** the same way — where the
code lives is invisible to the config:

* **Package-provided** — an installed plugin package registers a panel in the
  ``robovast.panel_types`` entry-point group and ships its built bundle as package data.
  Reference it by its registered type name (``- costmap:``); it is available to every
  campaign once the package is installed.
* **User-authored** (``custom``) — you build your own panel bundle and drop it next to the
  ``.vast``, referencing it by path:

  .. code-block:: yaml

     visualization:
       results:
         run_view:
           panels:
             - custom:
                 remote: panels/my_panel    # dir (or remoteEntry.js) relative to the .vast
                 module: ./myPanel          # the exposed module (default ./panel)
                 title: My view
                 position: { anchor: right, width: 420 }
                 # any further keys are panel-specific data bindings (as for built-ins)

**Writing a panel.** A panel is a React component implementing the same contract the
built-ins use — ``({ spec, clock, data }) => JSX`` — so it is time-synced and reads the
run's ``data.db`` exactly like a built-in: ``clock.t`` / ``clock.subscribe(...)`` for the
current playback time, ``data.series(table)`` / ``data.nearest(table, t)`` for run rows,
``spec.config`` for the panel's ``.vast`` bindings. A panel needing a specialized endpoint
(as the costmap panel needs nav2 grids) reaches it through the generic run-scoped
``data.fetchRun(endpoint, params)`` — the run view core stays free of panel-specific
knowledge. Build the component as a Module-Federation remote exposing that module, with
``react``/``react-dom`` marked as **shared singletons** pinned to the host's version
(``^18``); a broken or missing bundle shows an inline error, never a silent blank. See
:repo_link:`src/robovast_nav/web` for the reference build (the costmap panel) to copy from,
and the developer guide for the internals.

**Several panels in one package.** A package that ships more than one panel builds them into
a **single Module-Federation container** exposing one module per panel, and each panel's type
class sets a shared ``REMOTE_NAME`` (the container name) so the service points every type at
the one bundle. Adding a panel is then one more ``exposes`` entry plus one panel class — no
new build, and React/vendor chunks stay shared.

**Before writing a renderer**, check whether a built-in panel already draws your data. A
package whose data is a *table in an existing schema* should still ship its own **type** — so a
``.vast`` names it and gets its table, title and empty-state guidance — but that type can
**derive** from a built-in panel instead of reimplementing one. The host passes its own panel
components to a remote as ``props.builtins``; ``robovast_nav``'s ``nav2_behavior_tree`` is a few
lines that render ``builtins.ScenarioTree`` with nav2's defaults, and inherits every later
improvement to it. Write a renderer only when the host cannot draw the data at all — ``costmap``,
whose binary grids need their own endpoint.

**Serving a panel's data.** A panel reads the run's postprocessed ``data.db`` through ``data``
(``fetchRun`` for anything beyond plain table rows). When that data comes from a table your own
**postprocessing** step produced and needs custom serving (untruncated blobs, nearest-frame
selection, …), a package can also ship the endpoint: a small class registered in the
``robovast.service_endpoints`` entry-point group, serving ``GET /campaigns/{id}/<name>`` from the
campaign's data — no core change, and it works the same on local ``vast serve`` and the in-cluster
service. So an analysis package can own the whole chain end-to-end — *postprocessing step →
service endpoint → panel* — with nothing in core. The costmap panel is exactly this: its
``rosbags_costmap_to_csv`` step, its ``costmap`` endpoint, and its panel all ship in
``robovast_nav``. (Large binary artifacts don't even need an endpoint — serve them as ordinary files
via ``data.runFileUrl(path)`` for one run's, or ``data.campaignFileUrl(path)`` for one the whole
campaign shares, as the ``scene3d`` panel does.) See the developer guide for the endpoint contract.

.. _scene-descriptor-delivery:

**3D scene data delivery.** The ``scene3d`` panel renders a **scene descriptor** — ``scene.json`` +
``scene.bin`` + one PNG per texture, a compact browser-renderable export of the simulated world, defined
in :ref:`run-capture` and produced for roqsim by ``roqsim/export_web.py``. It is a *directory*, not a file: the
loader fetches ``scene.bin`` and the textures as **relative siblings** of ``scene.json``.

A campaign does not deliver it. The service resolves it per view:

.. code-block:: text

   GET  /campaigns/{id}/scene?config_name=&run_id=   status only; never starts a build
   POST /campaigns/{id}/scene/run                    the explicit trigger
   GET  /campaigns/{id}/scene_assets/{key}/{file}    the bytes, from the shared cache

The split matters: a ``GET`` that started a build would fire on a browser prefetch or a React
strict-mode double render, and each of those would launch an image pull. Status is modeled on
``data-status`` (*say why you are about to wait, before you wait*), starting work is a ``POST`` returning
``ActionResult`` as ``postprocessing/run`` is, and the bytes are served like a panel bundle because they
live in the service's cache rather than in the campaign's results. The cache key is in the asset path so
one URL prefix addresses the whole entry, which is what makes the loader's sibling fetches resolve.

The cache is **shared across campaigns** and durable (``~/.robovast/cache/scenes``, overridable with
``ROBOVAST_SCENE_CACHE``; size-capped by ``ROBOVAST_SCENE_CACHE_BYTES``, evicted whole-entry
least-recently-used). Two consequences worth knowing:

* A campaign whose image is gone — garbage-collected, or a mutable tag rebuilt under the same name —
  cannot have its geometry rebuilt, and the panel says so rather than showing an empty world. A campaign
  that records only a mutable tag refuses to cache at all, because an entry keyed on a tag may silently
  describe different bytes later.
* A downloaded or shared campaign has no descriptor in it. If a self-contained archive matters more than
  laziness, keep an ``execution.generate`` entry for that campaign.

.. _costmap-delivery:

**Costmap data delivery.** Occupancy grids cannot be flattened into ``data.db`` columns
usefully (a grid becomes thousands of per-cell columns). Instead the
``rosbags_costmap_to_csv`` postprocessing step stores each grid **losslessly and
compactly** — its int8 cells zlib-compressed — into a ``costmaps`` table, together with
the geometry (resolution in m/cell, width/height in cells, so the map spans
``width×resolution`` by ``height×resolution`` **meters**, and the origin pose). Record the
costmap topics in the scenario and add the step to postprocessing:

.. code-block:: yaml

   results_processing:
     postprocessing:
       - rosbags_tf_to_csv: { frames: [base_link, odom] }   # base_link=robot/path, odom=local costmap
       - rosbags_costmap_to_csv:
           topics: [/map, /global_costmap/costmap, /local_costmap/costmap]

The run-view costmap panel fetches the frame nearest the current time from the campaign
``costmap`` endpoint (delivered untruncated) and inflates it in the browser. The same
geometry is visible to an LLM via MCP ``describe_campaign_data`` (the ``costmaps`` table
description carries the map's size in meters, resolution, layers, and delivery), so it can
reason about the run without decoding grids.

Development
-----------

For UI development with hot reload, run the Vite dev server against a running
service:

.. code-block:: bash

   vast serve                # in one terminal (the service to talk to)
   cd frontend/ui && npm run dev      # in another (Vite on :5173)

The dev server proxies the API path prefixes to the service so the browser stays
same-origin (no CORS). Point it at a different service with
``ROBOVAST_SERVICE_URL``. See :ref:`web-ui-internals` in the developer guide for
the app's structure and how to extend it.
