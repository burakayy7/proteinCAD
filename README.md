# proteinCAD

A 3D viewer and editor for protein structures — built to handle single proteins
and large multi-chain nanostructures, and to be a place where computational
design tools can plug in later.

```
python3 -m proteincad
```

That serves the app at <http://127.0.0.1:8080/> and opens a browser. There is
nothing to install: no npm, no build step, no Python dependencies.

Try `?load=7CGO` for the 219-chain *Salmonella* flagellar motor–hook complex,
`?load=4HHB` for haemoglobin with its haems, or `?load=1BNA` for B-DNA.

## What it does

**Loading.** Drop a `.pdb` or `.cif` file anywhere on the window, type an id
into the box, or use `?load=4HHB` in the URL. The box takes either databank: a
four-character token is a PDB id, and `EMD-25575` is a cryo-EM density map from
EMDB — see [Density maps](#density-maps). Everything is cached under
`data/cache/` so the second visit works offline. Several structures can be open
at once; each keeps its own coordinates.

**Representations.** Cartoon, backbone trace, ball & stick, sticks, spacefill
and molecular surface — press `1`–`6`, or set one per structure in the tree.
Colour by chain, **protein/component**, structure, secondary structure, element,
residue type, hydrophobicity, B-factor/pLDDT or molecule type.

**Components.** An assembly is usually a handful of proteins in many copies, so
the inspector lists the distinct molecules rather than the chains — for 7CGO,
`Flagellar hook protein FlgE ×33`, `Flagellar M-ring protein ×57` and so on.
Clicking one selects every copy; colouring by protein/component paints each one
a single colour, which is what makes a 219-chain motor readable. The names come
from mmCIF `_entity` descriptions or PDB `COMPND` records, and fall back to
sequence identity so predicted models and design output group correctly too.

**Selecting.** Click a residue, alt-click a chain, cmd-click a single atom,
shift-click to add. Or write it out:

```
chain A and resi 10-40
protein and not water
byres (within 4.5 of resn HEM)
ss h and b > 70
```

The inspector shows what is selected, the sequence of the chain it belongs to
(coloured by secondary structure, click a letter to jump to a residue), and the
usual actions — focus, isolate, hide, invert.

**Editing.** Whatever you loaded or clicked last is the *active* structure —
marked in the tree — and the tools act on that, so several structures can be
moved independently. Set the target to `Structure` or `Chain`, then press `G` to
move or `T` to rotate (`Shift+G` forces whole-structure). Every tree row has a
✥ handle that grabs that structure or chain directly, and a ⧉ that duplicates
it. Positions and rotations can also be typed in.

A chain takes its ligands and waters with it. `Export` writes a PDB with all
transforms applied, so whatever you assembled on screen is what lands in the
file.

**Assembling.** `Ring of N about <axis>` repeats the active structure evenly
around an axis, keeping the original where it is — place one stator against the
rotor, then array eleven of them. Duplicates share the parsed structure, so
copies cost geometry only.

**Designing.** Pick residues on a target, get a new backbone built against them,
then give that backbone a sequence and check it folds back to the shape. See
[Building new structures](#building-new-structures) below.

**Turning.** For an assembly built of a rotor and an axle, the Motion tab turns
one against the other through a whole revolution and plots what the interface
does. See [Rotational landscapes](#rotational-landscapes) below.

**Measuring.** Press `M` and click two atoms. Distances follow the geometry when
chains are moved.

Press `?` in the app for the full key list.

## Layout

```
proteincad/          Python: server, API, config, job queue, design runners
  emdb.py            fetch and cache EMDB density maps; the bytes pass through
                     untouched, because the browser owns the only CCP4 reader
  landscape.py       rotational landscapes: symmetry axis, scoring backends,
                     what the curve says. Also the subprocess that runs a scan
  scans.py           where scans are kept: hashed so one is never run twice,
                     resumable so a fine one can be stopped
  colab_worker.py    standalone GPU-side worker (Colab, EC2, anywhere). Both
                     backbone engines live here: RFdiffusion's option
                     catalogue and ESM3's track catalogue, each next to the
                     code that turns it into a run
  ec2.py             starting and stopping an on-demand GPU, from the app
  cloud_api.py       the same API as a Lambda, behind Cognito and a queue
  sqs_worker.py      the GPU side of that: drain the queue, fetch the weights
                     it needs, run a container, then turn the machine off
notebooks/           Colab notebook that runs the worker
docs/ai-agent.md     what an agent driving this would need, and which parts
                     of it already exist. Design notes, nothing implemented
deploy/aws/          two ways to deploy, and one kept for its reasoning:
  README.md            a GPU box your own copy starts and stops
  SERVERLESS-DEPLOY.md sign-in, quotas, queue — the one to deploy
  go-live.sh           deploy -> config.json -> push, in that order
  status.sh            what is actually deployed, read-only
  PUBLIC-DEPLOY.md     superseded: an always-on box with no auth
  cdk/                 all of it as code
  worker/              the container the model runs in, and the two
                       CodeBuilds that make it and its weights
web/
  index.html         the shell
  config.example.json where the API is, on a hosted copy. Absent by default
  src/
    core/            structure model, density map model, selection language
    core/landscape.js     the geometric scan, in the browser. Mirrors
                     proteincad/landscape.py; a committed fixture holds them
                     to the same curve
    landscape-worker.js   runs it off the main thread
    io/              PDB and mmCIF readers, PDB writer, streaming CCP4 reader
    io/emdb.js       recognising an EMDB id, and reading EBI's entry document
    design/          job specs: cropping, contigs, volume-to-length
    render/          three.js: viewer, representations, picking, volume
    render/isosurface.js  surface nets, shared by the molecular surface and
                     the density map -- they contour the same kind of field
    ui/              panels, tree, inspector, design, motion
    ui/plot.js       the landscape curve and its scrubber, as inline SVG
    api.js           where the server is; the only place a token is attached
    auth.js          Cognito sign-in, when a deployment asks for one
  vendor/            three.js, checked in — no package manager involved
tools/preview_hosted.py  the hosted deployment, on your laptop: the real
                     Lambda code and the real worker against stand-in AWS,
                     so the sign-in buttons, the GPU panel and the model
                     downloads are all clickable with no AWS account
tools/check.mjs      tests for everything under core/ and io/
tools/push-adumbra.sh  check web/ is still subpath-portable, then vendor it
                     into the Adumbra website
data/samples/        a small structure to start from
```

Two rules keep this navigable:

1. **`web/src/core/` and `web/src/io/` never import three.js.** They are the
   parts a Python backend will mirror, and they run in plain node — which is why
   `node tools/check.mjs` can test the parsers, secondary-structure assignment,
   bond perception, the selection language and the PDB writer without a browser.
   They never import `api.js` either: where a deployment lives is not their
   business.

2. **The UI never touches three.js.** Panels read from `App` and call its
   methods; `App` owns the scene. Adding a panel means adding a file under
   `ui/`, not threading state through the renderer.

## How the rendering works

Atoms live in flat typed arrays (`Float32Array` for coordinates, `Uint8Array`
for elements); residues and chains are plain objects, since there are far fewer
of them and the code that walks them stays readable.

Each structure becomes a small object graph:

```
group           the structure          (movable)
  unit          one chain id and its ligands and waters   (movable)
    chain       one run of residues    (hideable)
      mesh      geometry
```

Hiding a chain is a flag. Moving a subunit writes a transform. Neither rebuilds
geometry. Colouring does not either: every vertex records which atom it came
from, so recolouring or highlighting a selection is one pass over a buffer.

The cartoon is a single sweep routine — a superellipse cross-section whose
width, thickness and squareness vary along the chain, which yields helices,
strands with arrowheads and loops without any per-type geometry code. Surfaces
are a Gaussian density contoured with surface nets. Picking renders one pixel
with each mesh's identity encoded as colour, so it costs the same whatever is on
screen.

Measured on this machine, on the 335,722-atom, 219-chain flagellar motor–hook
complex (7CGO):

| | |
|---|---|
| parse mmCIF | 0.35 s |
| secondary structure (DSSP) | 0.24 s |
| load, build cartoon, first frame | 1.2 s |
| render | 0.6 ms/frame |
| surface, all 219 chains | 1.2 s, 1.8M triangles |
| pick an atom | 2–5 ms |

## Building new structures

The Design tab turns what is on screen into a job for a protein design model.
The viewer's real contribution is the job spec — which residues to build
against, how much target to send, how big the new chain should be. Doing that by
hand means counting residue ranges and trimming PDB files; doing it by pointing
at the screen is the whole reason to have a 3D editor.

1. **Pick a site.** `Pick site`, then click residues on the target. They mark up
   in orange. These become the model's hotspot residues — the patch the new
   structure has to engage.
2. **Draw the shape.** Place a cylinder, box or sphere where the new structure
   should go. It appears just outside the picked patch, pointing away from the
   target, and the handle moves and resizes it. The panel reads out its volume
   and the chain length that implies (~133 Å³ per residue, from protein
   density). `Use as length` feeds that into the run.
3. **Check the spec.** `Preview spec` shows exactly what would be sent: the
   cropped target, the hotspots, the contig string, the length range. The crop
   is the polymer within a chosen radius of the site, collapsed to one span per
   chain — which is what keeps a 45,000-residue assembly down to something a GPU
   can hold.
4. **Run it.** The job is queued server-side and polled, because these take
   minutes to hours. Finished designs load back into the scene already
   positioned against the target, and are ordinary structures from then on —
   movable, colourable, exportable.
5. **Take one further.** Press `sequence` on a finished design. That runs the
   second stage below and comes back with a protein rather than a shape.

### All six RFdiffusion protocols, not just binder design

RFdiffusion is one script with about sixty configuration keys, and every
protocol in its documentation is that same script with different keys set. So
the panel offers the keys rather than implementing five pipelines. `make` picks
the protocol:

| protocol | what it does | needs |
| --- | --- | --- |
| Binder | a new chain against the residues you picked | a site |
| Motif scaffolding | keeps those residues exactly and builds a protein around them | a site |
| Unconditional | samples a protein of a given length from nothing | nothing |
| Symmetric oligomer | a symmetric assembly, `c4`, `d2`, tetrahedral… | nothing |
| Partial diffusion | variations on a structure you already have | a structure |
| Fold conditioned | conditions on secondary structure and adjacency | scaffold files |

Each one changes what the scene has to provide, which contig gets generated, and
which settings are worth showing. It also carries its own recommended settings —
switching to symmetric oligomer brings the oligomer contact potential with it —
and switching away takes them back, unless you changed them yourself.

**Contigs are editable.** The generated string is shown under the box and is
what almost every run uses, but `Edit auto` copies it in so it can be changed,
and `Auto` puts it back. That is the only way to express a motif, an inpainted
span, or a chain break that nothing can infer from a click.

**Advanced** is generated from a catalogue, not written out by hand: every key
in RFdiffusion's `base.yaml` that a person can meaningfully set, grouped, with
what it does on hover. Empty means *unset* — RFdiffusion's own default — which
is why booleans are `— / on / off` rather than a tickbox: a tickbox cannot tell
"I want recentring off" from "I never touched it", and those send different
commands. `Show all` reveals the settings the current protocol does not normally
use. `overrides` passes anything else straight through, so a key this build has
never heard of is still reachable.

The catalogue lives in one place — `RFDIFFUSION_OPTIONS` in `colab_worker.py`,
next to the code that turns it into a command line — and is served to the
browser over `/api/design/options`. The panel and the command line therefore
cannot disagree about a name, and adding a capability is one table entry rather
than three edits in two languages. `tools/check_server.py` checks the table
against `base.yaml` in both directions, so a key RFdiffusion adds or removes
shows up as a failing test rather than as a run that dies after loading a model.

Each finished job carries a `Command` button with the exact `run_inference.py`
line it ran. With this much settable, that is the only way to tell a setting
that was applied from one an endpoint running older code never heard of.

**Weights follow the protocol.** RFdiffusion picks its own checkpoint from the
job — hotspots select the complex model, inpainting selects `InpaintSeq`, fold
conditioning selects the `Fold` models — and picking one that is not on disk is
a crash a minute into the run. `setup` fetches the two a binder run chooses
between; the worker works out what any other job needs and downloads that one
(484 MB) before starting. `setup --weights all` fetches all eight up front.

### Two engines, and `with` picks between them

RFdiffusion is one way to draw a backbone. **ESM3-open** is another, and it
works nothing like it — so it is a second *engine* rather than a seventh
protocol. `with` picks the engine; `make` then offers that engine's protocols.

RFdiffusion denoises coordinates out of noise and returns a shape with every
residue set to glycine. ESM3 is a masked generative model over five *tracks* of
the same protein — sequence, structure, secondary structure, solvent
accessibility, function — any of which may be partly given and partly masked.
It fills in what is masked, one track at a time, conditioning each pass on
everything decoded so far. Which tracks you fill in *is* the protocol:

| protocol | given | produced | needs |
| --- | --- | --- | --- |
| De novo | nothing but a length | sequence and structure | nothing |
| Motif scaffolding | the picked residues, shape and identity | the rest of a protein around them | a site |
| Inverse folding | a backbone | a sequence that should fold to it | a structure |
| Structure prediction | a sequence | the structure it folds to | a sequence |
| Partial resample | a protein, partly re-masked | variations on it | a structure |

Because it writes the sequence and the structure on one pass, an ESM3 design
arrives as a protein rather than a shape waiting for one — and it reports its
own pTM and pLDDT with it. That does not make the second stage redundant: the
folding check is an *independent* model's opinion of the same sequence, and that
independence is the whole point of it. An ESM3 backbone goes through
ProteinMPNN and ESMFold by the same route an RFdiffusion one does, which is what
makes the numbers from the two engines comparable at all.

**The decode plan is the setting with no RFdiffusion equivalent.** There is no
contig here. What there is instead is the order the masked tracks are filled in,
with how many iterative steps and how hot to sample each one, written one pass
per line:

```
sequence:8:0.7
structure:8:0.0
```

Sequence before structure, in every plan that does both — deciding the residues
and then asking what they fold into is a different question from fitting
residues to a shape, and the first is the one whose answer the folding check can
take. Each protocol brings its own plan and the box is editable, exactly as the
contig box is. Steps above the number of masked positions is an error from
inside the sampler rather than a finer decode, so it is clamped, and the run
says that it was.

The other tracks are offered as prompts, which is how to ask for a fold without
drawing one: an SS8 string (`__HHHHHHHH__EEEE__`) constrains the secondary
structure per position, exposure targets (`12-24:5`) bury a stretch or put it on
the surface, and function terms (`30-70:IPR000719`) say what a span is for. A
typed sequence prompt with `_` for the positions to choose sets the length
itself.

**One thing to be straight about.** ESM3-open is trained on single chains, so
there is no target-conditioned binder protocol here — `|` in a sequence prompt
reaches multi-chain generation, and the panel calls it an experiment rather than
a protocol, because that is what it is.

The weights themselves are public: 5.5 GB over 22 files from
[the model's repository](https://huggingface.co/EvolutionaryScale/esm3-sm-open-v1),
downloadable anonymously with no token. A `HF_TOKEN` is honoured if one is set,
because the hub rate-limits anonymous downloads per address and a machine
created for a job is a fresh address each time — but it is an optimisation and
never a requirement. EvolutionaryScale's licence is non-commercial, which is a
thing to read before building a business on this and not a thing the download
enforces.

```bash
python3 proteincad/colab_worker.py setup --only esm3
python3 proteincad/colab_worker.py --generator rfdiffusion,esm3
```

`--only esm3` is deliberately not part of `setup`'s `all`: it is a second engine
rather than a missing piece of the first, and 5.5 GB that a copy using only
RFdiffusion has no use for. One worker serves both engines and both stages;
`--esm3-python` puts ESM3 in an environment of its own, which is what the
container does, because RFdiffusion's torch version is pinned hard by DGL and
ESM3's requirements have no reason to agree with it.

### Two stages, asked for separately

A backbone is not a protein. RFdiffusion returns a fold with every residue set
to glycine, because nothing has chosen the amino acids yet — so it is a picture
of a binder, not one. Turning it into a design anybody could make needs two more
steps, and the app runs them as a second job:

| stage | model | produces | cost |
| --- | --- | --- | --- |
| `kind: binder` | RFdiffusion | a backbone against the picked residues | minutes |
| `kind: fold` | ProteinMPNN, then ESMFold | a sequence for that backbone, and a prediction of what it folds into | seconds, then ~a minute per sequence |

They are separate jobs because the first is worth looking at before paying for
the second, and because the answer the second gives is the one that matters:

- **ProteinMPNN** picks the residues with the *target in place*, so the
  interface is designed for the surface it will actually touch. It takes a list
  of chains, which matters: RFdiffusion writes one output chain per contig block
  and keeps the original chain ids, so a crop that falls across five target
  fragments returns six chains. What separates binder from target is the
  B-factor marker the model writes — 1 for the motif it was given, 0 for
  everything it built — never the chain count.
- **ESMFold** then folds that sequence knowing nothing about the backbone — no
  alignment step, which is not a shortcut but the only honest option, since a
  protein that has never existed has no homologues to align to.
- The prediction is superposed back onto the backbone it came from. **How far it
  lands is the measurement.** Under 2 Å, with a confidence above 80, is the
  usual bar for a design worth making. Well above that means the backbone was a
  shape nobody can build — cheap to learn here, expensive to learn in a lab.

Because the prediction is superposed onto a backbone that is already sitting on
the picked residues, what loads into the scene is the predicted protein on the
user's own site. `Sequences` and `FASTA` give the designs themselves.

The halves fail independently on purpose. Sequence design needs a few megabytes
and seconds; folding wants a real GPU and about 8.5 GB of weights. A worker with
only the first still returns sequences, threaded onto the backbone, and says why
they were not folded.

### What actually generates the structure

`mock` ships enabled and needs no GPU. It builds a real helical bundle by NeRF
from ideal backbone torsions — correct bond lengths, continuous peptide bonds,
recognised as helices by the viewer's own DSSP — and places it where the volume
was drawn. **It is not a design method.** It exists so the workflow, transport,
placement and rendering can be exercised offline. Nothing it produces means
anything biologically.

`mock` answers a `fold` job too: real geometry, invented chemistry. The contacts
it reports are measured off the actual coordinates and are true; the sequence is
not a design and says so.

`remote` appears when a compute endpoint is configured, and forwards jobs to a
GPU running [`proteincad/colab_worker.py`](proteincad/colab_worker.py).
[`notebooks/proteincad_colab.ipynb`](notebooks/proteincad_colab.ipynb) sets that
up on Colab: prove the link with the `echo` generator first, then install the
models and restart the worker. One worker serves both stages. Nothing else
changes.

```bash
python3 -m proteincad --compute-url https://something.trycloudflare.com --compute-token <token>
# or PROTEINCAD_COMPUTE_URL / PROTEINCAD_COMPUTE_TOKEN
```

A Colab quick tunnel gets a new address every session, so **Design → Compute
endpoint** sets the same two values from the browser, without restarting
anything. That is on by default only when the server is bound to loopback; on a
shared address it is a way to make somebody else's server talk to yours, and
`PROTEINCAD_ALLOW_REMOTE_CONFIG=1` is then the deliberate choice.

`ec2` is the same worker on a machine the app turns on and off for itself. A
GPU instance bills by the second whether or not it is computing, so it is kept
**stopped**; the first job that needs one starts it, waits for the worker to
answer, and hands the spec over unchanged. When nothing has needed it for
fifteen minutes it is stopped again.

```bash
pip install 'proteincad[aws]'
python3 -m proteincad --ec2-instance i-0abc123 --ec2-region us-east-1
# PROTEINCAD_EC2_TOKEN must match the worker's PROTEINCAD_WORKER_TOKEN
```

Two watchdogs, because the failure that costs money is the app not being there
to turn the GPU off: one here, and one *on the box* — a systemd timer that
powers the machine off when the worker has been idle, which still works when
this process has crashed or the laptop has closed. A **GPU** section in the
Design panel shows the state, counts down to the stop, and has a Start button
for warming it up while a design is still being set up.

Everything needed on the AWS side — the AMI, the instance type, the security
group, the IAM policy, the systemd units and the install script — is in
[`deploy/aws/`](deploy/aws/README.md). Colab is unaffected: both can be
configured at once, and the `on` menu beside Run picks between them.

For a copy **other people** use, that shape is wrong: it has no notion of who
is asking, and the process answering the browser is the process holding the
GPU. [`deploy/aws/SERVERLESS-DEPLOY.md`](deploy/aws/SERVERLESS-DEPLOY.md) is
the other shape — Cognito in front, a queue in the middle, the model in a
container with no network and no credentials, and results through links that
expire.

There the GPU goes further than stopped: it **does not exist**. A launch
template rather than an instance, weights in S3 rather than on a disk, and a
machine that terminates itself after thirty idle minutes — about a dollar a
month when nobody is designing, against seventeen for a stopped one. Since a
start is then a real wait, it is a thing the user drives and watches: a Start
button, a Download button per model with the megabytes counting up, and a Run
button that stays greyed out until everything a protocol needs is there.

The viewer is the same folder either way: with no `config.json` it calls
relative URLs and expects its own Python server, which is what makes a laptop,
a Colab session and a website the same deployment.

RFdiffusion is the right fit for "build onto this patch" — binder design takes
hotspot residues and a contig with a length range, which is precisely what the
panel produces. Two things to hold onto:

- **Shape control is indirect** — length, hotspots, symmetry. If literal shape
  matters, Chroma's shape conditioner takes a point cloud, and the volume
  already exports one (`spec.volume.points`); that would be a second generator
  in the worker rather than a change here.
- **The folding check is a monomer prediction, not a complex one.** It answers
  "does this sequence fold to the shape that was drawn", which is the standard
  self-consistency filter and the one that kills most bad designs. It does not
  produce an interface pAE, which is what best predicts whether a binder works
  in a tube. That wants AF2 with an initial guess over the complex;
  `--folder` in the worker is where a second predictor slots in, and nothing
  above it changes.

### Adding a model

Write a generator in `colab_worker.py`:

```python
class MyGenerator(Generator):
    name = "mine"

    def generate(self, spec, job):
        for pdb in my_model(spec["target"]["pdb"], spec["target"]["hotspots"]):
            job["designs"].append({"name": "...", "pdb": pdb, "metrics": {...}})
            job["progress"] = len(job["designs"])
```

Nothing in the viewer, the API or the job queue changes. If the model runs on
the same machine as the app, subclass `Runner` in `proteincad/design.py`
instead and register it in `build_runners`.

## Density maps

Type `EMD-25575` where you would type a PDB id. The viewer fetches the map from
EMDB, contours it at the level the depositors recommend, and puts it in the tree
beside the structures — with a level slider, a colour, a style, and the same
move handle a structure has, so a model can be fitted against it by eye.

```
?load=EMD-25575     the D8-C4 designed rotor      (Courbet et al. 2022)
?load=EMD-25576     the D3-C5 designed rotor
```

The level is the control that matters: a map shown at the wrong one is a solid
block or nothing at all. It opens at EMDB's own recommended level, is labelled
in σ as well as in map units, and `EMDB level` puts it back. Where an entry has
a model deposited against it, a button loads that too.

**Maps are read as a stream, not as a file.** EMD-25576 is 400³ float32 — 244 MB
once decompressed, from a 5 MB download — and holding all of that to then throw
98% of it away is how a viewer runs a laptop out of memory on a file that
arrived in two seconds. So the bytes are reduced as they decompress: each source
voxel is added into its block's running total on the way past, and the only
array that ever exists is the one that gets rendered. Reducing by block average
rather than by taking every k-th voxel is what makes that possible with no
buffering at all, and it is also the better answer — a cryo-EM map is noisy, and
subsampling aliases that noise straight into the surface.

Three things in the CCP4 format exist to catch you out, and
`tools/check.mjs` has a case for each:

- **The data is not necessarily in x, y, z order.** MAPC/MAPR/MAPS name which
  crystal axis the fastest, middle and slowest stored axis actually is, and
  EMD-25575 is stored (3, 2, 1). Read as though it were (1, 2, 3) it comes out
  transposed about its diagonal — which, on a symmetric particle, looks almost
  right. All six orders are tested, each with a value per voxel that no
  transpose could satisfy by accident.
- **Byte order is whatever wrote the file**, declared two thirds of the way down
  the header, and some writers leave the field blank.
- **There are two places the origin can live**, and which one is authoritative
  depends on the program. A map placed at the wrong corner lines up with nothing.

Contouring is surface nets, in [`web/src/render/isosurface.js`](web/src/render/isosurface.js)
— the same code the molecular surface uses, because a Gaussian envelope over a
set of atoms and a cryo-EM map are the same kind of field. Only what fills the
field and what each vertex is attributed to differ.

The map is fetched from this server when there is one, because it caches and the
second visit then costs nothing. With no server — a static copy of `web/`, which
is what the website is — the browser goes to the EBI directly: EMDB serves both
the maps and the metadata with `Access-Control-Allow-Origin: *`, so a folder of
files on a CDN can read a map with nothing behind it.

| route | |
|---|---|
| `GET /api/map/{id}` | the map, still gzipped, cached under `data/cache/` |
| `GET /api/map/{id}/meta` | title, recommended contour level, fitted models |

## Rotational landscapes

A two-component assembly that shares a symmetry axis — a Cn ring threaded on a
Dn or Cn axle, the shape Courbet *et al.* designed in [Science
2022](https://www.science.org/doi/10.1126/science.abm1183) — has one degree of
freedom worth asking about: the angle. The **Motion** tab turns the rotor about
the shared axis in fixed steps and measures the interface at each one. What
comes out is a curve, drawn in a strip under the viewport.

The curve is the point. A flat one means the rotor diffuses freely. Wells mean
it has preferred orientations and has to climb out of them. Wells whose two
sides differ in height mean it is easier to leave one way than the other, which
is what a ratchet is.

1. **Say which part is which.** `Guess from molecules` takes the two largest
   molecules in the active structure and makes the one with more copies the
   rotor — usually right for a deposited assembly, and one button instead of
   forty alt-clicks. Otherwise select chains and press `From selection`.
2. **Scan.** 5° is the default (72 points); two components of a few thousand
   atoms take well under a minute. The curve draws itself as the angles arrive.
3. **Drag it.** The plot cursor and the assembly are the same control: dragging
   through the landscape turns the rotor in the viewport, `⟨⟨` and `⟩⟩` jump
   between deep wells, and `Reset` goes back to the orientation as loaded.
   Scrubbing is non-destructive — it is always the same rotation applied to the
   pose the scan was computed from, so returning to zero returns the exact
   coordinates.

### The axis is measured, not assumed

Symmetric assemblies are usually deposited with their axis on z, but a scan
about the wrong axis is a curve that means nothing while looking perfectly
reasonable. So it is recovered from each component's own symmetry: superpose a
chain onto its symmetry mates, and the rotation that does it has the axis in it.

Two components give two independent estimates, and **axis agree** reports the
angle between them — a large one means the parts do not share an axis and the
scan is not measuring what it claims to. A Dn axle is the case that catches a
naive implementation: half its chain pairs are related by perpendicular
two-folds rather than by the main rotation, and averaging every pair's axis
together points somewhere between them. Only the largest cluster of agreeing
axes is used.

### Period is forced by symmetry; depth is not

A Cn rotor turned by 360/n *is* the same rotor, so no energy function can tell
the two orientations apart. The axle's own symmetry says the same from the other
side, so the landscape has period 360/lcm(n, m) degrees whatever scores it —
45° for a C4 rotor on a D8 axle, which is the spacing Courbet *et al.* report
for their D8-C4 system.

That makes the period a **prediction** rather than a reading, which is what
`tools/check_server.py` tests this against: assemblies whose folds are known in
advance, whose curves must come back repeating at the right spacing. With exact
coordinates two symmetry-equivalent orientations score the *same number*, not
nearly the same one — the sample sphere used for buried area turns with the
rotor, because a fixed one gives slightly different answers for the same
configuration rotated and that noise invents minima.

Well depths carry no such guarantee. They are whatever the scorer says.

Three things the readout is careful about, because each is a way to read a
theorem as a result:

- **The period is measured by shifting, not read off the transform.** It is the
  smallest turn that leaves the landscape looking the same. Reading it off the
  strongest frequency is wrong twice over: a well with two dips in it puts more
  power on the second harmonic and would report half the period, and a sharp
  curve's harmonics *alias back down* past the Nyquist limit — a 45° period
  sampled every 10° puts its 32nd order onto the 4th, which looks exactly like a
  90° period and is not one.
- **A scan that could not see the period has not failed the check, it has not
  run it.** Two ways it cannot: the period has to land on the sample grid (45°
  is not a whole number of 10° steps, so no shift ever compares like with like),
  and eight wells cannot be found in twelve samples. The panel says "too coarse
  to check" and names a step that would work, rather than showing a ✕.
- **Asymmetry is unavailable, not zero, when there is one minimum per period.**
  The barrier leaving a well forwards and the barrier leaving its neighbour
  backwards are the same peak, so with a single well per period the two
  directions are equal by construction. A landscape needs structure *within* a
  period for the question to have an answer — the three main plus nine lesser
  minima reported for the C3-C3 system is what that looks like.

### What does the scoring, and where

`geometric` ships enabled and needs nothing installed. It measures buried
surface area (Shrake–Rupley, heavy atoms, 1.4 Å probe), atomic overlap and the
median interfacial gap, and combines them into a packing score in **arbitrary
units** — lower is better packed. It is a shape measure that finds the
orientations that pack well, which is most of what sets the shape of a landscape
for the all-α interfaces these assemblies use. It is not a force field, and a
number from it is not a binding energy.

**It runs in the browser.** It needs nothing but arithmetic and the coordinates
are already in memory, so a scan happens in a worker on the page: no upload, no
account, no server. A 72-angle scan of a 9,000-atom assembly is about half a
second, which is faster than the server computes it — 33× faster than the
Python, before the upload and the poll loop. That is what makes the tool work on
a static deployment, where there is no Python at all.

`rosetta` is **not implemented**. It is the shape a real energy function plugs
into — `available()`, `prepare()`, `score()` — and there is nothing behind it,
so the panel lists it as unavailable and says why. It is kept listed because an
interface with one implementation is one nobody has checked is general enough,
and because a menu showing "interface ddG — unavailable" is a truer account of
what this build can do than one that quietly offers only shape.

So what this tool measures today is **packing, not energy**. It finds the
orientations that fit and the barriers between them, which is most of what sets
the shape of a landscape for these interfaces; it cannot tell you a well is four
kcal/mol deep. [`docs/rosetta-backend.md`](docs/rosetta-backend.md) is what
implementing a real one involves, including the repacking decision that is the
difference between a scan that takes a minute and one that takes a day.

So the scan exists twice, in `web/src/core/landscape.js` and in
`proteincad/landscape.py`. Two implementations of one piece of arithmetic drift,
so `tools/fixtures/landscape-d8c4.pdb` is scanned by both suites and checked
against one committed curve in `landscape-d8c4.json`. If either side moves, a
test fails. Rebuild it with `python3 tools/fixtures/make-landscape-fixture.py`.

One validation needs no reference curve at all. **As loaded** reports where the
orientation you opened sits in its own landscape — for an experimental structure
the deposited pose should be at or near the bottom, and a poor rank is a result
about the scorer rather than about the assembly. The two sodium-driven stator
complexes in `data/cache/` (8ZZ0, 9IJM: PomA₅ on PomB₂) both come back rank 1 of
72, and both correctly report that only 4% of the turn is clash-free — they are
interdigitated, not free to rotate.

### How it runs

A scan is minutes of CPU, so it does not run in the server process: that would
hold a request open and pin a whole assembly in the memory of the thing serving
the viewer. It runs as `python3 -m proteincad.landscape <folder>` instead,
appending one line per angle to `points.ndjson`.

Two things follow from a scan being deterministic. It is **named after its own
contents** — a hash over the coordinates and the settings — so an identical
request is a directory listing rather than a job, and pressing Scan twice, or
after a reload, is free. And it is **resumable**: the lines already in the file
are the angles already done, so a cancel, a crash or a restart costs only the
angle in flight. The same lines are what the panel draws while the scan is
still running, which is why a partial curve appears rather than a spinner.

| route | |
|---|---|
| `POST /api/landscape` | scan an assembly; answers from cache when it can |
| `GET /api/landscape/{id}` | progress, the curve (`?points=1`), the descriptors |
| `POST /api/landscape/{id}/cancel` | stop one; what it computed is kept |
| `GET /api/landscape/options` | which scoring backends this build has |
| `GET /api/landscapes` | every scan on disk |

**`rise ±`** adds translation along the axis and makes the output a 2D grid: an
assembly can be free to turn only if it is also at the right height, and a scan
that holds the height fixed misses that. Each step multiplies the work by one
more whole turn, so it starts at zero. The plot shows the best height at each
angle, and each minimum reports the height it was found at.

## The Python side

The server is standard library only and is meant to be built on:

| route | |
|---|---|
| `GET /api/health` | status, what is cached, which runners exist |
| `GET /api/structure/{id}` | fetch a structure (samples → cache → RCSB) |
| `GET /api/structure/{id}/summary` | parse server-side, report contents |
| `POST /api/analyze` | measure posted atoms |
| `POST /api/session` | accept the viewer's scene description |
| `POST /api/design` | queue a design job |
| `GET /api/jobs`, `GET /api/jobs/{id}` | job state and progress |
| `GET /api/jobs/{id}/designs/{n}` | one result, as a PDB |
| `POST /api/jobs/{id}/cancel` | stop one |
| `GET /api/design/options` | every protocol and setting this build offers |
| `GET /api/gpu` | the on-demand instance: state, endpoint, when it stops |
| `POST /api/gpu/start`, `/stop` | warm it up, or stop paying for it now |

Adding an endpoint is one function:

```python
# proteincad/api.py
@route("POST", "/api/fold")
def fold(request):
    sequence = request.json["sequence"]
    return {"pdb": my_model.predict(sequence)}
```

and one call from the browser:

```js
const result = await fetch('api/fold', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ sequence }),
}).then((r) => r.json());
await app.loadText(result.pdb, 'designed.pdb');
```

`app.session()` serialises what the user is looking at — structures, chains,
sequences, the current selection, transforms and the camera — which is the
context a design model usually needs. `proteincad/structure.py` reads PDB and
mmCIF server-side without dependencies; swap it for gemmi or biotite when the
real work starts. `proteincad/analysis.py` holds the example computation
(centre of mass, radius of gyration, inter-chain contacts) and marks where a
model call goes.

Add scientific dependencies under `[project.optional-dependencies]` in
`pyproject.toml` rather than to the server itself, so `python3 -m proteincad`
keeps working on a bare machine.

### Configuration

Nothing that varies between a laptop, a Colab session and a web server is
written into the source. Everything below has a default, can be set by
environment variable, and (for the common ones) by a command line flag:

| | |
|---|---|
| `PROTEINCAD_HOST` / `PROTEINCAD_PORT` | where to listen (`127.0.0.1:8080`) |
| `PROTEINCAD_DATA_DIR` | structures, cache and job output |
| `PROTEINCAD_COMPUTE_URL` / `_TOKEN` | the GPU endpoint, if any |
| `PROTEINCAD_MAX_DESIGNS` | cap on designs per job (32) |
| `PROTEINCAD_ALLOW_REMOTE_CONFIG` | let the browser set the endpoint (off) |
| `PROTEINCAD_EC2_INSTANCE` / `_REGION` | a GPU instance to start on demand |
| `PROTEINCAD_EC2_TOKEN` / `_PORT` | how to reach the worker on it (`8000`) |
| `PROTEINCAD_EC2_IDLE` | minutes idle before it is stopped (15) |

The full EC2 list, including addressing and boot timeouts, is in
[`deploy/aws/README.md`](deploy/aws/README.md) and `proteincad/config.py`.

`proteincad/config.py` is the only file that knows these exist. The front end
only ever calls relative URLs (`api/…`), so it does not care what host it is
served from.

One exception, and it is the one that bites: `web/config.json` belongs to a
*deployment*, and a checkout that has deployed one is still holding it. Serve
that folder from `python3 -m proteincad` and the viewer calls the API Gateway
named in it rather than the server in front of you — so every route this server
has that the deployment has not reads as a missing feature. The server says so
at startup and hands you a URL with **`?local=1`** on it, which ignores the file
for that tab. `tools/preview_hosted.py` is the case where you do want it honoured.

Two things to set before putting this on a public site: bind to the interface
you mean with `PROTEINCAD_HOST`, and leave `ALLOW_REMOTE_CONFIG` off so a
visitor cannot point your server at an endpoint of their choosing.

That said — `server.py` has no authentication, by design. It is a single-user
server. Do not put it on the open internet and expect the two settings above to
make it safe; use
[`deploy/aws/SERVERLESS-DEPLOY.md`](deploy/aws/SERVERLESS-DEPLOY.md), which puts
a real one in front.

## Tests

```
python3 tools/preview_hosted.py # not a test: the hosted app on localhost, with
                                #   stand-in AWS. Ctrl-C tidies up after itself
node tools/check.mjs            # 168 checks: core model, parsers, selection,
                                #   specs, CCP4 maps, EMDB ids, and the
                                #   in-browser landscape against the same
                                #   fixture curve the Python is held to
python3 tools/check_server.py   # 547 checks: reader, analysis, API, design jobs,
                                #   the option catalogue, the EC2 lifecycle, the
                                #   EMDB cache, and rotational landscapes against
                                #   assemblies whose symmetry fixes the answer
                                #   in advance
python3 tools/check_cloud.py    # 309 checks: the Lambda API, quotas, ownership,
                                #   the launch lock, zone fallback, on-demand
                                #   weights and the queue worker — against
                                #   stand-in AWS
python3 tools/check_stack.py    # 125 checks: the rendered CloudFormation, once
                                #   `cdk synth` has made one. Skips if it has not
```

No AWS account and no boto3 is needed for any of them. The stand-ins live in
`tools/fake_aws.py`, small enough to read, which is the point: what the code is
asserted to do to DynamoDB, S3, SQS and EC2 is written down in a form you can
hold against an IAM policy.

The first covers real structures: crambin (disulfides, secondary structure both
from records and computed), haemoglobin (four chains, haem ligands, distance
selections, COMPND components and the sequence-identity fallback with those
records stripped), a B-DNA dodecamer (nucleic classification), ubiquitin as
mmCIF, and the 335k-atom motor (mmCIF entities, timing budgets). Files under
`data/cache/` are optional — checks that need them are skipped with a note.

```bash
curl -o data/cache/4hhb.pdb https://files.rcsb.org/download/4HHB.pdb
curl -o data/cache/1bna.pdb https://files.rcsb.org/download/1BNA.pdb
curl -o data/cache/1ubq.cif https://files.rcsb.org/download/1UBQ.cif
curl -o data/cache/7cgo.cif https://files.rcsb.org/download/7CGO.cif
```

## Publishing it

`web/` is the deliverable. There is no build step and no export folder: the app
is plain ES modules with a checked-in three.js and an import map, every path in
it is relative, and what `python3 -m proteincad` serves is byte-for-byte what a
static host serves. Copy the folder anywhere that can serve files.

It is vendored into the Adumbra website at `public/proteincad/`, reachable at
`/proteincad/` — which is why the relative paths matter: an absolute `/src/…`
would resolve against the site root and 404 on every asset.

```bash
tools/push-adumbra.sh            # check, then transfer
tools/push-adumbra.sh --check    # check only, change nothing
```

The checks are a contract test. The site adds a mobile layout and a link home
in files of its own (`adumbra.css`, `adumbra.js`), re-applied after every
transfer and coupled to a handful of names here: the classes `.workspace`,
`.topbar`, `.panel.left`, `.panel.right`, `.panel-head`, `.group.right`,
`.no-left`, `.no-right`, `.btn`, `.dialog`, `.shortcuts`; the ids
`#collapse-left` and `#collapse-right`; and the custom properties `--line`,
`--text`, `--muted`, `--radius`, `--accent-dim`. Renaming one of those breaks
the site's layout silently, so the script refuses to transfer without them.

Without the Python server, fetching by PDB id falls through to the RCSB
directly and an EMDB id falls through to the EBI, so loading structures and
maps, rendering, selection, measurement and export all work from a static host —
and so does the Motion tab, because the geometric scan runs in the browser. Only
the Design tab needs the server, and says so.

## Known limits

- The first model of an NMR ensemble is shown; alternate locations other than
  the first are dropped.
- A density map over about eight million voxels is block-averaged down to fit,
  so EMD-25576 is shown at 2.1 Å per voxel rather than its deposited 1.05. The
  tree says so. Nothing fits a model into a map here: a map can be moved by
  hand against one, but there is no refinement and no correlation score.
- mmCIF assembly operators (`pdbx_struct_assembly`) are not applied — what you
  see is the asymmetric unit as deposited.
- Export writes PDB only, so structures above 99,999 atoms or with chain ids
  longer than one character are renumbered and remapped on the way out.
- Surfaces are Gaussian, not solvent-excluded, and coarsen automatically on
  large structures.
- The folding check predicts the binder on its own and superposes it onto the
  backbone. It reports self-consistency, not an interface score, so it tells you
  whether the sequence folds — not whether it binds.
- ESM3-open has no target-conditioned binder protocol, because the open model is
  trained on single chains. It scaffolds a motif, folds, inverse folds and
  resamples; it does not design against a surface the way RFdiffusion's binder
  protocol does. A multi-chain prompt is reachable and is not a protocol.
- An ESM3 design is placed in the scene by superposing the motif it was given
  back onto the copy you are looking at, so a protocol with no motif — de novo,
  or structure prediction from a typed sequence — comes back in the model's own
  frame. The design says which of the two happened, and reports how far the
  motif landed from where it was asked to be.
- Only the open ESM3 weights run here. The larger models are served over
  EvolutionaryScale's own API, which is an account and a per-token bill rather
  than a GPU you already have, and nothing in this build calls out to one. The
  `esm` package imports its API client regardless, which is why `httpx` is
  installed beside it — the package does not declare it and cannot be imported
  without it.
- Fold conditioning is offered in full but not made easy: it needs `_ss.pt` and
  `_adj.pt` scaffold files generated in advance on the GPU machine by
  RFdiffusion's own `helper_scripts/make_secstruc_adj.py`. The panel takes the
  paths; it does not make the files.
- A design from a protocol with no target comes back in the model's own frame
  rather than placed in the scene — there is nothing to place it against.
- The first design of a session on EC2 waits a minute or two for the instance to
  boot. Pressing **Start** in the GPU section while the design is still being
  set up hides that; nothing else can.
- The EC2 worker is reached over plain HTTP with a bearer token, so its port
  must be restricted to the app's address in the security group. Put the app in
  the same VPC and use `PROTEINCAD_EC2_ADDRESS=private` if that matters.
- One instance, one job at a time. The queue already serialises, and the worker
  holds a GPU lock, but there is no pool and no second box.
