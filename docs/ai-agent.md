# Putting an agent behind proteinCAD

Notes toward a version of this that designs proteins on its own: an agent that
can look at what is on screen, decide what to build, write the job, run it, read
the number that comes back and decide what to do next.

Nothing here is implemented. This is the shape of the problem and which parts of
the existing code already solve bits of it, written down while it is fresh.

---

## The short version

The hard parts of an agent that drives a design tool are usually *actions* and
*guard rails* — how does it say what it wants, and how do you stop it saying
something impossible. This codebase already answers both, for reasons that had
nothing to do with agents:

- **`RFDIFFUSION_OPTIONS` in `proteincad/colab_worker.py`** is a typed, grouped,
  documented catalogue of every setting the model accepts, served to the browser
  over `/api/design/options` and checked against RFdiffusion's own `base.yaml`
  in both directions by `tools/check_server.py`. That is a tool schema. An agent
  handed it cannot invent a flag, and a flag RFdiffusion adds or removes shows up
  as a failing test rather than as a run that dies a minute in.

- **`validate_spec()` in `proteincad/design.py`** is imported by both the local
  server (`api.py`) and the Lambda (`cloud_api.py`). An agent becomes a third
  caller through the same door. It gets no privileged path and cannot express a
  job a human could not.

So what is left is mostly **state and memory**, which is where the work is.

One more thing that falls out for free: the job spec is the contract between
*deciding what to build* and *computing it* (the `Runner` interface in
`design.py`). An agent is another producer of specs. It goes where the Design
panel is, not where the model is — **the GPU path does not change at all.**

---

## Where the agent lives

The first real decision, and the one everything else depends on.

| | sees what the user sees | survives a closed tab | cost |
| --- | --- | --- | --- |
| in the browser, calling `App` | exactly | no | trivial to start, dead end |
| server-side, its own headless scene | needs rebuilding | yes | needs the scene model server-side |
| server-side, browser as a view | through sync | yes | a sync protocol to design |

Overnight campaigns rule out the first. The catch is that the scene model is
JavaScript — `web/src/core/` and `web/src/io/` — while `proteincad/structure.py`
is a much thinner server-side parser meant for measuring, not editing.

Three ways out:

1. **Run the JS core in node as a sidecar.** Those two directories never import
   three.js and never import `api.js`; `node tools/check.mjs` already exercises
   the parsers, DSSP, bond perception, the selection language and the spec
   builder with no browser. This is the least new code and the least drift.
2. Port what the agent needs into Python, and accept two implementations.
3. Keep the agent reasoning over *derived facts* rather than a live scene, and
   let the browser stay the only place a scene exists.

(1) is the recommendation. The headless half of this app already exists; it just
has no front door.

---

## What is missing

### A scene that outlives a tab

`POST /api/session` currently accepts the viewer's description of the scene and
throws it away — the docstring says as much. For an agent the scene has to
become a persisted, versioned, addressable object: load this, move that, crop
here, and afterwards be able to say what it did and why.

This is the single biggest gap. Everything else waits on it.

### A tool surface shaped like intentions

The REST API assumes a browser holding the scene in memory. Agent tools want to
be coarse and intention-shaped — *crop around these residues at this radius*,
*what touches this patch*, *what length does this volume imply* — not a hundred
fine-grained getters. Few, large, well-described tools beat many small ones, and
each should answer with numbers rather than prose.

The selection language is already most of this. `byres (within 4.5 of resn HEM)`
is a text interface to 3D state, which is exactly what an agent wants.

### Episodic memory, as a first-class object

Jobs are a flat list today. An agent needs a **campaign**: a goal, a budget, and
a chain of hypothesis -> spec -> result -> verdict -> next hypothesis.

It has to live on disk, not in context. A campaign runs for days and will exceed
any context window; the campaign record *is* the memory and the context is only
a working set. Design for the agent crashing between any two steps.

### Research, in a different trust zone

The model container runs `--network=none`, read-only, with no credentials
(`proteincad/sqs_worker.py`). That is deliberate, and it means retrieval — the
PDB, UniProt, literature — can never happen where the model runs. It belongs on
the API side, cached, with every fetched fact carrying its source.

The architecture already enforces this separation. Do not erode it.

### An objective function and a budget

Autonomy without a stopping condition is an expensive loop.

The number already exists: `rmsd_to_backbone` and `plddt` from the fold stage,
with `contacts` and `hotspot_contacts` beside them, and the usual bar of **under
2 A with a confidence above 80** (`foldVerdict` in `web/src/ui/design.js`). Make
it explicit — what counts as success, how many attempts, how many GPU-dollars,
and what *give up and report* looks like.

### Approval gates and blast radius

Every run is real money on a `g4dn.xlarge`. The quota machinery in
`cloud_api.py` — per-user daily, concurrent, global daily, machine starts — is
the right hook. Give the agent its own quota identity rather than borrowing a
user's, plus a hard spend ceiling and a kill switch.

Then decide which actions are autonomous (crop, measure, propose, fold) and
which need a human (spend GPU time, publish, export).

### Screening and audit

Autonomous generative protein design wants a sequence-screening step before
results leave the system, and an append-only record of what was asked and why.

Half of it exists: every job carries `job.log`, the exact `run_inference.py`
line it ran. Extend the habit — every artifact carries what produced it. That is
also what makes the agent debuggable, so it is not pure overhead.

---

## The loop, and why time is the hard part

> perceive -> propose -> validate -> run -> measure -> decide -> repeat

Four of those six map onto code that already exists: `app.session()` for
perception, `buildJobSpec` for the proposal, `validate_spec` for the check, and
the fold metrics for the measurement.

The trap is **duration**. A backbone run is minutes, a fold is minutes more, a
cold machine is five, a campaign is days. A chat-style loop holding a session
open across that will die, and re-running it burns the context re-deriving what
it already knew.

So the agent has to be **resumable and event-driven**: wake on a job-completion
event, read the campaign record, decide one step, write the decision down, exit.
The conversation is not the state. The campaign is.

---

## On "viewing" the protein

Mostly not pixels.

The useful perception is structured — sequence, chains, components, secondary
structure, what lies within 4.5 A of a patch, buried surface, the contig that
would be generated. All of it is computed already.

Rendered snapshots are worth having for two narrower jobs: a human skimming what
the agent did, and a gross sanity check ("is the binder inside the target?").
Neither is the primary channel.

---

## Build order

1. **Persist the scene.** Make `/api/session` real and versioned. Everything
   else waits on this.
2. **Expose the headless core as a tool server** — spec building, cropping,
   measuring. It already runs in node.
3. **Serve the option catalogue as the tool schema.** It is already the right
   shape; it needs converting, not designing.
4. **Define the campaign object** — goal, budget, attempts, metrics, verdict.
5. **Write the loop as resumable steps**, driven by job events, campaign on disk.
6. **Gate the spend** — agent quota, approval before GPU, kill switch.
7. *Then* widen to research and retrieval.

The demo that proves the whole thing is the agent version of the bar this
prototype already sets: **give it a target and a goal, let it run unattended,
and have it come back with the best RMSD and pLDDT and the exact commands that
produced them.** Everything above serves that. Anything that does not can wait.

---

## What will bite

- **Context is not memory.** If the campaign lives in the conversation, day two
  is a different agent.
- **Non-determinism meets a metered GPU.** Log the full basis of each decision
  or no run is ever reproducible.
- **The scene is in the browser and the agent is not.** This is the gap that
  gets underestimated.
- **Validation is the safety net.** Never give the agent a path that goes round
  `validate_spec`, however convenient it looks.
- **Build it against `mock` first.** The mock runner exercises the whole
  workflow — spec, transport, placement, rendering — with no GPU and no money.
  Debug the entire loop there before spending a cent.

---

## Model and API notes

For the record, at the time of writing:

- `claude-opus-5` with adaptive thinking (`thinking: {type: "adaptive"}`) is the
  sensible default; effort is a separate dial (`output_config.effort`).
- The option catalogue converts naturally into tool definitions with strict
  schema validation, so arguments arrive already valid.
- Prefer owning the loop over adopting a framework. The state machine is already
  yours, and the hard parts here — the scene and the budget — are not what a
  framework solves.
- Re-read the campaign record each turn rather than carrying it in context.
