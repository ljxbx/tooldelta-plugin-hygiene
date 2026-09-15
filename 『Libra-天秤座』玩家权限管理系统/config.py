"""Libra 配置加载与默认值。"""
from __future__ import annotations

from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "命令超时秒数": 30,
    "是否使用魔法指令模式运行": True,
    "等待输入超时时间(秒)": 20,
    "控制台菜单每页显示几项": 20,
    "受信任管理员XUID": [],
    "保护管理员XUID": [],
    "默认权限": "11111100",
    "实时管理": {
        "是否启用": False,
        "启动后立即检查": True,
        "在线权限检查间隔(秒)": 1,
        "管理员列表检查间隔(秒)": 60,
        "玩家进服检查延迟(秒)": 2,
        "是否自动修正受管理玩家": True,
        "未授权管理员处理(0:仅提醒,1:设为成员,2:设为访客)": 0,
        "权限修改最小间隔(秒)": 0.5,
        "单玩家修正冷却时间(秒)": 5,
        "修改后复核延迟(秒)": 1,
        "修改后复核超时时间(秒)": 5,
        "失败重试次数": 2,
        "失败重试间隔(秒)": 5,
        "处理记录保留条数": 1000,
    },
}


class ConfigManager:
    """隔离 ToolDelta 配置 API，便于测试和后续扩展。"""

    def __init__(self, cfg_module: Any, plugin_name: str, version: tuple[int, ...]):
        self.cfg_module = cfg_module
        self.plugin_name = plugin_name
        self.version = version

    def load(self) -> dict[str, Any]:
        defaults = {key: (value.copy() if isinstance(value, (list, dict)) else value) for key, value in DEFAULT_CONFIG.items()}
        if self.cfg_module is None:
            return defaults
        try:
            auto_to_std = getattr(self.cfg_module, "auto_to_std", None)
            standard = auto_to_std(defaults) if callable(auto_to_std) else defaults
            loaded, _ = self.cfg_module.get_plugin_config_and_version(
                self.plugin_name, standard, defaults, self.version
            )
            if not isinstance(loaded, dict):
                return defaults
            merged = dict(defaults)
            merged.update(loaded)
            if isinstance(defaults.get("实时管理"), dict):
                group = dict(defaults["实时管理"])
                if isinstance(loaded.get("实时管理"), dict):
                    group.update(loaded["实时管理"])
                merged["实时管理"] = group
            if set(merged) != set(loaded) and hasattr(self.cfg_module, "upgrade_plugin_config"):
                self.cfg_module.upgrade_plugin_config(self.plugin_name, merged, self.version)
            return merged
        except Exception:
            # 旧版配置可能因缺少新键而无法通过严格校验；尽量读取其配置项并补齐默认值。
            try:
                root = getattr(self.cfg_module, "TOOLDELTA_PLUGIN_CFG_DIR")
                import json
                from pathlib import Path
                raw = json.loads((Path(root) / f"{self.plugin_name}.json").read_text(encoding="utf-8"))
                old = raw.get("配置项", raw) if isinstance(raw, dict) else {}
                if isinstance(old, dict):
                    merged = dict(defaults)
                    merged.update(old)
                    if isinstance(defaults.get("实时管理"), dict):
                        group = dict(defaults["实时管理"])
                        if isinstance(old.get("实时管理"), dict):
                            group.update(old["实时管理"])
                        merged["实时管理"] = group
                    if hasattr(self.cfg_module, "upgrade_plugin_config"):
                        self.cfg_module.upgrade_plugin_config(self.plugin_name, merged, self.version)
                    return merged
            except Exception:
                pass
            return defaults
