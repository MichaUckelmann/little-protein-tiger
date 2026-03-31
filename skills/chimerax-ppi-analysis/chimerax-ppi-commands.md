# ChimeraX PPI Analysis Command Reference

## Table of Contents
1. Opening and Inspecting Structures
2. Interface Calculation
3. Hydrogen Bonds and Contacts
4. Selection and Naming
5. Residue Classification Queries
6. Visualization and Styling
7. Atomspec Quick Reference for PPI Analysis

---

## 1. Opening and Inspecting Structures

### Open from PDB
```
chimerax:open_structure  identifier="3kys"
```

### Get model overview
```
chimerax:get_model_info  model_id="#1"
```
Returns: atom count, residue count, chain IDs, chain descriptions, UniProt refs.

### Get single chain detail
```
chimerax:get_chain_info  model_id="#1"  chain_id="A"
```

### Hide extra chains (duplicate copies in asymmetric unit)
```
chimerax:run_command  command="hide #1/C,D cartoons"
chimerax:run_command  command="hide #1/C,D atoms"
```

---

## 2. Interface Calculation

### Buried surface area between two chains
```
chimerax:run_command  command="interfaces #1/A,B"
```
Output example: `1 buried areas: A B 1748`

Interpretation for binder modality selection:
- < 500 Å²: crystal packing contact, probably not biological
- 500–1000 Å²: small interface — cyclic peptide likely sufficient
- 1000–2000 Å²: typical stable PPI — cyclic peptide or small mini-protein
- > 2000 Å²: large interface — mini-protein recommended to cover enough surface

### Select interface residues (both sides)
```
chimerax:run_command  command="interfaces select #1/A contacting #1/B bothSides true"
```
Default thresholds: interface area >= 300 Å², per-residue area >= 15 Å².

### Select interface residues (target side only)
```
chimerax:run_command  command="interfaces select #1/A contacting #1/B bothSides false"
```

### Custom thresholds
```
chimerax:run_command  command="interfaces select #1/A contacting #1/B bothSides true interfaceResidueAreaCutoff 10"
```

---

## 3. Hydrogen Bonds and Contacts

### Interchain hydrogen bonds
```
chimerax:run_command  command="hbonds #1/A restrict #1/B reveal true color yellow"
```

### Van der Waals contacts
```
chimerax:run_command  command="contacts #1/A restrict #1/B reveal true color orange overlapCutoff -0.4 hbondAllowance 0.4"
```

### Salt bridges (charged residue contacts)
```
chimerax:run_command  command="contacts #1/A & :asp,glu restrict #1/B & :arg,lys,his reveal true"
```

---

## 4. Selection and Naming

### Save current selection as a named target
```
chimerax:run_command  command="name frozen iface sel"
```
`frozen` captures the selection at creation time. Without `frozen`, the name
re-evaluates to whatever `sel` means when used.

### Distance-based selections (zones)
Select chain A residues within 5 Å of chain B:
```
chimerax:run_command  command="select #1/A & (#1/B :< 5)"
```

---

## 5. Residue Classification Queries

These queries inventory the interface composition on a specific chain. Replace
`<target>` with the target chain ID (e.g. A).

### Hydrophobic (core binding energy)
```
chimerax:run_command  command="info residues iface & :ala,val,leu,ile,met,phe,trp,pro & #1/<target>"
```

### Aromatic (stacking, cation-π)
```
chimerax:run_command  command="info residues iface & :phe,tyr,trp,his & #1/<target>"
```

### Charged (salt bridges, specificity)
```
chimerax:run_command  command="info residues iface & :asp,glu,arg,lys & #1/<target>"
```

### Polar uncharged (H-bonds, orientation)
```
chimerax:run_command  command="info residues iface & :ser,thr,asn,gln,tyr,cys & #1/<target>"
```

### All interface residues on target
```
chimerax:run_command  command="info residues iface & #1/<target> & ~:hoh"
```

### All interface residues on partner
```
chimerax:run_command  command="info residues iface & #1/<partner> & ~:hoh"
```

---

## 6. Visualization and Styling

### Basic cartoon + coloring per chain
```
chimerax:run_command  command="cartoon #1/A,B"
chimerax:color_models  color="light blue"  target="#1/A"
chimerax:color_models  color="salmon"  target="#1/B"
```

### Display interface residues as sticks
```
chimerax:run_command  command="display iface & ~:hoh"
chimerax:run_command  command="style iface stick"
```

### Color by heteroatom
```
chimerax:run_command  command="color iface byhet"
```

### Label residues
```
chimerax:run_command  command="label #1/A:276,314,350 residues"
```

### Clean presentation setup
```
chimerax:run_command  command="select clear"
chimerax:run_command  command="set bgColor white"
chimerax:run_command  command="lighting soft"
chimerax:run_command  command="view #1/A,B"
```

### Focus on a specific region
```
chimerax:run_command  command="view #1/B:85-100 #1/A:240-280,350-410"
```

---

## 7. Atomspec Quick Reference for PPI Analysis

| Task | Atomspec |
|------|----------|
| All protein in chain A | `#1/A & protein` |
| Interface residues (after naming) | `iface` |
| Hydrophobic interface on target | `iface & :ala,val,leu,ile,met,phe,trp,pro & #1/A` |
| Target-side interface only | `iface & #1/A` |
| Partner-side interface only | `iface & #1/B` |
| Residues near a specific residue | `#1/A:95 :< 5` |
| Sidechain atoms only | `iface & sidechain` |
| Backbone only | `iface & backbone` |
| Exclude water | `iface & ~:hoh` |
| Aromatic residues at interface | `iface & :phe,tyr,trp,his` |
| Combine chain + residue type | `#1/B & :met,leu,phe & iface` |