"""Verify the emulator's public Keystore fixture with the production phone validator.

Run after KeystoreSigningTest, using the public cache/test-keystore-vector.json
fixture. No private key, invitation, enrollment or user content is exported.
"""
import argparse
import base64
import json
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from multidict import CIMultiDict

from permissive.mobile_auth import MobileState, public_key


def verify(path):
    vector = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    key, spki, ident = public_key(vector['publicKey'])
    raw = base64.b64decode(vector['signature'], validate=True)
    if len(raw) != 64:
        raise ValueError('The Android signature must be 64 bytes')
    key.verify(utils.encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big')),
               vector['canonical'].encode('ascii'), ec.ECDSA(hashes.SHA256()))
    headers = CIMultiDict(vector['headers'])
    authority = 'windows.tail123.ts.net:9444'
    created = int(re.search(r';created=([0-9]+);', headers['signature-input'])[1])
    with tempfile.TemporaryDirectory(prefix='android-keystore-proof-') as temporary:
        state = MobileState(temporary)
        try:
            state.configure('https://' + authority, 'http://127.0.0.1:8789')
            with state.db:
                state.db.execute('UPDATE settings SET installation_id=? WHERE id=1', (headers['permissive-instance-id'],))
                state.db.execute('INSERT INTO devices VALUES(?,?,?,?,NULL)', (ident, spki, 'Android Keystore test', created))
            # The request's short admission window need not still be current when
            # a CI host retrieves this public fixture after the emulator finishes.
            with patch('permissive.mobile_auth.time.time', return_value=created + 1):
                if state.verify('GET', authority, '/v1/workspaces', b'', headers) != ident:
                    raise ValueError('The host verified an unexpected phone')
                try:
                    state.verify('GET', authority, '/v1/workspaces', b'', headers)
                except ValueError as error:
                    if 'replay' not in str(error).lower():
                        raise
                else:
                    raise ValueError('The host accepted a replay')
        finally:
            state.close()
    return {'verified': True, 'curve': 'P-256', 'signatureBytes': 64, 'replayRejected': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fixture')
    print(json.dumps(verify(parser.parse_args().fixture)))
