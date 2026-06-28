"""
mode_tools.py — read ORCA `orca_pltvib` animation files, recover the equilibrium
geometry and the mode eigenvector, identify what moves, and emit a mode-displaced
geometry to clean a spurious imaginary.

No substrate-specific indices.

Generate the animation first, e.g.:
    orca_pltvib myrun.hess 6     # writes myrun.hess.v006.xyz (mode 6)

Then:
    syms, eq_xyz, evec = read_pltvib("myrun.hess.v006.xyz")
    top_movers(syms, evec)                      # what dominates the mode
    bond_component(eq_xyz, evec, i, j)          # closing(-)/opening(+) of bond i-j
    write_xyz("guess_clean.xyz", syms, displace(eq_xyz, evec, 0.4))  # off the ridge
"""
import numpy as np


def read_pltvib(path):
    """Parse an orca_pltvib animation (oscillation frames + constant eigenvector).
    Returns (symbols, equilibrium_xyz, eigenvector). Equilibrium = mean over the
    full oscillation; eigenvector = the per-atom displacement columns (5-7)."""
    L = open(path).read().splitlines()
    n = int(L[0])
    # count frames
    nf = 0; i = 0
    while i < len(L):
        try:
            int(L[i].strip())
        except (ValueError, IndexError):
            break
        nf += 1; i += 2 + n
    syms = [ln.split()[0] for ln in L[2:2 + n]]

    def block(k):
        off = k * (n + 2); body = L[off + 2:off + 2 + n]
        C = np.array([[float(x) for x in ln.split()[1:4]] for ln in body])
        D = np.array([[float(x) for x in ln.split()[4:7]] for ln in body])
        return C, D

    eq = np.mean([block(k)[0] for k in range(nf)], axis=0)
    evec = block(0)[1]
    return syms, eq, evec


def top_movers(syms, evec, k=8, roles=None):
    """Print the k atoms with the largest displacement in the mode. `roles` is an
    optional {index: label} dict to annotate known atoms."""
    mag = np.linalg.norm(evec, axis=1)
    order = np.argsort(mag)[::-1][:k]
    roles = roles or {}
    for i in order:
        print(f"  atom {i:3d} {syms[i]:2s} |disp|={mag[i]:.3f}  {roles.get(i, '')}")
    # heuristic: a mode dominated by H motion with no heavy reaction-coordinate
    # atom is almost always a spurious methyl/torsional or cap libration.
    hmass = sum(mag[i] for i in order if syms[i] == "H")
    total = sum(mag[i] for i in order)
    if total > 0 and hmass / total > 0.6:
        print("  NOTE: mode is H-dominated -> likely a spurious rotor/cap libration,"
              " not a reaction coordinate.")
    return order


def bond_component(eq_xyz, evec, i, j):
    """Projection of the relative displacement of atoms i,j onto their bond axis.
    Negative = the bond is closing in this mode; ~0 = the bond is idle."""
    ax = eq_xyz[j] - eq_xyz[i]; ax /= np.linalg.norm(ax)
    return float(np.dot(evec[j] - evec[i], ax))


def classify_ts(eq_xyz, evec, candidate_bonds):
    """Given candidate forming bonds [(i,j,label), ...], report which are active in
    this mode. Use on the lowest imaginary to confirm it is the intended reaction
    coordinate, and to tell concerted (two bonds active) from stepwise (one)."""
    out = []
    for i, j, lbl in candidate_bonds:
        c = bond_component(eq_xyz, evec, i, j)
        out.append((lbl, round(c, 3), "active(closing)" if c < -0.3 else
                    "active(opening)" if c > 0.3 else "idle"))
    for lbl, c, status in out:
        print(f"  {lbl}: closing-component={c:+.3f}  -> {status}")
    return out


def displace(eq_xyz, evec, max_shift=0.4, sign=+1):
    """Return geometry displaced along the mode so the largest per-atom shift is
    `max_shift` A. Use to push off a spurious-mode ridge before re-optimizing
    (then re-run OptTS with a finer grid)."""
    step = max_shift / np.abs(evec).max()
    return eq_xyz + sign * step * evec


def write_xyz(path, syms, xyz, comment="mode-displaced guess"):
    with open(path, "w") as f:
        f.write(f"{len(syms)}\n{comment}\n")
        for s, p in zip(syms, xyz):
            f.write(f"{s:2s} {p[0]:14.8f} {p[1]:14.8f} {p[2]:14.8f}\n")


if __name__ == "__main__":
    print("mode_tools: import and call; see module docstring for usage.")
