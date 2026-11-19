"""Machine-local GPTAgent configuration. No credentials or browser state are portable."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
import ntpath
from pathlib import Path
import re
import tomllib
from typing import Any, Mapping
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


_SCHEMA = {
    'identity': {'connector_name', 'display_name', 'machine_slug'},
    'paths': {'runtime_root', 'default_work_root', 'agent_data_dir'},
    'chatgpt': {'project_name', 'ultra_collaboration', 'plugin_app_id'},
    'browser': {'backend', 'cdp_endpoint', 'profile_dir', 'browser_executable'},
    'watchdog': {'enabled', 'replicas', 'spawn_interval_minutes', 'max_agent_age_minutes',
                 'risk_window_minutes', 'turn_rotation_minutes', 'max_consecutive_recoveries', 'no_progress_turn_limit'},
    'limits': {'max_depth', 'max_children_per_agent', 'max_parallel_children_per_agent', 'max_live_agents'},
    'agents': {'default_reasoning_effort', 'allow_pro', 'latency_priority_allowed_efforts'},
}
_SECRET = re.compile(r'cookie|authorization|password|secret|access.?token|refresh.?token|oauth|api.?key', re.I)


def _reject_unknown(data: Mapping[str, Any]) -> None:
    def walk(value: Any, path: str = '') -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if _SECRET.search(str(k)):
                    raise ConfigError(f'Credential fields are forbidden: {path}{k}')
                walk(v, f'{path}{k}.')
        elif isinstance(value, list):
            for v in value:
                walk(v, path)
    walk(data)
    for table, values in data.items():
        if table not in _SCHEMA or not isinstance(values, dict):
            raise ConfigError(f'Unknown/invalid config table: {table}')
        unknown = set(values) - _SCHEMA[table]
        if unknown:
            raise ConfigError(f'Unknown fields in {table}: {sorted(unknown)}')


def join_path(root: str, *parts: str) -> str:
    if ntpath.splitdrive(root)[0]:
        return ntpath.normpath(ntpath.join(root, *parts))
    return str(Path(root).joinpath(*parts))


def _absolute(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ConfigError(f'{field} must be a nonempty absolute path')
    if not (Path(value).is_absolute() or (ntpath.splitdrive(value)[0] and ntpath.isabs(value))):
        raise ConfigError(f'{field} must be absolute')
    return ntpath.normpath(value) if ntpath.splitdrive(value)[0] else str(Path(value))


def _strict_descendant(path: str, parent: str) -> bool:
    """Cross-platform containment check for machine-local persistent state."""
    if ntpath.splitdrive(path)[0] or ntpath.splitdrive(parent)[0]:
        child = ntpath.normcase(ntpath.normpath(path))
        root = ntpath.normcase(ntpath.normpath(parent))
        try:
            return child != root and ntpath.commonpath([child, root]) == root
        except ValueError:
            return False
    child = Path(path)
    root = Path(parent)
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return child != root


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f'{name} must be an integer in [{minimum}, {maximum}]')
    return value


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigError(f'{name} must be finite and in [{minimum}, {maximum}]')
    return float(value)


@dataclass(frozen=True)
class MachineConfig:
    connector_name: str
    display_name: str
    machine_slug: str
    runtime_root: str
    default_work_root: str
    agent_data_dir: str
    project_name: str = 'GPTAgent'
    cdp_endpoint: str = 'http://127.0.0.1:9232'
    profile_dir: str = ''
    browser_executable: str = 'auto'
    watchdog_enabled: bool = True
    watchdog_replicas: int = 1
    spawn_interval_minutes: float = 30
    max_agent_age_minutes: float = 60
    risk_window_minutes: float = 95
    turn_rotation_minutes: float = 60
    max_consecutive_recoveries: int = 3
    no_progress_turn_limit: int = 3
    max_depth: int = 3
    max_children_per_agent: int = 6
    max_parallel_children_per_agent: int = 3
    max_live_agents: int = 16
    default_reasoning_effort: str = 'extra_high'
    allow_pro: bool = False
    latency_priority_allowed_efforts: tuple[str, ...] = ('high', 'medium')
    ultra_collaboration: bool = False
    plugin_app_id: str = ''

    @property
    def database_path(self) -> Path:
        return Path(join_path(self.agent_data_dir, 'gptagent.db'))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, runtime_root: str | None = None) -> 'MachineConfig':
        _reject_unknown(data)
        ident, paths = data.get('identity', {}), data.get('paths', {})
        browser, wd = data.get('browser', {}), data.get('watchdog', {})
        limits, agents = data.get('limits', {}), data.get('agents', {})
        root = _absolute(paths.get('runtime_root', runtime_root or str(Path(__file__).resolve().parent.parent)), 'runtime_root')
        work = _absolute(paths.get('default_work_root', join_path(root, 'workspaces')), 'default_work_root')
        data_dir = _absolute(paths.get('agent_data_dir', join_path(root, 'chatgpt-agent-data')), 'agent_data_dir')
        profile = _absolute(browser.get('profile_dir', join_path(data_dir, 'browser-profile')), 'profile_dir')
        # The application may not silently take over an ordinary browser profile.
        normalized = profile.replace('\\', '/').lower().rstrip('/')
        if '/google/chrome/user data' in normalized or '/microsoft/edge/user data' in normalized:
            raise ConfigError('A dedicated GPTAgent profile is required; default browser profiles are forbidden')
        if not _strict_descendant(profile, data_dir):
            raise ConfigError('profile_dir must be a dedicated persistent subdirectory of agent_data_dir')
        endpoint = browser.get('cdp_endpoint', 'http://127.0.0.1:9232')
        if not isinstance(endpoint, str):
            raise ConfigError('cdp_endpoint must be a loopback HTTP endpoint')
        parsed = urlsplit(endpoint)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ConfigError('Invalid CDP port') from exc
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'} or not port
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
            raise ConfigError('CDP must be plain HTTP on a literal loopback address, with no credentials/path/query')
        if browser.get('backend', 'playwright-cdp') != 'playwright-cdp':
            raise ConfigError('Unsupported browser backend')
        slug = ident.get('machine_slug', 'local-pc')
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,62}', slug):
            raise ConfigError('Invalid machine_slug')
        connector = ident.get('connector_name', 'Local PC')
        display = ident.get('display_name', connector)
        project = data.get('chatgpt', {}).get('project_name', 'GPTAgent')
        chatgpt = data.get('chatgpt', {})
        collaboration = chatgpt.get('ultra_collaboration', False)
        plugin_app_id = str(chatgpt.get('plugin_app_id', '') or '').strip()
        if plugin_app_id and not re.fullmatch(r'asdk_app_[a-f0-9]{32}', plugin_app_id):
            raise ConfigError('chatgpt.plugin_app_id must be asdk_app_ followed by 32 lowercase hex characters')
        if type(collaboration) is not bool:
            raise ConfigError('chatgpt.ultra_collaboration must be boolean')
        if collaboration and project != 'GPTAgent':
            raise ConfigError('Ultra collaboration requires the exact GPTAgent Project')
        executable = browser.get('browser_executable', 'auto')
        for field, value in [('connector_name', connector), ('display_name', display), ('project_name', project), ('browser_executable', executable)]:
            if not isinstance(value, str) or not value.strip() or len(value) > 400 or '\x00' in value:
                raise ConfigError(f'Invalid {field}')
        for field, value in [('watchdog.enabled', wd.get('enabled', True)), ('agents.allow_pro', agents.get('allow_pro', False))]:
            if type(value) is not bool:
                raise ConfigError(f'{field} must be boolean')
        effort = agents.get('default_reasoning_effort', 'extra_high')
        if effort != 'extra_high':
            raise ConfigError('Default reasoning must remain extra_high; performance exceptions are per task')
        allowed = agents.get('latency_priority_allowed_efforts', ['high', 'medium'])
        if not isinstance(allowed, list) or set(allowed) != {'high', 'medium'} or len(allowed) != 2:
            raise ConfigError('Allowed performance efforts must be high and medium')
        interval = _number(wd.get('spawn_interval_minutes', 30), 'spawn_interval_minutes', 0.01, 1440)
        age = _number(wd.get('max_agent_age_minutes', 60), 'max_agent_age_minutes', 0.02, 2880)
        rotation = _number(wd.get('turn_rotation_minutes', 60), 'turn_rotation_minutes', 0.5, 1440)
        return cls(
            connector, display, slug, root, work, data_dir, project, endpoint.rstrip('/'), profile, executable,
            wd.get('enabled', True), _integer(wd.get('replicas', 1), 'replicas', 1, 1), interval, age,
            _number(wd.get('risk_window_minutes', 95), 'risk_window_minutes', 1, 2880), rotation,
            _integer(wd.get('max_consecutive_recoveries', 3), 'max_consecutive_recoveries', 1, 20),
            _integer(wd.get('no_progress_turn_limit', 3), 'no_progress_turn_limit', 1, 20),
            _integer(limits.get('max_depth', 3), 'max_depth', 1, 10),
            _integer(limits.get('max_children_per_agent', 6), 'max_children_per_agent', 1, 32),
            _integer(limits.get('max_parallel_children_per_agent', 3), 'max_parallel_children_per_agent', 1, 4),
            _integer(limits.get('max_live_agents', 16), 'max_live_agents', 3, 64),
            effort, agents.get('allow_pro', False), tuple(allowed), collaboration, plugin_app_id,
        )


def load_machine_config(path: str | Path | None = None, *, runtime_root: str | None = None) -> MachineConfig:
    root = runtime_root or os.environ.get('GPT_PC_HOME') or str(Path(__file__).resolve().parent.parent)
    config_path = Path(path) if path is not None else Path(join_path(root, 'config', 'machine.local.toml'))
    if config_path.exists():
        with config_path.open('rb') as stream:
            data = tomllib.load(stream)
    else:
        data = {}
    return MachineConfig.from_mapping(data, runtime_root=root)


def requested_model_policy(config: MachineConfig, *, model: str = 'non-pro', reasoning_effort: str | None = None,
                           performance_priority: str | None = None, allow_pro: bool = False,
                           user_pro_authorized: bool = False) -> dict[str, Any]:
    """No agent may self-upgrade to Pro or silently lower reasoning effort."""
    effort = reasoning_effort or config.default_reasoning_effort
    if effort not in {'extra_high', 'high', 'medium'}:
        raise ConfigError('Supported efforts: extra_high, high, medium')
    if effort != 'extra_high' and performance_priority not in {'latency', 'responsiveness', 'load'}:
        raise ConfigError('Lower effort requires explicit latency/responsiveness/load priority')
    if model != 'non-pro' and re.search(r'\bpro\b', model, re.I):
        if not (config.allow_pro and allow_pro and user_pro_authorized):
            raise ConfigError('Pro is forbidden without an explicit user authorization for this task')
    return {'requested_model': model, 'requested_reasoning_effort': effort,
            'allow_pro': bool(config.allow_pro and allow_pro and user_pro_authorized),
            'performance_priority': performance_priority}
