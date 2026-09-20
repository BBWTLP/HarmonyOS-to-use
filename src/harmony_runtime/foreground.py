"""Conservative foreground identity from explicit HarmonyOS diagnostic fields."""
import re


def parse_foreground(focus_before, missions, focus_after):
    def focus(text):
        values = re.findall(r"^\s*Focus window:\s*(\d+)\s*$", text.replace("\r", ""), re.M)
        return values[0] if len(values) == 1 else None

    first, last = focus(focus_before), focus(focus_after)
    unknown = {"bundle": None, "focus_id": last, "mission_id": None, "ability_id": None}
    if first is None or first != last:
        return {**unknown, "status": "unstable"}
    text = missions.replace("\r", "")
    blocks = re.split(r"(?=^\s*Mission ID #\d+\b)", text, flags=re.M)
    blocks = [b for b in blocks if re.match(r"^\s*Mission ID #" + first + r"\b", b)]
    if len(blocks) != 1:
        return {**unknown, "status": "unknown"}
    block = blocks[0]
    mission = re.findall(r"mission name #\[#([A-Za-z][A-Za-z0-9_.]+):[^]\n]+\]", block)
    bundles = re.findall(r"^\s*bundle name \[([A-Za-z][A-Za-z0-9_.]+)\]\s*$", block, re.M)
    abilities = re.findall(r"^\s*AbilityRecord ID #(\d+)\s*$", block, re.M)
    states = re.findall(r"^\s*state #([A-Z_]+)\b", block, re.M)
    app_states = re.findall(r"^\s*app state #([A-Z_]+)\s*$", block, re.M)
    if (len(mission) != 1 or bundles != mission or len(abilities) != 1
            or states != ["FOREGROUND"] or app_states != ["FOREGROUND"]):
        return {**unknown, "status": "unknown"}
    return {"bundle": bundles[0], "focus_id": first, "mission_id": first,
            "ability_id": abilities[0], "status": "verified"}
