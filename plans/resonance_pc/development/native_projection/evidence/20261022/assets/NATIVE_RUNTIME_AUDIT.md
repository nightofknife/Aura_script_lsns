# Native runtime geometry audit

Read-only offline inspection, no game calls, no input changes. Current Lua source hashes exactly match decoded bytecode in the 2026-10-04 logic archive. Pointer files are listed below. This is bytecode analysis, not recovered source text.

## Camera

Controller prototype root_1, native lines 40-60, instructions 21-40, explicitly assigns cam.transform.localPosition=Vector3(cfg.cameraX,cfg.cameraY,cfg.cameraZ), reads existing localEulerAngles, changes only x to cfg.rotateX, writes it back, then assigns fieldOfView=cfg.cameraView. It does NOT overwrite local Y/Z rotation there. Camera rightDir is read before that adjustment, relevant for future input work, not projection.

Scene Main Camera local quaternion decomposes to Unity ZXY application order (matrix Ry*Rx*Rz): Euler(30.0000053,135.0000051,-0.000000214). Replacing x with 29.6 and composing the existing parent Y180 rotation gives world camera center (50,41.5,-50), world yaw315/pitch29.6, with matrix in camera_native_candidate.json. This is the native-chain candidate, CONDITIONED on inherited FindCamera returning this scene Main Camera and no other code changing the scene parent. The inherited FindCamera implementation/scene instance-selection was not established by this bounded audit. Do not claim direct runtime telemetry.

The camera is perspective, vertical FOV12 for 3x3, zero lens shift, full viewport. Pixel focal length f=viewportHeight/(2*tan(6deg)); principal point requires actual viewport/crop alignment. Both image width/height and any client screenshot offsets remain image-side validation inputs. K should be fixed only once that image viewport contract is proven. Controller SetCameraToMove (root_65 native1497-1521) changes local Y/FOV to46/8 over0.5s, or back41.5/12. Movement/selection transitional frames cannot share the fixed normal-camera model.

## Cube root and reset

RubikCube.LoadModel root_3 native106-122 sets instantiated obj.transform.position=Vector3.zero and localEulerAngles=(51,-133,12). Subsequent input/reset changes root orientation, so that initial Euler MUST NOT be frozen for all recorded frames.

DataModel.RotateToFaceUp root_21 native376-413 uses face-index target world Euler angles: face1=(0,0,0), face2=(90,0,0), face3=(0,90,90), face4=(-90,180,0), face5=(0,-90,-90), face6=(180,-90,0). Its immediate path sets obj.transform.rotation directly; normal path stores current world rotation and target quaternion and arms isResetRoting. Controller.Update interpolates and eventually writes the target to obj.transform.rotation. Which face was selected remains observational/input-history data.

## Animator and 54 slot positions

Rubik prefab Animator resolves external#2 controller pathID8039580772517798322, named Rubik. m_ApplyRootMotion=false. Controller clip order: rogue_cube_in, rogue_cube_idle, rogue_cube_out, rogue_cube_empty. All state transition arrays and any-state transition arrays are empty. Default state is empty; Controller explicitly PlayAnim(cfg.enterAnimation) selects in. In is non-looping, duration1.5 seconds. Therefore do not automatically assume it enters the looping idle clip when in ends.

The runtime clip curves are stored as streamed and constant curves, not editor m_PositionCurves (empty). animation_decoded_streams.json decodes streamed frames: time f32, key count u32, each key channel index u32 plus4 float coefficients. Binding path hashes were mapped with CRC32 of actual hierarchy paths, e.g. Cube (face,row,col). Position binding attribute1 has xyz channels. At the final finite rogue_cube_in keyframe1.5s, ALL54 slot positions match serialized prefab positions within1e-5; no shrink from +/-4.8 was found. Root final position is zero, root scale one. rogue_cube_idle at2s also returns all54 slots to prefab origins but contains intermediate position motion and is looping. Both results are in animation_endpoint_check.json.

RubikCube.StopAnim root_5 native134-139 only sets animator.enabled=false if present. Current UICubeRogueMain scripts reference StopAnim only in RubikCube's definition and DataModel.ReleaseRot root_28 native559-572, before reparenting selected layer objects back and clearing rotation pivot. It is NOT proven to run automatically after viewing drag or after entrance completion; do not claim that. Frozen endpoint is instead justified by the non-looping in state with no transitions, subject to actual selected state being normal in/completed.

## What can be fixed for the offline experiment

- 3x3 native slot rest positions and rolePos/mesh transforms: supported for completed in state; retain failure/reject conditions for animation or game-layer rotation.
- root t=(0,0,0), scale1: supported by load code and in endpoint.
- root rotation: estimate per frame, optionally compare reset frames with6 native targets.
- camera C/R: use camera_native_candidate.json as explicit source-derived hypothesis and validate against held-out landmarks; inherited camera resolution remains a provenance gap.
- FOV12: supported for normal3x3 mode; K pixel focal/principal point must use proven captured viewport.

## Evidence

- script_receipts.json: absolute current source paths + SHA256.
- UICubeRoguewMainController.selected_disassembly.txt: camera init and movement-camera setup.
- UICubeRogueMainDataModel.selected_disassembly.txt: reset targets and ReleaseRot StopAnim.
- rubik_animator.json: exact root Animator reference.
- animation_objects.json: source bundle path/hash + clips and controllers raw typetrees.
- animation_decoded_streams.json: channel mappings and streamed curve keys.
- animation_endpoint_check.json: all54 endpoint comparison.
- camera_native_candidate.json: composed numeric camera matrices and assumptions.

No screenshot fitting or projection implementation performed here.
