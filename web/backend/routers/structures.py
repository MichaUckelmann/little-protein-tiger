"""
Structures router — serves CIF files for the Mol* viewer.

Endpoint:
  GET /structures/{pdb_id}.cif   Authenticated; strict PDB-ID validation
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from web.backend.auth import get_current_user_id

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_STRUCTURES_DIR = _ROOT / "data" / "structures"

router = APIRouter(tags=["structures"])


@router.get("/structures/{pdb_id}.cif")
def get_structure(
    pdb_id: str,
    user_id: int = Depends(get_current_user_id),  # auth guard
):
    """
    Return a CIF file for the Mol* structure viewer.

    Only authenticated users may access structures.
    PDB ID is validated to be exactly 4 alphanumeric characters to
    prevent path traversal.
    """
    # Strict guard: PDB IDs are exactly 4 alphanumeric characters
    if not re.match(r"^[A-Za-z0-9]{4}$", pdb_id):
        raise HTTPException(status_code=400, detail="Invalid PDB ID format")

    path = _STRUCTURES_DIR / f"{pdb_id.upper()}.cif"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Structure {pdb_id.upper()} not found. "
                   "It may need to be downloaded first.",
        )

    return FileResponse(
        path,
        media_type="chemical/x-cif",
        filename=f"{pdb_id.upper()}.cif",
    )
