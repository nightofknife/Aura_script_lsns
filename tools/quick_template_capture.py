from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np
from PIL import Image, ImageTk
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools._shared import ensure_directory, plan_path, sanitize_filename


@dataclass(frozen=True)
class BoundWindow:
    hwnd: int
    title: str
    process_name: str
    class_name: str


LIBRARY_INDEX_FILE = "_template_library.yaml"
PREPROCESS_MODES = ("none", "rgb_range", "hsv_range")


def default_preprocess_config() -> dict[str, Any]:
    return {
        "mode": "none",
        "rgb_lower": [0, 0, 0],
        "rgb_upper": [255, 255, 255],
        "hsv_lower": [0, 0, 0],
        "hsv_upper": [179, 255, 255],
    }


def apply_color_preprocess(image: Image.Image, preprocess: dict[str, Any] | None = None) -> Image.Image:
    config = dict(default_preprocess_config())
    if preprocess:
        config.update(preprocess)

    mode = str(config.get("mode") or "none").strip().lower()
    source = np.asarray(image.convert("RGB")).copy()
    if mode == "none":
        return Image.fromarray(source)

    if mode == "rgb_range":
        lower = np.array(_normalize_bounds(config.get("rgb_lower"), [0, 0, 0], 255), dtype=np.uint8)
        upper = np.array(_normalize_bounds(config.get("rgb_upper"), [255, 255, 255], 255), dtype=np.uint8)
        mask = cv2.inRange(source, lower, upper)
    elif mode == "hsv_range":
        hsv = cv2.cvtColor(source, cv2.COLOR_RGB2HSV)
        lower = np.array(_normalize_bounds(config.get("hsv_lower"), [0, 0, 0], [179, 255, 255]), dtype=np.uint8)
        upper = np.array(_normalize_bounds(config.get("hsv_upper"), [179, 255, 255], [179, 255, 255]), dtype=np.uint8)
        mask = cv2.inRange(hsv, lower, upper)
    else:
        raise ValueError(f"Unsupported preprocess mode: {mode}")

    filtered = cv2.bitwise_and(source, source, mask=mask)
    return Image.fromarray(filtered)


def _normalize_bounds(value: Any, default: list[int], maximum: int | list[int]) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        value = list(default)
    if isinstance(maximum, int):
        maximums = [maximum, maximum, maximum]
    else:
        maximums = list(maximum)

    normalized: list[int] = []
    for index in range(3):
        raw = int(value[index])
        normalized.append(max(min(raw, int(maximums[index])), 0))
    return normalized


def _sorted_bounds(lower: list[int], upper: list[int]) -> tuple[list[int], list[int]]:
    lower_out: list[int] = []
    upper_out: list[int] = []
    for low, high in zip(lower, upper):
        if low <= high:
            lower_out.append(int(low))
            upper_out.append(int(high))
        else:
            lower_out.append(int(high))
            upper_out.append(int(low))
    return lower_out, upper_out


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)


def _ensure_plan_library_registration(plan_name: str, output_dir: Path, library_name: str) -> Path:
    plan_dir = plan_path(plan_name)
    config_path = plan_dir / "config.yaml"
    config_data = _load_yaml(config_path)
    templates_cfg = config_data.setdefault("templates", {})
    if not isinstance(templates_cfg, dict):
        templates_cfg = {}
        config_data["templates"] = templates_cfg
    libraries = templates_cfg.setdefault("libraries", [])
    if not isinstance(libraries, list):
        libraries = []
        templates_cfg["libraries"] = libraries

    try:
        relative_path = output_dir.resolve().relative_to(plan_dir.resolve()).as_posix()
    except ValueError:
        relative_path = str(output_dir.resolve())

    existing = None
    for item in libraries:
        if isinstance(item, dict) and item.get("name") == library_name:
            existing = item
            break
    if existing is None:
        existing = {}
        libraries.append(existing)

    existing["name"] = library_name
    existing["path"] = relative_path
    existing["recursive"] = True
    existing["extensions"] = [".png", ".jpg", ".jpeg", ".bmp"]

    _write_yaml(config_path, config_data)
    return config_path


def _update_library_index(
    *,
    output_dir: Path,
    library_name: str,
    saved_template_path: Path,
    bound_window: BoundWindow,
    crop_rect: tuple[int, int, int, int],
    capture_size: tuple[int, int],
    preprocess: dict[str, Any] | None = None,
) -> Path:
    index_path = output_dir / LIBRARY_INDEX_FILE
    data = _load_yaml(index_path)
    library_payload = data.setdefault("library", {})
    library_payload["name"] = library_name
    library_payload["root"] = "."
    library_payload["extensions"] = [".png", ".jpg", ".jpeg", ".bmp"]
    library_payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    templates = data.setdefault("templates", [])
    if not isinstance(templates, list):
        templates = []
        data["templates"] = templates

    rel_file = saved_template_path.name
    existing = None
    for item in templates:
        if isinstance(item, dict) and item.get("file") == rel_file:
            existing = item
            break
    if existing is None:
        existing = {}
        templates.append(existing)

    existing.update(
        {
            "name": saved_template_path.stem,
            "file": rel_file,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "capture_size": [int(capture_size[0]), int(capture_size[1])],
            "crop_rect": [int(crop_rect[0]), int(crop_rect[1]), int(crop_rect[2]), int(crop_rect[3])],
            "preprocess": dict(preprocess or default_preprocess_config()),
            "window": {
                "hwnd": int(bound_window.hwnd),
                "title": bound_window.title,
                "process_name": bound_window.process_name,
                "class_name": bound_window.class_name,
            },
        }
    )

    templates.sort(key=lambda item: str(item.get("name") or item.get("file") or ""))
    _write_yaml(index_path, data)
    return index_path


def persist_template_metadata(
    *,
    plan_name: str | None,
    output_dir: Path,
    library_name: str,
    saved_template_path: Path,
    bound_window: BoundWindow,
    crop_rect: tuple[int, int, int, int],
    capture_size: tuple[int, int],
    preprocess: dict[str, Any] | None = None,
) -> dict[str, str | None]:
    config_path = None
    if plan_name:
        config_path = _ensure_plan_library_registration(plan_name, output_dir, library_name)
    index_path = _update_library_index(
        output_dir=output_dir,
        library_name=library_name,
        saved_template_path=saved_template_path,
        bound_window=bound_window,
        crop_rect=crop_rect,
        capture_size=capture_size,
        preprocess=preprocess,
    )
    return {
        "config_path": str(config_path) if config_path is not None else None,
        "index_path": str(index_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture a fixed manually-configured target window, crop a region, and save it as a template PNG."
    )
    parser.add_argument("--plan", help="Optional plan name. Defaults save path to plans/<plan>/templates.")
    parser.add_argument("--output-dir", type=Path, help="Custom output directory for saved templates.")
    parser.add_argument("--backend", default="gdi", help="Capture backend to use. Defaults to gdi.")
    parser.add_argument("--library-name", default="captured", help="Template library name to register/update.")
    parser.add_argument("--hwnd", type=int, help="Initial hwnd selector.")
    parser.add_argument("--title", help="Initial window title selector.")
    parser.add_argument("--title-exact", action="store_true", help="Require exact title match when binding.")
    parser.add_argument("--process-name", help="Initial process-name selector.")
    parser.add_argument("--pid", type=int, help="Initial pid selector.")
    parser.add_argument("--class-name", help="Initial class-name selector.")
    parser.add_argument("--class-exact", action="store_true", help="Require exact class match when binding.")
    return parser


def run_gui(
    *,
    plan_name: str | None,
    output_dir: Path,
    backend: str,
    library_name: str,
    hwnd: int | None,
    title: str | None,
    title_exact: bool,
    process_name: str | None,
    pid: int | None,
    class_name: str | None,
    class_exact: bool,
) -> int:
    import tkinter as tk
    from tkinter import messagebox, simpledialog

    from plans.aura_base.src.platform.runtime_config import RuntimeTargetConfig
    from plans.aura_base.src.platform.windows.capture_backends import build_capture_backend
    from plans.aura_base.src.platform.windows.window_target import WindowTarget

    def make_preview_photo(image: Image.Image, max_size: tuple[int, int]) -> ImageTk.PhotoImage:
        preview = image.copy()
        preview.thumbnail(max_size, Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(preview)

    def resolve_bound_window(
        *,
        selector_hwnd: int | None,
        selector_title: str | None,
        selector_title_exact: bool,
        selector_process_name: str | None,
        selector_pid: int | None,
        selector_class_name: str | None,
        selector_class_exact: bool,
    ) -> tuple[BoundWindow, RuntimeTargetConfig]:
        mode = None
        if selector_hwnd is not None:
            mode = "hwnd"
        elif selector_process_name or selector_pid is not None or selector_class_name:
            mode = "process"
        elif selector_title:
            mode = "title"
        else:
            raise ValueError("At least one selector is required: hwnd, title, process-name, pid, or class-name.")

        config = RuntimeTargetConfig(
            mode=mode,
            hwnd=selector_hwnd,
            title=selector_title,
            title_exact=selector_title_exact,
            process_name=selector_process_name,
            pid=selector_pid,
            class_name=selector_class_name,
            class_exact=selector_class_exact,
            require_visible=True,
            allow_child_window=False,
            allow_empty_title=False,
        )
        target = WindowTarget.create(config)
        summary = target.to_summary()
        bound = BoundWindow(
            hwnd=int(summary["hwnd"]),
            title=str(summary.get("title") or ""),
            process_name=str(summary.get("process_name") or ""),
            class_name=str(summary.get("class_name") or ""),
        )
        fixed_config = RuntimeTargetConfig(
            mode="hwnd",
            hwnd=bound.hwnd,
            require_visible=True,
            allow_child_window=False,
            allow_empty_title=False,
        )
        return bound, fixed_config

    def capture_window(bound_config: RuntimeTargetConfig) -> Image.Image:
        target = WindowTarget.create(bound_config)
        capture_backend = build_capture_backend(str(backend).lower(), target, {})
        try:
            capture = capture_backend.capture()
            if capture.image is None:
                raise RuntimeError("Capture backend returned an empty image.")
            return Image.fromarray(np.asarray(capture.image).copy())
        finally:
            capture_backend.close()

    root = tk.Tk()
    root.title("Quick Template Capture")
    root.geometry("760x400")

    state: dict[str, object] = {
        "bound_window": None,
        "bound_config": None,
    }

    form = tk.Frame(root)
    form.pack(fill="x", padx=12, pady=12)

    def add_row(row: int, label: str, width: int = 30, value: str = ""):
        tk.Label(form, text=label, anchor="w", width=12).grid(row=row, column=0, sticky="w", pady=4)
        var = tk.StringVar(value=value)
        entry = tk.Entry(form, textvariable=var, width=width)
        entry.grid(row=row, column=1, sticky="we", pady=4, padx=(6, 12))
        return var, entry

    hwnd_var, _ = add_row(0, "hwnd", value="" if hwnd is None else str(hwnd))
    title_var, _ = add_row(1, "title", value=title or "")
    process_var, _ = add_row(2, "process", value=process_name or "")
    pid_var, _ = add_row(3, "pid", value="" if pid is None else str(pid))
    class_var, _ = add_row(4, "class", value=class_name or "")
    library_var, _ = add_row(5, "library", value=library_name or "captured")

    title_exact_var = tk.BooleanVar(value=bool(title_exact))
    class_exact_var = tk.BooleanVar(value=bool(class_exact))
    tk.Checkbutton(form, text="title exact", variable=title_exact_var).grid(row=1, column=2, sticky="w")
    tk.Checkbutton(form, text="class exact", variable=class_exact_var).grid(row=4, column=2, sticky="w")

    info_frame = tk.LabelFrame(root, text="Fixed Target")
    info_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
    status_var = tk.StringVar(value="Configure selectors, click Bind Target, then use Capture Fixed Target.")
    tk.Label(info_frame, textvariable=status_var, anchor="w", justify="left").pack(fill="x", padx=10, pady=(10, 4))
    bound_summary_var = tk.StringVar(value="No target bound.")
    tk.Label(info_frame, textvariable=bound_summary_var, anchor="w", justify="left").pack(fill="x", padx=10, pady=(0, 10))

    def bind_target() -> None:
        raw_hwnd = hwnd_var.get().strip()
        raw_title = title_var.get().strip()
        raw_process = process_var.get().strip()
        raw_pid = pid_var.get().strip()
        raw_class = class_var.get().strip()

        selector_hwnd = int(raw_hwnd) if raw_hwnd else None
        selector_pid = int(raw_pid) if raw_pid else None
        selector_title = raw_title or None
        selector_process = raw_process or None
        selector_class = raw_class or None

        try:
            bound_window, bound_config = resolve_bound_window(
                selector_hwnd=selector_hwnd,
                selector_title=selector_title,
                selector_title_exact=bool(title_exact_var.get()),
                selector_process_name=selector_process,
                selector_pid=selector_pid,
                selector_class_name=selector_class,
                selector_class_exact=bool(class_exact_var.get()),
            )
        except Exception as exc:
            messagebox.showerror("Bind failed", str(exc), parent=root)
            return

        state["bound_window"] = bound_window
        state["bound_config"] = bound_config
        status_var.set("Target bound successfully. Capture will now use this fixed hwnd until you rebind.")
        bound_summary_var.set(
            f"hwnd={bound_window.hwnd}\n"
            f"title={bound_window.title or '-'}\n"
            f"process={bound_window.process_name or '-'}\n"
            f"class={bound_window.class_name or '-'}"
        )

    def launch_crop_dialog(bound_window: BoundWindow, image: Image.Image) -> None:
        dialog = tk.Toplevel(root)
        dialog.title(f"Crop Template - {bound_window.title or bound_window.process_name}")
        dialog.geometry("1200x900")

        screen_w = max(dialog.winfo_screenwidth() - 120, 400)
        screen_h = max(dialog.winfo_screenheight() - 220, 300)
        scale = min(screen_w / image.width, screen_h / image.height, 1.0)
        display_size = (max(int(image.width * scale), 1), max(int(image.height * scale), 1))
        display_image = image.resize(display_size, Image.Resampling.LANCZOS) if scale < 1.0 else image.copy()
        tk_image = ImageTk.PhotoImage(display_image)

        canvas = tk.Canvas(dialog, bg="#111111", width=display_size[0], height=display_size[1], cursor="cross")
        canvas.pack(fill="both", expand=True, padx=12, pady=12)
        canvas.create_image(0, 0, anchor="nw", image=tk_image)
        canvas.image = tk_image

        info_var = tk.StringVar(value="Drag on the image to select a region, then save.")
        tk.Label(dialog, textvariable=info_var, anchor="w").pack(fill="x", padx=12, pady=(0, 6))

        crop_state = {"start": None, "rect_id": None, "selection": None}

        def on_press(event):
            crop_state["start"] = (event.x, event.y)
            if crop_state["rect_id"] is not None:
                canvas.delete(crop_state["rect_id"])
                crop_state["rect_id"] = None

        def on_drag(event):
            if crop_state["start"] is None:
                return
            x0, y0 = crop_state["start"]
            x1, y1 = event.x, event.y
            if crop_state["rect_id"] is not None:
                canvas.delete(crop_state["rect_id"])
            crop_state["rect_id"] = canvas.create_rectangle(x0, y0, x1, y1, outline="#00ff88", width=2)

        def on_release(event):
            if crop_state["start"] is None:
                return
            x0, y0 = crop_state["start"]
            x1, y1 = event.x, event.y
            left, right = sorted((max(x0, 0), max(x1, 0)))
            top, bottom = sorted((max(y0, 0), max(y1, 0)))
            if right - left < 2 or bottom - top < 2:
                crop_state["selection"] = None
                info_var.set("Selection is too small. Drag a larger region.")
                return
            crop_state["selection"] = (left, top, right, bottom)
            info_var.set(f"Selected {right - left} x {bottom - top} (display pixels).")

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)

        button_row = tk.Frame(dialog)
        button_row.pack(fill="x", padx=12, pady=(0, 12))

        def open_preprocess_dialog(cropped_image: Image.Image) -> dict[str, Any] | None:
            preprocess_dialog = tk.Toplevel(dialog)
            preprocess_dialog.title("Color Preprocess")
            preprocess_dialog.geometry("1180x820")
            preprocess_dialog.transient(dialog)
            preprocess_dialog.grab_set()

            result_holder: dict[str, Any] = {"value": None}
            config = default_preprocess_config()
            mode_var = tk.StringVar(value=str(config["mode"]))
            rgb_lower_vars = [tk.IntVar(value=int(v)) for v in config["rgb_lower"]]
            rgb_upper_vars = [tk.IntVar(value=int(v)) for v in config["rgb_upper"]]
            hsv_lower_vars = [tk.IntVar(value=int(v)) for v in config["hsv_lower"]]
            hsv_upper_vars = [tk.IntVar(value=int(v)) for v in config["hsv_upper"]]

            preview_frame = tk.Frame(preprocess_dialog)
            preview_frame.pack(fill="both", expand=True, padx=12, pady=12)
            original_panel = tk.LabelFrame(preview_frame, text="Original")
            original_panel.pack(side="left", fill="both", expand=True, padx=(0, 6))
            processed_panel = tk.LabelFrame(preview_frame, text="Processed")
            processed_panel.pack(side="left", fill="both", expand=True, padx=(6, 0))
            original_label = tk.Label(original_panel)
            original_label.pack(fill="both", expand=True, padx=8, pady=8)
            processed_label = tk.Label(processed_panel)
            processed_label.pack(fill="both", expand=True, padx=8, pady=8)
            original_photo = make_preview_photo(cropped_image, (520, 360))
            original_label.configure(image=original_photo)
            original_label.image = original_photo

            config_frame = tk.Frame(preprocess_dialog)
            config_frame.pack(fill="x", padx=12, pady=(0, 12))

            mode_row = tk.Frame(config_frame)
            mode_row.pack(fill="x")
            tk.Label(mode_row, text="Mode", width=12, anchor="w").pack(side="left")
            for text, value in (("None", "none"), ("RGB Range", "rgb_range"), ("HSV Range", "hsv_range")):
                tk.Radiobutton(mode_row, text=text, variable=mode_var, value=value).pack(side="left", padx=(0, 8))

            channels_frame = tk.Frame(config_frame)
            channels_frame.pack(fill="x", pady=(8, 0))
            rgb_frame = tk.LabelFrame(channels_frame, text="RGB Bounds")
            rgb_frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
            hsv_frame = tk.LabelFrame(channels_frame, text="HSV Bounds")
            hsv_frame.pack(side="left", fill="both", expand=True, padx=(6, 0))

            preview_status_var = tk.StringVar(value="Adjust ranges to preview the processed template.")
            tk.Label(config_frame, textvariable=preview_status_var, anchor="w", justify="left").pack(fill="x", pady=(8, 0))

            def add_scale_group(parent, row_index, name, lower_var, upper_var, max_value):
                tk.Label(parent, text=name, width=4, anchor="w").grid(row=row_index, column=0, sticky="w", padx=(4, 6), pady=4)
                tk.Label(parent, text="L").grid(row=row_index, column=1, sticky="e")
                tk.Scale(parent, from_=0, to=max_value, orient="horizontal", variable=lower_var, length=180).grid(row=row_index, column=2, sticky="we", padx=(4, 8))
                tk.Label(parent, text="U").grid(row=row_index, column=3, sticky="e")
                tk.Scale(parent, from_=0, to=max_value, orient="horizontal", variable=upper_var, length=180).grid(row=row_index, column=4, sticky="we", padx=(4, 8))

            add_scale_group(rgb_frame, 0, "R", rgb_lower_vars[0], rgb_upper_vars[0], 255)
            add_scale_group(rgb_frame, 1, "G", rgb_lower_vars[1], rgb_upper_vars[1], 255)
            add_scale_group(rgb_frame, 2, "B", rgb_lower_vars[2], rgb_upper_vars[2], 255)
            add_scale_group(hsv_frame, 0, "H", hsv_lower_vars[0], hsv_upper_vars[0], 179)
            add_scale_group(hsv_frame, 1, "S", hsv_lower_vars[1], hsv_upper_vars[1], 255)
            add_scale_group(hsv_frame, 2, "V", hsv_lower_vars[2], hsv_upper_vars[2], 255)

            def collect_preprocess_config() -> dict[str, Any]:
                rgb_lower = [var.get() for var in rgb_lower_vars]
                rgb_upper = [var.get() for var in rgb_upper_vars]
                hsv_lower = [var.get() for var in hsv_lower_vars]
                hsv_upper = [var.get() for var in hsv_upper_vars]
                rgb_lower, rgb_upper = _sorted_bounds(rgb_lower, rgb_upper)
                hsv_lower, hsv_upper = _sorted_bounds(hsv_lower, hsv_upper)
                return {
                    "mode": mode_var.get(),
                    "rgb_lower": rgb_lower,
                    "rgb_upper": rgb_upper,
                    "hsv_lower": hsv_lower,
                    "hsv_upper": hsv_upper,
                }

            def refresh_processed_preview(*_args):
                preprocess_config = collect_preprocess_config()
                processed = apply_color_preprocess(cropped_image, preprocess_config)
                processed_photo = make_preview_photo(processed, (520, 360))
                processed_label.configure(image=processed_photo)
                processed_label.image = processed_photo
                preview_status_var.set(
                    f"Mode={preprocess_config['mode']} "
                    f"RGB={preprocess_config['rgb_lower']}..{preprocess_config['rgb_upper']} "
                    f"HSV={preprocess_config['hsv_lower']}..{preprocess_config['hsv_upper']}"
                )

            tracked_vars = [mode_var, *rgb_lower_vars, *rgb_upper_vars, *hsv_lower_vars, *hsv_upper_vars]
            for tracked_var in tracked_vars:
                tracked_var.trace_add("write", refresh_processed_preview)

            actions_row = tk.Frame(preprocess_dialog)
            actions_row.pack(fill="x", padx=12, pady=(0, 12))

            def accept_and_close():
                result_holder["value"] = collect_preprocess_config()
                preprocess_dialog.destroy()

            tk.Button(actions_row, text="Save Template", command=accept_and_close).pack(side="left")
            tk.Button(actions_row, text="Cancel", command=preprocess_dialog.destroy).pack(side="right")

            refresh_processed_preview()
            preprocess_dialog.wait_window()
            return result_holder["value"]

        def save_selection() -> None:
            selection = crop_state["selection"]
            if selection is None:
                messagebox.showwarning("No selection", "Please drag a region first.", parent=dialog)
                return
            raw_name = simpledialog.askstring("Template name", "Enter template name:", parent=dialog)
            if not raw_name:
                return
            name = sanitize_filename(raw_name)
            selected_library = sanitize_filename(library_var.get().strip() or "captured", fallback="captured")
            left, top, right, bottom = selection
            original_rect = (
                int(round(left / scale)),
                int(round(top / scale)),
                int(round(right / scale)),
                int(round(bottom / scale)),
            )
            cropped = image.crop(original_rect)
            preprocess_config = open_preprocess_dialog(cropped)
            if preprocess_config is None:
                return
            processed_cropped = apply_color_preprocess(cropped, preprocess_config)
            output_path = ensure_directory(output_dir) / f"{name}.png"
            processed_cropped.save(output_path)
            metadata_result = persist_template_metadata(
                plan_name=plan_name,
                output_dir=output_dir,
                library_name=selected_library,
                saved_template_path=output_path,
                bound_window=bound_window,
                crop_rect=(
                    int(original_rect[0]),
                    int(original_rect[1]),
                    int(original_rect[2] - original_rect[0]),
                    int(original_rect[3] - original_rect[1]),
                ),
                capture_size=(int(image.width), int(image.height)),
                preprocess=preprocess_config,
            )
            messagebox.showinfo(
                "Saved",
                "Saved template:\n"
                f"{output_path}\n\n"
                f"Library metadata:\n{metadata_result['index_path']}\n"
                f"Config registration: {metadata_result.get('config_path') or '-'}",
                parent=dialog,
            )
            info_var.set(
                f"Saved to {output_path}\n"
                f"Library index: {metadata_result['index_path']}\n"
                f"Preprocess mode: {preprocess_config.get('mode')}"
            )

        tk.Button(button_row, text="Save Selection", command=save_selection).pack(side="left")
        tk.Button(button_row, text="Close", command=dialog.destroy).pack(side="right")

    def capture_fixed_target() -> None:
        bound_window = state.get("bound_window")
        bound_config = state.get("bound_config")
        if not isinstance(bound_window, BoundWindow) or bound_config is None:
            messagebox.showwarning("No target", "Please bind a target window first.", parent=root)
            return
        try:
            image = capture_window(bound_config)
        except Exception as exc:
            messagebox.showerror("Capture failed", str(exc), parent=root)
            return
        launch_crop_dialog(bound_window, image)

    button_row = tk.Frame(root)
    button_row.pack(fill="x", padx=12, pady=(0, 12))
    tk.Button(button_row, text="Bind Target", command=bind_target).pack(side="left")
    tk.Button(button_row, text="Capture Fixed Target", command=capture_fixed_target).pack(side="left", padx=(8, 0))
    tk.Button(button_row, text="Quit", command=root.destroy).pack(side="right")

    if any(
        value not in (None, "", 0)
        for value in (
            hwnd,
            title,
            process_name,
            pid,
            class_name,
        )
    ):
        root.after(50, bind_target)

    root.mainloop()
    return 0


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.output_dir is not None:
        output_dir = args.output_dir.expanduser().resolve()
    elif args.plan:
        output_dir = (plan_path(args.plan) / "templates").resolve()
    else:
        output_dir = (Path(__file__).resolve().parents[1] / "logs" / "template_captures").resolve()

    try:
        return run_gui(
            plan_name=args.plan,
            output_dir=output_dir,
            backend=args.backend,
            library_name=args.library_name,
            hwnd=args.hwnd,
            title=args.title,
            title_exact=bool(args.title_exact),
            process_name=args.process_name,
            pid=args.pid,
            class_name=args.class_name,
            class_exact=bool(args.class_exact),
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
