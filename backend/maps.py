"""Radar metadata and world->radar transform.

Calibration values come from CS Demo Manager (src/node/database/maps/default-maps.ts) which in turn
mirrors the game's resource/overviews/<map>.txt. Radar images are 1024x1024:
    px = (x - pos_x) / scale
    py = (pos_y - y) / scale
`threshold_z`: players with z below it are on the lower level (maps with a *_lower.png).
"""
from __future__ import annotations

from .config import RADARS_DIR

RADAR_BASE_URL = "https://raw.githubusercontent.com/akiver/cs-demo-manager/main/static/images/maps/cs2/radars/"

MAP_DATA: dict[str, dict] = {
    "cs_agency": {"pos_x": -2597, "pos_y": 2079, "scale": 4.1817436, "threshold_z": 0},
    "cs_alpine": {"pos_x": -2107, "pos_y": 4687, "scale": 6.5296063, "threshold_z": 0},
    "cs_assault": {"pos_x": 4041, "pos_y": 7838, "scale": 4.6, "threshold_z": 0},
    "cs_italy": {"pos_x": -2647, "pos_y": 2592, "scale": 4.6, "threshold_z": 0},
    "cs_militia": {"pos_x": -1474, "pos_y": 2296, "scale": 4.5, "threshold_z": 25, "lower": True},
    "cs_office": {"pos_x": -1838, "pos_y": 1858, "scale": 4.1, "threshold_z": 0},
    "cs_shelter": {"pos_x": -3448, "pos_y": 3805, "scale": 3.311857, "threshold_z": 0},
    "de_ancient": {"pos_x": -2953, "pos_y": 2164, "scale": 5, "threshold_z": 0},
    "de_anubis": {"pos_x": -2796, "pos_y": 3328, "scale": 5.22, "threshold_z": 0},
    "de_assembly": {"pos_x": 1628, "pos_y": 4512, "scale": 2.84, "threshold_z": 0},
    "de_aztec": {"pos_x": -3200, "pos_y": 2841, "scale": 6, "threshold_z": 0},
    "de_basalt": {"pos_x": -2345, "pos_y": 2391, "scale": 4.37, "threshold_z": 0},
    "de_boulder": {"pos_x": -3273, "pos_y": 2930, "scale": 2.8491137, "threshold_z": 62, "lower": True},
    "de_brewery": {"pos_x": -4122, "pos_y": 4394, "scale": 2.1820312, "threshold_z": 0},
    "de_cache": {"pos_x": -2000, "pos_y": 3250, "scale": 5.5, "threshold_z": 0},
    "de_canals": {"pos_x": -2496, "pos_y": 1792, "scale": 4, "threshold_z": 0},
    "de_cbble": {"pos_x": -3840, "pos_y": 3072, "scale": 6, "threshold_z": 0},
    "de_debris": {"pos_x": -3015, "pos_y": 2919, "scale": 1.9445695, "threshold_z": 0},
    "de_dogtown": {"pos_x": -1741, "pos_y": 3295, "scale": 2.0323243, "threshold_z": 0},
    "de_dust": {"pos_x": -2850, "pos_y": 4073, "scale": 6, "threshold_z": 0},
    "de_dust2": {"pos_x": -2476, "pos_y": 3239, "scale": 4.4, "threshold_z": 0},
    "de_edin": {"pos_x": -383, "pos_y": 4420, "scale": 4.803717, "threshold_z": 0},
    "de_eldorado": {"pos_x": -3548, "pos_y": 2571, "scale": 2.0656068, "threshold_z": 0},
    "de_fachwerk": {"pos_x": -2311, "pos_y": 2874, "scale": 2.5537856, "threshold_z": 0},
    "de_golden": {"pos_x": -856, "pos_y": 2399, "scale": 4.506695, "threshold_z": 0},
    "de_grail": {"pos_x": -4395, "pos_y": 4203, "scale": 2.1756864, "threshold_z": 0},
    "de_inferno": {"pos_x": -2087, "pos_y": 3870, "scale": 4.9, "threshold_z": 0},
    "de_jura": {"pos_x": -2126, "pos_y": 2389, "scale": 2.504188, "threshold_z": 0},
    "de_memento": {"pos_x": -2111, "pos_y": 2534, "scale": 3.9720395, "threshold_z": 0},
    "de_mills": {"pos_x": -4810, "pos_y": -320, "scale": 5.148437, "threshold_z": 0},
    "de_mirage": {"pos_x": -3230, "pos_y": 1713, "scale": 5, "threshold_z": 0},
    "de_nuke": {"pos_x": -3453, "pos_y": 2887, "scale": 7, "threshold_z": -495, "lower": True},
    "de_overpass": {"pos_x": -4831, "pos_y": 1781, "scale": 5.2, "threshold_z": 0},
    "de_palacio": {"pos_x": -2443, "pos_y": 1896, "scale": 3.8676212, "threshold_z": 0},
    "de_palais": {"pos_x": -1353, "pos_y": 2044, "scale": 2.8003397, "threshold_z": 103, "lower": True},
    "de_poseidon": {"pos_x": -1046, "pos_y": 1166, "scale": 3.0124886, "threshold_z": 0},
    "de_rooftop": {"pos_x": -1847, "pos_y": 2301, "scale": 1.914732, "threshold_z": 0},
    "de_sanctum": {"pos_x": -1846, "pos_y": 674, "scale": 3.658203, "threshold_z": 0},
    "de_stronghold": {"pos_x": -2786, "pos_y": 2598, "scale": 2.9476247, "threshold_z": 0},
    "de_thera": {"pos_x": -85, "pos_y": 2261, "scale": 4.846961, "threshold_z": -4, "lower": True},
    "de_train": {"pos_x": -2308, "pos_y": 2078, "scale": 4.082077, "threshold_z": 0, "lower": True},
    "de_vertigo": {"pos_x": -3168, "pos_y": 1762, "scale": 4, "threshold_z": 11700, "lower": True},
    "de_warden": {"pos_x": -3150, "pos_y": 2681, "scale": 5.642361, "threshold_z": 0},
    "de_whistle": {"pos_x": -1825, "pos_y": 1104, "scale": 2.8, "threshold_z": 0},
}

# Maps fetched by default by scripts/fetch_radars.py (active duty + common FACEIT pool).
DEFAULT_MAPS = ["de_mirage", "de_inferno", "de_dust2", "de_nuke", "de_ancient", "de_anubis",
                "de_vertigo", "de_overpass", "de_train", "de_cache"]


def map_info(map_name: str) -> dict | None:
    d = MAP_DATA.get(map_name)
    if not d:
        return None
    out = dict(d)
    out["name"] = map_name
    out["size"] = 1024
    out["has_radar"] = (RADARS_DIR / f"{map_name}.png").exists()
    out["has_lower"] = bool(d.get("lower")) and (RADARS_DIR / f"{map_name}_lower.png").exists()
    return out


def world_to_radar(map_name: str, x: float, y: float) -> tuple[float, float]:
    d = MAP_DATA[map_name]
    return (x - d["pos_x"]) / d["scale"], (d["pos_y"] - y) / d["scale"]
