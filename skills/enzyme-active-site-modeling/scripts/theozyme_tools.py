"""
theozyme_tools.py — parameterized geometry helpers for grafting truncated
catalytic groups onto a QM transition state.

No substrate-specific indices. Everything takes positions/indices as arguments.

Depends on: numpy, rdkit  (pip install rdkit numpy --break-system-packages)

Typical use:
    core_syms, core_xyz = read_xyz("ts.xyz")          # the converged TS
    centroid = core_xyz.mean(0)
    placed = [core_xyz]                                # accumulate placed pieces
    # oxyanion-hole donor on a carbonyl O (idx O, carbonyl C idx Cc, plane atom Oalk):
    for d in lone_pair_dirs(core_xyz[O], core_xyz[Cc], core_xyz[Oalk]):
        frag, fxyz, anchor, donorN = place_donor(
            "CC(=O)NC", core_xyz[O], d, dist=2.85, existing=placed)
        placed.append(fxyz)
    # cation/neutral donor at an acceptor (e.g. imine N), donor N near, anchor far:
    frag, fxyz, anchor, donorN = place_donor(
        "C[NH3+]", core_xyz[N_acc], outward_dir(core_xyz[N_acc], centroid),
        dist=2.9, existing=placed)
"""
import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem


# ---------- basic linear algebra ----------
def align_vec(a, b):
    """Rotation matrix taking unit-ish vector a onto b (Rodrigues)."""
    a = a / np.linalg.norm(a); b = b / np.linalg.norm(b)
    v = np.cross(a, b); c = float(np.dot(a, b)); s = np.linalg.norm(v)
    if s < 1e-8:
        if c > 0:
            return np.eye(3)
        perp = np.array([1.0, 0, 0]) if abs(a[0]) < 0.9 else np.array([0, 1.0, 0])
        ax = np.cross(a, perp); ax /= np.linalg.norm(ax)
        K = np.array([[0, -ax[2], ax[1]], [ax[2], 0, -ax[0]], [-ax[1], ax[0], 0]])
        return np.eye(3) + 2 * K @ K
    K = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + K + K @ K * ((1 - c) / (s * s))


def rot_about(axis, ang):
    """Rotation matrix about `axis` by `ang` radians."""
    axis = axis / np.linalg.norm(axis)
    K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + np.sin(ang) * K + (1 - np.cos(ang)) * K @ K


def kabsch(P, Q):
    """Rigid transform (R, t) minimizing ||R·P + t − Q|| for paired point sets."""
    Pc = P.mean(0); Qc = Q.mean(0)
    H = (P - Pc).T @ (Q - Qc)
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1, 1, d]) @ U.T
    return R, Qc - R @ Pc


# ---------- io ----------
def read_xyz(path):
    L = open(path).read().splitlines(); n = int(L[0])
    S = [ln.split()[0] for ln in L[2:2 + n]]
    X = np.array([[float(x) for x in ln.split()[1:4]] for ln in L[2:2 + n]])
    return S, X


def write_xyz(path, syms, xyz, comment=""):
    with open(path, "w") as f:
        f.write(f"{len(syms)}\n{comment}\n")
        for s, p in zip(syms, xyz):
            f.write(f"{s:2s} {p[0]:14.8f} {p[1]:14.8f} {p[2]:14.8f}\n")


# ---------- fragment building ----------
def embed_fragment(smiles, seed=0xC0FFEE):
    """SMILES -> RDKit mol with one optimized 3D conformer (H added)."""
    m = Chem.AddHs(Chem.MolFromSmiles(smiles))
    p = AllChem.ETKDGv3(); p.randomSeed = seed
    if AllChem.EmbedMolecule(m, p) != 0:
        AllChem.EmbedMolecule(m, AllChem.ETKDGv2())
    if AllChem.MMFFOptimizeMolecule(m, maxIters=2000) != 0:
        AllChem.UFFOptimizeMolecule(m, maxIters=2000)
    return m


def coords(mol):
    c = mol.GetConformer()
    return np.array([list(c.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())])


# ---------- placement geometry ----------
def outward_dir(atom_pos, centroid):
    """Unit vector pointing from the molecule's centroid out through an atom."""
    d = atom_pos - centroid
    return d / np.linalg.norm(d)


def lone_pair_dirs(O_pos, C_pos, plane_other_pos, angle_deg=60.0):
    """Two sp2 carbonyl-O lone-pair directions, in the carbonyl plane.
    O_pos: carbonyl O; C_pos: its carbon; plane_other_pos: a third atom (e.g. the
    ester alkoxy O) defining the carbonyl plane. Returns two unit directions."""
    oc = O_pos - C_pos; oc /= np.linalg.norm(oc)
    pn = np.cross(O_pos - C_pos, plane_other_pos - C_pos); pn /= np.linalg.norm(pn)
    return [rot_about(pn, np.deg2rad(angle_deg)) @ oc,
            rot_about(pn, -np.deg2rad(angle_deg)) @ oc]


def relieve_clash(frag_xyz, dir_out, existing, min_dist=1.5, step=0.12, n=60):
    """Translate a fragment along dir_out until no atom is closer than min_dist
    to any already-placed atom."""
    base = np.vstack(existing)
    Pf = frag_xyz.copy()
    for _ in range(n):
        if np.linalg.norm(base[:, None, :] - Pf[None, :, :], axis=2).min() >= min_dist:
            break
        Pf = Pf + dir_out * step
    return Pf


def spin_to_open_space(frag_xyz, pivot, axis, existing, nsteps=36):
    """Spin a fragment about `axis` through `pivot`, keeping the donor->acceptor
    contact fixed, to maximize the minimum distance to already-placed atoms
    (swings bulky parts into open space). Returns the best orientation."""
    base = np.vstack(existing)
    best = frag_xyz; best_min = -1.0
    for th in np.linspace(0, 2 * np.pi, nsteps, endpoint=False):
        Pf = (rot_about(axis, th) @ (frag_xyz - pivot).T).T + pivot
        mn = np.linalg.norm(base[:, None, :] - Pf[None, :, :], axis=2).min()
        if mn > best_min:
            best_min = mn; best = Pf
    return best


# ---------- donor identification + high-level placement ----------
def amide_NH(mol):
    """Return (N_idx, H_idx) for an amide/ammonium-type N-H donor in a fragment.
    Picks the N bearing the most H; returns one of its H's."""
    Ns = [a.GetIdx() for a in mol.GetAtoms() if a.GetSymbol() == "N"]
    N = max(Ns, key=lambda i: sum(nb.GetSymbol() == "H"
            for nb in mol.GetAtomWithIdx(i).GetNeighbors()))
    H = [nb.GetIdx() for nb in mol.GetAtomWithIdx(N).GetNeighbors()
         if nb.GetSymbol() == "H"][0]
    return N, H


def farthest_heavy_anchor(mol, donor_idx):
    """Pick the heavy atom farthest (graph-wise, approximated by 3D distance) from
    the donor as the frozen backbone-surrogate anchor."""
    P = coords(mol)
    heavy = [a.GetIdx() for a in mol.GetAtoms() if a.GetSymbol() != "H"]
    return max(heavy, key=lambda i: np.linalg.norm(P[i] - P[donor_idx]))


def place_donor(smiles, acceptor_pos, direction, dist, existing,
                seed=0xC0FFEE, min_dist=1.5):
    """Build a donor fragment and place its N-H pointing at `acceptor_pos` along
    `direction`, with the body spun into open space and clashes relieved.

    Returns (mol, placed_xyz, anchor_idx, donorN_idx). Freeze `anchor_idx` (a
    Cartesian constraint) in all ORCA stages. `direction` should point from the
    acceptor outward (use outward_dir or one of lone_pair_dirs)."""
    mol = embed_fragment(smiles, seed)
    P = coords(mol)
    N, H = amide_NH(mol)
    anchor = farthest_heavy_anchor(mol, N)
    R = align_vec(P[H] - P[N], -direction)          # N-H points back at acceptor
    P = (R @ (P - P[N]).T).T + (acceptor_pos + direction * dist)
    P = spin_to_open_space(P, P[N], direction, existing)
    P = relieve_clash(P, direction, existing, min_dist=min_dist)
    return mol, P, anchor, N


def graft_residue(smiles, match_local, match_target, existing, seed=0xC0FFEE):
    """General rigid graft: align fragment atoms `match_local` (indices into the
    freshly embedded fragment) onto world positions `match_target` (Nx3) via
    Kabsch. Use when you need a specific multi-atom functional group placed
    (e.g. side-chain amide, metal first shell). Returns (mol, placed_xyz)."""
    mol = embed_fragment(smiles, seed)
    P = coords(mol)
    R, t = kabsch(P[np.array(match_local)], np.asarray(match_target))
    return mol, (R @ P.T).T + t


def assemble(core_syms, core_xyz, pieces):
    """Combine core + placed fragment pieces into one (syms, xyz, anchors).
    pieces: list of (mol, placed_xyz, anchor_local_idx). Returns global anchor
    indices (0-based) for the ORCA Cartesian constraints."""
    syms = list(core_syms); xyz = [core_xyz]; anchors = []; off = len(core_syms)
    for mol, Pf, anc in pieces:
        syms += [a.GetSymbol() for a in mol.GetAtoms()]
        xyz.append(Pf); anchors.append(off + anc); off += mol.GetNumAtoms()
    return syms, np.vstack(xyz), anchors


if __name__ == "__main__":
    print("theozyme_tools: import and call; see module docstring for usage.")
