"""Static AST scanner for package exports."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class ManifestScanError(ValueError):
    """Raised when package source cannot be converted into exports safely."""


@dataclass(frozen=True)
class ScannedService:
    name: str
    module: str
    class_name: str
    public: bool
    singleton: bool
    replace: Optional[str]
    description: str


@dataclass(frozen=True)
class ScannedAction:
    name: str
    module: str
    function_name: str
    public: bool
    read_only: bool
    timeout: Optional[int]
    description: str
    parameters: List[Dict[str, Any]]


class ExportScanner:
    SERVICE_DECORATORS = {"service_info"}
    ACTION_DECORATORS = {"action_info"}
    REQUIRES_DECORATORS = {"requires_services"}

    def __init__(self, package_path: Path, manifest_data: Dict[str, Any]):
        self.package_path = package_path
        self.src_path = package_path / "src"
        self._validated = False
        package_info = manifest_data.get("package", {}) or {}
        package_name = package_info.get("name")
        if not isinstance(package_name, str) or not package_name.strip():
            package_name = f"@{package_path.parent.name}/{package_path.name}"
        self.package_id = package_name.lstrip("@")
        self.dependencies = {str(name).lstrip("@") for name in (manifest_data.get("dependencies", {}) or {}).keys()}

    def scan_services(self) -> List[Dict[str, Any]]:
        self._ensure_validated()
        services: List[ScannedService] = []
        names_seen: set[str] = set()
        for py_file in self._iter_python_files("services"):
            module = self._module_path_for_file(py_file)
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    meta = self._extract_service_meta(node, source)
                    if meta is None:
                        continue
                    if meta["alias"] in names_seen:
                        raise ManifestScanError(f"Duplicate service alias '{meta['alias']}' in {self.package_path}")
                    names_seen.add(meta["alias"])
                    self._validate_service_deps(meta.get("deps") or {}, py_file)
                    services.append(
                        ScannedService(
                            name=meta["alias"],
                            module=module,
                            class_name=node.name,
                            public=bool(meta["public"]),
                            singleton=bool(meta["singleton"]),
                            replace=meta.get("replace"),
                            description=meta["description"],
                        )
                    )
                elif self._node_has_decorator(node, self.SERVICE_DECORATORS):
                    raise ManifestScanError(f"@service_info must decorate a top-level class: {py_file}")
        return [
            {
                "name": service.name,
                "module": service.module,
                "class": service.class_name,
                "public": service.public,
                "singleton": service.singleton,
                "replace": service.replace,
                "description": service.description,
            }
            for service in services
        ]

    def scan_actions(self) -> List[Dict[str, Any]]:
        self._ensure_validated()
        actions: List[ScannedAction] = []
        names_seen: set[str] = set()
        for py_file in self._iter_python_files("actions"):
            module = self._module_path_for_file(py_file)
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    meta = self._extract_action_meta(node, source)
                    if meta is None:
                        continue
                    if meta["name"] in names_seen:
                        raise ManifestScanError(f"Duplicate action name '{meta['name']}' in {self.package_path}")
                    names_seen.add(meta["name"])
                    service_deps = self._extract_requires_services(node)
                    self._validate_service_deps(service_deps, py_file)
                    actions.append(
                        ScannedAction(
                            name=meta["name"],
                            module=module,
                            function_name=node.name,
                            public=bool(meta["public"]),
                            read_only=bool(meta["read_only"]),
                            timeout=meta.get("timeout"),
                            description=meta["description"],
                            parameters=self._extract_action_parameters(node, service_deps),
                        )
                    )
                elif self._node_has_decorator(node, self.ACTION_DECORATORS):
                    raise ManifestScanError(f"@action_info must decorate a top-level function: {py_file}")
        return [
            {
                "name": action.name,
                "module": action.module,
                "function": action.function_name,
                "public": action.public,
                "read_only": action.read_only,
                "timeout": action.timeout,
                "description": action.description,
                "parameters": action.parameters,
            }
            for action in actions
        ]

    def _iter_python_files(self, category: str) -> Iterable[Path]:
        target_dir = self.src_path / category
        if not target_dir.exists():
            return []
        return sorted(path for path in target_dir.rglob("*.py") if path.name != "__init__.py")

    def _iter_all_source_files(self) -> Iterable[Path]:
        if not self.src_path.exists():
            return []
        return sorted(path for path in self.src_path.rglob("*.py") if path.name != "__init__.py")

    def _ensure_validated(self):
        if self._validated:
            return
        for py_file in self._iter_all_source_files():
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
            self._validate_imports(tree, py_file)
            self._validate_export_layout(tree, py_file)
        self._validated = True

    def _module_path_for_file(self, py_file: Path) -> str:
        relative_parts = py_file.relative_to(self.package_path).with_suffix("").parts
        module_parts = [self.package_path.parent.name, self.package_path.name, *relative_parts]
        return ".".join(module_parts)

    def _extract_service_meta(self, node: ast.ClassDef, source: str) -> Optional[Dict[str, Any]]:
        decorator = self._find_decorator(node, self.SERVICE_DECORATORS)
        if decorator is None:
            return None
        values = self._extract_call_arguments(decorator)
        alias = values.pop("alias", None)
        if alias is None and decorator.args:
            alias = self._literal_or_error(decorator.args[0], "service alias")
        alias = alias or self._infer_service_alias(node.name)
        if not alias:
            raise ManifestScanError(f"Unable to infer service alias for {node.name}")
        return {
            "alias": alias,
            "public": values.get("public", True),
            "singleton": values.get("singleton", True),
            "replace": values.get("replace"),
            "deps": values.get("deps") or {},
            "description": values.get("description") or (ast.get_docstring(node) or "").split("\n")[0].strip(),
        }

    def _extract_action_meta(self, node: ast.FunctionDef, source: str) -> Optional[Dict[str, Any]]:
        decorator = self._find_decorator(node, self.ACTION_DECORATORS)
        if decorator is None:
            return None
        values = self._extract_call_arguments(decorator)
        name = values.pop("name", None)
        if name is None and decorator.args:
            name = self._literal_or_error(decorator.args[0], "action name")
        return {
            "name": name or node.name,
            "public": values.get("public", True),
            "read_only": values.get("read_only", False),
            "timeout": values.get("timeout"),
            "description": values.get("description") or (ast.get_docstring(node) or "").split("\n")[0].strip(),
        }

    def _extract_requires_services(self, node: ast.FunctionDef) -> Dict[str, str]:
        decorator = self._find_decorator(node, self.REQUIRES_DECORATORS)
        if decorator is None:
            return {}
        deps: Dict[str, str] = {}
        for arg in decorator.args:
            service_id = self._literal_or_error(arg, "service dependency")
            alias = str(service_id).split("/")[-1]
            if alias in deps:
                raise ManifestScanError(f"Duplicate service injection alias '{alias}' in {node.name}")
            deps[alias] = service_id
        for kw in decorator.keywords:
            if kw.arg is None:
                raise ManifestScanError(f"Unsupported **kwargs in requires_services for {node.name}")
            deps[kw.arg] = self._literal_or_error(kw.value, "service dependency")
        return deps

    def _extract_action_parameters(self, node: ast.FunctionDef, service_deps: Dict[str, str]) -> List[Dict[str, Any]]:
        positional_defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + list(node.args.defaults)
        parameters: List[Dict[str, Any]] = []
        for arg, default in zip(node.args.args, positional_defaults):
            if arg.arg in {"self", "context", "engine"}:
                continue
            if arg.arg in service_deps:
                continue
            parameters.append(
                {
                    "name": arg.arg,
                    "type": "Any",
                    "required": default is None,
                    "default": None if default is None else self._literal_or_default(default),
                }
            )
        return parameters

    def _validate_service_deps(self, deps: Dict[str, str], py_file: Path):
        for dependency in deps.values():
            dependency = str(dependency)
            if "/" not in dependency:
                continue
            package_id = dependency.rsplit("/", 1)[0]
            if package_id in {self.package_id, "core"}:
                continue
            if package_id not in self.dependencies:
                raise ManifestScanError(
                    f"Cross-package dependency '{dependency}' in {py_file} is not declared in manifest.dependencies"
                )

    def _validate_imports(self, tree: ast.AST, py_file: Path):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("plans."):
                        raise ManifestScanError(
                            f"Package source must not import plan modules via absolute path: {py_file} -> {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("plans."):
                    raise ManifestScanError(
                        f"Package source must use relative imports for plan modules: {py_file} -> {node.module}"
                    )

    def _validate_export_layout(self, tree: ast.AST, py_file: Path):
        relative_path = py_file.relative_to(self.src_path).as_posix()
        if relative_path.startswith("services/"):
            expected_kind = "service"
        elif relative_path.startswith("actions/"):
            expected_kind = "action"
        else:
            expected_kind = None

        for node, parent in self._iter_defs_with_parents(tree):
            is_service = self._node_has_decorator(node, self.SERVICE_DECORATORS)
            is_action = self._node_has_decorator(node, self.ACTION_DECORATORS)
            if not is_service and not is_action:
                continue

            if not isinstance(parent, ast.Module):
                raise ManifestScanError(f"Nested decorated definitions are not allowed: {py_file}")

            if is_service:
                if not isinstance(node, ast.ClassDef):
                    raise ManifestScanError(f"@service_info must decorate a top-level class: {py_file}")
                if expected_kind != "service":
                    raise ManifestScanError(
                        f"Service export must be defined under src/services: {py_file}"
                    )

            if is_action:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    raise ManifestScanError(f"@action_info must decorate a top-level function: {py_file}")
                if expected_kind != "action":
                    raise ManifestScanError(
                        f"Action export must be defined under src/actions: {py_file}"
                    )

    @staticmethod
    def _iter_defs_with_parents(tree: ast.AST):
        stack: list[tuple[ast.AST, Optional[ast.AST]]] = [(tree, None)]
        while stack:
            node, parent = stack.pop()
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                yield node, parent
            children = list(ast.iter_child_nodes(node))
            for child in reversed(children):
                stack.append((child, node))

    @staticmethod
    def _node_has_decorator(node: ast.AST, names: set[str]) -> bool:
        return ExportScanner._find_decorator(node, names) is not None

    @staticmethod
    def _find_decorator(node: ast.AST, names: set[str]) -> Optional[ast.Call]:
        for decorator in getattr(node, "decorator_list", []):
            call = decorator if isinstance(decorator, ast.Call) else None
            target = call.func if call is not None else decorator
            dec_name = ExportScanner._decorator_name(target)
            if dec_name in names:
                if call is None:
                    call = ast.Call(func=decorator, args=[], keywords=[])
                return call
        return None

    @staticmethod
    def _decorator_name(node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _extract_call_arguments(self, call: ast.Call) -> Dict[str, Any]:
        values = {}
        for kw in call.keywords:
            if kw.arg is None:
                raise ManifestScanError("Unsupported **kwargs in decorator arguments")
            values[kw.arg] = self._literal_or_error(kw.value, kw.arg)
        return values

    @staticmethod
    def _literal_or_default(node: ast.AST) -> Any:
        try:
            return ast.literal_eval(node)
        except Exception:
            return None

    @staticmethod
    def _literal_or_error(node: ast.AST, field_name: str) -> Any:
        try:
            return ast.literal_eval(node)
        except Exception as exc:
            raise ManifestScanError(f"Decorator field '{field_name}' must be a literal value.") from exc

    @staticmethod
    def _infer_service_alias(class_name: str) -> str:
        base = class_name[:-7] if class_name.endswith("Service") and len(class_name) > 7 else class_name
        result = []
        for idx, ch in enumerate(base):
            if ch.isupper() and idx > 0 and not base[idx - 1].isupper():
                result.append("_")
            result.append(ch.lower())
        return "".join(result)
