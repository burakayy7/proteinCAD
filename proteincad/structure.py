"""A small structure reader for server-side work.

This mirrors the JavaScript model in web/src/core/structure.js closely enough to
be useful, but stays deliberately minimal: coordinates, elements, residues and
chains, with no dependencies. When you plug in a real pipeline you will
probably swap this for biotite / gemmi / biopython -- keep `Structure`'s shape
and the rest of the server will not notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# Approximate atomic masses, enough for a centre of mass.
MASSES = {
    "H": 1.008, "C": 12.011, "N": 14.007, "O": 15.999, "P": 30.974, "S": 32.06,
    "SE": 78.97, "FE": 55.845, "ZN": 65.38, "MG": 24.305, "CA": 40.078,
    "NA": 22.990, "K": 39.098, "CL": 35.45, "MN": 54.938, "CU": 63.546,
}

AMINO = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
    "MSE", "SEC", "PYL",
}
NUCLEIC = {"A", "C", "G", "U", "I", "DA", "DC", "DG", "DT", "DU", "DI"}
WATER = {"HOH", "DOD", "WAT", "H2O"}


@dataclass
class Residue:
    name: str
    seq: int
    chain: str
    kind: str
    start: int
    end: int


@dataclass
class Structure:
    name: str = "structure"
    title: str = ""
    coords: list[tuple[float, float, float]] = field(default_factory=list)
    elements: list[str] = field(default_factory=list)
    atom_names: list[str] = field(default_factory=list)
    residues: list[Residue] = field(default_factory=list)

    @property
    def atom_count(self) -> int:
        return len(self.coords)

    @property
    def chains(self) -> list[str]:
        seen: list[str] = []
        for residue in self.residues:
            if residue.chain not in seen:
                seen.append(residue.chain)
        return seen

    def masses(self) -> list[float]:
        return [MASSES.get(e.upper(), 12.0) for e in self.elements]

    def summary(self) -> dict:
        kinds: dict[str, int] = {}
        for residue in self.residues:
            kinds[residue.kind] = kinds.get(residue.kind, 0) + 1
        return {
            "name": self.name,
            "title": self.title,
            "atoms": self.atom_count,
            "residues": len(self.residues),
            "chains": self.chains,
            "residue_kinds": kinds,
        }


def classify(res_name: str, atom_names: Iterable[str]) -> str:
    name = res_name.strip().upper()
    if name in WATER:
        return "water"
    names = set(atom_names)
    if {"N", "CA", "C"} <= names or name in AMINO:
        return "protein"
    # A lone phosphorus is a phosphate ligand, not a nucleotide: require the
    # sugar as well.
    if "C3'" in names or ("P" in names and "C1'" in names) or name in NUCLEIC:
        return "nucleic"
    if len(names) == 1:
        return "ion"
    return "ligand"


def parse(text: str, name: str = "structure") -> Structure:
    """Read a PDB or mmCIF string, choosing the parser by content."""
    head = text[:4096]
    if head.lstrip().startswith("data_") or "_atom_site." in head:
        return parse_cif(text, name)
    return parse_pdb(text, name)


def parse_pdb(text: str, name: str = "structure") -> Structure:
    structure = Structure(name=name)
    key = None
    model = 0
    titles: list[str] = []
    pending_names: list[str] = []

    for line in text.splitlines():
        record = line[:6]
        if record == "MODEL ":
            model += 1
            continue
        if model > 1:
            continue
        if record == "TITLE ":
            titles.append(line[10:].strip())
            continue
        if record not in ("ATOM  ", "HETATM") or len(line) < 54:
            continue
        alt = line[16]
        if alt not in (" ", "A", "1"):
            continue

        atom_name = line[12:16].strip()
        res_name = line[17:20].strip()
        chain = line[20:22].strip() or "A"
        try:
            seq = int(line[22:26])
        except ValueError:
            seq = 0
        this = (chain, seq, line[26], res_name)
        if this != key:
            if structure.residues and pending_names:
                previous = structure.residues[-1]
                previous.kind = classify(previous.name, pending_names)
            pending_names = []
            key = this
            structure.residues.append(
                Residue(res_name, seq, chain, "ligand", len(structure.coords), len(structure.coords))
            )
        structure.residues[-1].end = len(structure.coords)
        pending_names.append(atom_name)

        element = line[76:78].strip() if len(line) >= 78 else ""
        if not element:
            element = "".join(c for c in atom_name if c.isalpha())[:1]
        structure.coords.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
        structure.elements.append(element)
        structure.atom_names.append(atom_name)

    if structure.residues and pending_names:
        previous = structure.residues[-1]
        previous.kind = classify(previous.name, pending_names)
    structure.title = " ".join(titles)
    return structure


def parse_cif(text: str, name: str = "structure") -> Structure:
    """Read the `_atom_site` loop of an mmCIF file. Values are whitespace
    separated; quoted values are rare in that category and handled simply."""
    structure = Structure(name=name)
    lines = text.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()
        if line == "loop_":
            tags: list[str] = []
            i += 1
            while i < n and lines[i].strip().startswith("_"):
                tags.append(lines[i].strip())
                i += 1
            if not tags or not tags[0].startswith("_atom_site."):
                continue
            columns = {tag.split(".", 1)[1]: index for index, tag in enumerate(tags)}
            _read_atom_rows(lines, i, n, columns, structure)
            break
        if line.startswith("_struct.title"):
            structure.title = line.split(None, 1)[1].strip("'\"") if len(line.split(None, 1)) > 1 else ""
        i += 1

    return structure


def _read_atom_rows(lines, i, n, columns, structure) -> None:
    def column(*candidates):
        for candidate in candidates:
            if candidate in columns:
                return columns[candidate]
        return -1

    c_atom = column("auth_atom_id", "label_atom_id")
    c_comp = column("auth_comp_id", "label_comp_id")
    c_asym = column("auth_asym_id", "label_asym_id")
    c_seq = column("auth_seq_id", "label_seq_id")
    c_alt = column("label_alt_id")
    c_symbol = column("type_symbol")
    c_x, c_y, c_z = column("Cartn_x"), column("Cartn_y"), column("Cartn_z")
    c_model = column("pdbx_PDB_model_num")

    key = None
    first_model = None
    pending: list[str] = []

    while i < n:
        raw = lines[i]
        i += 1
        if not raw or raw.startswith("#") or raw.startswith("_") or raw.startswith("loop_"):
            break
        row = raw.split()
        if len(row) < len(columns):
            continue

        if c_model >= 0:
            if first_model is None:
                first_model = row[c_model]
            elif row[c_model] != first_model:
                continue
        if c_alt >= 0 and row[c_alt] not in (".", "?", "A", "1"):
            continue

        res_name = row[c_comp].strip("'\"")
        chain = row[c_asym].strip("'\"")
        try:
            seq = int(row[c_seq])
        except ValueError:
            seq = 0
        this = (chain, seq, res_name)
        if this != key:
            if structure.residues and pending:
                previous = structure.residues[-1]
                previous.kind = classify(previous.name, pending)
            pending = []
            key = this
            structure.residues.append(
                Residue(res_name, seq, chain, "ligand", len(structure.coords), len(structure.coords))
            )
        structure.residues[-1].end = len(structure.coords)
        pending.append(row[c_atom].strip("'\""))

        structure.coords.append((float(row[c_x]), float(row[c_y]), float(row[c_z])))
        structure.elements.append(row[c_symbol] if c_symbol >= 0 else "C")
        structure.atom_names.append(row[c_atom].strip("'\""))

    if structure.residues and pending:
        previous = structure.residues[-1]
        previous.kind = classify(previous.name, pending)
