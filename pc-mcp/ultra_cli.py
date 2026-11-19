"""Single-step local entrypoint for Ultra while a live MCP schema awaits reload.

Uses the same AgentManager and Runtime identity/control checks as the MCP facade.
This is not a daemon, does not enable Ultra implicitly, and never starts Codex.
Usage: python ultra_cli.py --request-file request.json (or JSON on stdin).
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
import sys

from chatgpt_agent_store import AgentError, public_text


def dispatch(request: dict) -> dict:
    if not isinstance(request, dict):
        raise AgentError('REQUEST_INVALID', 'Expected a JSON object')
    import server
    allowed = set(inspect.signature(server.chatgpt_agent).parameters)
    unknown = set(request) - allowed
    if unknown:
        raise AgentError('REQUEST_FIELDS_INVALID', 'Unknown facade arguments', fields=sorted(unknown))
    action = request.get('action')
    if not action or not request.get('workspace_lease_id'):
        raise AgentError('CALLER_IDENTITY_REQUIRED', 'action and the current active workspace_lease_id are required')
    arguments = dict(request)
    arguments.pop('action')
    result = server._get_chatgpt_agent_manager().dispatch(action, **arguments)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-file', type=Path)
    args = parser.parse_args(argv)
    try:
        text = args.request_file.read_text(encoding='utf-8-sig') if args.request_file else sys.stdin.read(200001)
        if len(text) > 200000:
            raise AgentError('REQUEST_TOO_LARGE', 'Request exceeds 200000 characters')
        result = dispatch(json.loads(text))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('status') not in {'blocked','reconciling'} else 2
    except (AgentError, ValueError, OSError) as exc:
        print(json.dumps({'status':'error','code':getattr(exc,'code',type(exc).__name__),
                          'message':public_text(str(exc), 1000),'details':getattr(exc,'details',{})}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
