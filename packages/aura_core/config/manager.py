"""
插件配置管理器

实现三层配置合并：插件默认配置、用户全局配置、项目配置
"""

from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, TYPE_CHECKING
import yaml
import json

if TYPE_CHECKING:
    from ..packaging.manifest import PluginManifest


class ConfigManager:
    """插件配置管理器（三层配置合并）"""

    def __init__(self, manifest: "PluginManifest"):
        self.manifest = manifest

    def get_merged_config(self) -> Dict[str, Any]:
        """
        获取合并后的配置（三层合并）
        1. 插件默认配置（plugin/config/default.yaml）
        2. 用户全局配置（~/.aura/plugins/{plugin_name}.yaml）
        3. 项目配置（project/config.yaml 中的 plugins 部分）
        """
        # 1. 加载插件默认配置
        default_config = self.manifest.read_config()

        # 2. 加载用户全局配置
        user_config = self._load_user_config()

        # 3. 加载项目配置
        project_config = self._load_project_config()

        # 4. 深度合并
        if self.manifest.configuration.merge_strategy == "merge":
            return self._deep_merge(default_config, user_config, project_config)
        else:
            # replace 策略：后者完全覆盖前者
            result = default_config.copy()
            if user_config:
                result = user_config.copy()
            if project_config:
                result = project_config.copy()
            return result

    def _load_user_config(self) -> Dict[str, Any]:
        """从 ~/.aura/plugins/{plugin_name}.yaml 加载用户配置"""
        if not self.manifest.configuration.allow_user_override:
            return {}

        user_config_dir = Path.home() / ".aura" / "plugins"
        user_config_dir.mkdir(parents=True, exist_ok=True)

        plugin_name = self.manifest.package.name.replace("@", "").replace("/", "_")
        user_config_path = user_config_dir / f"{plugin_name}.yaml"

        if user_config_path.exists():
            with open(user_config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        return {}

    def _load_project_config(self) -> Dict[str, Any]:
        """从项目 config.yaml 的 plugins 部分加载配置"""
        project_config_path = Path.cwd() / "config.yaml"

        if project_config_path.exists():
            with open(project_config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
                plugins_config = config.get("plugins", {})
                return plugins_config.get(self.manifest.package.name, {})
        return {}

    def _deep_merge(self, *dicts) -> Dict[str, Any]:
        """深度合并多个字典"""
        result = {}
        for d in dicts:
            if not d:
                continue
            for key, value in d.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = self._deep_merge(result[key], value)
                else:
                    result[key] = value
        return result

    def validate_config(self, config: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """验证配置是否符合 Schema"""
        if not self.manifest.configuration.config_schema:
            return True, []

        schema_path = self.manifest.path / self.manifest.configuration.config_schema
        if not schema_path.exists():
            return True, []

        try:
            from jsonschema import validate, ValidationError
            with open(schema_path, 'r', encoding='utf-8') as f:
                schema = json.load(f)

            validate(instance=config, schema=schema)
            return True, []
        except ValidationError as e:
            return False, [str(e)]
        except Exception as e:
            return False, [f"Schema validation error: {e}"]

    def export_user_template(self, output_path: Optional[Path] = None) -> Path:
        """导出用户配置模板"""
        if output_path is None:
            plugin_name = self.manifest.package.name.replace("@", "").replace("/", "_")
            output_path = Path.home() / ".aura" / "plugins" / f"{plugin_name}.yaml"

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 如果有用户模板，使用模板
        if self.manifest.configuration.user_template:
            template_path = self.manifest.path / self.manifest.configuration.user_template
            if template_path.exists():
                import shutil
                shutil.copy(template_path, output_path)
                return output_path

        # 否则使用默认配置
        default_config = self.manifest.read_config()
        with open(output_path, 'w', encoding='utf-8') as f:
            yaml.dump(default_config, f, allow_unicode=True, sort_keys=False)

        return output_path
