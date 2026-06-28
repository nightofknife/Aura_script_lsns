# packages/aura_core/template_renderer.py
# [NEW] This entire file is new.

from typing import Any, Dict

from jinja2 import Environment, BaseLoader, UndefinedError

from packages.aura_core.logger import logger
from .context import ExecutionContext

class TemplateRenderer:
    """
    A dedicated service for rendering Jinja2 templates.
    It uses the ExecutionContext as the data source for rendering.
    """
    def __init__(self):
        self.jinja_env = Environment(loader=BaseLoader(), enable_async=True)
        # We can add more global functions here if needed in the future

    async def render_value(self, value: Any, context: ExecutionContext, state_store_snapshot: Dict[str, Any]) -> Any:
        """
        Recursively renders a value (string, dict, list) using Jinja2.
        """
        if isinstance(value, str):
            if "{{" not in value and "{%" not in value:
                return value
            try:
                template = self.jinja_env.from_string(value)
                render_scope = context.get_render_scope(state_store_snapshot)
                return await template.render_async(render_scope)
            except UndefinedError as e:
                logger.warning(f"Template rendering error for '{value}': {e.message}. Returning None.")
                return None
            except Exception as e:
                logger.error(f"An unexpected error occurred while rendering template '{value}': {e}", exc_info=True)
                return None
        elif isinstance(value, dict):
            return {k: await self.render_value(v, context, state_store_snapshot) for k, v in value.items()}
        elif isinstance(value, list):
            return [await self.render_value(item, context, state_store_snapshot) for item in value]
        else:
            return value

