from dataclasses import replace
import pytest
from machine_config import ConfigError, MachineConfig, load_machine_config, requested_model_policy


def test_defaults_and_derived_paths(tmp_path):
    config = MachineConfig.from_mapping({}, runtime_root=str(tmp_path))
    assert config.default_reasoning_effort == 'extra_high'
    assert config.allow_pro is False
    assert config.database_path == tmp_path / 'chatgpt-agent-data' / 'gptagent.db'
    assert config.profile_dir == str(tmp_path / 'chatgpt-agent-data' / 'browser-profile')
    assert config.watchdog_replicas == 1
    assert config.turn_rotation_minutes == 60
    assert config.max_parallel_children_per_agent == 3


def test_two_machines_one_source():
    q = MachineConfig.from_mapping({'identity': {'connector_name': 'GPT-PC', 'machine_slug': 'gpt-pc-1'}, 'paths': {'runtime_root': r'C:\GPT-PC', 'default_work_root': r'D:\projects'}})
    t = MachineConfig.from_mapping({'identity': {'connector_name': 'remote-pc', 'machine_slug': 'remote-pc'}, 'paths': {'runtime_root': r'E:\portable-runtime', 'default_work_root': r'D:\remote-work'}})
    assert q.connector_name != t.connector_name and q.agent_data_dir != t.agent_data_dir
    assert t.agent_data_dir == r'E:\portable-runtime\chatgpt-agent-data'


def test_browser_profile_must_stay_under_persistent_agent_data():
    paths = {'runtime_root': r'C:\GPT-PC', 'agent_data_dir': r'C:\GPT-PC\chatgpt-agent-data'}
    with pytest.raises(ConfigError, match='persistent subdirectory of agent_data_dir'):
        MachineConfig.from_mapping({
            'paths': paths,
            'browser': {'profile_dir': r'C:\GPT-PC\workspaces\research\browser-profile'},
        })
    config = MachineConfig.from_mapping({
        'paths': paths,
        'browser': {'profile_dir': r'C:\GPT-PC\chatgpt-agent-data\browser-profile'},
    })
    assert config.profile_dir == r'C:\GPT-PC\chatgpt-agent-data\browser-profile'


@pytest.mark.parametrize('data', [
    {'identity': {'oauth_token': 'never-print'}}, {'paths': {'secret': 'never-print'}},
    {'browser': {'cookies': []}}, {'unknown': {}}, {'limits': {'unknown': 1}},
    {'browser': {'cdp_endpoint': 'http://0.0.0.0:9232'}},
    {'browser': {'cdp_endpoint': 'http://localhost:9232'}},
    {'browser': {'cdp_endpoint': 'http://127.0.0.1:9232/?token=abc'}},
    {'browser': {'cdp_endpoint': 'https://127.0.0.1:9232'}},
    {'browser': {'cdp_endpoint': 'http://user:pass@127.0.0.1:9232'}},
    {'paths': {'runtime_root': 'relative'}},
    {'browser': {'profile_dir': r'C:\Users\user\AppData\Local\Google\Chrome\User Data'}},
    {'limits': {'max_depth': True}}, {'limits': {'max_live_agents': 0}},
    {'limits': {'max_parallel_children_per_agent': 0}}, {'limits': {'max_parallel_children_per_agent': 5}},
    {'watchdog': {'spawn_interval_minutes': float('nan')}},
    {'watchdog': {'turn_rotation_minutes': 0}}, {'watchdog': {'replicas': 3}},
    {'agents': {'default_reasoning_effort': 'medium'}},
    {'agents': {'allow_pro': 'false'}}, {'watchdog': {'enabled': 1}},
])
def test_reject_invalid_config(tmp_path, data):
    with pytest.raises(ConfigError):
        MachineConfig.from_mapping(data, runtime_root=str(tmp_path))


def test_template_and_local_load():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    assert load_machine_config(root / 'config' / 'machine.example.toml').allow_pro is False
    # Portable tests never require another computer's ignored machine.local.toml.
    assert load_machine_config(root / 'config' / 'machine.example.toml').machine_slug == 'local-pc'


def test_model_defaults_and_pro_gate(tmp_path):
    c = MachineConfig.from_mapping({}, runtime_root=str(tmp_path))
    assert requested_model_policy(c)['requested_reasoning_effort'] == 'extra_high'
    for model in ['Pro', 'GPT-6 Pro']:
        with pytest.raises(ConfigError):
            requested_model_policy(c, model=model, allow_pro=True, user_pro_authorized=True)
    c = replace(c, allow_pro=True)
    with pytest.raises(ConfigError):
        requested_model_policy(c, model='Pro', allow_pro=True)
    assert requested_model_policy(c, model='Pro', allow_pro=True, user_pro_authorized=True)['allow_pro']


@pytest.mark.parametrize('effort', ['high', 'medium'])
def test_effort_lowering_requires_explicit_performance_priority(tmp_path, effort):
    c = MachineConfig.from_mapping({}, runtime_root=str(tmp_path))
    with pytest.raises(ConfigError):
        requested_model_policy(c, reasoning_effort=effort)
    assert requested_model_policy(c, reasoning_effort=effort, performance_priority='load')['requested_reasoning_effort'] == effort


def test_parallel_child_limit_is_machine_local_and_bounded(tmp_path):
    c = MachineConfig.from_mapping({'limits': {'max_parallel_children_per_agent': 3}}, runtime_root=str(tmp_path))
    assert c.max_parallel_children_per_agent == 3



def test_plugin_app_id_is_machine_local_and_validated(tmp_path):
    app_id = "asdk_app_" + "a" * 32
    c = MachineConfig.from_mapping({"chatgpt": {"plugin_app_id": app_id}}, runtime_root=str(tmp_path))
    assert c.plugin_app_id == app_id
    with pytest.raises(ConfigError, match="plugin_app_id"):
        MachineConfig.from_mapping({"chatgpt": {"plugin_app_id": "asdk_app_bad"}}, runtime_root=str(tmp_path))
