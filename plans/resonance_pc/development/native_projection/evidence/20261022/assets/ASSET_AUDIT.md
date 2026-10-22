# Current installed native resource audit (2026-10-22)

Read-only source inspection. No game input, process telemetry, source edits, or implementation validation. Outputs are under this directory. Existing devtools decoder and UnityPy runtime used; only four named resource bundles decoded.

## Source receipts

`resource_receipt.json` contains installed source absolute paths and complete SHA256, including CubeRogueFactory.bin, CubeFactory.bin, TagFactory.bin, prefab.asset, rogue_cube_mf_n.asset, rogue_cube_mf_01.asset, scenes/rubikcube.unity.asset. Current CubeRogueFactory and CubeFactory hashes differ from the 2026-10-04 archive: current configs were decoded anew. `config_selected.json` contains CURRENT selected scalar fields; `CubeRogueFactory.json` and `CubeFactory.json` contain full decoded records. `tag_cells.json` maps 8 event types to their current cell model and icon paths.

## Prefab geometry

`rubik_hierarchy.json` is the actual Rubik prefab transform hierarchy (328 nodes). Root position zero, identity rotation, unit scale in the serialized asset. It contains 54 direct Cube(face,row,col) children, Rot, and Center hierarchy. Faces 1..6 centers are respectively (0,4.8,0), (0,0,-4.8), (4.8,0,0), (0,0,4.8), (-4.8,0,0), (0,-4.8,0). Each face has 9 slot origins; tangent coordinates are -2.4,0,2.4. Thus common center is zero and pitch 2.4, NOT GenerateFace fallback unit geometry.

ALL 54 rolePos nodes named Rogue_Cube_MF_02 have local position (0,0,-0.1949128658), identity rotation and unit scale. The serialized six face rotations orient local +Z outward: up, back, right, forward, left, down. The Rubik slot placeholder children Rogue_Cube_MF_01/02/03 only have Transform, not a mesh renderer. Therefore do not use the similarly named raw MF_01 mesh as if it were the current rendered board.

The game ShowCubeIcon loads TagFactory.cellModel, parents grid to rolePos with zero localPosition and identity localRotation, then sets texture on child Icon. Current TagFactory mappings include N_04 normal fight, N_03 boss/elite, N_06 adventure, N_02 rest, N_01 reward, N_05 store; empty model is N_Empty per code.

`cell_mesh_geometry.json` contains actual prefab child matrices composed into grid-root coordinates, linked mesh asset IDs, vertices, and UV0 for N_01..N_06 and Empty. Prefab external #4 points to CAB-36d3b4f496167fc800e28a20db4db2f6, the MF_N mesh resource dependency. Each variant has a 122-vertex outer structural shell with extents about +/-1.035 to +/-1.042. This provides a small family of known geometry for structure fitting before icon classification, but visual stability/visible hard edges under materials and animations remain unvalidated.

N_01 and Empty Icon have 4 vertices, x about [-0.86096,0.86331], y [-0.864295,0.864295], z about 0.933826 in grid-root coordinates. After rolePos offset their depth relative to slot is about 0.7389136. UVs run right-to-left; use extracted UV ordering rather than assumed corner ordering. N_05 and N_06 Icon depths differ slightly (0.946667 and 0.948304), so a single identical plane for every semantic type is an approximation. For a fixed type, all nine slots of each face produce coplanar icon geometry up to export floating-point noise. Across type variants, they are close but not exactly coplanar.

CRITICAL LIMIT: these are serialized prefab transforms, not observed runtime poses. Root/slot transforms can be affected by entrance/exit animation and game layer rotations. The game uses an Animator and PlayAnim/StopAnim; an animation audit or image overlay is required before claiming serialized slot origins equal stable live runtime positions. The exact root is explicitly changed by LoadModel and viewing inputs. This audit does not resolve animation/shader deformation.

## Camera evidence

Current CubeRogueFactory has 3x3 Rubik with configured camera (-50,41.5,50), rotateX=29.6, vertical FOV=12; movement view cameraY=46 and FOV=8. It also contains 4x4 Rubik_4/FOV14 and 5x5 Rubik_5/FOV16. A 54-slot implementation must explicitly scope to the current 3x3 mode.

`rubikcube.unity.asset.json`: Main Camera GameObject id20, Transform id354, Camera id672. Perspective camera (orthographic=false), full normalized viewport, FOV12, lensShift zero. Serialized local position (-50,41.5,50), local quaternion (0.0990457684,0.8923991323,-0.2391176522,0.3696438074). Parent Transform id351 has position0, quaternion(0,1,0,0), scale1; parent has no parent. Thus scene LOCAL camera position cannot be used as WORLD extrinsics without composing this parent and accounting for Controller runtime assignments. The scene also has a distinct UI Camera id671/FOV60; do not select the first Camera blindly. Controller setup may overwrite camera world/local values and requires separate code verification.

## Required gates before implementation claims

1. Resolve runtime initialization order and camera parent/world transforms from current Controller bytecode.
2. Determine stable animation endpoint slot transforms or verify native projection against existing live screenshots before treating serialized geometry as calibrated.
3. Verify client image viewport/aspect and screenshot crop; FOV alone does not establish image K.
4. Verify which structural mesh edges remain visible across materials, lighting, hover/selection and overlay states.
5. Match projected Icon quad through actual UVs; keep 3D entities/inspiration separate from planar icon classification.
