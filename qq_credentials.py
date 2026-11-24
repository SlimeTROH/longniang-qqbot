"""QQ password-login material protected by Windows CurrentUser DPAPI.

Only the password MD5 required by the installed NapCat API is retained.
It is still a login secret and is never written in cleartext or logged.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path

CREDENTIAL_FILE = Path(__file__).resolve().parent / '.secrets' / 'qq-login.dpapi'


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(raw: bytes, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise OSError('Windows DPAPI required')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,
                       ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        success = fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(out))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.c_void_p,
                       ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        success = fn(ctypes.byref(source),'QQ bot login',None,None,None,1,ctypes.byref(out))
    fn.restype = wintypes.BOOL
    if not success:
        raise OSError('Windows credential protection unavailable')
    try:
        return ctypes.string_at(out.data,out.size)
    finally:
        kernel.LocalFree(ctypes.cast(out.data,ctypes.c_void_p))
        ctypes.memset(buffer,0,len(raw))


def save_password(account: str, password: str) -> None:
    if not str(account).isdecimal() or not password:
        raise ValueError('Account and password required')
    digest = hashlib.md5(password.encode('utf-8')).hexdigest()
    payload = json.dumps({'account':str(account),'password_md5':digest}).encode('utf-8')
    encrypted = _crypt(payload)
    CREDENTIAL_FILE.parent.mkdir(parents=True,exist_ok=True)
    temporary = CREDENTIAL_FILE.with_suffix('.tmp')
    temporary.write_bytes(encrypted)
    os.replace(temporary,CREDENTIAL_FILE)


def password_digest(account: str) -> str:
    if not CREDENTIAL_FILE.is_file():
        return ''
    record = json.loads(_crypt(CREDENTIAL_FILE.read_bytes(),decrypt=True))
    if record.get('account') != str(account):
        return ''
    digest = str(record.get('password_md5') or '')
    if len(digest) != 32 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Stored login material invalid')
    return digest
