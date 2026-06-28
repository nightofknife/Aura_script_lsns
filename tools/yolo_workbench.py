from __future__ import annotations

import argparse
import os
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(REPO_ROOT))

from tools._shared import discover_plan_names, sanitize_filename
from tools.yolo_project_lib import (
    YoloProjectError,
    check_environment,
    complete_labelimg_session,
    create_labelimg_session,
    create_project,
    export_training_dataset,
    import_images,
    launch_labelimg,
    list_projects,
    project_display_payload,
    project_root,
)


class YoloWorkbenchApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("YOLO Dataset Workbench")
        self.root.geometry("1040x520")

        self.plan_var = tk.StringVar()
        self.project_var = tk.StringVar()
        self.summary_var = tk.StringVar(value="No project selected.")
        self.classes_var = tk.StringVar(value="-")
        self.latest_export_var = tk.StringVar(value="-")
        self.env_var = tk.StringVar(value="-")

        self._build_ui()
        self.refresh_plan_choices()

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill="x")

        ttk.Label(top, text="Plan").grid(row=0, column=0, sticky="w")
        self.plan_box = ttk.Combobox(top, textvariable=self.plan_var, state="readonly", width=24)
        self.plan_box.grid(row=0, column=1, sticky="we", padx=(6, 12))
        self.plan_box.bind("<<ComboboxSelected>>", lambda _e: self.on_plan_changed())

        ttk.Label(top, text="Project").grid(row=0, column=2, sticky="w")
        self.project_box = ttk.Combobox(top, textvariable=self.project_var, state="readonly", width=28)
        self.project_box.grid(row=0, column=3, sticky="we", padx=(6, 12))
        self.project_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_project_view())

        ttk.Button(top, text="Refresh", command=self.refresh_project_view).grid(row=0, column=4, sticky="ew")

        button_bar = ttk.Frame(self.root, padding=(12, 0))
        button_bar.pack(fill="x")
        buttons = [
            ("Create Project", self.create_project_dialog),
            ("Import Images", self.import_images_action),
            ("Open Manual Label Session", self.open_manual_session),
            ("Complete Manual Session", self.complete_manual_session),
            ("Open Draft Review Session", self.open_review_session),
            ("Complete Draft Review", self.complete_review_session),
            ("Export Dataset", self.export_dataset_action),
            ("Open Project Folder", self.open_project_folder),
        ]
        for index, (label, callback) in enumerate(buttons):
            ttk.Button(button_bar, text=label, command=callback).grid(
                row=index // 4,
                column=index % 4,
                sticky="ew",
                padx=4,
                pady=4,
            )

        info = ttk.LabelFrame(self.root, text="Project Summary", padding=12)
        info.pack(fill="both", expand=True, padx=12, pady=(8, 12))
        ttk.Label(info, textvariable=self.summary_var, justify="left").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(info, text="Classes").grid(row=1, column=0, sticky="nw", pady=(12, 0))
        ttk.Label(info, textvariable=self.classes_var, justify="left").grid(row=1, column=1, sticky="w", pady=(12, 0), padx=(6, 24))
        ttk.Label(info, text="Latest Export").grid(row=2, column=0, sticky="nw", pady=(12, 0))
        ttk.Label(info, textvariable=self.latest_export_var, justify="left").grid(row=2, column=1, columnspan=3, sticky="w", pady=(12, 0), padx=(6, 0))
        ttk.Label(info, text="Environment").grid(row=3, column=0, sticky="nw", pady=(12, 0))
        ttk.Label(info, textvariable=self.env_var, justify="left").grid(row=3, column=1, columnspan=3, sticky="w", pady=(12, 0), padx=(6, 0))

    def refresh_plan_choices(self) -> None:
        plans = discover_plan_names()
        self.plan_box["values"] = plans
        if not self.plan_var.get() and plans:
            self.plan_var.set(plans[0])
        self.on_plan_changed()

    def on_plan_changed(self) -> None:
        plan_name = self.plan_var.get().strip()
        projects = list_projects(plan_name) if plan_name else []
        self.project_box["values"] = projects
        if projects:
            if self.project_var.get() not in projects:
                self.project_var.set(projects[0])
        else:
            self.project_var.set("")
        self.refresh_project_view()

    def current_project_dir(self) -> Path | None:
        plan_name = self.plan_var.get().strip()
        project_name = self.project_var.get().strip()
        if not plan_name or not project_name:
            return None
        return project_root(plan_name, project_name)

    def refresh_project_view(self) -> None:
        project_dir = self.current_project_dir()
        if project_dir is None or not project_dir.is_dir():
            self.summary_var.set("No project selected.")
            self.classes_var.set("-")
            self.latest_export_var.set("-")
            self.env_var.set(self._render_env())
            return

        payload = project_display_payload(project_dir)
        project = payload["project"]
        sample_summary = payload["sample_summary"]
        self.summary_var.set(
            f"Plan={project.get('plan_name')}  Project={project.get('project_name')}\n"
            f"Samples: unlabeled={sample_summary.get('unlabeled', 0)}  "
            f"draft={sample_summary.get('draft_generated', 0)}  "
            f"approved={sample_summary.get('approved', 0)}  "
            f"manual={sample_summary.get('in_manual_session', 0)}  "
            f"review={sample_summary.get('in_review_session', 0)}  "
            f"ignored={sample_summary.get('ignored', 0)}"
        )
        self.classes_var.set(", ".join(project.get("class_names") or []) or "-")
        self.latest_export_var.set(self._render_latest_export(project.get("latest_export")))
        self.env_var.set(self._render_env())

    def _render_env(self) -> str:
        env = check_environment()
        parts = [
            f"python={'ok' if env.runtime_python else 'missing'}",
            f"labelImg={'ok' if env.labelimg_command else 'missing'}",
        ]
        if env.messages:
            parts.append("messages=" + "; ".join(env.messages))
        return " | ".join(parts)

    def _render_latest_export(self, latest_export) -> str:
        if not isinstance(latest_export, dict) or not latest_export:
            return "-"
        return (
            f"id={latest_export.get('export_id')}  "
            f"train={latest_export.get('train_count')}  "
            f"val={latest_export.get('val_count')}  "
            f"dir={latest_export.get('export_dir')}"
        )

    def create_project_dialog(self) -> None:
        plan_name = self.plan_var.get().strip()
        if not plan_name:
            messagebox.showwarning("Missing plan", "Please choose a plan first.", parent=self.root)
            return
        project_name = simpledialog.askstring("Create Project", "Project name:", parent=self.root)
        if not project_name:
            return
        classes_raw = simpledialog.askstring(
            "Create Project",
            "Classes (comma or newline separated):",
            parent=self.root,
        )
        if not classes_raw:
            messagebox.showwarning("Missing classes", "At least one class is required.", parent=self.root)
            return
        class_names = [item.strip() for item in classes_raw.replace("\n", ",").split(",") if item.strip()]
        try:
            create_project(plan_name=plan_name, project_name=project_name, class_names=class_names)
        except Exception as exc:
            messagebox.showerror("Create failed", str(exc), parent=self.root)
            return
        self.on_plan_changed()
        self.project_var.set(sanitize_filename(project_name, fallback="project"))
        self.refresh_project_view()

    def import_images_action(self) -> None:
        project_dir = self.current_project_dir()
        if project_dir is None:
            messagebox.showwarning("No project", "Please select a project first.", parent=self.root)
            return
        file_paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Import Images",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp")],
        )
        if not file_paths:
            return
        try:
            result = import_images(project_dir, [Path(path) for path in file_paths])
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc), parent=self.root)
            return
        messagebox.showinfo(
            "Import complete",
            f"Imported: {result['imported_count']}\nSkipped: {len(result['skipped'])}",
            parent=self.root,
        )
        self.refresh_project_view()

    def open_manual_session(self) -> None:
        self._open_label_session("manual")

    def open_review_session(self) -> None:
        self._open_label_session("review")

    def _open_label_session(self, session_type: str) -> None:
        project_dir = self.current_project_dir()
        if project_dir is None:
            messagebox.showwarning("No project", "Please select a project first.", parent=self.root)
            return
        env = check_environment()
        if env.labelimg_command is None:
            messagebox.showerror("LabelImg missing", "LabelImg was not found in config, .venv, or PATH.", parent=self.root)
            return
        batch_size = simpledialog.askinteger(
            "Batch Size",
            f"{'Manual' if session_type == 'manual' else 'Review'} batch size:",
            parent=self.root,
            minvalue=1,
            initialvalue=50,
        )
        if batch_size is None:
            return
        try:
            session = create_labelimg_session(project_dir, session_type=session_type, batch_size=batch_size)
            launched = launch_labelimg(project_dir, session)
        except Exception as exc:
            messagebox.showerror("Session failed", str(exc), parent=self.root)
            return
        messagebox.showinfo(
            "Session opened",
            f"Session: {session.session_id}\nLabelImg pid: {launched['pid']}",
            parent=self.root,
        )
        self.refresh_project_view()

    def complete_manual_session(self) -> None:
        self._complete_label_session("manual")

    def complete_review_session(self) -> None:
        self._complete_label_session("review")

    def _complete_label_session(self, session_type: str) -> None:
        project_dir = self.current_project_dir()
        if project_dir is None:
            messagebox.showwarning("No project", "Please select a project first.", parent=self.root)
            return
        try:
            result = complete_labelimg_session(project_dir, session_type=session_type)
        except Exception as exc:
            messagebox.showerror("Complete failed", str(exc), parent=self.root)
            return
        messagebox.showinfo(
            "Session completed",
            f"Approved: {result['approved_count']}\nReverted: {result['reverted_count']}",
            parent=self.root,
        )
        self.refresh_project_view()

    def export_dataset_action(self) -> None:
        project_dir = self.current_project_dir()
        if project_dir is None:
            messagebox.showwarning("No project", "Please select a project first.", parent=self.root)
            return
        try:
            result = export_training_dataset(project_dir)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self.root)
            return
        messagebox.showinfo(
            "Dataset exported",
            f"Export dir: {result['export_dir']}\nTrain: {result['train_count']}\nVal: {result['val_count']}",
            parent=self.root,
        )
        self.refresh_project_view()

    def open_project_folder(self) -> None:
        project_dir = self.current_project_dir()
        if project_dir is None:
            messagebox.showwarning("No project", "Please select a project first.", parent=self.root)
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(project_dir))  # type: ignore[attr-defined]
            else:
                raise YoloProjectError("Opening folders is only supported on Windows for this tool.")
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc), parent=self.root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="YOLO dataset-building workbench for Aura plans.")
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    root = tk.Tk()
    YoloWorkbenchApp(root)
    root.mainloop()
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
