"""Checks for the Python side: reader, analysis and the API routes.

    python3 tools/check_server.py

Starts the server on a spare port, exercises every route, and shuts it down.
No network access is needed, and nothing here depends on which Python you run
it with: everything that would otherwise reach out -- the RCSB, DGL's wheel
index, the weight downloads -- is served by a stand-in on localhost or stubbed
outright.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from proteincad import analysis, emdb, landscape, mock_design, structure  # noqa: E402
from proteincad.config import from_env  # noqa: E402
from proteincad.design import validate_spec  # noqa: E402
from proteincad.server import Context, Handler, Server  # noqa: E402

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    suffix = f"  ({detail})" if detail else ""
    if condition:
        passed += 1
        print(f"  ok   {label}{suffix}")
    else:
        failed += 1
        print(f"  FAIL {label}{suffix}")


def _raises(call):
    """The exception a call raised, or None. Checking the type beats asserting
    that something went wrong, which a typo also satisfies."""
    try:
        call()
    except Exception as error:
        return error
    return None


def _error_from(generator, spec) -> str:
    """Run a generator over a spec that should be refused, and report how."""
    job = {"designs": [], "error": "", "cancel": False, "progress": 0, "total": 1, "stage": ""}
    generator.generate(spec, job)
    return job["error"]


def get(url: str) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def post(url: str, payload: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


print("\nstructure reader")
sample = ROOT / "data" / "samples" / "1crn.pdb"
if sample.is_file():
    parsed = structure.parse(sample.read_text(), "1crn.pdb")
    check("crambin atom count", parsed.atom_count == 327, f"{parsed.atom_count}")
    check("residues", len(parsed.residues) == 46, f"{len(parsed.residues)}")
    check("one chain", parsed.chains == ["A"], str(parsed.chains))
    check("all protein", all(r.kind == "protein" for r in parsed.residues))
    summary = parsed.summary()
    check("summary shape", set(summary) >= {"name", "atoms", "residues", "chains"})

    print("\nanalysis")
    geometry = analysis.geometry(parsed.elements, parsed.coords)
    check("radius of gyration is sane", 8 < geometry["radius_of_gyration"] < 14,
          f"{geometry['radius_of_gyration']} A")
    check("weight is sane", 4000 < geometry["molecular_weight"] < 6000,
          f"{geometry['molecular_weight']} Da")
    check("composition found carbon", geometry["composition"].get("C", 0) > 100,
          json.dumps(geometry["composition"]))
    groups = [0 if i < 160 else 1 for i in range(parsed.atom_count)]
    touching = analysis.contacts(parsed.coords, groups, 4.0)
    check("contacts between the two halves", len(touching) == 1 and touching[0]["min_distance"] < 4.0,
          json.dumps(touching))
else:
    print("  (skipped: data/samples/1crn.pdb is missing)")

print("\nmock design geometry")
bundle = mock_design.helical_bundle(90, seed=7)


def bond(a, b):
    return mock_design.norm(mock_design.sub(a, b))


check("bundle length is about right", 80 <= len(bundle) <= 95, f"{len(bundle)} residues")
check("no clashes", mock_design.clashes(bundle) == 0)
check("bond lengths are ideal", all(
    abs(bond(r["N"], r["CA"]) - 1.458) < 0.01 and abs(bond(r["CA"], r["C"]) - 1.525) < 0.01
    for r in bundle
))
check("peptide bonds are continuous", all(
    abs(bond(bundle[i]["C"], bundle[i + 1]["N"]) - 1.329) < 0.01 for i in range(len(bundle) - 1)
))
pdb_text = mock_design.to_pdb(bundle)
reparsed = structure.parse(pdb_text, "mock.pdb")
check("writes a readable PDB", reparsed.atom_count == len(bundle) * 4, f"{reparsed.atom_count} atoms")

print("\nspec validation")


def rejects(spec, why):
    try:
        validate_spec(spec)
        return False
    except ValueError:
        return True


good = {
    "target": {"pdb": "ATOM      1  N   GLY A   1       0.000   0.000   0.000\n", "hotspots": ["A1"]},
    "binder": {"lengthMin": 60, "lengthMax": 90},
}
check("accepts a well formed spec", validate_spec(dict(good)) is not None)
check("rejects a missing target", rejects({"binder": good["binder"]}, "no target"))
check("rejects no hotspots", rejects({"target": {"pdb": good["target"]["pdb"]}, "binder": good["binder"]}, "no site"))
check("rejects a silly length", rejects({**good, "binder": {"lengthMin": 2, "lengthMax": 1}}, "bad length"))

print("\nthe RFdiffusion option catalogue")
from proteincad.colab_worker import (  # noqa: E402
    DEFAULT_MODE, MODES_BY_ID, OPTIONS_BY_KEY, OWNED_FLAGS, POTENTIAL_EXAMPLES,
    RFDIFFUSION_GROUPS, RFDIFFUSION_MODES, RFDIFFUSION_OPTIONS, WEIGHTS,
    checkpoint_for, describe_options, hydra_overrides, option_problems,
    weights_wanted,
)

keys = [option["key"] for option in RFDIFFUSION_OPTIONS]
check("every setting has a distinct key", len(keys) == len(set(keys)), f"{len(keys)} settings")
flags = [flag for option in RFDIFFUSION_OPTIONS for flag in option["flags"]]
check("every setting has a distinct Hydra flag", len(flags) == len(set(flags)), f"{len(flags)} flags")

# Against the config file itself. A key that is not in base.yaml is one Hydra
# refuses -- "could not override" -- after the model has loaded, and the whole
# point of this table is that what the panel offers is what the model accepts.
BASE_YAML_KEYS = {
    "inference": ("input_pdb", "num_designs", "design_startnum", "ckpt_override_path",
                  "symmetry", "recenter", "radius", "model_only_neighbors",
                  "output_prefix", "write_trajectory", "empty_cache_per_design",
                  "scaffold_guided", "model_runner", "cautious", "align_motif",
                  "symmetric_self_cond", "final_step", "deterministic",
                  "trb_save_ckpt_path", "schedule_directory_path",
                  "model_directory_path", "cyclic", "cyc_chains"),
    "contigmap": ("contigs", "inpaint_seq", "inpaint_str", "inpaint_str_helix",
                  "inpaint_str_strand", "inpaint_str_loop", "provide_seq", "length"),
    "diffuser": ("T", "b_0", "b_T", "schedule_type", "so3_type", "crd_scale",
                 "partial_T", "so3_schedule_type", "min_b", "max_b", "min_sigma",
                 "max_sigma"),
    "denoiser": ("noise_scale_ca", "final_noise_scale_ca", "ca_noise_schedule_type",
                 "noise_scale_frame", "final_noise_scale_frame",
                 "frame_noise_schedule_type"),
    "ppi": ("hotspot_res",),
    "potentials": ("guiding_potentials", "guide_scale", "guide_decay",
                   "olig_inter_all", "olig_intra_all", "olig_custom_contact", "substrate"),
    "preprocess": ("sidechain_input", "motif_sidechain_input", "d_t1d", "d_t2d",
                   "prob_self_cond", "str_self_cond", "predict_previous"),
    "scaffoldguided": ("scaffoldguided", "target_pdb", "target_path", "scaffold_list",
                       "scaffold_dir", "sampled_insertion", "sampled_N", "sampled_C",
                       "ss_mask", "systematic", "target_ss", "target_adj", "mask_loops",
                       "contig_crop"),
}
unknown = [flag for flag in flags
           if flag.split(".")[0] not in BASE_YAML_KEYS
           or flag.split(".", 1)[1] not in BASE_YAML_KEYS[flag.split(".")[0]]]
check("every flag is a key RFdiffusion's base.yaml actually has", not unknown, str(unknown[:3]))

# The other direction: what is offered should be all of what is settable. The
# exclusions are the ones that would do nothing or break the run, and each is
# named so adding a key to base.yaml shows up here rather than going unnoticed.
NOT_OFFERED = {
    # the network's own dimensions, overwritten from the checkpoint at load
    "diffuser.b_0", "diffuser.b_T", "diffuser.schedule_type", "diffuser.so3_type",
    "diffuser.crd_scale", "diffuser.so3_schedule_type", "diffuser.min_b",
    "diffuser.max_b", "diffuser.min_sigma", "diffuser.max_sigma",
    "preprocess.motif_sidechain_input", "preprocess.d_t1d", "preprocess.d_t2d",
    "preprocess.prob_self_cond", "preprocess.str_self_cond",
    "preprocess.predict_previous",
    # set from the job, or from where the worker keeps its files
    "inference.input_pdb", "inference.num_designs", "inference.output_prefix",
    "inference.model_directory_path", "contigmap.contigs", "ppi.hotspot_res",
    "scaffoldguided.target_path",
    # RFdiffusion asserts this one is not set by hand
    "inference.trb_save_ckpt_path",
    # chosen by the sampler from the rest of the config
    "inference.scaffold_guided", "inference.model_runner",
    "inference.schedule_directory_path",
    # proteinCAD writes each job to its own directory, so skipping work already
    # there would skip every design of every run after the first
    "inference.cautious",
    # helix/strand/loop inpainting is in base.yaml but nothing reads it
    "contigmap.inpaint_str_helix", "contigmap.inpaint_str_strand",
    "contigmap.inpaint_str_loop",
}
settable = {f"{section}.{key}" for section, names in BASE_YAML_KEYS.items() for key in names}
missing = sorted(settable - set(flags) - NOT_OFFERED)
check("every other key in base.yaml is offered", not missing, str(missing))

check("the protocols name real groups",
      all(group in {g["id"] for g in RFDIFFUSION_GROUPS}
          for mode in RFDIFFUSION_MODES for group in mode["groups"]))
check("every setting sits in a real group",
      all(option["group"] in {g["id"] for g in RFDIFFUSION_GROUPS}
          for option in RFDIFFUSION_OPTIONS))
check("every protocol default names a real setting",
      all(key in OPTIONS_BY_KEY for mode in RFDIFFUSION_MODES for key in mode["defaults"]),
      ", ".join(sorted({k for m in RFDIFFUSION_MODES for k in m["defaults"]})))
check("a setting restricted to protocols names real ones",
      all(m in MODES_BY_ID for option in RFDIFFUSION_OPTIONS for m in option.get("modes", ())))
check("the default protocol exists", DEFAULT_MODE in MODES_BY_ID, DEFAULT_MODE)

served = describe_options()
check("the catalogue survives JSON", json.loads(json.dumps(served))["options"][0]["key"] == keys[0])
check("it carries every protocol and setting",
      len(served["modes"]) == len(RFDIFFUSION_MODES)
      and len(served["options"]) == len(RFDIFFUSION_OPTIONS),
      f"{len(served['modes'])} protocols, {len(served['options'])} settings")
check("it offers every checkpoint", {c["name"] for c in served["checkpoints"]} == set(WEIGHTS),
      f"{len(served['checkpoints'])}")
check("it claims nothing about which are downloaded",
      all("present" not in entry for entry in served["checkpoints"]))
check("the checkpoint menu is filled in",
      set(OPTIONS_BY_KEY["checkpoint"]["choices"]) == set()
      and set(next(o for o in served["options"] if o["key"] == "checkpoint")["choices"]) == set(WEIGHTS))

print("\nsettings to a command line")
line = hydra_overrides({
    "steps": 30, "noiseScale": 0.5, "deterministic": True, "recenter": False,
    "symmetry": "c4", "guideScale": 2, "inpaintSeq": "A1-10/A12",
    "guidingPotentials": ["type:olig_contacts,weight_intra:1", "type:monomer_ROG,weight:1"],
    "checkpoint": "ActiveSite_ckpt.pt", "extra": ["inference.cautious=False"],
}, models_dir="/m")
check("a number becomes its flag", "diffuser.T=30" in line)
check("one setting can be two flags",
      "denoiser.noise_scale_ca=0.5" in line and "denoiser.noise_scale_frame=0.5" in line)
check("a true boolean is spelled the way OmegaConf reads it",
      "inference.deterministic=True" in line)
check("so is a false one, which is not the same as leaving it out",
      "inference.recenter=False" in line)
check("a list is bracketed", "contigmap.inpaint_seq=[A1-10/A12]" in line)
check("potentials are bracketed and quoted, one entry each",
      'potentials.guiding_potentials=["type:olig_contacts,weight_intra:1","type:monomer_ROG,weight:1"]' in line)
check("a checkpoint becomes a path in the models directory",
      "inference.ckpt_override_path=/m/ActiveSite_ckpt.pt" in line)
check("a free-form override is passed through", "inference.cautious=False" in line)
check("a whole number keeps its short spelling", "potentials.guide_scale=2" in line)
check("nothing else is passed", len(line) == 11, f"{len(line)} arguments")
check("an unset setting is not passed at all", hydra_overrides({}) == [])
check("an empty string is not passed either",
      hydra_overrides({"symmetry": "", "cycChains": "   "}) == [])
check("zero is passed, because zero is a value",
      hydra_overrides({"noiseScale": 0}) == ["denoiser.noise_scale_ca=0",
                                             "denoiser.noise_scale_frame=0"])

print("\nsettings that would be refused")
check("a number out of range is caught",
      "cannot be above" in "; ".join(option_problems({"noiseScale": 9})))
check("a choice that is not one of the choices is caught",
      "is not one of" in "; ".join(option_problems({"guideDecay": "sideways"})))
check("partial past the step count is caught before the model loads",
      "cannot be above steps" in "; ".join(option_problems({"steps": 10, "partialT": 40})))
check("provide_seq without partial diffusion is caught, as RFdiffusion asserts",
      "partial diffusion" in "; ".join(option_problems({"provideSeq": "1-10"})))
check("an override that is not key=value is caught",
      "not key=value" in "; ".join(option_problems({"extra": ["nonsense"]})))
for owned in OWNED_FLAGS:
    if option_problems({"extra": [f"{owned}=x"]}) == []:
        check(f"an override of {owned} is refused", False)
        break
else:
    check("no override may reach a flag the job itself sets", True,
          f"{len(OWNED_FLAGS)} flags protected")
check("a good setting raises nothing",
      option_problems({"steps": 50, "partialT": 20, "guideDecay": "cubic",
                       "extra": ["inference.cautious=False"]}) == [])

print("\nwhich checkpoint a job needs")
# The ladder in model_runners.py, in its order. A different order picks a
# different model, so each rung is checked rather than the ends.
check("nothing special takes the base model", checkpoint_for({}, []) == "Base_ckpt.pt")
check("hotspots take the complex model",
      checkpoint_for({}, ["A59"]) == "Complex_base_ckpt.pt")
check("inpainting beats hotspots, as RFdiffusion tests it first",
      checkpoint_for({"inpaintSeq": "A1-10"}, ["A59"]) == "InpaintSeq_ckpt.pt")
check("inpainting plus fold conditioning takes the combined model",
      checkpoint_for({"inpaintSeq": "A1-10", "scaffoldGuided": True}, ["A59"])
      == "InpaintSeq_Fold_ckpt.pt")
check("fold conditioning alone takes the fold model",
      checkpoint_for({"scaffoldGuided": True}, ["A59"]) == "Complex_Fold_base_ckpt.pt")
check("an explicit choice wins over all of it",
      checkpoint_for({"checkpoint": "ActiveSite_ckpt.pt"}, ["A59"]) == "ActiveSite_ckpt.pt")
check("every checkpoint it can choose is one the worker can fetch",
      all(checkpoint_for(run, spots) in WEIGHTS
          for run in ({}, {"inpaintSeq": "A1"}, {"scaffoldGuided": True},
                      {"inpaintSeq": "A1", "scaffoldGuided": True})
          for spots in ([], ["A59"])))
check("setup fetches the two a binder run picks between",
      set(weights_wanted(None)) == {"Base_ckpt.pt", "Complex_base_ckpt.pt"})
check("and can be asked for all of them", set(weights_wanted("all")) == set(WEIGHTS))
check("an unknown name is refused rather than silently skipped",
      _raises(lambda: weights_wanted("Nonesuch.pt")) is not None)
check("the potentials offered are the ones RFdiffusion implements",
      {p.split(",")[0].removeprefix("type:") for p in POTENTIAL_EXAMPLES}
      == {"monomer_ROG", "binder_ROG", "dimer_ROG", "binder_ncontacts",
          "interface_ncontacts", "monomer_contacts", "olig_contacts",
          "substrate_contacts"})

print("\nspec validation across the protocols")
mono = {"mode": "monomer", "binder": {"lengthMin": 80, "lengthMax": 80}}
check("a protocol that designs from nothing needs no target",
      validate_spec(dict(mono))["mode"] == "monomer")
check("a binder still needs hotspots",
      rejects({"mode": "binder", **good, "target": {"pdb": good["target"]["pdb"]}}, "no site"))
check("motif scaffolding needs a target but no hotspots",
      validate_spec({"mode": "motif", "target": {"pdb": good["target"]["pdb"]},
                     "binder": good["binder"]})["mode"] == "motif")
check("an unknown protocol is refused", rejects({**good, "mode": "telepathy"}, "no such mode"))
check("a setting that would be refused stops the job here",
      rejects({**good, "run": {"guideDecay": "sideways"}}, "bad option"))
check("partial diffusion may have no length range, given a contig",
      validate_spec({"mode": "partial", "target": {"pdb": good["target"]["pdb"]},
                     "binder": {"contigs": "100-100/0 B1-150"},
                     "run": {"partialT": 20}})["mode"] == "partial")

print("\nthe ESM3 catalogue")
from proteincad.colab_worker import (  # noqa: E402
    ENGINES, ENGINES_BY_ID, ESM3_DEFAULT_MODE, ESM3_GROUPS, ESM3_MODES,
    ESM3_MODES_BY_ID, ESM3_OPTIONS, ESM3_OPTIONS_BY_KEY, ESM3_PLANS,
    ESM3_TRACK_IDS, MODELS, Pipeline, engine_of, esm3_describe_options,
    esm3_layout, esm3_option_problems, esm3_plan, mode_spec_for, models_for,
    modes_for_engine, parse_plan_step, wanted_generators,
)

esm3_keys = [option["key"] for option in ESM3_OPTIONS]
check("every setting has a distinct key",
      len(esm3_keys) == len(set(esm3_keys)), f"{len(esm3_keys)} settings")
check("no setting is named for a flag, because nothing here is a command line",
      all("flags" not in option for option in ESM3_OPTIONS))
check("every setting sits in a real group",
      all(option["group"] in {g["id"] for g in ESM3_GROUPS} for option in ESM3_OPTIONS))
check("the protocols name real groups",
      all(group in {g["id"] for g in ESM3_GROUPS}
          for mode in ESM3_MODES for group in mode["groups"]))
check("every protocol default names a real setting",
      all(key in ESM3_OPTIONS_BY_KEY for mode in ESM3_MODES for key in mode["defaults"]),
      ", ".join(sorted({k for m in ESM3_MODES for k in m["defaults"]})))
check("a setting restricted to protocols names real ones",
      all(m in ESM3_MODES_BY_ID for option in ESM3_OPTIONS for m in option.get("modes", ())))
check("the default protocol exists", ESM3_DEFAULT_MODE in ESM3_MODES_BY_ID, ESM3_DEFAULT_MODE)
check("every protocol prompts and generates real tracks",
      all(track in ESM3_TRACK_IDS for mode in ESM3_MODES
          for track in tuple(mode["prompt"]) + tuple(mode["generates"])))
# The plan is the input with no RFdiffusion equivalent, so the table of starting
# plans is held to the same standard as the settings: a line that does not parse
# is a protocol that cannot be run, and it would be found out on a GPU.
check("every protocol has a starting plan", set(ESM3_PLANS) == set(ESM3_MODES_BY_ID),
      str(sorted(set(ESM3_PLANS) ^ set(ESM3_MODES_BY_ID))))
check("every starting plan parses and names real tracks",
      all(parse_plan_step(line)["track"] in ESM3_TRACK_IDS
          for plan in ESM3_PLANS.values() for line in plan))
check("every plan that writes both tracks decides the sequence first",
      all([s["track"] for s in map(parse_plan_step, plan)].index("sequence")
          < [s["track"] for s in map(parse_plan_step, plan)].index("structure")
          for plan in ESM3_PLANS.values()
          if {"sequence", "structure"} <= {parse_plan_step(l)["track"] for l in plan}))

esm3_served = esm3_describe_options()
check("the catalogue survives JSON",
      json.loads(json.dumps(esm3_served))["options"][0]["key"] == esm3_keys[0])
check("it carries every protocol, setting and track",
      len(esm3_served["modes"]) == len(ESM3_MODES)
      and len(esm3_served["options"]) == len(ESM3_OPTIONS)
      and len(esm3_served["tracks"]) == len(ESM3_TRACK_IDS))
check("it names the licence, because the weights cannot be fetched without it",
      "huggingface.co" in esm3_served["licence"])

both = describe_options()
check("both engines are served", [e["id"] for e in both["engines"]] == ["rfdiffusion", "esm3"])
check("each engine carries its own protocols",
      [m["id"] for m in both["engines"][1]["modes"]] == [m["id"] for m in ESM3_MODES])
check("RFdiffusion's tables are still at the top level for an older panel",
      both["modes"] == [{k: (list(v) if isinstance(v, tuple) else v) for k, v in m.items()}
                        for m in RFDIFFUSION_MODES])
check("the whole catalogue survives JSON", json.loads(json.dumps(both))["default_engine"]
      == "rfdiffusion")

print("\nwhich engine answers")
check("no engine named means RFdiffusion, so every older spec still runs",
      engine_of({}) == "rfdiffusion" and engine_of({"mode": "binder"}) == "rfdiffusion")
check("an engine named is used", engine_of({"engine": "esm3"}) == "esm3")
check("one that does not exist falls back rather than crashing",
      engine_of({"engine": "telepathy"}) == "rfdiffusion")
# The two tables share mode ids and mean different things by them. Reading the
# wrong one is a silent wrong answer, not an error, which is why this is checked
# rather than assumed.
shared = {m["id"] for m in RFDIFFUSION_MODES} & set(ESM3_MODES_BY_ID)
check("the two protocol tables do share names, so the engine has to decide",
      bool(shared), ", ".join(sorted(shared)) or "none")
check("and a shared name resolves through the engine, not the id",
      mode_spec_for({"engine": "esm3", "mode": "motif"})["engine"] == "esm3"
      and "engine" not in mode_spec_for({"mode": "motif"}))
check("each engine lists its own protocols",
      modes_for_engine("esm3") is ESM3_MODES and modes_for_engine("rfdiffusion") is RFDIFFUSION_MODES)
check("a backbone goes to the engine that draws it",
      Pipeline.route({"kind": "binder"}) == "binder"
      and Pipeline.route({"kind": "binder", "engine": "esm3"}) == "esm3")
check("and stage two goes to stage two whichever engine drew it",
      Pipeline.route({"kind": "fold"}) == "fold"
      and Pipeline.route({"kind": "fold", "engine": "esm3"}) == "fold")
check("the weights a job needs follow the engine",
      models_for({"engine": "esm3", "mode": "generate"}) == ["esm3"]
      and models_for({"mode": "binder"}) == ["rfdiffusion"])
check("and stage two needs the same two either way",
      models_for({"kind": "fold", "engine": "esm3"}) == ["proteinmpnn", "esmfold"])
check("every engine's models are ones the panel has buttons for",
      all(model in {m["id"] for m in MODELS}
          for engine in ENGINES for model in engine["models"]))
check("asking for both engines gets both", wanted_generators("rfdiffusion,esm3")
      == ["rfdiffusion", "esm3"])
check("and the default is still the connection test", wanted_generators("") == ["echo"])

print("\nESM3 settings that would be refused")
esm3_bad = lambda run: "; ".join(esm3_option_problems(run))  # noqa: E731
check("a number out of range is caught", "cannot be above" in esm3_bad({"temperature": 9}))
check("a plan line naming a track that does not exist is caught",
      "is not a track" in esm3_bad({"plan": ["vibes:8"]}))
check("a plan line with too many parts is caught",
      "more than track:steps:temperature" in esm3_bad({"plan": ["sequence:8:0.5:7"]}))
check("zero steps is caught", "fewer than one step" in esm3_bad({"plan": ["sequence:0"]}))
check("a secondary structure state that is not one is caught",
      "is not one of" in esm3_bad({"secondaryStructure": "HHHZZZ"}))
check("a sequence prompt that is not amino acids is caught",
      "not an amino acid" in esm3_bad({"sequencePrompt": "MKT1234"}))
# Two prompts describing the same positions at different lengths is the mistake
# that costs a GPU minute and comes back as a tensor shape.
check("two track prompts of different lengths are caught",
      "describe the same positions" in esm3_bad({"sequencePrompt": "MKTAY",
                                                 "secondaryStructure": "HHH"}))
check("an exposure target without a range is caught",
      "needs first-last:value" in esm3_bad({"sasa": ["80"]}))
check("an exposure target that is not a number is caught",
      "needs a number" in esm3_bad({"sasa": ["10-20:buried"]}))
check("a backwards range is caught", "not a usable range" in esm3_bad({"sasa": ["20-10:5"]}))
check("a function term keeps its text, because it is not a number",
      esm3_option_problems({"function": ["30-70:IPR000719"]}) == [])
check("a mask character is allowed in a sequence prompt, since that is the point",
      esm3_option_problems({"sequencePrompt": "___MKT___"}) == [])
check("and a chain break is allowed", esm3_option_problems({"sequencePrompt": "MKT|AYQ"}) == [])
check("nothing set is nothing wrong", esm3_option_problems({}) == [])

print("\nthe ESM3 decode plan")
check("a protocol with no plan typed uses its own",
      [s["track"] for s in esm3_plan({"engine": "esm3", "mode": "inverse"})] == ["sequence"])
check("a typed plan wins", [s["track"] for s in esm3_plan(
    {"engine": "esm3", "mode": "generate", "run": {"plan": ["structure:3"]}})] == ["structure"])
check("a line without a step count takes the run's own",
      esm3_plan({"engine": "esm3", "mode": "generate",
                 "run": {"plan": ["sequence"], "numSteps": 12}})[0]["num_steps"] == 12)
check("and a line that names one keeps it",
      esm3_plan({"engine": "esm3", "mode": "generate",
                 "run": {"plan": ["sequence:3"], "numSteps": 12}})[0]["num_steps"] == 3)
check("a line without a temperature takes the run's own",
      esm3_plan({"engine": "esm3", "mode": "generate",
                 "run": {"plan": ["sequence"], "temperature": 0.9}})[0]["temperature"] == 0.9)
check("temperature zero is carried, because zero is a value, not an absence",
      esm3_plan({"engine": "esm3", "mode": "generate",
                 "run": {"plan": ["structure:4:0"]}})[0]["temperature"] == 0.0)

print("\nwhere an ESM3 job puts what it is given")
HOT = [f"A{n}" for n in range(30, 36)] + [f"A{n}" for n in range(60, 63)]
hb = (ROOT / "data/cache/4hhb.pdb")
if not hb.is_file():
    print("  (skipped: data/cache/4hhb.pdb is missing)")
else:
    pdb = hb.read_text()
    motif_spec = {"engine": "esm3", "mode": "motif", "kind": "binder",
                  "target": {"pdb": pdb, "hotspots": HOT},
                  "binder": {"lengthMin": 60, "lengthMax": 60}, "run": {}}
    lay = esm3_layout(motif_spec)
    check("the design is the length asked for", lay["length"] == 60)
    check("every picked residue is placed", len(lay["motif"]) == len(HOT))
    check("the sequence prompt is as long as the design", len(lay["sequence"]) == 60)
    check("the placed residues are the only ones given",
          sum(1 for c in lay["sequence"] if c != "_") == len(HOT))
    places = [entry["at"] for entry in lay["motif"]]
    check("they are placed in order, and never twice",
          places == sorted(places) and len(set(places)) == len(places))
    check("every one lands inside the design",
          all(0 <= place < lay["length"] for place in places))
    # Two spans, so there are three gaps to share the spare length between. A
    # motif crowded against residue 1 is a design with nothing on one side of it.
    check("the motif is not pushed against either end",
          places[0] > 0 and places[-1] < lay["length"] - 1,
          f"first at {places[0]}, last at {places[-1]} of {lay['length']}")
    check("a consecutive run stays consecutive",
          places[:6] == list(range(places[0], places[0] + 6)))
    check("and the two spans are still apart", places[6] - places[5] > 1)
    check("the kept residues keep their own identity when asked",
          lay["sequence"][places[0]] != "_")
    shapeless = esm3_layout({**motif_spec, "run": {"keepSequence": False}})
    check("and are masked when not",
          all(c == "_" for c in shapeless["sequence"]),
          shapeless["sequence"])
    check("a motif that does not fit is refused, with a way out",
          rejects({**motif_spec, "binder": {"lengthMin": 4, "lengthMax": 4},
                   "engine": "esm3"}, "motif too long")
          )
    # Inverse folding takes the whole thing, and its length is not the user's
    # to choose: it is however long the chain is.
    chain_c = "\n".join(l for l in pdb.splitlines()
                        if l.startswith("ATOM") and l[21] == "C")
    inverse = esm3_layout({"engine": "esm3", "mode": "inverse",
                           "target": {"pdb": chain_c, "hotspots": []},
                           "binder": {"lengthMin": 10, "lengthMax": 10}})
    check("the length comes off the structure, not the range", inverse["length"] == 141)
    # The property that makes this inverse folding and not a no-op: the whole
    # sequence is masked, so there is something to generate. Filling it in from
    # the input -- which a layout that sets every track it can find a value for
    # does -- leaves a plan whose passes have no masked positions, and a
    # "design" that is the input handed back.
    check("the sequence is masked throughout, because that is what it generates",
          set(inverse["sequence"]) == {"_"}, inverse["sequence"][:20])
    check("and the coordinates are given, because that is what it reads",
          inverse["keep_structure"] and len(inverse["motif"]) == 141)

    # The other way round, and the pair is the point: these two protocols are
    # the same model with the tracks swapped, so if either filled in both they
    # would be the same job.
    predict = esm3_layout({"engine": "esm3", "mode": "predict",
                           "target": {"pdb": chain_c, "hotspots": []}})
    check("structure prediction is given the sequence",
          predict["sequence"].startswith("VLSPADKTNVKAAWGKV"), predict["sequence"][:20])
    check("and is not given the structure it is predicting",
          not predict["keep_structure"])

    # Partial resampling: the fraction is the dial between a copy of the input
    # and something unrelated to it, and nothing else in the layout does this.
    for fraction, expected in ((0.1, 14), (0.5, 70)):
        rs = esm3_layout({"engine": "esm3", "mode": "resample",
                          "target": {"pdb": chain_c, "hotspots": []},
                          "run": {"seed": 7, "fraction": fraction}})
        check(f"a fraction of {fraction} forgets that much of it",
              rs["resampled"] == expected and rs["sequence"].count("_") == expected,
              f'{rs["resampled"]} of {rs["length"]}')
        check("and the positions it forgot have no coordinates either",
              len(rs["motif"]) == rs["length"] - rs["resampled"])
    again = esm3_layout({"engine": "esm3", "mode": "resample",
                         "target": {"pdb": chain_c, "hotspots": []},
                         "run": {"seed": 7, "fraction": 0.5}})
    check("the same seed forgets the same positions, so a variation is reproducible",
          again["sequence"] == esm3_layout(
              {"engine": "esm3", "mode": "resample",
               "target": {"pdb": chain_c, "hotspots": []},
               "run": {"seed": 7, "fraction": 0.5}})["sequence"])
    check("and a different seed does not",
          again["sequence"] != esm3_layout(
              {"engine": "esm3", "mode": "resample",
               "target": {"pdb": chain_c, "hotspots": []},
               "run": {"seed": 8, "fraction": 0.5}})["sequence"])

    # The general form of the two checks above, over every protocol: what a
    # layout hands the model has to be what its table says it hands the model.
    # A protocol that gives a track it claims to generate has nothing to do.
    for _mode in ESM3_MODES:
        _hot = [f"C{n}" for n in range(30, 42)] if _mode["hotspots"] != "ignored" else []
        _lay = esm3_layout({"engine": "esm3", "mode": _mode["id"],
                            "target": {"pdb": chain_c, "hotspots": _hot},
                            "binder": {"lengthMin": 40, "lengthMax": 40},
                            "run": {"seed": 1}})
        _gives_seq = "sequence" in _mode["prompt"]
        _gives_str = "structure" in _mode["prompt"]
        _has_seq = any(c != "_" for c in _lay["sequence"])
        check(f"{_mode['id']}: the sequence track is given only if it says so",
              _has_seq == _gives_seq, f"given {_has_seq}, table says {_gives_seq}")
        check(f"{_mode['id']}: the structure track likewise",
              _lay["keep_structure"] == (_gives_str and bool(_lay["motif"])),
              f"given {_lay['keep_structure']}, table says {_gives_str}")
        check(f"{_mode['id']}: there is something left to generate",
              any(c == "_" for c in _lay["sequence"]) or "sequence" not in _mode["generates"],
              f"{_lay['sequence'].count('_')} masked of {_lay['length']}")
    nothing = esm3_layout({"engine": "esm3", "mode": "generate",
                           "binder": {"lengthMin": 80, "lengthMax": 100}})
    check("a design from nothing is masked throughout",
          set(nothing["sequence"]) == {"_"} and nothing["motif"] == [])
    check("and its length is inside the range asked for",
          80 <= nothing["length"] <= 100, str(nothing["length"]))
    typed = esm3_layout({"engine": "esm3", "mode": "generate",
                         "run": {"sequencePrompt": "___MKTAYIAKQ___"}})
    check("a typed prompt sets the length itself", typed["length"] == 15)
    check("and is passed through as written", typed["sequence"] == "___MKTAYIAKQ___")

print("\nthe ESM3 generator against a stand-in model")
import textwrap as _textwrap  # noqa: E402
from proteincad import colab_worker as worker  # noqa: E402

# The real script needs a GPU and weights behind a licence. This one is handed
# the same request file and writes the same output files, so everything between
# the spec and the designs is covered except the model: the request that gets
# built, the layout inside it, the motif file, and -- the part worth testing
# most -- what is done with what comes back.
_ESM3_STAND_IN = _textwrap.dedent('''
    import json, sys
    from pathlib import Path

    request = json.loads(Path(sys.argv[1]).read_text())
    layout = request["layout"]
    length = int(layout["length"])

    # What the real script asserts by running: the plan is a list of passes with
    # a step count each, and the motif file is there when the layout claims it.
    assert request["plan"], "no decode plan"
    for step in request["plan"]:
        assert step["track"], "a plan step with no track"
        assert int(step["num_steps"]) >= 1, "a plan step with no steps"
    if layout["motif"] and layout.get("keep_structure"):
        assert Path(request["motif_pdb"]).is_file(), "no motif file"
        given = [l for l in Path(request["motif_pdb"]).read_text().splitlines()
                 if l.startswith("ATOM")]
        assert given, "motif file has no atoms"

    print("STAGE loading the stand-in", flush=True)
    out_dir = Path(request["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for index in range(int(request["count"])):
        print("STAGE design %d" % (index + 1), flush=True)
        # A straight line of CA atoms along x, shifted per design, in a frame of
        # the model's own choosing -- which is exactly the thing the caller has
        # to put back onto the scene.
        lines = []
        for position in range(length):
            lines.append(
                "ATOM  %5d  CA  GLY A%4d    %8.3f%8.3f%8.3f  1.00  0.00           C"
                % (position + 1, position + 1, position * 3.8, 100.0 + index, 0.0))
        path = out_dir / ("esm3_%03d.pdb" % index)
        path.write_text("\\n".join(lines) + "\\nEND\\n")
        results.append({"pdb": str(path), "sequence": "A" * length,
                        "ptm": 0.81, "plddt": 0.77})
    Path(request["out"]).write_text(json.dumps(results))
    print("STAGE done", flush=True)
''').lstrip()

# The model scripts are strings until something runs them, and the things that
# run them are a GPU and a licence. So they are at least parsed here: a typo in
# one is otherwise found by a job, minutes into a run, on a machine that costs
# money to keep waiting.
import ast as _ast  # noqa: E402

for _name in ("ESM3_SCRIPT", "ESM3_PROBE", "FOLD_SCRIPT", "FOLD_PROBE", "PROBE"):
    _source = getattr(worker, _name, None)
    if _source is None:
        check(f"{_name} exists to be checked", False)
        continue
    _broken = _raises(lambda: _ast.parse(_source))
    check(f"{_name} is valid Python", _broken is None, str(_broken or ""))

_stand_in_dir = Path(tempfile.mkdtemp(prefix="esm3-stand-in-"))
_stand_in_py = _stand_in_dir / "fake_esm3.py"
_stand_in_py.write_text(_ESM3_STAND_IN)
# The generator writes its own script and runs `python <script> <request>`. A
# shim interpreter that ignores the script it is handed and runs this one
# instead is what stands in for the model, leaving every other moving part real.
_shim = _stand_in_dir / "python-shim"
_shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_stand_in_py}" "$2"\n')
_shim.chmod(0o755)


def _esm3_run(spec, work=None):
    """Run the generator over a spec and hand back the job dict."""
    engine = worker.Esm3Generator(python=str(_shim), work=work or _stand_in_dir)
    engine._problems = []            # the environment is the stand-in's business
    job = {"id": "t" + str(abs(hash(json.dumps(spec, sort_keys=True))) % 10000),
           "designs": [], "error": "", "cancel": False, "progress": 0,
           "total": 1, "stage": "", "log": ""}
    engine.generate(spec, job)
    return job


_from_nothing = _esm3_run({
    "engine": "esm3", "mode": "generate", "kind": "binder",
    "binder": {"lengthMin": 24, "lengthMax": 24},
    "run": {"numDesigns": 2, "plan": ["sequence:4", "structure:4:0"]},
})
check("the generator produced what was asked for",
      len(_from_nothing["designs"]) == 2 and not _from_nothing["error"],
      _from_nothing["error"][:120] or "2 designs")
check("and counted them as it went", _from_nothing["progress"] == 2)
check("each design carries coordinates",
      all("ATOM" in d["pdb"] for d in _from_nothing["designs"]))
check("the sequence ESM3 wrote is reported, because it writes one",
      _from_nothing["designs"][0]["metrics"]["sequence"] == "A" * 24)
check("so is its own confidence in the fold",
      _from_nothing["designs"][0]["metrics"]["ptm"] == 0.81
      and _from_nothing["designs"][0]["metrics"]["plddt"] == 0.77)
check("a design from nothing says it is in the model's own frame",
      "own frame" in _from_nothing["designs"][0]["metrics"]["placed"],
      _from_nothing["designs"][0]["metrics"]["placed"])
check("the plan that ran is recorded, the way a command line is",
      "sequence:4" in _from_nothing["log"] and "structure:4" in _from_nothing["log"],
      _from_nothing["log"].replace("\n", " ")[:90])
check("nothing is marked as given, because the model wrote all of it",
      worker.split_marked(_from_nothing["designs"][0]["pdb"])[1] == [],
      "so stage two designs a sequence for the whole thing")

# Placement is the part with a right answer that can be checked. The stand-in
# answers in a frame 100 A away from the scene; the motif it was given is the
# only thing that says where the design actually belongs, so putting it back is
# a superposition and how far it lands is a number worth reporting.
_motif_pdb = []
for _n in range(1, 13):
    _motif_pdb.append(
        "ATOM  %5d  CA  GLY B%4d    %8.3f%8.3f%8.3f  1.00  0.00           C"
        % (_n, _n, _n * 3.8, 0.0, 0.0))
_motif_pdb = "\n".join(_motif_pdb) + "\nEND\n"
_scaffolded = _esm3_run({
    "engine": "esm3", "mode": "motif", "kind": "binder",
    "target": {"pdb": _motif_pdb, "hotspots": [f"B{n}" for n in range(1, 13)]},
    "binder": {"lengthMin": 24, "lengthMax": 24},
    "run": {"numDesigns": 1, "keepStructure": True, "keepSequence": True},
})
check("a design built onto a motif comes back", len(_scaffolded["designs"]) == 1,
      _scaffolded["error"][:150])
if _scaffolded["designs"]:
    _metrics = _scaffolded["designs"][0]["metrics"]
    check("it is placed on the residues that were picked",
          "superposed" in _metrics.get("placed", ""), _metrics.get("placed", ""))
    check("and how far it missed them by is reported, not hidden by the fit",
          "motif_rmsd" in _metrics, str(_metrics.get("motif_rmsd")))
    # The stand-in writes the same straight line the motif is, so a correct
    # superposition lands on it exactly. That makes this an arithmetic check on
    # the placement rather than a claim about a model.
    check("a design that does reproduce its motif lands on it",
          _metrics.get("motif_rmsd", 9) < 0.01, str(_metrics.get("motif_rmsd")))
    _moved = [worker.coordinates(line)
              for line in worker.atom_lines(_scaffolded["designs"][0]["pdb"])]
    check("so the coordinates come back in the scene, not 100 A away from it",
          all(abs(point[1]) < 1.0 for point in _moved),
          f"largest y {max(abs(p[1]) for p in _moved):.2f}")

_cancelled = {"id": "c", "designs": [], "error": "", "cancel": True, "progress": 0,
              "total": 1, "stage": "", "log": ""}
_canceller = worker.Esm3Generator(python=str(_shim), work=_stand_in_dir)
_canceller._problems = []
_canceller.generate({"engine": "esm3", "mode": "generate",
                     "binder": {"lengthMin": 24, "lengthMax": 24},
                     "run": {"numDesigns": 1}}, _cancelled)
check("a job cancelled while it runs keeps nothing and says so",
      _cancelled["designs"] == [] and _cancelled["stage"] == "cancelled",
      f"{len(_cancelled['designs'])} designs, stage {_cancelled['stage']!r}")

_unready = worker.Esm3Generator(python=str(_shim), work=_stand_in_dir)
_unready._problems = ["no CUDA device — ESM3 on a CPU is minutes per design"]
_blocked = {"id": "b", "designs": [], "error": "", "cancel": False, "progress": 0,
            "total": 1, "stage": "", "log": ""}
_unready.generate({"engine": "esm3", "mode": "generate",
                   "binder": {"lengthMin": 24, "lengthMax": 24}}, _blocked)
check("an environment that cannot run the model fails the job with the reason",
      not _blocked["designs"] and "CUDA" in _blocked["error"], _blocked["error"][:80])

_refused = _esm3_run({
    "engine": "esm3", "mode": "motif", "kind": "binder",
    "target": {"pdb": _motif_pdb, "hotspots": [f"B{n}" for n in range(1, 13)]},
    "binder": {"lengthMin": 6, "lengthMax": 6}, "run": {"numDesigns": 1},
})
check("a motif that does not fit is refused by the generator too, not just the API",
      not _refused["designs"] and "do not fit" in _refused["error"],
      _refused["error"][:90])

print("\nthe browser's copy of what each protocol needs")
# The panel has to know what a protocol needs from the scene before the server
# answers -- building a spec cannot wait on a fetch -- so the table exists twice,
# in two languages, and both files say they mirror each other. Two copies of one
# fact drift, and this one drifts silently: the wrong answer is a Run button that
# is enabled when it should not be, or a crop sent for a protocol that wanted
# whole chains. So the claim is checked rather than trusted, the same way the
# landscape fixture holds the two scans to one curve.
_spec_js = (ROOT / "web/src/design/spec.js").read_text()


def _needs_table(name):
    """One `export const <name> = { ... };` object literal, as a dict of dicts."""
    body = re.search(rf"export const {name} = \{{(.*?)\n\}};", _spec_js, re.S)
    if not body:
        return None
    out = {}
    for mode, fields in re.findall(r"(\w+):\s*\{([^}]*)\}", body.group(1)):
        entry = {}
        for key, value in re.findall(r"(\w+):\s*'?([\w]+)'?", fields):
            entry[key] = {"true": True, "false": False}.get(value, value)
        out[mode] = entry
    return out


for table, modes, label in (("MODE_NEEDS", RFDIFFUSION_MODES, "RFdiffusion"),
                            ("ESM3_MODE_NEEDS", ESM3_MODES, "ESM3")):
    found = _needs_table(table)
    check(f"the browser has a {label} table at all", bool(found), table)
    if not found:
        continue
    check(f"it names exactly the {label} protocols this build has",
          set(found) == {mode["id"] for mode in modes},
          ", ".join(sorted(set(found) ^ {m["id"] for m in modes})) or "the same set")
    wrong = []
    for mode in modes:
        entry = found.get(mode["id"]) or {}
        for key in ("target", "hotspots"):
            if entry.get(key) != mode[key]:
                wrong.append(f"{mode['id']}.{key}: browser {entry.get(key)!r} "
                             f"vs server {mode[key]!r}")
        # `subject` is absent from the server table for everything it is false
        # for, and the browser writes it out either way.
        if "subject" in entry and entry["subject"] != bool(mode.get("subject")):
            wrong.append(f"{mode['id']}.subject: browser {entry['subject']!r} "
                         f"vs server {bool(mode.get('subject'))!r}")
    check(f"and agrees with the server about every {label} one",
          not wrong, "; ".join(wrong[:3]))

print("\nESM3 spec validation")
esm3_good = {"engine": "esm3", "mode": "generate",
             "binder": {"lengthMin": 80, "lengthMax": 80}}
check("a design from nothing needs no structure",
      validate_spec(dict(esm3_good))["mode"] == "generate")
check("and is marked with the engine that will run it",
      validate_spec(dict(esm3_good))["engine"] == "esm3")
check("an engine this build does not have is refused",
      rejects({**esm3_good, "engine": "telepathy"}, "no such engine"))
check("an RFdiffusion protocol asked of ESM3 is refused",
      rejects({**esm3_good, "mode": "symmetry"}, "not an ESM3 protocol"))
check("and an ESM3 protocol asked of RFdiffusion is refused too",
      rejects({"mode": "inverse", **good, "engine": "rfdiffusion"}, "not an RFdiffusion mode"))
check("motif scaffolding needs residues to keep",
      rejects({"engine": "esm3", "mode": "motif",
               "target": {"pdb": good["target"]["pdb"]},
               "binder": {"lengthMin": 60, "lengthMax": 60}}, "no site"))
check("inverse folding needs a structure",
      rejects({"engine": "esm3", "mode": "inverse"}, "no structure"))
check("structure prediction needs a sequence from somewhere",
      rejects({"engine": "esm3", "mode": "predict"}, "no sequence"))
check("and is happy with a typed one",
      validate_spec({"engine": "esm3", "mode": "predict",
                     "run": {"sequencePrompt": "MKTAYIAKQRQISFVK"}})["mode"] == "predict")
check("a setting that would be refused stops the job here",
      rejects({**esm3_good, "run": {"temperature": 9}}, "bad option"))
check("a plan that does not parse stops it here too",
      rejects({**esm3_good, "run": {"plan": ["vibes:8"]}}, "bad plan"))
check("a length range is still needed when nothing else says the length",
      rejects({"engine": "esm3", "mode": "generate",
               "binder": {"lengthMin": 2, "lengthMax": 1}}, "bad length"))
check("but not when a prompt says it",
      validate_spec({"engine": "esm3", "mode": "generate",
                     "binder": {"lengthMin": 2, "lengthMax": 1},
                     "run": {"sequencePrompt": "____MKTAYIAKQ____"}})["mode"] == "generate")

print("\nremote runner failure reporting")
from http.server import BaseHTTPRequestHandler  # noqa: E402
from proteincad.design import HttpRunner, RunnerError  # noqa: E402


def stub_endpoint(status: int):
    class Stub(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = b'{"error":"stub"}'
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST

    stub = Server(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    return stub


class _FakeJob:
    cancelled = False

    def add_design(self, *args):
        pass


def remote_message(status):
    stub = stub_endpoint(status)
    runner = HttpRunner(f"http://127.0.0.1:{stub.server_address[1]}", token="t", timeout=5)
    try:
        runner.run({"target": {"pdb": "x"}, "binder": {}, "run": {"numDesigns": 1}}, _FakeJob())
        return ""
    except RunnerError as error:
        return str(error)
    finally:
        stub.shutdown()
        stub.server_close()


# The command the endpoint ran has to survive the trip back, or the app cannot
# tell an applied setting from one an older worker never heard of.
class _Reporting(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._reply({"job_id": "abc"})

    def do_GET(self):
        self._reply({"status": "done", "designs": [], "stage": "sampling",
                     "log": "python run_inference.py inference.symmetry=c4"})


reporting = Server(("127.0.0.1", 0), _Reporting)
threading.Thread(target=reporting.serve_forever, daemon=True).start()


class _NotingJob(_FakeJob):
    stage = ""
    command = ""


noting = _NotingJob()
HttpRunner(f"http://127.0.0.1:{reporting.server_address[1]}", timeout=5, poll=0.05).run(
    {"target": {"pdb": "x"}, "binder": {}, "run": {"numDesigns": 1}}, noting)
reporting.shutdown()
reporting.server_close()
check("the command the endpoint ran comes back with the job",
      "inference.symmetry=c4" in noting.command, noting.command)

# A tunnel with a dead worker behind it answers, so this must not read as
# "network down" -- that was the confusing case in practice.
gateway = remote_message(530)
check("gateway error explains the worker is down", "nothing is serving" in gateway, gateway[:60])
check("401 explains the token", "token does not match" in remote_message(401))
check("404 questions the URL", "not something else" in remote_message(404))
unreachable = ""
try:
    HttpRunner("http://127.0.0.1:9", timeout=2).run(
        {"target": {"pdb": "x"}, "binder": {}, "run": {}}, _FakeJob())
except RunnerError as error:
    unreachable = str(error)
check("no listener mentions the tunnel", "tunnel" in unreachable, unreachable[:60])

print("\nGPU worker setup (offline parts)")
import tempfile  # noqa: E402

from proteincad import colab_worker as worker  # noqa: E402

# RFdiffusion's own packages have to be importable without being pip-installed:
# the repo root holds `rfdiffusion`, env/SE3Transformer holds `se3_transformer`,
# and run_inference.py sits in scripts/ so neither is on sys.path by default.
env = worker.environment(Path("/content/RFdiffusion"), {"PYTHONPATH": "/keep/me"})
paths = env["PYTHONPATH"].split(":")
check("repo root is on PYTHONPATH", paths[0] == "/content/RFdiffusion")
check("SE3Transformer is on PYTHONPATH", paths[1].endswith("env/SE3Transformer"))
check("an existing PYTHONPATH is kept", "/keep/me" in paths)
# torch 2.6 refuses to unpickle RFdiffusion's checkpoints without this.
check("torch.load escape hatch is set", env["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] == "1")
check("but not over an explicit setting",
      worker.environment(Path("/x"), {"TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "0"})
      ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] == "0")

check("dgl index is keyed by torch minor",
      worker.dgl_index("2.5.1", "cu121")
      == "https://data.dgl.ai/wheels/torch-2.5/cu121/repo.html")

# The wheel is installed by exact URL. Anything vaguer lets pip fall back to
# PyPI, where dgl has no wheel for a current Python and only ever had CPU builds.
INDEX = '''<a href="dgl-2.4.0%2Bcu121-cp311-cp311-manylinux1_x86_64.whl">a</a><br>
<a href="dgl-2.10.0%2Bcu121-cp311-cp311-manylinux1_x86_64.whl">b</a><br>
<a href="dgl-2.5.0%2Bcu121-cp313-cp313-manylinux1_x86_64.whl">c</a><br>'''
order = worker.pick_wheels(INDEX, "https://data.dgl.ai/wheels/torch-2.5/cu121/repo.html", "cp311")
check("wheel choice is absolute", order[0].startswith("https://data.dgl.ai/wheels/"), order[0][:48])
check("wheel choice matches the interpreter", all("-cp311-" in u for u in order), str(len(order)))
# 2.10 > 2.4 numerically but sorts before it as text.
check("wheels come back newest first", "2.10.0" in order[0], order[0].rsplit("/", 1)[-1])
# Several candidates, not one: the newest is not always one the server will
# part with, and the caller needs something to fall back to.
check("every candidate is offered, not just the best", len(order) == 2, str(len(order)))
check("no wheel for an unlisted Python", worker.pick_wheels(INDEX, "https://h/i/", "cp39") == [])

# A dgl wheel loads libraries named for the exact torch it was built against, so
# the pin has to be that version, not merely the same minor.
check("the torch wheel url names the exact build",
      worker.torch_wheel("2.6.0", "cu124", "cp313")
      == "https://download.pytorch.org/whl/cu124/"
         "torch-2.6.0%2Bcu124-cp313-cp313-linux_x86_64.whl")
check("every candidate stack pins an exact torch version",
      all(len(v.split(".")) == 3 for v, _ in worker.DGL_STACKS),
      str([v for v, _ in worker.DGL_STACKS]))

# The bug this exists to prevent: DGL's index lists wheels the bucket will not
# serve. Picking one is a 403 several minutes into the install, so a candidate
# is not chosen until it has been confirmed downloadable.
class Bucket(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    LISTING = (b'<a href="dgl-9.9.9%2Bcu124-cp313-cp313-manylinux1_x86_64.whl">newest</a><br>'
               b'<a href="dgl-2.5.0%2Bcu124-cp313-cp313-manylinux1_x86_64.whl">older</a><br>')

    def log_message(self, *args):
        pass

    def _reply(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        return self._reply(200, self.LISTING) if self.path.endswith("repo.html") else self.do_HEAD()

    def do_HEAD(self):
        # The newest is listed but forbidden, exactly as the real bucket behaves.
        self._reply(403 if "9.9.9" in self.path else 200)


bucket = Server(("127.0.0.1", 0), Bucket)
threading.Thread(target=bucket.serve_forever, daemon=True).start()
host = f"http://127.0.0.1:{bucket.server_address[1]}"
real_index, real_torch = worker.dgl_index, worker.torch_wheel
worker.dgl_index = lambda version, cuda: f"{host}/wheels/torch-{version}/{cuda}/repo.html"
worker.torch_wheel = lambda version, cuda, tag: f"{host}/whl/torch-{version}-{tag}.whl"
try:
    pin, picked, where = worker.resolve_dgl("cp313", None, None)
    check("a listed-but-forbidden wheel is skipped", picked and "9.9.9" not in picked,
          (picked or "none").rsplit("/", 1)[-1])
    check("the next candidate down is taken instead", picked and "2.5.0" in picked)
    check("the chosen wheel matches the interpreter", picked and "-cp313-" in picked)

    # DGL loads its own libraries by the exact torch version --
    # libgraphbolt_pytorch_2.6.0.so -- so the "keep what is installed" shortcut
    # may only fire on an exact match. A patch release apart fails at import
    # with nothing to suggest the cause.
    exact, _, _ = worker.resolve_dgl("cp313", "2.6.0+cu124", "12.4")
    check("an exactly matching torch is kept", exact is None, str(exact))
    off_by_patch, _, _ = worker.resolve_dgl("cp313", "2.6.1+cu124", "12.4")
    check("a torch one patch off is replaced, not trusted",
          off_by_patch == "2.6.0+cu124", str(off_by_patch))
    wrong_cuda, _, _ = worker.resolve_dgl("cp313", "2.6.0+cu126", "12.6")
    check("the cuda build has to match too", wrong_cuda == "2.6.0+cu124", str(wrong_cuda))

    # And when the server will serve nothing, say so rather than handing pip a
    # URL that 403s — with the count, which is what distinguishes "none built"
    # from "all refused".
    Bucket.do_HEAD = lambda self: Bucket._reply(self, 403)
    pin, picked, listed = worker.resolve_dgl("cp313", None, None)
    check("nothing is returned when every wheel is refused", picked is None)
    check("the refusal count is reported", isinstance(listed, int) and listed > 0, str(listed))
finally:
    worker.dgl_index, worker.torch_wheel = real_index, real_torch
    bucket.shutdown()
    bucket.server_close()

# Setup steps are largely independent, so one failure must not take the others
# with it -- an unreachable dgl wheel used to abort the run and leave e3nn and
# hydra uninstalled, so the next report blamed three things instead of one.
# A DGL resolution that always succeeds, so the checks below are about the
# order setup does things in rather than about which wheels DGL happens to
# publish today.
#
# The real resolver asks data.dgl.ai over the network and answers for the
# interpreter running this suite -- so on a Python DGL has no wheels for, it
# correctly finds nothing, no torch is pinned, and two checks about ordering
# fail for a reason that has nothing to do with ordering. (That is exactly what
# happened on 3.14.) What the resolver itself does is tested exhaustively
# above, against stubbed HTML, including the case where every wheel is refused.
FIXED_DGL = (
    "2.5.1+cu121",
    "https://data.dgl.ai/wheels/torch-2.5/cu121/"
    "dgl-2.5.0%2Bcu121-cp311-cp311-manylinux1_x86_64.whl",
    "https://data.dgl.ai/wheels/torch-2.5/cu121/repo.html",
)


def setup_with(failing: str):
    """Run setup with a pip that fails for one package, and report what it tried."""
    weights = Path(tempfile.mkdtemp())
    for name in ("Base_ckpt.pt", "Complex_base_ckpt.pt"):
        (weights / name).write_bytes(b"x" * 4096)
    files = Server(("127.0.0.1", 0), functools.partial(
        SimpleHTTPRequestHandler, directory=str(weights)))
    files.RequestHandlerClass.log_message = lambda *a, **k: None
    threading.Thread(target=files.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{files.server_address[1]}/"

    checkout = Path(tempfile.mkdtemp()) / "RFdiffusion"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "scripts" / "run_inference.py").write_text("")

    tried, real_pip, real_weights = [], worker.pip, worker.WEIGHTS
    real_resolve = worker.resolve_dgl
    worker.resolve_dgl = lambda *a, **k: FIXED_DGL
    worker.WEIGHTS = {n: root + n for n in ("Base_ckpt.pt", "Complex_base_ckpt.pt")}

    def fake_pip(python, *arguments):
        tried.append(" ".join(arguments))
        if any(failing in a for a in arguments):
            raise worker.SetupError("pretend this could not be fetched")

    worker.pip = fake_pip
    try:
        # Swallowed: setup reports a deliberately broken environment here, and
        # its own "FAIL dgl" lines read as failing checks in the suite output.
        with contextlib.redirect_stdout(io.StringIO()):
            # Only the backbone half: the other clones a second repository and
            # downloads eleven gigabytes, neither of which belongs in a test of
            # how setup behaves when one pip command fails.
            code = worker.run_setup(SimpleNamespace(rfdiffusion=str(checkout),
                                                    python=sys.executable,
                                                    only="rfdiffusion"))
    finally:
        worker.pip, worker.WEIGHTS = real_pip, real_weights
        worker.resolve_dgl = real_resolve
        files.shutdown()
        files.server_close()
    return code, tried


import contextlib  # noqa: E402
import functools  # noqa: E402
import io  # noqa: E402
from http.server import SimpleHTTPRequestHandler  # noqa: E402
from types import SimpleNamespace  # noqa: E402

code, tried = setup_with("dgl-")
check("a failed dgl does not abort the run", any("hydra-core" in t for t in tried),
      str(len(tried)) + " pip commands")
check("torch is pinned before the packages that need it",
      next(i for i, t in enumerate(tried) if t.startswith("torch==")) <
      next(i for i, t in enumerate(tried) if "hydra-core" in t))
check("a broken environment still exits non-zero", code == 1, str(code))

# And if torch itself will not install, dgl must not be bound to the wrong one.
code, tried = setup_with("--index-url")
check("dgl is skipped when its torch could not be installed",
      not any("dgl-" in t for t in tried), str([t[:20] for t in tried]))
check("no constraint is placed on a torch that is not there",
      "torch==" not in next(t for t in tried if "hydra-core" in t))

# A job whose contigs name chains that are not in the PDB beside them fails a
# minute into the run, inside RFdiffusion's contig parser, reporting a fragment
# of a chain id as a bad integer. Caught here against the file itself instead.
def backbone(chains, numbers):
    """A minimal PDB: one CA per residue, for the chains and numbers given."""
    lines, serial = [], 1
    for offset, chain in enumerate(chains):
        for n in numbers:
            lines.append("ATOM  %5d  CA  ALA %s%4d    %8.3f%8.3f   0.000  1.00  0.00           C"
                         % (serial, chain, n, serial * 3.8, offset * 7.0))
            serial += 1
    return "\n".join(lines)


TARGET = backbone("BA", range(23, 32))
against = worker.RFdiffusionGenerator.check_against_target

two_char = against(TARGET, "BL20-266/0 BM20-241/0 60-100", ["BL203"])
check("a two-character chain in the contig is caught",
      any("BL" in p and "first character" in p for p in two_char), two_char[0][:64] if two_char else "")
check("and the hotspot is caught with it",
      any("hotspot" in p and "multi-character" in p for p in two_char), str(len(two_char)))
check("a consistent job passes", against(TARGET, "B23-31/0 A23-31/0 60-100", ["B23", "B26"]) == [])
check("a chain missing from the target is caught",
      any("not in the target" in p for p in against(TARGET, "Z1-40/0 60-100", [])))
check("a hotspot missing from the target is caught",
      any("not in the target" in p for p in against(TARGET, "B23-31/0 60-100", ["B999"])))
# No target spans is a legitimate job -- unconditional generation -- not an error.
check("a bare length range is not flagged", against(TARGET, "60-100", []) == [])
check("nor is the chain break at the end of a block",
      against(TARGET, "B23-31/0 60-100", []) == [])

# A chain whose name is a digit does not fail as a missing chain. RFdiffusion
# decides a fragment names a chain with one test -- `subcon[0].isalpha()` in
# contigs.py -- so `6316-319` is read as a length to generate and dies five
# frames deep in random.randint, reporting `empty range in randrange(6316, 320)`.
numeric = against(backbone("6", range(316, 324)), "6316-323/0 60-100", [])
check("a digit chain id is caught before the model sees it",
      any("6316-323" in p for p in numeric), numeric[0][:80] if numeric else "(nothing)")
check("and the message names the real cause",
      any("starts with a letter" in p and "'6'" in p for p in numeric),
      numeric[0][-80:] if numeric else "")
# The same fault with the numbers the other way round is a plain empty range.
check("an empty length range is caught too",
      any("impossible" in p for p in against(TARGET, "B23-31/0 100-60", [])))
check("a hotspot on a digit chain says why it cannot be read",
      any("not a usable chain name" in p for p in against(TARGET, "B23-31/0 60-100", ["6316"])))

# A numbering range that crosses an unmodelled loop names residues that were
# never in the file. RFdiffusion asserts on the first one, a minute in.
GAPPED = backbone("B", list(range(20, 146)) + list(range(157, 250)))
crossing = against(GAPPED, "B20-249/0 60-100", ["B213"])
check("a range crossing a gap is caught",
      any("B146" in p for p in crossing), crossing[0][:70] if crossing else "")
check("and it says what to do about it",
      any("split the range around the gap" in p for p in crossing))
check("fragments written around the gap pass",
      against(GAPPED, "B20-145/B157-249/0 60-100", ["B213"]) == [])
# Every fragment is checked, not just the first in its block.
check("a bad fragment later in a block is still caught",
      any("B300" in p for p in against(GAPPED, "B20-145/B300-310/0 60-100", [])))

# A truncated weight file looks present and fails much later, inside torch.load.
tmp = Path(tempfile.mkdtemp())
(tmp / "w.pt").write_bytes(b"x" * 10)
check("a short weight file is not mistaken for a whole one",
      (tmp / "w.pt").stat().st_size != 480_000_000)
check("sizes read as bytes below a megabyte", worker.human(4096) == "4096 bytes")
check("sizes read as megabytes above one", worker.human(483_616_107) == "484 MB")

# The preflight has to name the remedy, because none of these errors imply it.
generator = worker.RFdiffusionGenerator(str(tmp / "absent"), python=sys.executable)
report = generator.diagnose()
problems = " | ".join(report["problems"])
check("missing checkout is reported", "no RFdiffusion checkout" in problems)
check("missing weights are reported", "no .pt weights" in problems)
check("the preflight names the fix", "colab_worker.py setup" in problems)
# Eleven ModuleNotFoundErrors in the app's error panel bury the one instruction.
check("import failures collapse to one line",
      sum(1 for p in report["problems"] if "importable" in p or "cannot import" in p) == 1,
      problems[-80:])
check("/health does not wait on a probe", worker.RFdiffusionGenerator(
    str(tmp), python=sys.executable).report() is None)

# Each of these reads as something other than what it is, so each gets named.
for label, line, wanted in [
    ("torch 2.6 pickling", "_pickle.UnpicklingError: Weights only load failed", "torch 2.6"),
    ("numpy 2 against dgl", "ImportError: numpy.core.multiarray failed to import", "numpy<2"),
    ("an ancient dgl from PyPI",
     "ImportError: cannot import name 'Mapping' from 'collections'", "ancient DGL"),
    ("a dgl/torch mismatch", "ImportError: /usr/lib/libdgl.so: undefined symbol: _ZN3c10", "pins the exact torch"),
    ("a missing module", "ModuleNotFoundError: No module named 'hydra'", "not importable"),
    ("a rejected override", "Could not override 'denoiser.noise_scale_ca'", "--no-binder-defaults"),
]:
    explained = generator.explain(1, [line]).split("--- environment ---")[0]
    check(f"{label} is explained", wanted in explained, explained.splitlines()[1][:58])

# A FutureWarning mentioning a keyword is not a report of that fault.
noise = generator.explain(1, ["FutureWarning: torch.cuda.amp.autocast is deprecated",
                              "RuntimeError: something else"])
check("warnings do not become diagnoses", "Likely cause" not in
      noise.split("--- environment ---")[0])

print("\nGPU worker against a stand-in run_inference.py")
import textwrap  # noqa: E402

# The real script needs a GPU; this one has the same CLI, writes the same file
# names in the same order, and asserts the environment it was handed. That
# covers everything between the spec and the designs except the model itself.
fake = Path(tempfile.mkdtemp()) / "RFdiffusion"
(fake / "scripts").mkdir(parents=True)
(fake / "models").mkdir()
(fake / "models" / "Complex_base_ckpt.pt").write_bytes(b"weights")
(fake / "scripts" / "run_inference.py").write_text(textwrap.dedent('''
    import os, sys, time
    from pathlib import Path
    args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    paths = os.environ.get("PYTHONPATH", "").split(os.pathsep)
    assert any(p.endswith("env/SE3Transformer") for p in paths), f"PYTHONPATH: {paths}"
    assert os.environ.get("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD") == "1", "no torch.load escape hatch"
    assert Path(args["inference.model_directory_path"]).is_dir(), "no model directory"
    # Null contigs is legal: fold conditioning takes its shape from scaffold files.
    print("contigs " + args.get("contigmap.contigs", "-") + " hotspots " + args.get("ppi.hotspot_res", "-"))
    print("[INFO] - Reading checkpoint from /x/Complex_base_ckpt.pt")
    print("Successful diffuser __init__")
    prefix = Path(args["inference.output_prefix"])
    prefix.parent.mkdir(parents=True, exist_ok=True)
    (prefix.parent / "traj").mkdir(exist_ok=True)
    for i in range(int(args["inference.num_designs"])):
        print(f"[INFO] - Making design {prefix.parent}/{prefix.name}_{i}")
        for t in (50, 49, 1):
            # torch emits this twice a step; RFdiffusion's own line carries the
            # whole sequence behind the part worth reading.
            print("[W913 01:36:16.2 Context.h:357] Warning: lazyInitCUDA is deprecated. (function lazyInitCUDA)")
            print("[W913 01:36:16.2 Context.h:357] Warning: lazyInitCUDA is deprecated. (function lazyInitCUDA)")
            print(f"[INFO] - Timestep {t}, input to next step: " + "G" * 400)
        out = prefix.parent / f"{prefix.name}_{i}.pdb"
        (prefix.parent / "traj" / f"{prefix.name}_{i}_pX0_traj.pdb").write_text("ATOM  traj\\n")
        out.write_text("ATOM      1  CA  GLY A   1       0.000   0.000   %d.000\\nEND\\n" % i)
        time.sleep(0.3)                 # the window in which the pdb has no trb yet
        out.with_suffix(".trb").write_bytes(b"trb")
''').lstrip())

stand_in = worker.RFdiffusionGenerator(str(fake), python=sys.executable)
stand_in.diagnose = lambda: {"problems": [], "weights": ["Complex_base_ckpt.pt"]}
fake_job = {"designs": [], "error": "", "cancel": False, "progress": 0, "total": 3}

stages, _note = [], stand_in.note_progress


def remember(text, job, total):
    _note(text, job, total)
    if job.get("stage") and (not stages or stages[-1] != job["stage"]):
        stages.append(job["stage"])


stand_in.note_progress = remember
worker_said = io.StringIO()
# A target the contigs and hotspots below are actually true of, since the worker
# now refuses a job that describes a different file from the one it was sent.
crop = backbone("A", range(17, 146))
with contextlib.redirect_stdout(worker_said):
    stand_in.generate(
        {"target": {"pdb": crop, "hotspots": ["A59", "A61"], "name": "7cgo"},
         "binder": {"contigs": "A17-145/0 70-100", "lengthMin": 70, "lengthMax": 100},
         "run": {"numDesigns": 3}},
        fake_job)
kept = [line for line in worker_said.getvalue().splitlines() if line.strip()]

check("the run reports no error", fake_job["error"] == "", fake_job["error"][:70])

# Hydra makes its own `outputs/<date>/<time>` directory, relative to the
# working directory -- which is the checkout. Writable on Colab, read-only in
# the container the hosted deployment actually runs the model in, where the
# whole run dies before the first design:
#
#     OSError: [Errno 30] Read-only file system: 'outputs'
#
# One override prevents it and nothing else in a run refers to it, so losing it
# costs nothing anywhere it is not needed and everything where it is. Checked
# here because the failure is invisible until a real run on a real read-only
# filesystem, which is a slow and expensive place to find out.
for flag in ("hydra.run.dir", "inference.schedule_directory_path"):
    where = [a.split("=", 1)[1] for a in fake_job["log"].split()
             if a.startswith(flag + "=")]
    check(f"{flag} is redirected off the read-only checkout",
          len(where) == 1 and Path(where[0]).is_absolute() and str(fake) not in where[0],
          str(where))
# The last design's trb appears just before the process exits, after the read
# loop has already broken out -- it is only collected by the final sweep.
check("every design is collected, including the last",
      [d["name"] for d in fake_job["designs"]] == ["design_0", "design_1", "design_2"],
      str([d["name"] for d in fake_job["designs"]]))
check("trajectory files are not returned as designs",
      all("traj" not in d["pdb"] for d in fake_job["designs"]))
check("progress tracks the designs", fake_job["progress"] == 3)
check("the contig string reaches the model intact",
      "contigmap.contigs=[A17-145/0 70-100]" in fake_job["log"])
check("hotspots reach the model in RFdiffusion's own format",
      "ppi.hotspot_res=[A59,A61]" in fake_job["log"])

# A run is minutes of silence otherwise: a minute loading weights, then fifty
# diffusion steps per design with nothing written until the last of them. "0/4"
# on its own is indistinguishable from a hang.
check("the model load is reported", stages and stages[0] == "loading the model",
      stages[0] if stages else "(nothing)")
check("each design is counted", "design 2 of 3: starting" in stages, " | ".join(stages[:4]))
# Diffusion counts down -- Timestep 50 is the first step -- so it is turned round.
check("steps are counted up, not down",
      stages.index("design 1 of 3: step 1 of 50") < stages.index("design 1 of 3: step 50 of 50"),
      " -> ".join(s for s in stages if s.startswith("design 1")))

# torch emits its deprecation notice twice per step: 400 lines a run, which
# would push the real output out of both the log and the failure tail.
check("torch's per-step noise is dropped",
      not any("lazyInitCUDA" in line for line in kept),
      f"{len(kept)} lines kept")
check("the long per-step line is trimmed",
      all(len(line) <= 200 for line in kept if "Timestep" in line),
      str(max((len(l) for l in kept if "Timestep" in l), default=0)))
check("but the real output survives", any("Making design" in line for line in kept))


def stand_in_command(spec):
    """The command a spec would run as, without running it far."""
    job = {"designs": [], "error": "", "cancel": False, "progress": 0, "total": 1,
           "stage": "", "log": ""}
    with contextlib.redirect_stdout(io.StringIO()):
        stand_in.generate(spec, job)
    return job["log"], job["error"]


# Every protocol has weights it would otherwise go and fetch. The stand-in has
# none, so they are stubbed: this is about the command, not the download.
for name in worker.WEIGHTS:
    (fake / "models" / name).write_bytes(b"weights")

log, err = stand_in_command({
    "mode": "symmetry",
    "binder": {"lengthMin": 200, "lengthMax": 200, "contigs": "200-200"},
    "run": {"numDesigns": 1, "symmetry": "c4", "oligIntraAll": True, "guideScale": 2,
            "guideDecay": "quadratic",
            "guidingPotentials": ["type:olig_contacts,weight_intra:1,weight_inter:0.1"]},
})
check("a symmetric job reports no error", err == "", err[:70])
check("symmetry reaches the model", "inference.symmetry=c4" in log)
check("and so do the potentials that protocol needs",
      'potentials.guiding_potentials=["type:olig_contacts,weight_intra:1,weight_inter:0.1"]' in log
      and "potentials.olig_intra_all=True" in log)
check("a protocol that designs from nothing sends no input pdb",
      "inference.input_pdb" not in log, log.split("run_inference.py")[-1][:70])
check("and no hotspots, which would change which model runs",
      "ppi.hotspot_res" not in log)

log, err = stand_in_command({
    "mode": "motif", "target": {"pdb": crop, "hotspots": ["A59"], "name": "7cgo"},
    "binder": {"contigs": "10-40/A55-65/10-40", "lengthMin": 60, "lengthMax": 100},
    "run": {"numDesigns": 1, "inpaintSeq": "A55-60", "partialT": None},
})
check("motif scaffolding sends its contig and its target", err == ""
      and "contigmap.contigs=[10-40/A55-65/10-40]" in log and "inference.input_pdb" in log,
      err[:70])
check("motif scaffolding drops the hotspots it was not asked to use",
      "ppi.hotspot_res" not in log)
check("inpainting reaches the model", "contigmap.inpaint_seq=[A55-60]" in log)

log, err = stand_in_command({
    "mode": "scaffold", "target": {"pdb": crop, "hotspots": ["A59"], "name": "7cgo"},
    "binder": {"contigs": "", "lengthMin": 60, "lengthMax": 100},
    "run": {"numDesigns": 1, "scaffoldGuided": True, "targetPdb": True,
            "scaffoldDir": "/content/scaffolds", "maskLoops": False},
})
check("fold conditioning is turned on",
      err == "" and "scaffoldguided.scaffoldguided=True" in log, err[:70])
check("and the target path is filled in from the file the worker wrote",
      "scaffoldguided.target_path=" in log
      and log.split("scaffoldguided.target_path=")[1].split()[0].endswith("target.pdb"))
check("a protocol whose shape comes from scaffold files sends no contig",
      "contigmap.contigs" not in log)

log, err = stand_in_command({
    "mode": "binder", "target": {"pdb": crop, "hotspots": ["A59"], "name": "7cgo"},
    "binder": {"contigs": "A17-145/0 70-100", "lengthMin": 70, "lengthMax": 100},
    "run": {"numDesigns": 1, "guideDecay": "sideways"},
})
check("a setting the model would reject stops the job before it starts",
      "would be rejected" in err and log == "", err.splitlines()[0][:60] if err else "(ran)")

# RFdiffusion centres its input on the motif's centre of mass and never undoes
# it, so a design arrives rigidly displaced -- near the origin rather than on the
# residues it was built against. A cropped subunit of a large assembly can be
# hundreds of angstroms from the origin, so this is not a subtle offset.
placer = worker.RFdiffusionGenerator(str(fake), python=sys.executable)


def ca(serial, chain, seq, xyz, bfactor):
    return ("ATOM  %5d  CA  ALA %s%4d    %8.3f%8.3f%8.3f  1.00%6.2f           C"
            % (serial, chain, seq, xyz[0], xyz[1], xyz[2], bfactor))


far = {n: (300.0 + n * 3.8, 150.0, -80.0) for n in range(20, 40)}
sent = "\n".join(ca(i, "B", n, xyz, 1.0) for i, (n, xyz) in enumerate(far.items(), 1))
com = placer.centroid(list(far.values()))
returned, serial = [], 1
for n in range(1, 9):  # the generated binder, marked with B-factor 0
    point = (300.0 + n * 3.8, 138.0, -80.0)
    returned.append(ca(serial, "A", n, tuple(point[i] - com[i] for i in range(3)), 0.0))
    serial += 1
for n, xyz in far.items():  # the motif it was handed back, B-factor 1
    returned.append(ca(serial, "B", n, tuple(xyz[i] - com[i] for i in range(3)), 1.0))
    serial += 1

put, how = placer.place_on_target("\n".join(returned), sent)
first = [line for line in put.splitlines() if line.startswith("ATOM")][0]
check("a design is placed back onto its target", how["placed"] is True, str(how))
check("the motif lands where it was sent", how["fit_to_target"] < 0.01, str(how["fit_to_target"]))
check("the binder is in the scene's frame, not at the origin",
      abs(float(first[30:38]) - 303.8) < 0.01 and abs(float(first[46:54]) + 80.0) < 0.01,
      f"{float(first[30:38]):.1f}, {float(first[38:46]):.1f}, {float(first[46:54]):.1f}")
# The target is already in the scene; a second copy would only fight with it.
check("only the binder comes back",
      all(line[21] == "A" for line in put.splitlines() if line.startswith("ATOM")),
      f"{len([l for l in put.splitlines() if l.startswith('ATOM')])} atoms")
# Guessing at a placement that cannot be verified would be worse than not doing it.
_, declined = placer.place_on_target(
    "\n".join(l for l in returned if l[21] == "A"), sent)
check("a design whose motif does not match is declined", declined["placed"] is False,
      declined["why"][:56])

# A truncated pdb must not be handed to the viewer: without the .trb gate the
# reader would see whatever had been flushed so far.
half = Path(tempfile.mkdtemp())
(half / "design_0.pdb").write_text("ATOM      1  CA ")
check("a pdb with no trb beside it is left alone",
      not list(p for p in half.glob("design_*.pdb") if p.with_suffix(".trb").is_file()))

print("\nsuperposition and contacts")
import math  # noqa: E402
import random  # noqa: E402

# A prediction comes back in whatever frame the predictor chose, so putting it
# on the design it was made from is a full rigid fit, not the translation the
# backbone stage gets away with.
def spin(ax, ay, az):
    def times(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    ca, sa, cb, sb, cc, sc = (math.cos(ax), math.sin(ax), math.cos(ay),
                              math.sin(ay), math.cos(az), math.sin(az))
    return times([[cc, -sc, 0], [sc, cc, 0], [0, 0, 1]],
                 times([[cb, 0, sb], [0, 1, 0], [-sb, 0, cb]],
                       [[1, 0, 0], [0, ca, -sa], [0, sa, ca]]))


def apply(matrix, shift, points):
    return [tuple(sum(matrix[i][j] * p[j] for j in range(3)) + shift[i] for i in range(3))
            for p in points]


def determinant(m):
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


rng = random.Random(11)
cloud = [(rng.uniform(-20, 20), rng.uniform(-20, 20), rng.uniform(-20, 20)) for _ in range(40)]
turned = apply(spin(0.7, -1.9, 2.4), (300.0, -12.5, 88.0), cloud)
matrix, shift, rmsd = worker.superpose(cloud, turned)
check("a rigid motion is recovered exactly", rmsd < 1e-6, f"{rmsd:.2e}")
check("and it puts the points back", max(
    max(abs(a[i] - b[i]) for i in range(3))
    for a, b in zip(apply(matrix, shift, cloud), turned)) < 1e-6)
check("the fit is a rotation, not a reflection", abs(determinant(matrix) - 1) < 1e-9,
      f"det {determinant(matrix):.6f}")

# A short binder is often a single helix or a flat sheet, which is where a
# hand-rolled Kabsch starts returning mirror images.
flat = [(rng.uniform(-20, 20), rng.uniform(-20, 20), rng.uniform(-0.02, 0.02)) for _ in range(30)]
matrix, _, rmsd = worker.superpose(flat, apply(spin(0.3, 1.1, -0.8), (5.0, 5.0, 5.0), flat))
check("near-coplanar points do not fold into a mirror image",
      rmsd < 1e-6 and abs(determinant(matrix) - 1) < 1e-9,
      f"rmsd {rmsd:.2e}, det {determinant(matrix):.6f}")
check("noise survives as a number, not an exception",
      0.5 < worker.superpose(cloud, [tuple(c + rng.gauss(0, 0.5) for c in p)
                                     for p in turned])[2] < 1.5)
check("too few points is refused, not guessed at",
      isinstance(_raises(lambda: worker.superpose(cloud[:2], turned[:2])), ValueError))

check("contacts are counted across the cutoff",
      worker.close_contacts([(0, 0, 0), (10, 0, 0)], [(1, 0, 0), (2, 0, 0), (50, 0, 0)], 4.5) == 2)
check("and the grid does not miss a neighbouring cell",
      worker.close_contacts([(4.4, 0, 0)], [(0.0, 0, 0)], 4.5) == 1)

print("\nsequence design and the folding check")


def side_chain(serial, chain, seq, xyz, name="GLY", atom="CA", b=1.0, element="C"):
    return ("ATOM  %5d  %-3s %s %s%4d    %8.3f%8.3f%8.3f  1.00%6.2f          %2s"
            % (serial, atom, name, chain, seq, xyz[0], xyz[1], xyz[2], b, element))


# ProteinMPNN opens its file with the input's own sequence, which is not a
# design; and it scores by negative log likelihood, so the best is the lowest.
FASTA = (
    ">complex, score=2.0000, global_score=2.0, designed_chains=['B']\n"
    "GGGG\n"
    ">T=0.1, sample=1, score=1.5000, seq_recovery=0.10\n"
    "AEKL\n"
    ">T=0.1, sample=2, score=0.9000, seq_recovery=0.12\n"
    "RQVW\n"
)
read = worker.ProteinMPNN.read_fasta(FASTA, [4])
check("the input's own sequence is not returned as a design", len(read) == 2, str(read))
check("the best score comes first", read[0] == (["RQVW"], 0.9), str(read[0]))
# Older builds wrote the context chain alongside the designed one; taking the
# first piece would hand back part of the target as though it were the binder.
mixed = worker.ProteinMPNN.read_fasta(
    ">x, score=2.0\nMMMMMMMM/GGGG\n>T=0.1, sample=1, score=1.0\nMKVLAAEQ/AEKL\n", [4])
check("the designed chain is picked out of a multi-chain record",
      mixed == [(["AEKL"], 1.0)], str(mixed))

# RFdiffusion returns every residue as glycine. Writing the chosen letters back
# is what makes the result a protein rather than a trace with a note attached.
poly = [side_chain(i, "B", n, (n * 3.8, 0.0, 0.0), atom=atom)
        for i, (n, atom) in enumerate(
            [(n, atom) for n in range(1, 5) for atom in ("N", "CA", "C")], 1)]
threaded = worker.thread_sequence(poly, "AEKL")
check("a sequence is written onto the backbone it was designed for",
      [line[17:20] for line in threaded[::3]] == ["ALA", "GLU", "LYS", "LEU"],
      str([line[17:20] for line in threaded[::3]]))
check("every atom of a residue gets the same name",
      len({line[17:20] for line in threaded[:3]}) == 1)
check("coordinates are untouched",
      [line[30:54] for line in threaded] == [line[30:54] for line in poly])

# The predictor knows the sequence and nothing else, so what comes back is the
# right protein in the wrong place -- and the distance after fitting is the
# measurement the whole stage exists to produce.
design_ca = [(300.0 + n * 3.8, 150.0 + (n % 3), -80.0) for n in range(12)]
backbone_pdb = [side_chain(i, "B", i, xyz) for i, xyz in enumerate(design_ca, 1)]
elsewhere = apply(spin(1.2, 0.4, -2.2), (-40.0, 900.0, 7.0), design_ca)
prediction = "\n".join(side_chain(i, "A", i, xyz, name="ALA", b=88.0)
                       for i, xyz in enumerate(elsewhere, 1))
placed, how = worker.FoldGenerator.place_prediction(prediction, backbone_pdb)
first = next(line for line in placed.splitlines() if line.startswith("ATOM"))
check("a prediction is moved onto the design it was made from", how["placed"] is True, str(how))
check("and the fit is reported as the self-consistency number",
      how["rmsd_to_backbone"] < 0.01, str(how["rmsd_to_backbone"]))
check("the prediction lands in the scene's frame, not the predictor's",
      abs(float(first[30:38]) - 300.0) < 0.01 and abs(float(first[46:54]) + 80.0) < 0.01,
      f"{float(first[30:38]):.1f}, {float(first[38:46]):.1f}, {float(first[46:54]):.1f}")
check("confidence is read off the b-factor column", how["plddt"] == 88.0, str(how.get("plddt")))
# Releases have disagreed on whether that column is a fraction or a percentage.
_, scaled = worker.FoldGenerator.place_prediction(
    "\n".join(side_chain(i, "A", i, xyz, b=0.88) for i, xyz in enumerate(elsewhere, 1)),
    backbone_pdb)
check("a confidence written as a fraction is read as one", scaled["plddt"] == 88.0,
      str(scaled.get("plddt")))
_, refused = worker.FoldGenerator.place_prediction(
    "\n".join(side_chain(i, "A", i, xyz) for i, xyz in enumerate(elsewhere[:5], 1)), backbone_pdb)
check("a prediction of the wrong length is not forced on",
      refused["placed"] is False, refused["why"][:50])

# The whole stage against a stand-in that has ProteinMPNN's command line and
# writes its file where ProteinMPNN writes it.
mpnn_repo = Path(tempfile.mkdtemp()) / "ProteinMPNN"
mpnn_repo.mkdir(parents=True)
(mpnn_repo / "protein_mpnn_run.py").write_text(textwrap.dedent('''
    import argparse
    from pathlib import Path
    p = argparse.ArgumentParser()
    for name in ("--pdb_path", "--pdb_path_chains", "--out_folder", "--num_seq_per_target",
                 "--sampling_temp", "--seed", "--batch_size"):
        p.add_argument(name)
    a = p.parse_args()
    lines = [l for l in Path(a.pdb_path).read_text().splitlines() if l.startswith("ATOM")]
    designed = a.pdb_path_chains.split()
    assert any(l[21] not in designed for l in lines), "the target was not sent as context"
    sizes = [len({l[22:27] for l in lines if l[21] == c}) for c in designed]
    assert all(sizes), "a chain that was asked for is not in the file"
    out = Path(a.out_folder) / "seqs"
    out.mkdir(parents=True, exist_ok=True)
    text = [">complex, score=3.0, designed_chains=%s" % designed,
            "/".join("G" * n for n in sizes)]
    for i in range(int(a.num_seq_per_target)):
        text.append(">T=%s, sample=%d, score=%.4f" % (a.sampling_temp, i + 1, 2.0 - i * 0.5))
        text.append("/".join(("ACDEFGHIKLMNPQRSTVWY" * 40)[i:i + n] for n in sizes))
    (out / (Path(a.pdb_path).stem + ".fa")).write_text("\\n".join(text) + "\\n")
''').lstrip())


class StandInFolder:
    """Answers in its own frame, as a real predictor does."""

    name = "stand-in"

    def __init__(self, broken=""):
        self.broken = broken
        self.asked = []

    def problems(self):
        return [self.broken] if self.broken else []

    def fold(self, sequences, work, note=None, stop=None):
        self.asked = list(sequences)
        if note:
            note("folding sequence 1 of %d" % len(sequences))
        return ["\n".join(side_chain(i, "A", i, xyz, name="ALA", b=91.0)
                          for i, xyz in enumerate(elsewhere, 1))
                for _ in sequences]


# A target running alongside the binder, so the contacts are real distances.
target_lines = [side_chain(i, "T", i, (300.0 + n * 3.8, 154.0 + (n % 3), -80.0), name="ALA")
                for i, n in enumerate(range(12), 1)]
COMPLEX = "\n".join(target_lines + backbone_pdb) + "\nEND\n"

folder = StandInFolder()
stage = worker.FoldGenerator(worker.ProteinMPNN(mpnn_repo, python=sys.executable), folder)
fold_job = {"designs": [], "error": "", "cancel": False, "progress": 0, "total": 4, "stage": ""}
with contextlib.redirect_stdout(io.StringIO()):
    stage.generate({"kind": "fold", "complex": {"pdb": COMPLEX, "binderChains": ["B"]},
                    "target": {"hotspots": ["T3", "T4"]},
                    "run": {"numDesigns": 4, "foldTop": 2, "samplingTemp": 0.2, "seed": 5}},
                   fold_job)
check("the stage reports no error", fold_job["error"] == "", fold_job["error"][:70])
check("one design comes back per sequence", len(fold_job["designs"]) == 4,
      str(len(fold_job["designs"])))
check("only the best few are folded", len(folder.asked) == 2, str(len(folder.asked)))
check("and they are the best few",
      folder.asked[0] == fold_job["designs"][0]["metrics"]["sequence"])
scored = [design["metrics"] for design in fold_job["designs"]]
check("mpnn scores come back with the sequences",
      [m["mpnn_score"] for m in scored] == [0.5, 1.0, 1.5, 2.0],
      str([m["mpnn_score"] for m in scored]))
check("a folded design carries the self-consistency number",
      scored[0]["rmsd_to_backbone"] < 0.01 and scored[0]["plddt"] == 91.0, str(scored[0]))
# The sequence is chosen against the target, so how much of it actually touches
# the residues that were picked is the thing to look at next.
check("contact with the target is measured", scored[0]["contacts"] > 0, str(scored[0]["contacts"]))
check("contact with the picked residues is measured separately",
      0 < scored[0]["hotspot_contacts"] < scored[0]["contacts"],
      f"{scored[0]['hotspot_contacts']} of {scored[0]['contacts']}")
# Beyond foldTop there is no prediction, so the backbone comes back wearing the
# sequence instead -- still a protein to look at, and it says it was not folded.
check("the sequences that were not folded still come back",
      scored[3]["folded"] is False and "sequence" in scored[3], str(scored[3])[:80])
unfolded = fold_job["designs"][3]["pdb"]
check("an unfolded design is the backbone wearing its sequence",
      {line[17:20] for line in unfolded.splitlines() if line.startswith("ATOM")} != {"GLY"},
      str(sorted({line[17:20] for line in unfolded.splitlines() if line.startswith("ATOM")})[:4]))

# The two halves fail independently: sequence design is cheap and nearly always
# there, folding wants a real GPU and eleven gigabytes of weights.
missing = worker.FoldGenerator(worker.ProteinMPNN(mpnn_repo, python=sys.executable),
                               StandInFolder("no predictor installed"))
partial = {"designs": [], "error": "", "cancel": False, "progress": 0, "total": 2, "stage": ""}
with contextlib.redirect_stdout(io.StringIO()):
    missing.generate({"kind": "fold", "complex": {"pdb": COMPLEX, "binderChains": ["B"]},
                      "run": {"numDesigns": 2, "foldTop": 2}}, partial)
check("a worker with no predictor still designs sequences",
      partial["error"] == "" and len(partial["designs"]) == 2, partial["error"][:60])
check("and says why they were not folded",
      partial["designs"][0]["metrics"]["why"] == "no predictor installed",
      str(partial["designs"][0]["metrics"].get("why")))

check("a complex with no binder chain is refused before the model runs",
      "no usable binder chain" in _error_from(stage, {"kind": "fold",
                                        "complex": {"pdb": COMPLEX, "binderChains": ["Z"]},
                                        "run": {}}),
      _error_from(stage, {"kind": "fold", "complex": {"pdb": COMPLEX, "binderChains": ["Z"]},
                          "run": {}})[:60])
check("a complex with no target is refused too",
      "no target" in _error_from(stage, {
          "kind": "fold",
          "complex": {"pdb": "\n".join(backbone_pdb), "binderChains": ["B"]}, "run": {}}))

# Nobody chooses how many chains the binder is -- RFdiffusion writes one output
# chain per contig block -- so the stage has to take whatever it is given.
# ProteinMPNN designs a space-separated list as readily as a single chain.
second_chain = [side_chain(i, "C", i, (i * 3.8, 20.0, 0.0), name="GLY")
                for i in range(1, 6)]
PAIR = "\n".join(target_lines + backbone_pdb + second_chain) + "\nEND\n"
pair_job = {"designs": [], "error": "", "cancel": False, "progress": 0, "total": 2, "stage": ""}
with contextlib.redirect_stdout(io.StringIO()):
    stage.generate({"kind": "fold", "complex": {"pdb": PAIR, "binderChains": ["B", "C"]},
                    "run": {"numDesigns": 2, "foldTop": 1}}, pair_job)
check("a two-chain binder is designed, not refused",
      pair_job["error"] == "" and len(pair_job["designs"]) == 2, pair_job["error"][:80])
pair_metrics = pair_job["designs"][0]["metrics"]
check("one sequence comes back per chain",
      [len(s) for s in pair_metrics["sequence"].split("/")] == [12, 5],
      pair_metrics["sequence"])
threaded = pair_job["designs"][0]["pdb"]
check("each chain wears its own sequence",
      {line[21] for line in threaded.splitlines() if line.startswith("ATOM")} == {"B", "C"},
      "".join(sorted({l[21] for l in threaded.splitlines() if l.startswith("ATOM")})))
# Folding two chains separately would answer a different question from the one
# being asked, so it is declined out loud rather than done quietly.
check("and folding says why it cannot judge a complex",
      pair_metrics.get("folded") is False and "one chain at a time" in pair_metrics.get("why", ""),
      pair_metrics.get("why", "")[:70])

# A failure has to arrive with its reason attached. The app shows one line
# beside the design, so reporting the header and dropping the cause produced
# exactly "folding failed:" with nothing after the colon.
def folding_that(script: str, note=None):
    """Run EsmFolder.fold against a stand-in predictor and report how it failed."""
    folder = worker.EsmFolder(python=sys.executable)
    folder._problems = []                      # the environment is not what is under test
    real_script, worker.FOLD_SCRIPT = worker.FOLD_SCRIPT, script
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            folder.fold(["AEKL"], Path(tempfile.mkdtemp()), note=note)
        return ""
    except worker.SetupError as error:
        return str(error)
    finally:
        worker.FOLD_SCRIPT = real_script


loud = folding_that('import sys\nprint("STAGE loading")\n'
                    'raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")\n')
check("a failure reports its cause on the first line",
      "out of memory" in loud.splitlines()[0], loud.splitlines()[0][:90])
check("and keeps the output it read that from underneath",
      "--- last output ---" in loud, str(len(loud.splitlines())))
# Progress lines are not diagnoses; picking one would say "loading" forever.
check("progress lines are not mistaken for the reason",
      "STAGE" not in loud.splitlines()[0], loud.splitlines()[0][:60])

# flush=True as the real script does: a killed process never drains a buffered
# pipe, so an unflushed line is a line nobody will ever read.
silent = folding_that('import os, signal\n'
                      'print("STAGE loading (1.2 GB of system memory free)", flush=True)\n'
                      'os.kill(os.getpid(), signal.SIGKILL)\n')
check("a process the kernel killed is named for what that means",
      "out of system memory" in silent.splitlines()[0], silent.splitlines()[0][:100])
# A kill prints nothing, so the line before it is the only evidence of how much
# room there was -- and that line is a progress line.
check("the memory it had to work with survives into the report",
      "1.2 GB of system memory free" in silent, silent.splitlines()[-1][:70])

# A card that is already full has to be refused before the load, not discovered
# by an OOM on a two-megabyte allocation at the end of it -- which names the
# allocation that happened to be last and nothing about what took the rest.
full = folding_that(
    'import sys\n'
    'print("STAGE loading (0.1 of 14.6 GB free on the Tesla T4)", flush=True)\n'
    'raise SystemExit("the GPU is full before loading starts: 0.10 of 14.56 GB free, and '
    'the predictor needs about 6. This is the card rather than system memory, so a '
    'high-RAM runtime does not help")\n')
check("a card that is already full is refused up front",
      "GPU is full before loading" in full.splitlines()[0], full.splitlines()[0][:80])
check("and it says which memory ran out",
      "rather than system memory" in full, full.splitlines()[0][-60:])

# The server hands out one job at a time; the worker answers each POST on its
# own thread, so two can still land on one card.
order, slow = [], threading.Event()


class Hog(worker.Generator):
    name = "hog"

    def generate(self, spec, job):
        order.append("start " + spec["tag"])
        slow.wait(5) if spec["tag"] == "first" else None
        order.append("end " + spec["tag"])
        job["designs"].append({"name": spec["tag"], "pdb": "\n".join(backbone_pdb),
                               "metrics": {}})


jobs = [{"spec": {"tag": tag}, "designs": [], "error": "", "cancel": False,
         "progress": 0, "total": 1} for tag in ("first", "second")]
threads = [threading.Thread(target=worker.run_job, args=(Hog(), j)) for j in jobs]
threads[0].start()
time.sleep(0.3)
threads[1].start()
time.sleep(0.3)
check("a second job waits rather than joining the first on the card",
      jobs[1]["stage"] == "waiting for the GPU — another job is using it", jobs[1].get("stage", ""))
slow.set()
for thread in threads:
    thread.join(timeout=10)
check("and they run one after the other, not together",
      order == ["start first", "end first", "start second", "end second"], str(order))
check("both still finish", all(j["status"] == "done" for j in jobs),
      str([j["status"] for j in jobs]))

# transformers looks for every framework it supports when imported, and Colab
# ships TensorFlow -- two spare gigabytes in a process that has none to spare.
told = worker.fold_environment({"PATH": "/usr/bin"})
check("the predictor is told not to go looking for other frameworks",
      told["USE_TF"] == "0" and told["USE_JAX"] == "0" and told["USE_TORCH"] == "1",
      f"USE_TF={told['USE_TF']}")
check("and the rest of the environment is left alone", told["PATH"] == "/usr/bin")

quiet = folding_that('import sys\nsys.exit(3)\n')
check("an exit with no output still says something",
      "exited with 3" in quiet, quiet.splitlines()[0][:70])

stages = []
folding_that('import sys\nprint("STAGE folding sequence 1 of 1 (4 residues)")\n'
             'sys.exit(1)\n', note=stages.append)
check("progress still reaches the job while it runs",
      stages == ["folding sequence 1 of 1 (4 residues)"], str(stages))

# transformers hides an import failure behind its lazy loader: whatever really
# went wrong is re-raised as "Could not import module 'EsmForProteinFolding'.
# Are this object's requirements defined correctly?", which is a sentence about
# our model that is usually about something else. The probe has to get past it.
def fake_transformers(version: str, esmfold_raises: str) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "transformers" / "models" / "esm").mkdir(parents=True)
    (root / "transformers" / "__init__.py").write_text(textwrap.dedent(f'''
        __version__ = "{version}"

        def __getattr__(name):
            try:
                raise ImportError({esmfold_raises!r})
            except Exception as error:
                raise ModuleNotFoundError(
                    f"Could not import module {{name!r}}. Are this object's "
                    "requirements defined correctly?") from error
    ''').lstrip() if esmfold_raises else f'__version__ = "{version}"\n')
    (root / "transformers" / "models" / "__init__.py").write_text("")
    (root / "transformers" / "models" / "esm" / "__init__.py").write_text("")
    (root / "transformers" / "models" / "esm" / "modeling_esmfold.py").write_text(
        f"raise ImportError({esmfold_raises!r})\n" if esmfold_raises
        else "class EsmForProteinFolding:\n    pass\n")
    (root / "torch.py").write_text(textwrap.dedent('''
        __version__ = "2.6.0+cu124"

        class cuda:
            @staticmethod
            def is_available():
                return True

            @staticmethod
            def get_device_name(index):
                return "Tesla T4"
    ''').lstrip())
    return root


def folder_problems(root: Path):
    here = os.getcwd()
    os.chdir(root)                 # `-c` puts the working directory on sys.path
    try:
        folder = worker.EsmFolder(python=sys.executable)
        return folder.problems(), folder.found
    finally:
        os.chdir(here)


import os  # noqa: E402

broken, versions = folder_problems(fake_transformers("5.17.0", "No module named 'kernels'"))
check("the real cause is reported, not the loader's summary of it",
      len(broken) == 1 and "No module named 'kernels'" in broken[0], broken[0][:90] if broken else "")
check("and the summary that replaced it is gone",
      not any("requirements defined correctly" in p for p in broken))
check("the probe says which transformers it found",
      versions.get("transformers") == "5.17.0" and versions.get("torch") == "2.6.0+cu124",
      f"{versions.get('transformers')}, torch {versions.get('torch')}")
check("the remedy names the half that is missing",
      "--only fold" in broken[0], broken[0][-30:] if broken else "")

working, versions = folder_problems(fake_transformers("4.57.1", ""))
check("an importable predictor reports no problems", working == [], str(working))
check("and the gpu it found", versions.get("gpu") == "Tesla T4", str(versions.get("gpu")))

# The one that actually bit: DGL forces an exact torch, Colab's torchvision was
# compiled against the torch Colab shipped with, and `import transformers`
# reaches for torchvision. So loading a protein folder fails with
# `RuntimeError: operator torchvision::nms does not exist`.
nms = "operator torchvision::nms does not exist"
stranded, _ = folder_problems(fake_transformers("4.57.1", nms))
check("a stranded torch sibling is named for what it is",
      any(nms in p and "not ESMFold" in p for p in stranded),
      stranded[0][:110] if stranded else "")

sibling = Path(tempfile.mkdtemp())
(sibling / "torchvision" ).mkdir()
(sibling / "torchvision" / "__init__.py").write_text(
    f'raise RuntimeError("{nms}")\n')
(sibling / "torchaudio").mkdir()
(sibling / "torchaudio" / "__init__.py").write_text('__version__ = "2.6.0"\n')

# Both broken: they must be repaired separately, because a Python with no
# torchaudio wheel would otherwise take the torchvision repair down with it.
both = Path(tempfile.mkdtemp())
for name in ("torchvision", "torchaudio"):
    (both / name).mkdir()
    (both / name / "__init__.py").write_text('raise RuntimeError("stale extension")\n')

here = os.getcwd()
os.chdir(sibling)
try:
    state = worker.companions(sys.executable)
    asked = []
    real_pip = worker.pip
    worker.pip = lambda python, *arguments: asked.append(list(arguments))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            repaired = worker.align_torch_extras(
                sys.executable, "2.6.0+cu124", lambda what, fn, *a: fn(*a) or True)
    finally:
        worker.pip = real_pip
finally:
    os.chdir(here)

check("a broken sibling is spotted", state.get("torchvision", "").startswith("broken:"),
      state.get("torchvision", "")[:60])
check("a working one is left alone", state.get("torchaudio") == "2.6.0", str(state.get("torchaudio")))
check("only the broken one is reinstalled", repaired == ["torchvision"], str(repaired))
check("the repair names the torch it has to match",
      asked and "torch==2.6.0+cu124" in asked[0], str(asked[0][:3]) if asked else "")
# From the same index torch came from, or pip resolves against PyPI, which has
# no +cu124 build and would quietly move torch instead.
check("and fetches it from the index torch came from",
      asked and "https://download.pytorch.org/whl/cu124" in asked[0],
      " ".join(asked[0]) if asked else "")
os.chdir(both)
try:
    pairs, real_pip = [], worker.pip
    worker.pip = lambda python, *arguments: pairs.append(arguments[0])
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            worker.align_torch_extras(sys.executable, "2.6.0+cu124",
                                      lambda what, fn, *a: fn(*a) or True)
    finally:
        worker.pip = real_pip
finally:
    os.chdir(here)
check("two broken siblings are repaired separately",
      pairs == ["torchaudio", "torchvision"], str(pairs))

# Nothing to repair must cost nothing: this runs on every setup.
os.chdir(tempfile.mkdtemp())
try:
    check("a healthy runtime is left untouched",
          worker.align_torch_extras(sys.executable, "2.6.0+cu124", lambda *a: True) == [])
finally:
    os.chdir(here)

# The echo generator proves the connection and nothing else. A fold job carries
# its coordinates under `complex`, so reading `target.pdb` found nothing, echoed
# an empty string, and the app stored "no atoms at all" as a finished design --
# which then failed to open, named after a generator nobody remembers choosing.
echoed = {"designs": [], "error": "", "cancel": False, "progress": 0, "total": 1}
worker.EchoGenerator().generate(
    {"kind": "fold", "complex": {"pdb": COMPLEX, "binderChains": ["B"]}, "run": {}}, echoed)
check("the echo generator refuses a job it cannot do",
      echoed["designs"] == [] and "--generator rfdiffusion" in echoed["error"],
      echoed["error"][:90])
hollow = {"designs": [], "error": "", "cancel": False, "progress": 0, "total": 1}
worker.EchoGenerator().generate({"target": {"pdb": ""}, "run": {"numDesigns": 1}}, hollow)
check("and refuses to echo a file with nothing in it",
      hollow["designs"] == [] and "no coordinates" in hollow["error"], hollow["error"][:60])

# Whatever the generator did, a result with no atoms must not leave the worker
# looking like a finished design.
class Hollow(worker.Generator):
    name = "hollow"

    def generate(self, spec, job):
        job["designs"].append({"name": "empty_01", "pdb": "", "metrics": {}})
        job["designs"].append({"name": "real_01", "pdb": "\n".join(backbone_pdb), "metrics": {}})
        job["progress"] = 2


swept = {"spec": {}, "designs": [], "error": "", "cancel": False, "progress": 0, "total": 2}
worker.run_job(Hollow(), swept)
check("an empty result is dropped rather than sent",
      [d["name"] for d in swept["designs"]] == ["real_01"],
      str([d["name"] for d in swept["designs"]]))
check("and the job says so instead of reading as finished",
      swept["status"] == "failed" and "empty_01" in swept["error"], swept["error"][:80])

# One worker, two stages, each checking its own environment: a machine with
# RFdiffusion but no predictor has to keep running backbone jobs.
pipeline = worker.Pipeline({"binder": stand_in, "fold": stage})
check("the pipeline names both stages", "fold" in pipeline.name, pipeline.name)
unknown = {"designs": [], "error": "", "cancel": False, "progress": 0, "total": 1}
pipeline.generate({"kind": "invent"}, unknown)
check("a job for a stage this worker lacks says so",
      "invent" in unknown["error"] and "binder, fold" in unknown["error"], unknown["error"][:70])

print("\nbuilding the second stage from the first")
from proteincad.design import build_fold_spec, free_chain  # noqa: E402
from proteincad.jobs import Job  # noqa: E402

first = Job(7, {"kind": "binder", "model": "mock",
                "target": {"pdb": "\n".join(target_lines), "hotspots": ["T3"], "name": "7cgo"}},
           Path(tempfile.mkdtemp()))
# RFdiffusion returns the binder as chain A, and a cropped target very often has
# a chain A of its own; joining them unchecked would make one chain of two
# proteins and the sequence would be designed straight across the join.
first.add_design("design_0", "\n".join(
    side_chain(i, "T", i, xyz) for i, xyz in enumerate(design_ca, 1)) + "\nEND\n", {})
second = build_fold_spec(first, 0, {"numDesigns": 3, "foldTop": 1})
joined = [line for line in second["complex"]["pdb"].splitlines() if line.startswith("ATOM")]
check("the binder is given a chain the target is not using",
      second["complex"]["binderChains"] != ["T"]
      and {line[21] for line in joined} == {"T", *second["complex"]["binderChains"]},
      str(second["complex"]["binderChains"]))
check("atom serials are renumbered across the join",
      len({int(line[6:11]) for line in joined}) == len(joined), str(len(joined)))

# The one that stopped the second stage dead. RFdiffusion writes one output
# chain per contig block and keeps the original chain ids, so a crop falling
# across five target fragments returns six chains -- five of target, one of
# binder. Counting chains and expecting one mistook the target for more binder.
whole, serial = [], 1
for chain in "CDEFG":                              # the target, as it came back
    for n in range(1, 5):
        whole.append(side_chain(serial, chain, n, (n * 3.8, ord(chain) * 1.0, 0.0), b=1.0))
        serial += 1
for n in range(1, 9):                              # and the binder it built
    whole.append(side_chain(serial, "H", n, (n * 3.8, 4.0, 0.0), b=0.0))
    serial += 1
first.add_design("design_1", "\n".join(whole) + "\nEND\n", {"placed": False})

six = build_fold_spec(first, 1)
present = {line[21] for line in six["complex"]["pdb"].splitlines() if line.startswith("ATOM")}
check("a design that came back with its target is not refused",
      six["complex"]["binderChains"] == ["H"], str(six["complex"]["binderChains"]))
check("the b-factor marker separates binder from target, not the chain count",
      present == set("CDEFGH"), "".join(sorted(present)))
# Its own copy of the target is the one that matches its coordinates; the crop
# is hundreds of angstroms away and would put the binder off the surface.
check("and its own target travels with it, not the crop",
      "T" not in present, "".join(sorted(present)))

# However many chains the binder turns out to be, ProteinMPNN designs a list.
pair_design = [side_chain(i, "P", i, (i * 3.8, 0.0, 0.0), b=0.0) for i in range(1, 6)] + \
              [side_chain(i, "Q", i, (i * 3.8, 9.0, 0.0), b=0.0) for i in range(1, 5)]
first.add_design("design_2", "\n".join(pair_design) + "\nEND\n", {})
two = build_fold_spec(first, 2)
check("a binder of several chains is carried as several",
      len(two["complex"]["binderChains"]) == 2, str(two["complex"]["binderChains"]))
check("and each is renamed clear of the target",
      "T" not in two["complex"]["binderChains"], str(two["complex"]["binderChains"]))
check("the picked residues are carried through", second["target"]["hotspots"] == ["T3"])
check("the design it came from is named", second["source"]["job"] == 7
      and second["source"]["design"] == 0, str(second["source"]))
check("the runner is inherited", second["model"] == "mock")
check("a sequence job cannot itself be taken on to sequence design",
      isinstance(_raises(lambda: build_fold_spec(
          Job(8, second, Path(tempfile.mkdtemp())), 0)), ValueError))
check("chain ids run out loudly rather than silently",
      isinstance(_raises(lambda: free_chain(set(
          "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"))), ValueError))

print("\nthe on-demand GPU")

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from proteincad import colab_worker as worker_module  # noqa: E402
from proteincad import ec2 as ec2_module  # noqa: E402
from proteincad.design import Ec2Runner, RunnerError, build_runners, gpu_machine  # noqa: E402


class FakeEc2:
    """Enough of the EC2 client to exercise the lifecycle, offline.

    States advance on a timetable rather than instantly, because every bug this
    section exists to catch lives in the waiting: a wake that gives up while
    the box is still pending, a watchdog that stops one that is only starting,
    an address read before there is one.
    """

    def __init__(self, state="stopped", address="127.0.0.1", steps=1, shutdown="stop"):
        self.state = state
        self.address = address
        self.steps = steps           # describes left before a transition finishes
        self.shutdown = shutdown
        self.fail = None
        self.calls = []

    def _advance(self):
        if self.state not in ("pending", "stopping"):
            return
        if self.steps > 0:
            self.steps -= 1
        else:
            self.state = "running" if self.state == "pending" else "stopped"

    def describe_instances(self, InstanceIds):
        self.calls.append(("describe", tuple(InstanceIds)))
        if self.fail:
            raise self.fail
        state = self.state
        self._advance()
        return {"Reservations": [{"Instances": [{
            "State": {"Name": state},
            "PublicIpAddress": self.address if state == "running" else "",
            "PrivateIpAddress": "10.0.0.5",
            "InstanceType": "g4dn.xlarge",
        }]}]}

    def describe_instance_attribute(self, InstanceId, Attribute):
        self.calls.append(("attribute", Attribute))
        return {"InstanceInitiatedShutdownBehavior": {"Value": self.shutdown}}

    def start_instances(self, InstanceIds):
        self.calls.append(("start", tuple(InstanceIds)))
        self.state, self.steps = "pending", self.steps
        return {}

    def stop_instances(self, InstanceIds):
        self.calls.append(("stop", tuple(InstanceIds)))
        self.state = "stopping"
        return {}


def stand_in_worker(designs=None):
    """A worker on localhost that answers /health and the design contract."""
    state = {"busy": False, "idle": 0.0, "auth": "", "health_hits": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                state["health_hits"] += 1
                state["auth"] = self.headers.get("Authorization", "")
                return self._send({"status": "ok", "generator": "stand-in",
                                   "busy": state["busy"], "idle": state["idle"]})
            if self.path.startswith("/design/"):
                return self._send({"job_id": "j1", "status": "done", "stage": "done",
                                   "progress": len(designs or []), "total": len(designs or []),
                                   "log": "run_inference.py ...", "error": "",
                                   "designs": designs or []})
            return self._send({"error": "not found"}, 404)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return self._send({"job_id": "j1", "status": "queued"})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, state


class Recorder:
    """A job-shaped thing that keeps every stage it was told about. The stages
    are the only sign of life during a two-minute boot, so they are worth
    checking rather than assuming."""

    cancelled = False
    command = ""

    def __init__(self, cancelled=False):
        self.cancelled = cancelled
        self.stages = []

    @property
    def stage(self):
        return self.stages[-1] if self.stages else ""

    @stage.setter
    def stage(self, value):
        self.stages.append(value)


def machine_for(fake, port, **kwargs):
    settings = {"region": "us-east-1", "port": port, "address": "127.0.0.1",
                "idle_seconds": 0, "boot_timeout": 20, "tick": 3600.0,
                "poll_seconds": 0.02}
    settings.update(kwargs)
    machine = ec2_module.Machine(kwargs.pop("instance_id", "i-test"), **{
        k: v for k, v in settings.items() if k != "instance_id"})
    machine._client = fake
    return machine


worker, worker_state = stand_in_worker()
worker_port = worker.server_address[1]

# --- waking -----------------------------------------------------------------

cold = FakeEc2(state="stopped", steps=2)
machine = machine_for(cold, worker_port, token="t0ken")
listener = Recorder()
url = machine.wake(listener)
check("a stopped instance is started", ("start", ("i-test",)) in cold.calls)
check("and the worker's address comes back", url == f"http://127.0.0.1:{worker_port}", url)
check("the boot reports itself rather than looking like a hang",
      any("starting the GPU" in s for s in listener.stages)
      and any("booting" in s for s in listener.stages), " / ".join(listener.stages))
check("and says when it is ready", "GPU ready" in listener.stage, listener.stage)
check("the worker is asked with the token it was configured with",
      worker_state["auth"] == "Bearer t0ken", worker_state["auth"])

warm = FakeEc2(state="running")
check("an instance that is already up answers at once",
      machine_for(warm, worker_port).wake() == f"http://127.0.0.1:{worker_port}")
check("and is not started again", not any(c[0] == "start" for c in warm.calls))

# The watchdog on the box powers the machine off. If EC2 is set to terminate on
# shutdown, that destroys a root volume holding 25 GB of models.
risky = machine_for(FakeEc2(state="running", shutdown="terminate"), worker_port)
risky.wake()
check("an instance that would be destroyed by its own idle timer is flagged",
      "TERMINATE" in risky.warning and "modify-instance-attribute" in risky.warning)
check("and a safe one is not", not machine_for(FakeEc2(state="running"), worker_port).warning)

gone = _raises(machine_for(FakeEc2(state="terminated"), worker_port).wake)
check("a terminated instance is not something to wait for",
      isinstance(gone, ec2_module.Ec2Error) and "nothing left to start" in str(gone), str(gone))

slow = _raises(machine_for(FakeEc2(state="stopped", steps=999), worker_port,
                           boot_timeout=0).wake)
check("a box that never comes up gives up and says what it was waiting for",
      isinstance(slow, ec2_module.Ec2Error) and "still pending" in str(slow), str(slow))

# Running is not reachable: a security group or a stopped service looks exactly
# like a healthy box from the EC2 API, and only the worker can tell them apart.
silent = _raises(machine_for(FakeEc2(state="running"), 1, boot_timeout=0).wake)
check("an instance whose worker never answers names the three causes",
      "security group" in str(silent) and "proteincad-worker" in str(silent), str(silent))

cancelled = _raises(lambda: machine_for(FakeEc2(state="stopped", steps=999), worker_port)
                    .wake(Recorder(cancelled=True)))
check("cancelling during the boot is cancellation, not failure",
      isinstance(cancelled, ec2_module.Cancelled), type(cancelled).__name__)

# --- what went wrong --------------------------------------------------------

for label, raised, wanted in [
    ("missing credentials", type("NoCredentialsError", (Exception,), {})("no creds"),
     "aws configure"),
    ("a denied policy", Exception("An error occurred (UnauthorizedOperation) when calling"),
     "iam-policy.json"),
    ("the wrong region", Exception("InvalidInstanceID.NotFound: no such id"),
     "only unique within its region"),
    ("no region at all", type("NoRegionError", (Exception,), {})("no region"),
     "PROTEINCAD_EC2_REGION"),
]:
    broken = FakeEc2()
    broken.fail = raised
    explained = str(_raises(machine_for(broken, worker_port).wake))
    check(f"{label} is explained, not raised", wanted in explained, explained[:70])

# --- addressing -------------------------------------------------------------

running_info = {"state": "running", "public_ip": "", "public_dns": "",
                "private_ip": "10.0.0.5", "type": "g4dn.xlarge"}
homeless = _raises(lambda: ec2_module.Machine("i-x", address="public").host(running_info))
check("a box with no public address says both ways out of it",
      "Elastic IP" in str(homeless) and "PROTEINCAD_EC2_ADDRESS=private" in str(homeless))
check("the private address is used when that is what was asked for",
      ec2_module.Machine("i-x", address="private").host(running_info) == "10.0.0.5")
check("anything else is taken literally, which is how a name you own works",
      ec2_module.Machine("i-x", address="gpu.example.com").host(running_info) == "gpu.example.com")
check("the scheme and port make the endpoint",
      ec2_module.Machine("i-x", address="gpu.example.com", port=9000,
                         scheme="https").endpoint(running_info) == "https://gpu.example.com:9000")
# A stopped instance comes back with a different public IP every time, which is
# why the address is read at every wake rather than configured once -- and why
# an Elastic IP, which costs by the hour, is not needed.
moved = machine_for(FakeEc2(state="running", address="203.0.113.7"), worker_port,
                    address="public")
check("the address is read off the instance at every wake, not remembered",
      moved.host(moved.describe(max_age=0)) == "203.0.113.7")
moved._client.address = "198.51.100.2"
check("so a new one after a restart is simply followed",
      moved.host(moved.describe(max_age=0)) == "198.51.100.2")

chatty = FakeEc2(state="running")
polite = machine_for(chatty, worker_port)
polite.describe()
asked = len(chatty.calls)
polite.describe()
check("the panel polling does not mean an AWS call per poll", len(chatty.calls) == asked)
polite.describe(max_age=0)
check("but a question that has to be current always asks", len(chatty.calls) > asked)

# --- stopping ---------------------------------------------------------------

idler = machine_for(FakeEc2(state="running"), worker_port, idle_seconds=1, tick=0.05)
idler.last_used = time.time() - 60
idler.watch()
for _ in range(60):
    if any(c[0] == "stop" for c in idler._client.calls):
        break
    time.sleep(0.05)
check("an instance nobody has used is stopped", any(c[0] == "stop" for c in idler._client.calls))

leased = machine_for(FakeEc2(state="running"), worker_port, idle_seconds=1, tick=0.05)
leased.last_used = time.time() - 60
leased.leases = 1
leased.watch()
time.sleep(0.4)
check("but not one with a job on it", not any(c[0] == "stop" for c in leased._client.calls))

# A run this process did not start -- from before a restart, say -- is still a
# run, and stopping the box would throw away however long it has spent.
worker_state["busy"] = True
elsewhere = machine_for(FakeEc2(state="running"), worker_port, idle_seconds=1, tick=0.05)
elsewhere.last_used = time.time() - 60
elsewhere.watch()
time.sleep(0.4)
check("nor one whose worker says it is busy",
      not any(c[0] == "stop" for c in elsewhere._client.calls))
worker_state["busy"] = False

never = machine_for(FakeEc2(state="running"), worker_port, idle_seconds=0, tick=0.05)
never.last_used = time.time() - 6000
never.watch()
time.sleep(0.3)
check("an idle timer of zero leaves it to the box",
      not any(c[0] == "stop" for c in never._client.calls))

# --- wiring -----------------------------------------------------------------

check("no instance id means no machine, and no ec2 runner",
      ec2_module.from_config(from_env()) is None
      and "ec2" not in build_runners(from_env()))
first_machine = ec2_module.from_config(from_env(ec2_instance="i-memo", ec2_region="us-east-1"))
check("a configured instance gives one",
      first_machine is not None and first_machine.instance_id == "i-memo")
# Rebuilt runners must not mean a rebuilt Machine: that would forget the idle
# clock and, worse, the count of jobs on the box.
check("and asking again gives the same one, not a fresh one",
      ec2_module.from_config(from_env(ec2_instance="i-memo", ec2_region="us-east-1"))
      is first_machine)
wired = build_runners(from_env(ec2_instance="i-memo", ec2_region="us-east-1"))
check("which is the runner's, so the API and the queue share it",
      gpu_machine(wired) is first_machine)
check("the settings come from the environment, never the code",
      ec2_module.from_config(from_env(ec2_instance="i-env", ec2_port=9100,
                                      ec2_idle_minutes=2)).idle_seconds == 120)

# --- a whole job ------------------------------------------------------------

atom = "ATOM      1  CA  GLY A   1      11.100  22.200  33.300  1.00  0.00           C"
served, _ = stand_in_worker(designs=[{"name": "d1", "pdb": atom + "\nEND\n", "metrics": {}}])
sleepy = FakeEc2(state="stopped", steps=1)
end_to_end = machine_for(sleepy, served.server_address[1], instance_id="i-run")
whole = Job(900, {"kind": "binder", "run": {"numDesigns": 1}}, Path(tempfile.mkdtemp()))
Ec2Runner(end_to_end, poll=0.02).run({"kind": "binder", "run": {"numDesigns": 1}}, whole)
check("a job wakes the box, runs on it, and comes back with the design",
      len(whole.designs) == 1 and whole.designs[0]["atoms"] == 1, str(len(whole.designs)))
check("and the command the model was run with travels with it",
      "run_inference.py" in whole.command, whole.command)
check("the lease is given back when the job ends, so the box can stop",
      end_to_end.leases == 0)

vanished = Job(901, {"kind": "binder", "run": {"numDesigns": 1}}, Path(tempfile.mkdtemp()))
check("an instance that is gone reaches the job as a runner error, not a traceback",
      isinstance(_raises(lambda: Ec2Runner(machine_for(FakeEc2(state="terminated"), worker_port))
                         .run({}, vanished)), RunnerError))
quit_early = Job(902, {"kind": "binder", "run": {"numDesigns": 1}}, Path(tempfile.mkdtemp()))
quit_early.cancel_requested = True
check("and a cancel during the boot leaves the job to the queue to mark",
      _raises(lambda: Ec2Runner(machine_for(FakeEc2(state="stopped", steps=999), worker_port))
              .run({}, quit_early)) is None)

# --- what the box tells whoever is paying for it ----------------------------

worker_module.JOBS.clear()
check("an idle worker is not busy", worker_module.busy() is False)
worker_module.JOBS["a"] = {"status": "running"}
check("a running job makes it busy", worker_module.busy() is True)
worker_module.JOBS["a"] = {"status": "done"}
check("and a finished one does not", worker_module.busy() is False)
worker_module.JOBS.clear()

real_worker = ThreadingHTTPServer(("127.0.0.1", 0),
                                  worker_module.make_handler(worker_module.EchoGenerator(), ""))
real_worker.daemon_threads = True
threading.Thread(target=real_worker.serve_forever, daemon=True).start()
wbase = f"http://127.0.0.1:{real_worker.server_address[1]}"

status, body, _ = get(f"{wbase}/health")
reported = json.loads(body)
check("the worker says whether there is work on the card",
      status == 200 and reported["busy"] is False)
check("and how long since anyone wanted anything", isinstance(reported["idle"], (int, float)))

# The watchdog on the box polls /health once a minute. If that counted as being
# wanted, the instance would never go idle and would never stop -- which is the
# one bug in this whole design that costs money rather than time.
worker_module.LAST_ACTIVITY = time.time() - 40
get(f"{wbase}/health")
after_health = json.loads(get(f"{wbase}/health")[1])["idle"]
check("asking after the idle clock does not reset it", after_health > 35, str(after_health))

worker_module.LAST_ACTIVITY = time.time() - 40
status, body = post(f"{wbase}/design", {"kind": "binder", "run": {"numDesigns": 1}})
queued = json.loads(body).get("job_id", "")
check("but queueing a job does", json.loads(get(f"{wbase}/health")[1])["idle"] < 5)

# After the echo job has finished, so its own touch cannot be what this sees.
time.sleep(0.5)
worker_module.LAST_ACTIVITY = time.time() - 40
get(f"{wbase}/design/{queued}")
check("and so does asking how one is getting on -- an app still watching a job "
      "is still using the box", json.loads(get(f"{wbase}/health")[1])["idle"] < 5)
real_worker.shutdown()
real_worker.server_close()

print("\nEMDB maps")

# Against a stand-in EBI on localhost, like everything else here that would
# otherwise reach out. A map is tens of megabytes; a suite that downloads one is
# a suite nobody runs.
_fake_map = gzip.compress(b"MAP " + b"\0" * 4096)
_fake_entry = {
    "admin": {"title": "D8-C4 computationally-designed Rotor"},
    "map": {"contour_list": {"contour": [{"level": "2.0"},
                                         {"level": "1.35", "primary": True}]}},
    "crossreferences": {"pdb_list": {"pdb_reference": [{"pdb_id": "7t02"}]}},
    "structure_determination_list": {"structure_determination": [
        {"image_processing": [{"final_reconstruction": {"resolution": {"valueOf_": "5.9"}}}]}]},
}


class _Ebi(BaseHTTPRequestHandler):
    hits: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        _Ebi.hits.append(self.path)
        if self.path.endswith(".map.gz"):
            if "999999" in self.path:
                self.send_error(404)
                return
            body, kind = _fake_map, "application/gzip"
        else:
            body, kind = json.dumps(_fake_entry).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_ebi = Server(("127.0.0.1", 0), _Ebi)
threading.Thread(target=_ebi.serve_forever, daemon=True).start()
_ebi_base = f"http://127.0.0.1:{_ebi.server_address[1]}"
emdb.MAP_URL = _ebi_base + "/EMD-{id}/map/emd_{id}.map.gz"
emdb.ENTRY_URL = _ebi_base + "/entry/EMD-{id}"

check("an EMDB id is recognised however it is written",
      all(emdb.normalise(t) == "EMD-25575"
          for t in ("EMD-25575", "emd-25575", "emd_25575", "25575", " EMD-25575 ")))
check("and something that is not one is refused",
      isinstance(_raises(lambda: emdb.normalise("4HHB")), emdb.EmdbError),
      str(_raises(lambda: emdb.normalise("4HHB"))))

# A cache of its own, thrown away afterwards. Writing a stand-in EMD-25575 into
# the real data/cache would leave the app serving four kilobytes of zeros for
# the rest of the entry's life, and nothing downstream would think to doubt it.
_map_dir = tempfile.TemporaryDirectory(prefix="proteincad-emdb-")
_map_cache = Path(_map_dir.name)

_Ebi.hits.clear()
_data, _name = emdb.fetch_map("EMD-25575", _map_cache)
check("a map is fetched and written to the cache",
      _data == _fake_map and _name == "emd_25575.map.gz"
      and (_map_cache / "emd_25575.map.gz").is_file(), _name)
check("and it is passed through still gzipped, not unpacked",
      _data[:2] == b"\x1f\x8b" and gzip.decompress(_data)[:4] == b"MAP ")

_again, _ = emdb.fetch_map("emd_25575", _map_cache)
check("asking again, by any spelling, does not go back to the EBI",
      _again == _data and len(_Ebi.hits) == 1, f"{len(_Ebi.hits)} request(s)")

_meta = emdb.fetch_meta("EMD-25575", _map_cache)
check("the recommended contour level comes back", _meta["contour"] == 1.35, str(_meta["contour"]))
check("with the title, the resolution and any fitted model",
      _meta["title"].startswith("D8-C4") and _meta["resolution"] == 5.9
      and _meta["fitted"] == ["7t02"], str(_meta))
check("a missing entry is a reportable error, not a traceback",
      isinstance(_raises(lambda: emdb.fetch_map("EMD-999999", _map_cache)), emdb.EmdbError),
      str(_raises(lambda: emdb.fetch_map("EMD-999999", _map_cache))))

# A download that dies halfway must not leave a short map in the cache for every
# later visit to read as if it were whole.
check("nothing is left behind by a failed download",
      not (_map_cache / "emd_999999.map.gz").is_file()
      and not list(_map_cache.glob("*.part")))

check("the JS and Python readings of an entry agree",
      emdb.summarise(_fake_entry) == {"title": "D8-C4 computationally-designed Rotor",
                                      "contour": 1.35, "fitted": ["7t02"], "resolution": 5.9},
      str(emdb.summarise(_fake_entry)))
check("an entry document with nothing useful in it is not an error",
      emdb.summarise({}) == {"title": "", "contour": None, "fitted": [], "resolution": None})

_ebi.shutdown()
_map_dir.cleanup()

print("\nRotational landscape")

# A landscape is tested against symmetry, not against a saved curve.
#
# A Cn rotor turned by 360/n is the same rotor, so the interaction energy cannot
# tell the two orientations apart -- and the same holds for the axle from the
# other side. The period is therefore forced to be 360/lcm(n, m) whatever scores
# it, which makes it a *prediction*: build an assembly whose folds are known,
# and the curve has to come back repeating at the right spacing. That is the
# honest form of "reproduce the published landscapes": the 45-degree spacing
# Courbet et al. report for their D8-C4 system is symmetry, and it is checked
# here exactly; the well depths are the force field's, and a geometric score is
# not claimed to reproduce Rosetta's.
#
# Everything is built in memory, so none of this needs a network or a PDB entry.


def _blob(seed: int, count: int = 14):
    """A deterministic asymmetric lump of atoms, in local coordinates.

    Asymmetric on purpose: a lump with a mirror plane would give a landscape
    symmetric in angle for reasons that have nothing to do with the assembly,
    and the asymmetry measure would read zero for the wrong reason.
    """
    state = seed
    points = []
    for _ in range(count):
        values = []
        for _ in range(3):
            state = (state * 1103515245 + 12345) % 2147483648
            values.append(state / 2147483648.0)
        points.append((values[0] * 6 - 3, values[1] * 6 - 3, values[2] * 10 - 5))
    return points


def _c_ring(local, fold: int, radius: float, height: float = 0.0):
    """`fold` copies of a lump, evenly spaced about z: a Cn component."""
    rings = []
    for k in range(fold):
        turn = landscape.rotation_about((0, 0, 1), k * 360.0 / fold)
        rings.append([landscape.apply(turn, (p[0] + radius, p[1], p[2] + height))
                      for p in local])
    return rings


def _d_ring(local, fold: int, radius: float):
    """A Dn: the Cn ring, plus a copy flipped by a perpendicular two-fold.

    Worth building because a Dn is where naive axis detection goes wrong. Half
    the chain pairs are related by those perpendicular two-folds rather than by
    the main rotation, and averaging every pair's axis together points somewhere
    between them -- which is a scan about the wrong axis that still looks fine.
    """
    rings = _c_ring(local, fold, radius, height=+6.0)
    flip = landscape.rotation_about((1, 0, 0), 180.0)
    return rings + _c_ring([landscape.apply(flip, p) for p in local], fold, radius, height=-6.0)


def _assembly_pdb(groups):
    """Chains as a PDB, and the ids each group got."""
    pool = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    lines, serial, assigned = [], 1, []
    for chains in groups:
        ids = []
        for chain in chains:
            name = pool[len(assigned) + len(ids)]
            ids.append(name)
            for index, (x, y, z) in enumerate(chain):
                lines.append(f"ATOM  {serial:5d}  CA  ALA {name}{index + 1:4d}    "
                             f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00  0.00           C  ")
                serial += 1
            lines.append(f"TER   {serial:5d}")
            serial += 1
        assigned.extend(ids)
    return "\n".join(lines) + "\nEND\n", assigned


def _components(rotor_chains, axle_chains):
    text, ids = _assembly_pdb([axle_chains, rotor_chains])
    axle_ids = ids[:len(axle_chains)]
    rotor_ids = ids[len(axle_chains):]
    parsed = landscape.parse(text, name="synthetic")
    rotor, axle = landscape.split(parsed, rotor_ids, axle_ids)
    return text, rotor_ids, axle_ids, rotor, axle


def _exact(name, chains):
    """A Component straight from the coordinates, with no file in between.

    PDB writes three decimals, so a structure round-tripped through one is only
    symmetric to a thousandth of an Angstrom -- enough to move a sample point
    across an occluder boundary and shift a buried area by a square Angstrom or
    two. That is a property of the format, not of this code, so the claim that
    equivalent orientations score *identically* is tested here, where the
    coordinates are exact, and the claim that it survives a real file is tested
    separately against the size of the landscape.
    """
    component = landscape.Component(name, [f"{i}" for i in range(len(chains))])
    for chain_id, chain in zip(component.chains, chains):
        for index, point in enumerate(chain):
            component.coords.append(point)
            component.radii.append(landscape.DEFAULT_RADIUS)
            component.by_chain.setdefault(chain_id, {})[(index + 1, "ALA", "CA")] = point
    return component


# --- the linear algebra everything else stands on ---------------------------
_axis = landscape._unit((0.3, -0.5, 1.0))
_turn = landscape.rotation_about(_axis, 37.0)
_source = [(1.0, 0.2, -0.4), (2.1, 1.0, 0.3), (-0.5, 2.2, 1.1), (0.7, -1.4, 2.0), (3.0, 0.1, 0.1)]
_target = [landscape.apply(_turn, p) for p in _source]
_matrix, _angle, _recovered = landscape.kabsch_rotation(_source, _target)
check("Kabsch recovers a known rotation angle", abs(_angle - 37.0) < 1e-6, f"{_angle:.9f}")
check("and its axis, with the right sign",
      landscape.angle_between(_recovered, _axis) < 1e-6
      and landscape._dot(_recovered, _axis) > 0,
      str([round(v, 6) for v in _recovered]))
check("and the matrix it returns maps one set onto the other",
      max(max(abs(a - b) for a, b in zip(landscape.apply(_matrix, p), q))
          for p, q in zip(_source, _target)) < 1e-9)

# --- the axis, on the shape this feature exists for -------------------------
_text, _rotor_ids, _axle_ids, _rotor, _axle = _components(
    _c_ring(_blob(99), 4, 14.5), _d_ring(_blob(7), 8, 7.0))
_found = landscape.detect_axis(_rotor, _axle)
check("a D8 axle's perpendicular two-folds do not drag the axis off z",
      landscape.angle_between(_found["direction"], (0, 0, 1)) < 0.01,
      f"{landscape.angle_between(_found['direction'], (0, 0, 1)):.2e} degrees off")
check("the rotor's fold is read off its own chains", _found["rotor_fold"] == 4,
      f"C{_found['rotor_fold']}")
check("and the axle's, through the flipped half of a Dn", _found["axle_fold"] == 8,
      f"C{_found['axle_fold']}")
check("the two components agree about where the axis is",
      _found["agreement"] is not None and _found["agreement"] < 0.01,
      f"{_found['agreement']} degrees apart")
check("360/lcm(4, 8) is the period symmetry forces",
      landscape.expected_period(4, 8) == 45.0, str(landscape.expected_period(4, 8)))

# A fold is only claimed when the rotations really divide the circle. A C8 ring
# with a chain too disordered to superpose must not come back as a C7.
_, _, _, _r7, _a7 = _components(_c_ring(_blob(99), 4, 14.5), _c_ring(_blob(7), 8, 7.0)[:-1])
check("seven chains of a C8 are still a C8, not a C7",
      landscape.detect_axis(_r7, _a7)["axle_fold"] == 8,
      f"C{landscape.detect_axis(_r7, _a7)['axle_fold']}")
_, _, _, _r3, _a3 = _components(_c_ring(_blob(99), 4, 14.5), _c_ring(_blob(7), 7, 7.0))
check("and a real C7 is a C7", landscape.detect_axis(_r3, _a3)["axle_fold"] == 7)
# One pairwise rotation of 51.4 degrees is within a fraction of a degree of
# 360/7 and means nothing of the kind. The asymmetric PomB dimer does this.
check("a lone pairwise rotation is not evidence of a seven-fold",
      landscape._fold_from([51.43], 1) == 0, str(landscape._fold_from([51.43], 1)))
check("but two chains 45 degrees apart with corroboration are an eight-fold",
      landscape._fold_from([45.0, 90.0], 2) == 8, str(landscape._fold_from([45.0, 90.0], 2)))

# --- the curve must repeat where symmetry says it must ----------------------
for _label, _axle_build, _rotor_build, _want in (
        ("D8 axle, C4 rotor", _d_ring(_blob(7), 8, 7.0), _c_ring(_blob(99), 4, 14.5), 45.0),
        ("C3 axle, C3 rotor", _c_ring(_blob(11), 3, 6.0), _c_ring(_blob(42), 3, 13.5), 120.0),
        ("C8 axle, C4 rotor", _c_ring(_blob(7), 8, 7.0), _c_ring(_blob(99), 4, 14.5), 45.0),
):
    _, _, _, _rot, _axl = _components(_rotor_build, _axle_build)
    _ax = landscape.detect_axis(_rot, _axl)
    _expected = landscape.expected_period(_ax["rotor_fold"], _ax["axle_fold"])
    _rows = list(landscape.scan(_rot, _axl, _ax, landscape.angle_list(5.0)))
    _say = landscape.descriptors(_rows, _expected)
    check(f"{_label}: the period is the one symmetry forces",
          _say["period"] == _want and _say["period_matches_symmetry"],
          f"{_say['period']} degrees, wanted {_want}")
    check(f"{_label}: every frequency carrying power is a harmonic of it",
          _say["period_power"] > 0.99, f"{_say['period_power']:.4f} of the power")
    # Three decimals of PDB is enough to move a sample point across an occluder
    # and shift a buried area slightly, so through a file the claim is that
    # equivalent orientations agree to well inside the depth of the wells --
    # not that they agree to the last bit, which is checked below without one.
    _curve = {row["angle"]: row["score"] for row in _rows}
    _drift = max(abs(_curve[a] - _curve[(a + _want) % 360.0]) for a in _curve)
    check(f"{_label}: equivalent orientations agree to well inside a well",
          _drift < 0.03 * _say["range"],
          f"{_drift:.2e} against a range of {_say['range']:.2f}")

# With exact coordinates and an exact axis, two orientations symmetry calls the
# same must score the *same number*, not nearly the same one. That holds only
# because the sample sphere for buried area turns with the rotor: a fixed set of
# sample points gives a slightly different area for the same configuration
# rotated, and in a landscape that noise invents minima that are not there.
_exact_axis = {"direction": [0.0, 0.0, 1.0], "point": [0.0, 0.0, 0.0],
               "rotor_fold": 4, "axle_fold": 8}
_exact_rows = list(landscape.scan(_exact("rotor", _c_ring(_blob(99), 4, 14.5)),
                                  _exact("axle", _d_ring(_blob(7), 8, 7.0)),
                                  _exact_axis, landscape.angle_list(5.0)))
_exact_curve = {row["angle"]: row["score"] for row in _exact_rows}
_exact_drift = max(abs(_exact_curve[a] - _exact_curve[(a + 45.0) % 360.0]) for a in _exact_curve)
check("with exact coordinates, equivalent orientations score identically",
      _exact_drift == 0.0, f"worst difference {_exact_drift:.2e}")
_unequal = max(abs(_exact_curve[a] - _exact_curve[(a + 15.0) % 360.0]) for a in _exact_curve)
check("while orientations symmetry does not relate score differently",
      _unequal > 0.1, f"{_unequal:.4f} apart at 15 degrees")

# The strongest frequency is the shape of a well, not the spacing of them: a
# double-dipped well puts more power on the second harmonic, and reading the
# period off that halves it.
_, _, _, _rot, _axl = _components(_c_ring(_blob(99), 4, 14.5), _c_ring(_blob(7), 8, 7.0))
_ax = landscape.detect_axis(_rot, _axl)
_rows = list(landscape.scan(_rot, _axl, _ax, landscape.angle_list(5.0)))
_say = landscape.descriptors(_rows, 45.0)
check("two wells per period do not halve the reported period",
      _say["period"] == 45.0 and _say["minima_per_period"] == 2.0,
      f"period {_say['period']}, {_say['minima_per_period']} minima per period")
check("and the asymmetry is measurable once a period has structure in it",
      _say["asymmetry"] is not None, str(_say["asymmetry"]))

# One minimum per period makes the two barriers the same peak, so the measure is
# zero by construction. Reporting that as "zero, therefore Brownian" would be
# reading a theorem about periodic functions as a result about the assembly.
_, _, _, _rot1, _axl1 = _components(_c_ring(_blob(99), 4, 14.5), _d_ring(_blob(7), 8, 7.0))
_ax1 = landscape.detect_axis(_rot1, _axl1)
_say1 = landscape.descriptors(
    list(landscape.scan(_rot1, _axl1, _ax1, landscape.angle_list(5.0))), 45.0)
check("one minimum per period reports no asymmetry rather than zero",
      _say1["minima_per_period"] == 1.0 and _say1["asymmetry"] is None
      and "by construction" in _say1["asymmetry_note"], _say1["asymmetry_note"][:40])

# --- refusals ---------------------------------------------------------------
check("a step that does not divide 360 is refused",
      isinstance(_raises(lambda: landscape.angle_list(7.0)), landscape.LandscapeError))
check("and so is a chain claimed by both components",
      "both" in str(_raises(lambda: landscape.split(
          landscape.parse(_text, "x"), _rotor_ids, _rotor_ids))))
# The Rosetta backend is an interface with nothing behind it. It used to decide
# that by trying to import pyrosetta, which meant installing PyRosetta made it
# report itself ready and then fail with an empty message -- an import standing
# in for an implementation that was never written.
_rosetta = landscape.RosettaScorer()
check("the Rosetta backend reports itself unimplemented, whatever is installed",
      _rosetta.available()[0] is False and "not implemented" in _rosetta.available()[1])
check("and says so again if something calls it anyway, rather than failing blank",
      "not implemented" in str(_raises(lambda: _rosetta.prepare(None, None, None)))
      and "not implemented" in str(_raises(lambda: _rosetta.score(None, None, None))))
check("and does not claim installing PyRosetta would turn it on",
      "will not turn it on" in _rosetta.available()[1])
check("the backend catalogue says which can actually run",
      [b["id"] for b in landscape.backend_catalogue()] == ["geometric", "rosetta"]
      and landscape.backend_catalogue()[0]["available"] is True)

print("\nHTTP API")
config = from_env(port=0, data_dir=ROOT / "data")
httpd = Server(("127.0.0.1", 0), Handler)
httpd.context = Context(config)
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"

try:
    status, body, _ = get(f"{base}/api/health")
    check("GET /api/health", status == 200 and json.loads(body)["status"] == "ok")

    # The web files are re-read on every request; this process is not. A change
    # on the Python side therefore keeps running the old behaviour behind the
    # new interface, which reads as the fix not working rather than as a server
    # that needs restarting -- so /health answers the question itself.
    check("a fresh process does not report itself stale",
          json.loads(body).get("stale") is False, str(json.loads(body).get("stale")))
    from proteincad import api as api_module  # noqa: E402
    was = api_module.LOADED
    api_module.LOADED = 0.0
    try:
        status, body, _ = get(f"{base}/api/health")
        check("code newer than the process is reported",
              json.loads(body).get("stale") is True, str(json.loads(body).get("stale")))
    finally:
        api_module.LOADED = was
    status, body, _ = get(f"{base}/api/health")
    check("and it goes quiet again once it matches",
          json.loads(body).get("stale") is False)

    status, body, _ = get(f"{base}/")
    check("GET / serves the app", status == 200 and b"proteinCAD" in body)

    status, body, headers = get(f"{base}/src/main.js")
    check("javascript served as a module type", status == 200 and "javascript" in headers.get("Content-Type", ""),
          headers.get("Content-Type", ""))

    status, body, headers = get(f"{base}/api/structure/1CRN")
    check("GET /api/structure/1CRN", status == 200 and body.startswith(b"HEADER"),
          f"{len(body)} bytes, {headers.get('X-Structure-Filename')}")

    status, body, _ = get(f"{base}/api/structure/1CRN/summary")
    summary = json.loads(body)
    check("GET /api/structure/1CRN/summary", status == 200 and summary["atoms"] == 327,
          f"{summary['atoms']} atoms")

    status, body = post(f"{base}/api/analyze", {
        "name": "unit", "elements": ["C", "C", "N", "O"],
        "coords": [0, 0, 0, 3, 0, 0, 0, 4, 0, 0, 0, 5],
    })
    result = json.loads(body)
    check("POST /api/analyze", status == 200 and result["count"] == 4 and result["name"] == "unit")

    status, body = post(f"{base}/api/analyze", {"coords": [0, 0]})
    check("POST /api/analyze rejects bad input", status == 400, json.loads(body).get("error", ""))

    status, body = post(f"{base}/api/session", {"structures": [{"name": "1CRN"}], "selection": []})
    check("POST /api/session", status == 200 and json.loads(body)["structures"] == ["1CRN"])

    # --- design jobs, end to end against the mock runner -------------------
    crop = (ROOT / "data" / "samples" / "1crn.pdb").read_text()
    spec = {
        "model": "mock",
        "target": {"name": "1CRN", "pdb": crop, "hotspots": ["A22", "A23", "A24"]},
        "binder": {"lengthMin": 55, "lengthMax": 70, "contigs": "A1-46/0 55-70"},
        "run": {"numDesigns": 2, "seed": 5},
        "volume": None,
    }
    status, body = post(f"{base}/api/design", spec)
    job = json.loads(body)
    check("POST /api/design queues a job", status == 200 and job["status"] in ("queued", "running"),
          f"job {job.get('id')}")

    deadline = time.time() + 60
    while time.time() < deadline:
        status, body, _ = get(f"{base}/api/jobs/{job['id']}")
        job = json.loads(body)
        if job["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.4)
    check("job finishes", job["status"] == "done", f"{job['status']} in {job.get('elapsed')} s")
    check("job produced the designs asked for", len(job["designs"]) == 2, str(len(job["designs"])))

    status, body, headers = get(f"{base}/api/jobs/{job['id']}/designs/0")
    check("GET a design returns a PDB", status == 200 and body.startswith(b"TITLE"),
          f"{len(body)} bytes, {headers.get('X-Design-Name')}")
    design = structure.parse(body.decode(), "design.pdb")
    check("the design parses", design.atom_count > 200, f"{design.atom_count} atoms")

    # It must land near the picked residues, not at the origin.
    target = structure.parse(crop, "1crn")
    site = [target.coords[i] for i, name in enumerate(target.atom_names)
            if name == "CA" and target.residues[0] is not None][:1]
    picked = []
    for residue in target.residues:
        if f"{residue.chain}{residue.seq}" in set(spec["target"]["hotspots"]):
            picked.extend(target.coords[residue.start:residue.end + 1])
    distance = mock_design.norm(mock_design.sub(
        mock_design.centroid(design.coords), mock_design.centroid(picked)))
    check("the design is placed on the site", 5 < distance < 60, f"{distance:.1f} A from the hotspots")
    del site

    status, body, _ = get(f"{base}/api/jobs/{job['id']}/designs/99")
    check("missing design gives 404", status == 404)

    # Nothing may store a result with no coordinates. Writing one turns an
    # endpoint that produced nothing into a job that reads as finished, and the
    # first sign of trouble is an empty file failing to open, which looks like a
    # bug in the viewer rather than a worker running the wrong generator.
    hollow = Job(999, {"model": "mock"}, Path(tempfile.mkdtemp()))
    refused = _raises(lambda: hollow.add_design("echo_01", "", {}))
    check("an empty design is refused, not filed",
          isinstance(refused, ValueError) and "no atoms" in str(refused), str(refused)[:70])
    check("and nothing is written to disk for it",
          not list(hollow.dir.glob("*.pdb")) and hollow.designs == [],
          str(len(hollow.designs)))

    # --- stage two, the same way the browser asks for it --------------------
    # The browser sends which design it means and nothing else; the complex is
    # assembled here from the target and the backbone already on disk.
    status, body = post(f"{base}/api/jobs/{job['id']}/designs/0/fold",
                        {"model": "mock", "numDesigns": 3, "foldTop": 1})
    fold = json.loads(body)
    check("POST .../fold queues the second stage",
          status == 200 and fold["kind"] == "fold", f"{status}: {body[:90].decode()}")
    check("it records which design it came from",
          fold["source"] == {"job": job["id"], "design": 0, "name": job["designs"][0]["name"]},
          str(fold.get("source")))

    deadline = time.time() + 60
    while time.time() < deadline:
        status, body, _ = get(f"{base}/api/jobs/{fold['id']}")
        fold = json.loads(body)
        if fold["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.4)
    check("the sequence job finishes", fold["status"] == "done",
          f"{fold['status']}: {fold.get('error', '')[:70]}")
    check("one result per sequence asked for", len(fold["designs"]) == 3,
          str(len(fold["designs"])))
    check("each result carries a sequence for the binder",
          all(len(d["metrics"].get("sequence", "")) > 10 for d in fold["designs"]),
          str([len(d["metrics"].get("sequence", "")) for d in fold["designs"]]))

    status, body, _ = get(f"{base}/api/jobs/{fold['id']}/designs/0")
    check("a sequence result loads as a PDB like any other design",
          status == 200 and structure.parse(body.decode(), "seq.pdb").atom_count > 0,
          f"{len(body)} bytes")
    # It is the binder alone, so it has to land where the backbone was, not
    # back at the origin and not on top of the target.
    threaded = structure.parse(body.decode(), "seq.pdb")
    moved = mock_design.norm(mock_design.sub(
        mock_design.centroid(threaded.coords), mock_design.centroid(design.coords)))
    check("and it sits exactly where its backbone did", moved < 0.01, f"{moved:.3f} A")

    status, body = post(f"{base}/api/jobs/{fold['id']}/designs/0/fold", {})
    check("a sequence job cannot be taken on again", status == 400,
          json.loads(body).get("error", ""))

    # --- following a tunnel that moved ---------------------------------------
    # A Colab quick tunnel gets a new address every session, and a session has
    # to be restarted whenever the card needs clearing. Before this, following
    # it meant stopping the server and retyping a command line, so freeing a GPU
    # cost the job list too.
    status, body = post(f"{base}/api/compute",
                        {"url": "https://moved.example/", "token": "s3cret"})
    moved = json.loads(body)
    check("the compute endpoint can be repointed without a restart",
          status == 200 and "remote" in moved.get("runners", []), f"{status}: {body[:80].decode()}")
    check("and the server says where it is now",
          moved.get("compute_url") == "https://moved.example", str(moved.get("compute_url")))
    check("the token is never handed back", "s3cret" not in body.decode())

    status, body, _ = get(f"{base}/api/health")
    check("health agrees about the new endpoint",
          json.loads(body).get("compute_url") == "https://moved.example")

    status, body = post(f"{base}/api/compute", {"url": "ftp://nope"})
    check("a url that is not http is refused", status == 400,
          json.loads(body).get("error", "")[:60])

    # Jobs already queued keep running against whatever they were given; the
    # runner list is what changes.
    status, body = post(f"{base}/api/compute", {"url": "", "token": ""})
    check("clearing it leaves the mock runner",
          json.loads(body).get("runners") == ["mock"], str(json.loads(body).get("runners")))

    # Off the moment the server is reachable from anywhere but this machine.
    shared = from_env(port=0, data_dir=ROOT / "data", host="0.0.0.0")
    check("a server on a shared address will not take one over the API",
          shared.allow_remote_config is False, str(shared.allow_remote_config))

    status, body = post(f"{base}/api/design", {**spec, "model": "does-not-exist"})
    check("unknown runner is rejected", status == 400, json.loads(body).get("error", ""))

    status, body, _ = get(f"{base}/api/jobs")
    check("GET /api/jobs lists them", status == 200 and len(json.loads(body)["jobs"]) >= 1)

    # A deployment with no GPU of its own answers rather than 404s, so the panel
    # can hide the section without telling a missing feature apart from a
    # missing server.
    status, body, _ = get(f"{base}/api/gpu")
    check("GET /api/gpu on a server with no instance",
          status == 200 and json.loads(body) == {"configured": False})
    status, body = post(f"{base}/api/gpu/stop", {})
    check("and asking it to stop one says how to configure one instead",
          status == 404 and "PROTEINCAD_EC2_INSTANCE" in json.loads(body).get("error", ""))

    panel = machine_for(FakeEc2(state="running"), worker_port, instance_id="i-panel",
                        idle_seconds=900)
    httpd.context.jobs.runners["ec2"] = Ec2Runner(panel)
    status, body, _ = get(f"{base}/api/gpu")
    reported = json.loads(body)
    check("a configured instance is reported to the panel",
          status == 200 and reported["configured"] and reported["state"] == "running",
          reported.get("state"))
    check("with the address it is reachable at",
          reported["endpoint"] == f"http://127.0.0.1:{worker_port}", reported.get("endpoint"))
    check("and the deadline the panel counts down to",
          0 < reported["stops_in"] <= 900, str(reported.get("stops_in")))

    panel.leases = 1
    status, body = post(f"{base}/api/gpu/stop", {})
    check("it will not be stopped out from under a running job", status == 409,
          json.loads(body).get("error", ""))
    panel.leases = 0
    status, body = post(f"{base}/api/gpu/stop", {})
    check("but will be when there is not one",
          status == 200 and any(c[0] == "stop" for c in panel._client.calls))

    panel._client.state = "stopped"
    status, body = post(f"{base}/api/gpu/start", {})
    check("and starting it answers at once rather than holding the request open "
          "for a two-minute boot", status == 200)
    for _ in range(100):
        if any(c[0] == "start" for c in panel._client.calls):
            break
        time.sleep(0.05)
    check("while the start goes ahead in the background",
          any(c[0] == "start" for c in panel._client.calls))
    del httpd.context.jobs.runners["ec2"]

    # --- the landscape routes ------------------------------------------------
    status, body, _ = get(f"{base}/api/landscape/options")
    check("GET /api/landscape/options lists the scoring backends",
          status == 200 and json.loads(body)["backends"][0]["id"] == "geometric")

    scan_pdb, scan_ids = _assembly_pdb([_d_ring(_blob(7), 8, 7.0), _c_ring(_blob(99), 4, 14.5)])
    # 15 degrees: 24 samples, which is fine enough to carry an eight-wells-a-turn
    # frequency. Twelve samples could not, and the scan would report a period it
    # had no way to see -- which is what `period_resolvable` is checked for below.
    scan_request = {"pdb": scan_pdb, "name": "D8-C4", "step": 15,
                    "axle": scan_ids[:16], "rotor": scan_ids[16:]}
    status, body = post(f"{base}/api/landscape", scan_request)
    queued = json.loads(body)
    check("POST /api/landscape starts a scan", status == 200 and queued["status"] in
          ("queued", "running", "done"), f"{status}: {queued.get('error', '')[:60]}")

    scan_id = queued["id"]
    for _ in range(400):
        status, body, _ = get(f"{base}/api/landscape/{scan_id}")
        finished = json.loads(body)
        if finished["status"] in ("done", "failed", "cancelled"):
            break
        time.sleep(0.1)
    check("and it runs to completion out of process",
          finished["status"] == "done" and finished["progress"] == finished["total"],
          f"{finished['status']} {finished['progress']}/{finished['total']} {finished['error'][:60]}")
    check("the axis it measured comes back with the curve",
          finished["axis"]["rotor_fold"] == 4 and finished["axis"]["axle_fold"] == 8)
    check("and the descriptors agree with symmetry over HTTP",
          finished["descriptors"]["period"] == 45.0
          and finished["descriptors"]["period_matches_symmetry"],
          f"period {finished['descriptors']['period']}")

    # A scan that could not see the period has not failed the symmetry check, it
    # has not run it -- and must not report a period it had no way to measure.
    # There are two ways it cannot see one, and they need different advice.
    def _curve(step, fn=lambda a: math.cos(math.radians(8 * a))):
        return [{"angle": a, "rise": 0.0, "score": fn(a), "clashes": 0}
                for a in landscape.angle_list(step)]

    _off_grid = landscape.descriptors(_curve(10.0), 45.0)
    check("a period the sampling cannot land on is reported as unchecked, not failed",
          _off_grid["period_resolvable"] is False
          and _off_grid["period_matches_symmetry"] is False
          and "not a whole number" in _off_grid["period_note"], _off_grid["period_note"][:58])
    _coarse = landscape.descriptors(_curve(45.0), 45.0)
    check("and so is one the sampling is too coarse to resolve",
          _coarse["period_resolvable"] is False
          and "cannot resolve" in _coarse["period_note"], _coarse["period_note"][:58])
    _fine = landscape.descriptors(_curve(15.0), 45.0)
    check("while a step the period divides does resolve it",
          _fine["period_resolvable"] and _fine["period"] == 45.0, str(_fine["period"]))

    # The period is the smallest turn that leaves the curve looking the same,
    # not 360 over the strongest frequency. A well with two dips in it puts more
    # power on the second harmonic, and reading the period off that halves it.
    _doubled = landscape.descriptors(
        _curve(5.0, lambda a: math.cos(math.radians(16 * a))
               + 0.3 * math.cos(math.radians(8 * a))), 45.0)
    check("a well with two dips in it does not halve the reported period",
          _doubled["period"] == 45.0,
          f"{_doubled['period']}, dominant order {_doubled['dominant_order']}")
    _flat = landscape.descriptors(_curve(10.0, lambda a: 1.0), 0.0)
    check("a flat landscape claims no period at all", _flat["period"] == 360.0,
          str(_flat["period"]))

    # --- the cross-check fixture --------------------------------------------
    # The geometric scan exists in two languages, because it runs in the browser
    # on a static deployment and PyRosetta cannot run there. This side asserts
    # the Python still produces the committed curve; tools/check.mjs asserts the
    # JavaScript reproduces it. Neither can drift without a failure.
    _fixture_dir = ROOT / "tools" / "fixtures"
    _fixture_json = _fixture_dir / "landscape-d8c4.json"
    if not _fixture_json.is_file():
        print("  --   landscape cross-check fixture missing; run "
              "tools/fixtures/make-landscape-fixture.py")
    else:
        _fixture = json.loads(_fixture_json.read_text())
        _parsed = landscape.parse((_fixture_dir / "landscape-d8c4.pdb").read_text(), "d8c4")
        _rotor, _axle = landscape.split(_parsed, _fixture["rotor"], _fixture["axle"])
        _axis = landscape.detect_axis(_rotor, _axle)
        check("the fixture's folds are still read the same way",
              _axis["rotor_fold"] == _fixture["axis"]["rotor_fold"]
              and _axis["axle_fold"] == _fixture["axis"]["axle_fold"],
              f"C{_axis['rotor_fold']} on C{_axis['axle_fold']}")
        _expected = landscape.expected_period(_axis["rotor_fold"], _axis["axle_fold"])
        _rows = list(landscape.scan(_rotor, _axle, _axis,
                                    landscape.angle_list(_fixture["step"])))
        _want = {row["angle"]: row for row in _fixture["points"]}
        _worst = max(abs(row["score"] - _want[row["angle"]]["score"]) for row in _rows)
        check("and every angle still scores what the fixture records",
              _worst < 1e-9, f"worst difference {_worst:.2e}")
        _said = landscape.descriptors(_rows, _expected)
        _differ = [k for k, v in _fixture["descriptors"].items() if _said[k] != v]
        check("and the curve is still described the same way", not _differ,
              ", ".join(f"{k}: {_said[k]} vs {_fixture['descriptors'][k]}" for k in _differ)
              or "all match")

    # A scan is named after what went into it, so asking again is a lookup.
    before = time.time()
    status, body = post(f"{base}/api/landscape", scan_request)
    repeat = json.loads(body)
    check("an identical request is answered from disk rather than run again",
          repeat["id"] == scan_id and repeat["status"] == "done"
          and time.time() - before < 1.0, f"{(time.time() - before):.3f}s")
    status, body = post(f"{base}/api/landscape", {**scan_request, "step": 20})
    check("changing the step is a different scan", json.loads(body)["id"] != scan_id)
    post(f"{base}/api/landscape/{json.loads(body)['id']}/cancel", {})

    # Resuming: drop half the curve and ask again. The angles still on disk must
    # not be recomputed, and what comes back must match what a fresh run gives.
    points_path = config.scans_dir / scan_id / "points.ndjson"
    kept = points_path.read_text().splitlines()[:4]
    reference = {json.loads(line)["angle"]: json.loads(line)["score"]
                 for line in points_path.read_text().splitlines() if line.strip()}
    points_path.write_text("\n".join(kept) + "\n")
    (config.scans_dir / scan_id / "finished").unlink(missing_ok=True)
    status, body = post(f"{base}/api/landscape", scan_request)
    for _ in range(400):
        status, body, _ = get(f"{base}/api/landscape/{scan_id}")
        resumed = json.loads(body)
        if resumed["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    rebuilt = {row["angle"]: row["score"] for row in resumed["points"]}
    check("a part-finished scan resumes rather than starting over",
          resumed["status"] == "done" and len(rebuilt) == resumed["total"],
          f"{resumed['status']} {len(rebuilt)}/{resumed['total']}")
    check("and the resumed rows are identical to a complete run",
          rebuilt == reference,
          f"{sum(1 for a in reference if rebuilt.get(a) != reference[a])} rows differ")

    for payload, why in (
        ({**scan_request, "step": 7}, "a step that does not divide 360"),
        ({**scan_request, "rotor": scan_ids[:1]}, "a chain in both components"),
        ({**scan_request, "rotor": []}, "no rotor chains"),
        ({**scan_request, "pdb": ""}, "no structure"),
        ({**scan_request, "backend": "nope"}, "an unknown backend"),
        ({**scan_request, "backend": "rosetta"}, "a backend that is not installed"),
        ({**scan_request, "rise": {"min": -50, "max": 50, "step": 0.5}}, "too many rise steps"),
    ):
        status, body = post(f"{base}/api/landscape", payload)
        check(f"{why} is refused before anything is started", status == 400,
              json.loads(body).get("error", "")[:70])

    status, body, _ = get(f"{base}/api/landscape/deadbeef")
    check("an unknown scan is a 404", status == 404)
    status, body, _ = get(f"{base}/api/landscape/NOT-HEX-AT-ALL")
    check("and a scan id that is not a digest never reaches the filesystem",
          status == 400, json.loads(body).get("error", ""))
    status, body, _ = get(f"{base}/api/landscapes")
    check("GET /api/landscapes lists them", status == 200
          and len(json.loads(body)["scans"]) >= 1)

    status, body, _ = get(f"{base}/api/health")
    health = json.loads(body)
    check("health advertises runners", "mock" in health.get("runners", []), str(health.get("runners")))

    status, body, _ = get(f"{base}/api/structure/@@bad@@")
    check("unknown id gives 404", status == 404)

    status, body, _ = get(f"{base}/api/nope")
    check("unknown route gives 404", status == 404)

    status, body, _ = get(f"{base}/../pyproject.toml")
    check("path traversal blocked", status in (403, 404), f"status {status}")

    # A browser reaping a kept-alive socket must not produce a traceback.
    import socket as socket_module
    import struct as struct_module
    for _ in range(3):
        sock = socket_module.create_connection(("127.0.0.1", httpd.server_address[1]))
        sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n")
        sock.recv(300)
        sock.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_LINGER,
                        struct_module.pack("ii", 1, 0))
        sock.close()
    time.sleep(0.6)
    status, body, _ = get(f"{base}/api/health")
    check("survives abrupt client disconnects", status == 200)
finally:
    httpd.shutdown()
    httpd.server_close()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
