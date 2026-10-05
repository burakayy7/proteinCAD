# A real energy backend, and what it would take

`RosettaScorer` in `proteincad/landscape.py` is an interface with nothing behind
it. This is what writing the implementation involves, and the decisions it
forces — written down because the hard parts are not the PyRosetta calls, and
somebody picking this up should know that before starting.

## What is there now, honestly

`GeometricScorer` is complete and works. It measures three things off the
coordinates — buried surface area, atomic overlap and the median interfacial gap
— and combines them into a packing score in arbitrary units. It is the thing
that runs when you press Scan, in the browser, and it is real.

What it is not is an energy. It has no electrostatics, no hydrogen bonding, no
desolvation and no torsional terms, and its weights were chosen to make burial
drive the curve rather than fitted against anything. For a rotor on an axle,
where the interface is mostly shape, it finds the orientations that pack and the
barriers between them — which is most of what sets the *shape* of a landscape.
It cannot tell you a well is 4 kcal/mol deep, and nothing in the app claims it
can.

So the gap is specifically: **depths in physical units, comparable between
designs and comparable with a published number.**

## The parts that are easy

Scoring a complex with PyRosetta is a handful of lines:

```python
import pyrosetta
pyrosetta.init("-mute all -ignore_unrecognized_res")
pose = pyrosetta.pose_from_pdb_string(complex_pdb)
scorefxn = pyrosetta.get_fa_scorefxn()      # ref2015
bound = scorefxn(pose)
```

And an interface ddG is the usual bound-minus-unbound:

```
ddG = score(complex) - score(rotor alone) - score(axle alone)
```

`InterfaceAnalyzerMover` will do that, and will also hand back shape
complementarity and buried SASA, which would replace this file's own
approximations for those.

## The parts that are not

**The unbound state has to mean something.** Taking the two components apart and
rescoring without letting anything relax gives you the energy of two surfaces
frozen in the conformation they held each other in. The convention is to repack
side chains in the separated state. That is a choice, it changes every number,
and it has to be the same choice at every angle or the curve is comparing
different things.

**Repacking is what decides whether this is usable.** Without it, `fa_rep`
dominates at every angle that clashes, and the landscape is the same steric wall
the geometric score already reports — a slower way to learn the same thing. With
it, side chains move out of the way and you get a landscape that means what the
word suggests. But a repack of a 9,000-atom interface is seconds to minutes, and
a 72-angle scan needs two of them per angle. The arithmetic:

| | per angle | 72 angles |
|---|---|---|
| score only, no repack | ~0.5 s | ~1 min |
| repack the interface only | ~10–60 s | 12 min – 1.2 h |
| full repack both states | minutes | hours to a day |

That is the real reason this backend is server-side, and the reason it needs the
existing job queue rather than a request: at the useful end it is an overnight
job, not an interactive one.

**Rigid-body moves, not coordinate rewriting.** Rebuilding a pose from
coordinates at every angle throws away Rosetta's neighbour bookkeeping and is
needlessly slow. The right shape is one pose with a `FoldTree` carrying a single
jump between rotor and axle, and `pose.set_jump()` per angle — which is also
exactly the degree of freedom this whole feature is about, so the mapping is
clean.

**It will not reproduce a published landscape exactly**, and expecting it to is
the trap. Courbet et al. used their own protocol, their own Rosetta version and
their own relaxation; ref2015 with a different repack scheme gives a different
curve with the same shape. The thing to check against is the symmetry-forced
period — which `tools/check_server.py` already does and which no energy function
can get wrong — and the rank of the deposited orientation, not the depths.

## Where it plugs in

`Scorer` in `proteincad/landscape.py` is three methods: `available()`,
`prepare(rotor, axle, axis)` and `score(rotor_coords, axle, sphere)`. The scan
loop, the descriptors, the job queue, the cache and the panel all go through
that interface and none of them know which backend ran — the plot labels its own
y-axis from `label` and `unit`. Adding one is a class and an entry in
`BACKENDS`.

The one thing to get right is that `available()` must answer for the
*implementation*, not for whether a library imports. It used to do the latter,
which meant installing PyRosetta made this backend report itself ready and then
fail with an empty message.

## If you want physical numbers without the licence

A middle option worth considering before reaching for Rosetta: a simplified
force field in the same geometric scorer — Lennard-Jones, a distance-dependent
Coulomb term, a hydrogen-bond geometry term and a desolvation term proportional
to buried non-polar area. That is what most docking scoring functions are, it
needs atom typing and partial charges but no licence, it is testable, and it
would run in the browser like the current one does. It would give numbers in
kcal/mol-like units that are comparable between designs — which may be all the
"energy" this tool actually needs — without claiming to be Rosetta.
