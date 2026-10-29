"""Extract settlement text IDs and prefab provenance; never run the game."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--game-data-reader", type=Path, required=True)
    parser.add_argument("--lua-reader", type=Path, required=True)
    parser.add_argument("--pythonlibs", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    plan = root / "plans/resonance_pc"
    data = next(args.game_root.glob("*_Data"))
    sys.path[:0] = [str(args.pythonlibs), str(root / "tools")]
    from _trade_investment_native import inspect_prefab, load_bundle

    game = module("battle_native_data", args.game_data_reader)
    lua = module("battle_lua_reader", args.lua_reader)
    text_path = data / "Patch/BinaryConfig/TextFactory.bin"
    table = game.BinaryConfig(text_path)
    ids = {"victory": [80606157, 80606163, 80606164], "defeat": [80606176],
           "timeout": [80606186], "next": [80606161], "arrest": [80610301]}
    titles = {key: list(dict.fromkeys(table.get(i)["text"] for i in values)) for key, values in ids.items()}
    sources = {}
    for relative in ("ui/settlement/settlement.asset", "ui/prison/arrestprisoner/arresttips.asset"):
        path = data / "Patch/Asset" / relative
        rows = inspect_prefab(load_bundle(path, data / "il2cpp_data/Metadata/global-metadata.dat"))
        relevant = [n for n in rows["nodes"] if any(
            token in n["path"] for token in ("Txt_Victory", "Txt_Defeat", "Txt_victory", "Btn_Next", "Btn_Add", "Btn_Min", "Txt_Select"))]
        sources[relative] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "nodes": relevant}
    scripts = {}
    for relative, selected in (
        ("UISettlement/UISettlementLuaBehaviour.lua", {2099}),
        ("UISettlement/UISettlementViewFunction.lua", {133, 246, 379}),
        ("UIArrestTips/UIArrestTipsDataModel.lua", {5, 23, 36, 56}),
    ):
        path = data / "Patch/Script" / relative
        node = lua.Reader(path.read_bytes()).function()
        functions = []
        def walk(item):
            if item["start"] in selected:
                functions.append({"start": item["start"], "end": item["end"], "constants": item["values"]})
            for child in item["children"]:
                walk(child)
        walk(node)
        scripts[relative] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "functions": functions}
    config = {
        "reference_client": [1280, 720], "recognition": "native_title_exact_ocr_with_settlement_controls",
        "titles": titles, "text_ids": ids,
        "text_source": {"file": "Patch/BinaryConfig/TextFactory.bin", "sha256": hashlib.sha256(text_path.read_bytes()).hexdigest()},
        "settlement_source": {"prefab": "ui/settlement/settlement.asset", "sha256": sources["ui/settlement/settlement.asset"]["sha256"],
            "victory_nodes": ["/Settlement/Group_Victory/Group_MVP/Txt_Victory", "/Settlement/Group_Victory/Group_Tips/Txt_victory_CN", "/Settlement/Group_Victory/Group_CoreTips/Txt_victory_CN"],
            "defeat_node": "/Settlement/Group_Defeat/Img_Bg/Txt_Defeat",
            "next_callbacks": ["Settlement_Group_Victory_Btn_Next_Click", "Settlement_Group_Defeat_Btn_Next_Click"]},
        "arrest": {"prefab": "ui/prison/arrestprisoner/arresttips.asset", "sha256": sources["ui/prison/arrestprisoner/arresttips.asset"]["sha256"],
            "min_text": "最小", "plus_text": "+1", "count_region": [460, 315, 350, 100],
            "plus_region": [770, 340, 115, 80], "min_region": [330, 340, 110, 80]},
        "live_validation": "not_run",
    }
    output = plan / "data/meta/battle_combat_runtime_pc.json"
    output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    evidence = plan / "development/template_sources/battle/combat"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "native_sources.json").write_text(json.dumps({"prefabs": sources, "scripts": scripts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "evidence": str(evidence / "native_sources.json"), "source": "native_only", "live_validation": "not_run"}))


if __name__ == "__main__":
    main()
