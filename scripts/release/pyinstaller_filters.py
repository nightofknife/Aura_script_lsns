from __future__ import annotations


_EXCLUDED_SUBMODULE_PREFIXES = {
    "numpy": (
        "numpy._pyinstaller",
        "numpy.f2py",
        "numpy.testing",
        "numpy.typing",
    ),
    "onnxruntime": (
        "onnxruntime.backend",
        "onnxruntime.capi.convert_npz_to_onnx_adapter",
        "onnxruntime.datasets",
        "onnxruntime.quantization",
        "onnxruntime.tools",
        "onnxruntime.transformers",
    ),
    "av": (
        "av.__main__",
        "av.datasets",
    ),
    "screeninfo": ("screeninfo.__main__",),
    "dotenv": ("dotenv.__main__",),
}

_COMMON_EXCLUDED_DATA_GLOBS = (
    "**/*.h",
    "**/*.hpp",
    "**/*.pxd",
    "**/*.pxi",
    "**/*.pyi",
    "**/*.pyx",
    "**/*.a",
    "**/*.exp",
    "**/*.lib",
    "**/*.pdb",
    "**/py.typed",
)

_EXCLUDED_DATA_GLOBS = {
    "numpy": (
        "**/tests",
        "**/tests/**",
        "_pyinstaller",
        "_pyinstaller/**",
        "f2py",
        "f2py/**",
        "testing",
        "testing/**",
    ),
    "onnxruntime": (
        "backend",
        "backend/**",
        "datasets",
        "datasets/**",
        "quantization",
        "quantization/**",
        "tools",
        "tools/**",
        "transformers",
        "transformers/**",
    ),
    "av": (
        "datasets",
        "datasets/**",
    ),
}


def should_collect_submodule(package_name: str, module_name: str) -> bool:
    normalized_package = str(package_name).strip()
    normalized_module = str(module_name).strip()
    if normalized_package == "numpy" and "tests" in normalized_module.split("."):
        return False
    return not any(
        normalized_module == prefix or normalized_module.startswith(f"{prefix}.")
        for prefix in _EXCLUDED_SUBMODULE_PREFIXES.get(normalized_package, ())
    )


def excluded_data_globs(package_name: str) -> tuple[str, ...]:
    package_globs = _EXCLUDED_DATA_GLOBS.get(str(package_name).strip(), ())
    return _COMMON_EXCLUDED_DATA_GLOBS + package_globs
