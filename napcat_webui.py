"""Authenticated client for a local NapCat WebUI."""
from __future__ import annotations

import hashlib
import time

import httpx


class WebUI:
    def __init__(self, web: dict):
        self.token = str(web.get('token') or '')
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{int(web.get('port', 6099))}/api/",
            trust_env=False, timeout=httpx.Timeout(8.0, connect=3.0),
        )
        self.credential = ''
        self.expires = 0.0

    async def _login(self) -> None:
        response = await self.client.post(
            'auth/login',
            json={'hash': hashlib.sha256((self.token + '.napcat').encode()).hexdigest()},
        )
        response.raise_for_status()
        result = response.json()
        data = result.get('data') or {}
        if data.get('require2FA'):
            raise PermissionError('WebUI requires manual 2FA')
        if result.get('code') != 0 or not data.get('Credential'):
            raise PermissionError('WebUI authentication unavailable')
        self.credential = data['Credential']
        self.expires = time.monotonic() + 3000.0

    async def call(self, action: str, **params):
        if not self.credential or self.expires <= time.monotonic():
            await self._login()
        response = await self.client.post(
            'QQLogin/' + action, json=params,
            headers={'Authorization': 'Bearer ' + self.credential},
        )
        response.raise_for_status()
        result = response.json()
        if result.get('code') != 0:
            self.credential = ''
            message = str(result.get('message') or '').strip()
            detail = f': {message}' if message else ''
            raise RuntimeError(f'NapCat API rejected {action}{detail}')
        return result.get('data')

    async def close(self):
        await self.client.aclose()
