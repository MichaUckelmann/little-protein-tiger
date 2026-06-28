# ORCA input templates

Version-specific. Verify keywords/constraint syntax against the ORCA manual for
your build. Method line below is a reasonable hybrid-DFT default for organic
active sites; choose deliberately per system. `nprocs`, `maxcore`, solvent, and
grid are tuning knobs.

Standard method line used throughout (edit as appropriate):
`wB97X-D3 def2-TZVP def2/J RIJCOSX <task> TightSCF DefGrid3 SlowConv`

Constraint syntax (0-based atom indices):
- freeze a forming bond at its current value: `{B i j C}`
- freeze an atom in space (anchor):           `{C i C}`

Charge/multiplicity go on the coordinate line: `* xyzfile <charge> <mult> file.xyz`.
Closed shell: omit `UKS`. Open shell / metals: add `UKS` and set the multiplicity.

---

## Stage 1 — constrained relax (settle the pocket)
Freeze the forming bond(s) AND every residue anchor.

```
! wB97X-D3 def2-TZVP def2/J RIJCOSX Opt TightSCF DefGrid3 SlowConv
%maxcore 4000
%pal nprocs 8 end
%geom
   Constraints
      {B 0 18 C}        # forming bond(s) — replace with your indices
      {C 35 C}          # anchor 1  — replace with your anchors
      {C 47 C}
      {C 59 C}
   end
   MaxIter 200
end
%cpcm smd true; SMDsolvent "water" end
* xyzfile 0 1 guess_pocket.xyz
```

## Stage 2 — TS optimization
Anchors stay frozen; forming bonds released.

```
! wB97X-D3 def2-TZVP def2/J RIJCOSX OptTS Freq TightSCF TightOpt DefGrid3 SlowConv
%maxcore 4000
%pal nprocs 8 end
%geom
   Constraints {C 35 C} {C 47 C} {C 59 C} end
   Calc_Hess true
   Recalc_Hess 5
   MaxIter 300
end
%cpcm smd true; SMDsolvent "water" end
* xyzfile 0 1 relax_pocket.xyz
```

For metals, split Freq off and use NumFreq (analytic Freq is unreliable there):

```
! UKS wB97X-D3 def2-TZVP def2/J RIJCOSX NumFreq TightSCF DefGrid3
%maxcore 4000
%pal nprocs 8 end
%geom Constraints {C 36 C} {C 48 C} {C 60 C} end end
%cpcm smd true; SMDsolvent "water" end
* xyzfile 1 2 ts_metal.xyz      # e.g. Cu(II): charge +1, doublet
```

## Stage 3 — frequency (closed shell)
Anchors frozen. Focus on the single reaction-coordinate imaginary; ignore tiny
modes from the frozen anchors / truncated caps.

```
! wB97X-D3 def2-TZVP def2/J RIJCOSX Freq TightSCF DefGrid3
%maxcore 4000
%pal nprocs 8 end
%geom Constraints {C 35 C} {C 47 C} {C 59 C} end end
%cpcm smd true; SMDsolvent "water" end
* xyzfile 0 1 ts_pocket.xyz
```

## Reactant-complex relaxed scan (for the barrier)
Walk the forming bond out to the pre-reaction complex; lowest point = reactant.
Run the SAME scan for bare and pocket (anchors frozen for the pocket).

```
! wB97X-D3 def2-TZVP def2/J RIJCOSX Opt TightSCF DefGrid3 SlowConv
%maxcore 4000
%pal nprocs 8 end
%geom
   Scan B 0 18 = 2.20, 3.60, 10 end     # forming bond, ~0.16 A steps
   Constraints {C 35 C} {C 47 C} {C 59 C} end   # omit this line for the bare run
   MaxIter 200
end
%cpcm smd true; SMDsolvent "water" end
* xyzfile 0 1 ts_pocket.xyz
```
Barrier: ΔE‡ = [E(TS, from OptTS FINAL SINGLE POINT ENERGY)
               − E(reactant, lowest scan "Actual Energy")] × 627.5095  kcal/mol.

## IRC (confirm the saddle connects the intended reactant/product)
```
! wB97X-D3 def2-TZVP def2/J RIJCOSX IRC TightSCF DefGrid3
%maxcore 4000
%pal nprocs 8 end
%irc
   InitHess read
   Hess_Filename "ts_run.hess"
   MaxIter 100
end
%cpcm smd true; SMDsolvent "water" end
* xyzfile 0 1 ts_run.xyz
```

## Matched reference (rigorous catalysis check)
Same atoms/charge as the pocket, but translate the donor residues away from the
reacting atoms (keep them in the system so the implicit-solvent/donor count
matches) and recompute TS and reactant. ΔΔE‡ vs the on-site pocket isolates true
TS stabilization from solvation bookkeeping. Build by shifting each donor
fragment along its outward direction by ~6-8 A and re-freezing the anchors.
