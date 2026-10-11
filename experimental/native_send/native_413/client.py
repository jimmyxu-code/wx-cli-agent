"""JSON command-line client for a private native 4.1.13 Unix socket.

No GUI, injection, model calls, HTTP server, or automatic send retry. Prepare
and status are available unarmed. The native controller alone can arm one
explicitly approved handle; sending errors remain distinguishable from delivery.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys


class RpcError(RuntimeError):
    def __init__(self,code,uncertain=False,native_code=None):
        super().__init__(code);self.code=code;self.uncertain=uncertain;self.native_code=native_code


def owned_socket(path):
    path=Path(path)
    if not path.is_absolute() or len(os.fsencode(path))>=104 or path.parent.resolve()!=path.parent:
        raise RpcError('canonical_private_socket_required')
    directory=path.parent.lstat();entry=path.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid!=os.geteuid() or stat.S_IMODE(directory.st_mode)!=0o700:
        raise RpcError('private_socket_directory_required')
    if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid!=os.geteuid() or stat.S_IMODE(entry.st_mode)!=0o600:
        raise RpcError('owned_socket_required')
    return path


def receive_exact(connection,n):
    result=bytearray()
    while len(result)<n:
        value=connection.recv(n-len(result))
        if not value:raise RpcError('native_response_incomplete')
        result.extend(value)
    return bytes(result)


class Client:
    def __init__(self,path):self.path=path
    def request(self,op,body=b''):
        if len(body)>16664:raise RpcError('bounded_payload_required')
        is_send=op==2;attempted=False
        try:
            path=owned_socket(self.path)
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(4);connection.connect(str(path))
                # Authentication is local OS identity, not a bearer token in logs.
                libc=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
                uid=ctypes.c_uint();gid=ctypes.c_uint()
                if libc.getpeereid(connection.fileno(),ctypes.byref(uid),ctypes.byref(gid)) or uid.value!=os.geteuid():
                    raise RpcError('same_uid_server_required')
                attempted=True;connection.sendall(b'WXN1'+struct.pack('<II',op,len(body))+body)
                magic,result,size=struct.unpack('<4siI',receive_exact(connection,12))
                if magic!=b'WXR1' or size>256:raise RpcError('invalid_native_response',is_send)
                data=receive_exact(connection,size)
                if result:
                    # A rejected unarmed request never reaches native enqueue.
                    raise RpcError('native_rejected',is_send and result not in (-8,-10,-12),result)
                return data
        except (OSError,TimeoutError):raise RpcError('native_transport_failed',is_send and attempted) from None
        except RpcError as error:
            if is_send and attempted and error.code=='native_response_incomplete':error.uncertain=True
            raise

    def capabilities(self):
        data=self.request(6)
        if len(data)!=16:raise RpcError('invalid_capabilities')
        schema,build,bound,armed=struct.unpack('<IIII',data)
        if schema!=1 or build!=269602 or bound not in (0,1) or armed not in (0,1):raise RpcError('invalid_capabilities')
        return {'schema':schema,'backend':'wechat_native_413','build':build,'bound':bool(bound),'oneApprovedHandleArmed':bool(armed),
            'guiUsed':False,'deliveryVerified':False,'automaticRetry':False}

    def prepare(self,recipient,text,timestamp,client_message_id):
        if not isinstance(recipient,str) or not isinstance(text,str) or '\0' in recipient or '\0' in text:
            raise RpcError('valid_text_fields_required')
        r=recipient.encode('utf8');t=text.encode('utf8')
        if not 1<=len(r)<=128 or not 1<=len(t)<=16384:raise RpcError('bounded_text_fields_required')
        if any(type(n) is not int or not 1<=n<2**63 for n in (timestamp,client_message_id)):
            raise RpcError('positive_wire_integer_required')
        data=self.request(1,struct.pack('<QQII',timestamp,client_message_id,len(r),len(t))+r+t)
        if len(data)!=8:raise RpcError('invalid_preparation_response')
        handle=struct.unpack('<Q',data)[0]
        if not handle:raise RpcError('invalid_handle')
        return {'handle':handle,'phase':'prepared','submitted':False,'guiUsed':False}

    def handle_command(self,op,handle):
        if type(handle) is not int or not 1<=handle<2**64:raise RpcError('positive_handle_required')
        return self.request(op,struct.pack('<Q',handle))

    def enqueue(self,handle):
        data=self.handle_command(2,handle)
        if data:raise RpcError('invalid_enqueue_response',True)
        return {'handle':handle,'enqueueAccepted':True,'deliveryConfirmed':False,'automaticRetry':False,'guiUsed':False}

    def arm_from_controller(self,handle,authority):
        if type(handle) is not int or not 1<=handle<2**64 or not isinstance(authority,bytes) or len(authority)!=32:
            raise RpcError('private_controller_authority_required')
        if self.request(8,struct.pack('<Q',handle)+authority):raise RpcError('invalid_arm_response')
        return {'handle':handle,'oneApprovedHandleArmed':True}

    def validate_local_buffer(self,handle):
        if self.handle_command(9,handle):raise RpcError('invalid_diagnostic_response')
        return {'handle':handle,'ownPayloadNativeBufferRoundTrip':True,'submitted':False}

    def probe_thread(self):
        if self.request(10):raise RpcError('invalid_probe_response')
        return {'localThreadCreated':True,'nativeTransportCalled':False}

    def probe_thread_state(self):
        data=self.request(11)
        if len(data)!=4:raise RpcError('invalid_probe_response')
        phase=struct.unpack('<I',data)[0]
        if phase not in (0,1,2):raise RpcError('invalid_probe_response')
        return phase

    def current_account_present(self):
        data=self.request(15)
        if len(data)!=4:raise RpcError('invalid_account_presence_response')
        present=struct.unpack('<I',data)[0]
        if present not in (0,1):raise RpcError('invalid_account_presence_response')
        return bool(present)

    def status(self,handle):
        data=self.handle_command(3,handle)
        if len(data)!=52:raise RpcError('invalid_status_response')
        h,client_id,transport,phase,errtype,errcode,ret,present,serialized,parsed,destroyed=struct.unpack('<QQIIiiiIIII',data)
        if h!=handle or phase not in (1,2,3,4,5) or any(n not in (0,1) for n in (present,serialized,parsed,destroyed)):
            raise RpcError('invalid_status_response')
        acknowledged=(phase==3 and errtype==0 and errcode==0 and present==1 and ret==0 and serialized==1 and parsed==1)
        return {'handle':handle,'clientMessageId':client_id,'transportId':transport,'phase':phase,'networkErrorType':errtype,
            'networkErrorCode':errcode,'responseRet':ret if present else None,'serialized':bool(serialized),'parsed':bool(parsed),
            'destroyed':bool(destroyed),'nativeAcknowledgementSucceeded':acknowledged,'recipientDeliveryVerified':False}

    def cancel(self,handle):
        if self.handle_command(4,handle):raise RpcError('invalid_cancel_response')
        return {'handle':handle,'cancelled':True,'submitted':False}

    def release(self,handle):
        if self.handle_command(5,handle):raise RpcError('invalid_release_response')
        return {'handle':handle,'released':True}


def json_command(client,command):
    if not isinstance(command,dict) or not isinstance(command.get('op'),str):raise RpcError('json_command_required')
    op=command['op']
    if op=='capabilities' and set(command)=={'op'}:return client.capabilities()
    if op=='prepare' and set(command)=={'op','recipient','text','timestamp','client_message_id'}:
        return client.prepare(command['recipient'],command['text'],command['timestamp'],command['client_message_id'])
    if op in ('enqueue','status','cancel','release') and set(command)=={'op','handle'}:
        return getattr(client,op)(command['handle'])
    raise RpcError('exact_command_fields_required')


def unique_object(pairs):
    value={}
    for name,item in pairs:
        if name in value:raise ValueError('duplicate_json_field')
        value[name]=item
    return value


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--socket',type=Path,required=True)
    args=parser.parse_args();client=Client(args.socket)
    # JSON Lines are independent commands. An uncertain enqueue is reported and
    # stops the stream; it is never silently replayed after reconnect.
    while True:
        line=sys.stdin.buffer.readline(100001)
        if not line:break
        try:
            if len(line)>100000:raise RpcError('bounded_json_required')
            output=json_command(client,json.loads(line,object_pairs_hook=unique_object));print(json.dumps(output,ensure_ascii=False),flush=True)
        except (ValueError,UnicodeError):
            print(json.dumps({'error':'invalid_json_or_utf8','automaticRetry':False}),flush=True);raise SystemExit(1)
        except RpcError as error:
            print(json.dumps({'error':error.code,'nativeCode':error.native_code,'uncertain':error.uncertain,'automaticRetry':False}),flush=True)
            raise SystemExit(1)
