from .models import ListingClaims
from .reference import REFERENCE


class ListingError(ValueError):
    pass


def parse_listing(form: dict[str, str], allow_other: bool = False) -> ListingClaims:
    raw_model = form.get("model", "").strip()
    model = raw_model.upper().replace("GEFORCE ", "")
    if allow_other and model == "OTHER (NOT IN TABLE)":
        model = "Other (not in table)"
    elif model not in REFERENCE:
        raise ListingError("Choose a supported demo model." if not allow_other else "Choose a model from the list.")
    try:
        vram = int(form.get("vram_gb", ""))
    except ValueError as exc:
        raise ListingError("VRAM must be a whole number.") from exc
    if not 1 <= vram <= 256:
        raise ListingError("VRAM must be between 1 and 256 GB.")
    partner = form.get("board_partner", "").strip()
    if not partner or len(partner) > 64:
        raise ListingError("Board partner is required and limited to 64 characters.")
    serial = form.get("serial", "").strip() or None
    vbios = form.get("vbios_version", "").strip() or None
    notes = form.get("notes", "").strip()
    for label, value, limit in (("serial", serial, 64), ("BIOS version", vbios, 64), ("notes", notes, 500)):
        if value and len(value) > limit:
            raise ListingError(f"{label} is too long.")
    return ListingClaims(model, vram, partner, serial, vbios, notes)
