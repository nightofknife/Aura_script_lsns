"""Local native-asset extraction support for investment template generation."""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import json
import re
import sys
from pathlib import Path


def configure(pythonlibs: Path, devtools_src: Path) -> None:
    sys.path[:0] = [str(pythonlibs.resolve()), str(devtools_src.resolve())]


def load_bundle(path: Path, metadata: Path):
    import UnityPy
    from UnityPy.helpers.ArchiveStorageManager import brute_force_key

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return UnityPy.load(str(path))
    except LookupError as exc:
        match = re.search(r"key_sig = (.+), data_sig = (.+),\s*and fp", str(exc))
        if match is None:
            raise RuntimeError("Bundle encryption signatures could not be parsed") from None
        key_signature, data_signature = ast.literal_eval(f"({match[1]}, {match[2]})")
        key = brute_force_key(str(metadata), key_signature, data_signature, verbose=False)
        if key is None:
            raise RuntimeError("Could not read local native UI bundle")
        UnityPy.set_assetbundle_decrypt_key(key)
        return UnityPy.load(str(path))


def inspect_prefab(env) -> dict:
    objects = {obj.path_id: obj.read_typetree() for obj in env.objects
               if obj.type.name in ("GameObject", "RectTransform", "MonoBehaviour")}
    transforms = {pid: tree for pid, tree in objects.items() if "m_AnchoredPosition" in tree}

    def node_path(pid):
        tree = transforms.get(pid)
        if tree is None:
            return ""
        return node_path(tree["m_Father"]["m_PathID"]) + "/" + objects[tree["m_GameObject"]["m_PathID"]]["m_Name"]

    def bounds(pid):
        tree = transforms[pid]
        parent = tree["m_Father"]["m_PathID"]
        if not parent:
            return 0., 0., 1920., 1080.
        px, py, pw, ph = bounds(parent)
        amin, amax = tree["m_AnchorMin"], tree["m_AnchorMax"]
        pivot, pos, size = tree["m_Pivot"], tree["m_AnchoredPosition"], tree["m_SizeDelta"]
        width = pw * (amax["x"] - amin["x"]) + size["x"]
        height = ph * (amax["y"] - amin["y"]) + size["y"]
        x = px + pw * (amin["x"] + (amax["x"] - amin["x"]) * pivot["x"]) + pos["x"] - width * pivot["x"]
        y = py + ph * (amin["y"] + (amax["y"] - amin["y"]) * pivot["y"]) + pos["y"] - height * pivot["y"]
        return x, y, width, height

    nodes = []
    for pid, tree in transforms.items():
        gid = tree["m_GameObject"]["m_PathID"]
        components = [{"path_id": oid, "tree": obj} for oid, obj in objects.items()
                      if obj.get("m_GameObject", {}).get("m_PathID") == gid and "m_AnchoredPosition" not in obj]
        x, y, width, height = bounds(pid)
        nodes.append({"path": node_path(pid), "transform_id": pid, "game_object_id": gid,
                      "active": objects[gid].get("m_IsActive"), "rect_native": [x, y, width, height],
                      "rect_client": [round(x * 2/3), round((1080-y-height) * 2/3), round(width * 2/3), round(height * 2/3)],
                      "transform": tree, "components": components})
    return {"nodes": nodes, "externals": [[entry.path for entry in asset.externals] for asset in env.assets]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--pythonlibs", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    configure(args.pythonlibs, args.devtools_src)
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = args.game_data / "il2cpp_data/Metadata/global-metadata.dat"
    pref = load_bundle(args.game_data / "Patch/Asset/ui/hometrade/hometradeupgrade.asset", metadata)
    data = inspect_prefab(pref)
    (args.output / "native_nodes.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    assets = {}
    for rel in ("ui/font.asset", "ui/hometrade.asset", "ui/common.asset"):
        env = load_bundle(args.game_data / "Patch/Asset" / rel, metadata)
        rows = []
        for obj in env.objects:
            if obj.type.name in ("Font", "Sprite"):
                item = obj.read()
                row = {"path_id": obj.path_id, "type": obj.type.name, "name": item.m_Name}
                if obj.type.name == "Font":
                    row["sha256"] = hashlib.sha256(bytes(item.m_FontData)).hexdigest()
                rows.append(row)
        assets[rel] = rows
    (args.output / "native_assets.json").write_text(json.dumps(assets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"prefab_nodes": len(data["nodes"]), "assets": {k: len(v) for k, v in assets.items()}}))


if __name__ == "__main__":
    main()
