"""Explicit opt-in adapter for audited local wechat_chatter HTTP servers.
No injection, process control, queue, automatic retries, or delivery claims.
Caller must bind human authorization to exact recipient/content before send().
"""
import base64
import hashlib
import http.client
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


class SendError(Exception):
    def __init__(self, code, uncertain=False):
        super().__init__(code)
        self.code, self.uncertain = code, uncertain


class ChatterBackend:
    def __init__(self, endpoint, token, version, verified_build_sha256=None, timeout=20):
        url = urlsplit(endpoint)
        if url.scheme != 'http' or url.hostname not in ('127.0.0.1', '::1') or url.path not in ('', '/') or url.query or url.fragment or url.username:
            raise SendError('loopback_endpoint_required')
        if not token or '\n' in token or '\r' in token:
            raise SendError('private_token_required')
        if not 1 <= timeout <= 60:
            raise SendError('invalid_timeout')
        if not isinstance(version, str) or not re.fullmatch(r'4\.1\.\d+(?:\.\d+)?', version):
            raise SendError('exact_mac_wechat_version_required')
        if verified_build_sha256 is not None and not re.fullmatch('[a-f0-9]{64}', str(verified_build_sha256)):
            raise SendError('invalid_operator_build_attestation')
        self.host, self.port = url.hostname, url.port or 58080
        self.token, self.version, self.timeout = token, version, timeout
        self.verified = bool(verified_build_sha256 and re.fullmatch('[a-f0-9]{64}', verified_build_sha256))

    def _request(self, method, body=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, '/send_private_msg', body=body,
                         headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
            response = conn.getresponse()
            data = response.read(65537)
            if len(data) > 65536:
                raise SendError('oversized_backend_response', method == 'POST')
            return response.status, data
        except (OSError, http.client.HTTPException):
            # POST may have reached backend even when response never arrives.
            raise SendError('transport_failed', method == 'POST') from None
        finally:
            conn.close()

    def capabilities(self, probe=False):
        route = False
        if probe:
            status, _ = self._request('GET')  # upstream GET only returns 405; no send
            route = status == 405
        return {'backend': 'wechat_chatter_native_hook', 'configuredVersion': self.version,
                'versionAttestedByOperator': self.verified, 'routePresent': route,
                'compatibilityVerifiedByAdapter': False,
                'runtimeReadinessVerified': False, 'text': self.verified,
                'fileContent': self.verified, 'preserveFilename': False,
                'deliveryReceipt': False, 'automaticRetry': False}

    def send(self, recipient_id, text=None, file_path=None, expected_sha256=None, allow_filename_change=False):
        if not self.verified:
            raise SendError('version_build_not_verified')
        if not isinstance(recipient_id, str) or not re.fullmatch('[A-Za-z0-9_@.-]{1,128}', recipient_id):
            raise SendError('exact_recipient_id_required')
        if (text is None) == (file_path is None):
            raise SendError('one_payload_required')
        if text is not None:
            if not isinstance(text, str) or not text.strip() or len(text) > 10000 or '\0' in text:
                raise SendError('invalid_text')
            message = {'type': 'text', 'data': {'text': text}}
        else:
            if not allow_filename_change:
                raise SendError('filename_preservation_unverified')
            path = Path(file_path)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
                raise SendError('invalid_file')
            data = path.read_bytes()
            if not expected_sha256 or hashlib.sha256(data).hexdigest() != expected_sha256:
                raise SendError('file_hash_mismatch')
            message = {'type': 'file', 'data': {'file': 'base64://' + base64.b64encode(data).decode('ascii')}}
        status, raw = self._request('POST', json.dumps({'user_id': recipient_id, 'message': [message]}).encode())
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SendError('invalid_backend_response', True) from None
        if status != 200 or not isinstance(result, dict) or result.get('status') != 'ok':
            # Upstream errors can happen after upload or submission: never auto retry.
            raise SendError('backend_rejected_or_uncertain', True)
        return {'backend': 'wechat_chatter_native_hook', 'backendAccepted': True,
                'deliveryConfirmed': False, 'automaticRetry': False,
                'filenamePreserved': None if text is not None else False}
