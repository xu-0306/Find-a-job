"""Native listener settings shared by the launcher and the admin page."""
import json
import os
from pathlib import Path

from pydantic import Field, model_validator

from .store import Input


class Ports(Input):
    admin_port: int = Field(default=8041, strict=True, ge=1, le=65535)
    local_port: int = Field(default=8088, strict=True, ge=1, le=65535)
    public_port: int = Field(default=8443, strict=True)

    @model_validator(mode='after')
    def check_ports(self):
        if self.admin_port == self.local_port:
            raise ValueError('本機後台與前台連接埠不能相同。')
        if self.public_port not in (443, 8443, 10000):
            raise ValueError('Tailscale Funnel 公開埠只支援 443、8443、10000。')
        return self


def config_path(directory=None):
    return Path(directory or os.environ.get('JOB_COMPANION_DATA', 'data')) / 'funnel.json'


def read_config(path):
    if not path.exists():
        return {'enabled': False, **Ports().model_dump()}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or type(data.get('enabled')) is not bool:
        raise ValueError('Invalid sharing settings')
    return {'enabled': data['enabled'], **Ports.model_validate(
        {name: data[name] for name in Ports.model_fields if name in data}).model_dump()}


def write_config(path, config):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(config) + '\n', encoding='utf-8')
    temporary.replace(path)
