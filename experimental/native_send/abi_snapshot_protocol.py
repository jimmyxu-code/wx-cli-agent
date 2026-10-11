"""Schema 3 task-metadata candidates only. No raw addresses or response claims."""
import hashlib
import hmac
import re

ALLOWED = {'post_start': 0x4279edc, 'callback_map': 0x427b228}
OBSERVATION_SITES = ['post_start', 'callback_map']
REGISTER_NAMES = ('x0', 'x1', 'x2', 'x3', 'x4', 'x19', 'x20', 'x22', 'x23', 'x25', 'sp')
OBJECT_REGISTER = {'post_start': 'x20', 'callback_map': 'x22'}


def identity(value, salt):
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError('invalid_register')
    return hmac.new(salt, value.to_bytes(8, 'little'), hashlib.sha256).hexdigest()[:24]


def validate_marker(value):
    if not isinstance(value, str) or len(value) > 64 or re.fullmatch(r'[A-Za-z0-9_.:-]*', value) is None:
        raise ValueError('invalid_external_marker')
    return value


def packet(event, registers, salt, *, transport_id, command_id, external_marker=''):
    if event not in ALLOWED:
        raise ValueError('unsupported_event')
    if not isinstance(salt, bytes) or len(salt) != 32:
        raise ValueError('private_salt_required')
    if not isinstance(registers, dict) or not all(type(registers.get(k)) is int for k in REGISTER_NAMES):
        raise ValueError('registers_missing')
    identities = {name: identity(registers[name], salt) for name in REGISTER_NAMES}
    for value in (transport_id, command_id):
        if type(value) is not int or not 0 <= value < 2**32:
            raise ValueError('invalid_typed_field')
    scalar = registers['x23'] & 0xffffffff
    matches = transport_id == scalar
    if event == 'post_start' and not matches:
        # The selected PC is after str w23,[x20+8]. A disagreement means the
        # expected observation is not stable; do not save a misleading record.
        raise ValueError('post_start_field_mismatch')
    return {
        'schema': 3, 'event': event, 'verified': False,
        'rawAddressesIncluded': False,
        'registerIdentity': identities,
        'objectIdentity': identities[OBJECT_REGISTER[event]],
        'ownerIdentityCandidate': identities['x19'],
        'transportIdCandidate': transport_id,
        'commandIdCandidate': command_id,
        'w23Candidate': scalar,
        'mapKeyCandidate': scalar if event == 'callback_map' else None,
        'equalities': {'transportIdEqualsW23': matches},
        'taskIdCandidate': None,
        'responseTaskId': None,
        'networkResponseVerified': False,
        'deliveryConfirmed': False,
        'externalMarker': validate_marker(external_marker),
    }
