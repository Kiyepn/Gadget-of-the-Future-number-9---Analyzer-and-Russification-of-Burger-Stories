# brain/secrets_dpapi.py
"""Шифрование API-ключа средствами Windows DPAPI (CryptProtectData).

Зачем: DeepSeek-ключ раньше лежал в data/settings.json обычным текстом.
Файл копировался в релизную сборку, попадал в бэкапы, и ключ утекал.
Теперь ключ шифруется под текущего пользователя Windows: расшифровать его
можно только на этой машине и только под этим пользователем.

Если DPAPI недоступен (не Windows / ошибка), функции возвращают None, и
вызывающий код откатывается на обычное хранение (совместимость сохраняется).
"""
import base64
import ctypes
import ctypes.wintypes as wt


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wt.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def is_available():
    try:
        return ctypes.windll.crypt32 is not None
    except Exception:
        return False


def _make_blob(data):
    """Возвращает (blob, keepalive) — keepalive держит буфер от сборщика мусора."""
    if not data:
        data = b"\x00"
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def protect(plain_text):
    """Шифрует строку. Возвращает base64-строку или None при ошибке."""
    if not plain_text:
        return None
    if not is_available():
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB), wt.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_DATA_BLOB),
        ]
        crypt32.CryptProtectData.restype = wt.BOOL

        in_blob, keepalive = _make_blob(plain_text.encode("utf-8"))
        out_blob = _DATA_BLOB()
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, None, None, None, 0,
            ctypes.byref(out_blob),
        )
        del keepalive
        if not ok:
            return None
        try:
            raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        return base64.b64encode(raw).decode("ascii")
    except Exception:
        return None


def unprotect(encoded):
    """Расшифровывает base64-строку. Возвращает строку или None при ошибке."""
    if not encoded:
        return None
    if not is_available():
        return None
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except Exception:
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wt.LPWSTR),
            ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            wt.DWORD, ctypes.POINTER(_DATA_BLOB),
        ]
        crypt32.CryptUnprotectData.restype = wt.BOOL

        in_blob, keepalive = _make_blob(raw)
        out_blob = _DATA_BLOB()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0,
            ctypes.byref(out_blob),
        )
        del keepalive
        if not ok:
            return None
        try:
            plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        return plain.decode("utf-8", errors="replace").strip()
    except Exception:
        return None
