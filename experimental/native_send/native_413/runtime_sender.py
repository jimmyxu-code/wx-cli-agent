"""One explicitly approved native File Helper test. No GUI send or retries.

This is an experimental 4.1.13 controller, separate from read-only observers.
It consumes a private JSON proposal and matching human approval record. A
durable claim is written before enqueue; an uncertain result is never retried.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import time

import runtime_dryrun as runtime
from runtime_dryrun import trial,attached,collector,lldb,Guard
from client import Client,RpcError,json_command

def proposal_digest(proposal):
    return hashlib.sha256(json.dumps(proposal,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def load_approved_proposal(proposal_path,approval_path,clock=time.time):
    try:
        proposal=json.loads(trial.read_private(proposal_path));approval=json.loads(trial.read_private(approval_path))
    except (OSError,ValueError,trial.TrialError):raise Guard('private_proposal_and_approval_required') from None
    if not isinstance(proposal,dict) or not isinstance(approval,dict):raise Guard('json_objects_required')
    if set(proposal)!={'schema','kind','recipient','text','timestamp','client_message_id','maximum_sends','gui_input'}:
        raise Guard('exact_proposal_fields_required')
    if (proposal['schema']!=1 or proposal['kind']!='send_text' or proposal['recipient']!='filehelper'
        or proposal['maximum_sends']!=1 or proposal['gui_input'] is not False):raise Guard('single_self_test_only')
    text=proposal['text']
    if not isinstance(text,str) or not text or '\x00' in text or len(text.encode('utf8'))>16384:
        raise Guard('bounded_text_required')
    for name in ('timestamp','client_message_id'):
        if type(proposal[name]) is not int or not 1<=proposal[name]<2**63:raise Guard('bounded_wire_integer_required')
    if (approval.get('schema')!=1 or approval.get('human_approved') is not True
        or approval.get('proposal_sha256')!=proposal_digest(proposal)
        or type(approval.get('expires_at')) not in (int,float)
        or not clock()<approval['expires_at']<=clock()+3600):raise Guard('fresh_exact_human_approval_required')
    return proposal

def claim_once(path,proposal):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump({'schema':1,'proposal_sha256':proposal_digest(proposal),'client_message_id':proposal['client_message_id'],
            'state':'enqueue_attempt_claimed','automatic_retry_allowed':False},stream)
        stream.flush();os.fsync(stream.fileno())

def execute(root,library,proposal_path,approval_path,claim_path,seconds=110,report_name='native-self-send-result.json',place_window=None):
    if not re.fullmatch(r'(?:native-self-send-result(?:-\d{2})?|native-agent-send-result-[0-9a-f]{32})\.json',report_name):raise Guard('exact_private_report_name_required')
    if (root/report_name).exists():raise Guard('preserve_existing_trial_report')
    proposal=load_approved_proposal(proposal_path,approval_path)
    if claim_path.exists():raise Guard('one_shot_already_claimed_no_retry')
    root=trial.canonical_root(root);library=library.resolve(strict=True)
    library_hash=hashlib.sha256(library.read_bytes()).hexdigest();runtime.check_library(library,library_hash)
    debugger=process=child=owned_identity=None;prepared=False;original_hash=None
    socket_directory=Path(tempfile.mkdtemp(prefix='wxn-')).resolve();socket_path=socket_directory/'rpc.sock';socket_identity=None
    result={'type':'native_self_send_trial','guiUsed':False,'maximumSends':1,'enqueueCalled':False,
        'recipientDeliveryVerified':False,'automaticRetryAllowed':False}
    stage='prepare_exact_copy';deadline=time.monotonic()+seconds
    def interrupted(signum,frame):raise Guard('bounded_sender_interrupted')
    previous=signal.signal(signal.SIGALRM,interrupted);signal.alarm(seconds)
    try:
        attached.prepare(root);prepared=True
        original_hash=json.loads(trial.read_private(root/'attach-preparation.json'))['originalHash']
        app=root/trial.APP_NAME;main=app/trial.MAIN_REL;module_path=app/trial.MODULE_REL
        child=subprocess.Popen([str(main)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            close_fds=True,env={k:os.environ[k] for k in ('HOME','USER','LOGNAME','TMPDIR','PATH','LANG') if k in os.environ})
        time.sleep(2)
        if child.poll() is not None:raise Guard('temporary_child_exited')
        if child.pid not in trial.exact_processes_under(app):raise Guard('exact_child_required')
        if place_window is not None:place_window(child.pid)
        stage='attach_exact_child';lldb.SBDebugger.Initialize();debugger=lldb.SBDebugger.Create()
        debugger.SkipLLDBInitFiles(True);debugger.SkipAppInitFiles(True);debugger.SetAsync(True)
        target=debugger.CreateTarget(str(main));listener=lldb.SBListener('native-one-shot-self-send')
        error=lldb.SBError();process=target.AttachToProcessWithID(listener,child.pid,error)
        if not error.Success() or not process.IsValid() or process.GetProcessID()!=child.pid or process.GetState()!=lldb.eStateStopped:
            raise Guard('owned_attach_failed')
        owned_identity=(process.GetProcessID(),process.GetUniqueID())
        module=target.FindModule(lldb.SBFileSpec(str(module_path)))
        if not module.IsValid() or module.GetUUIDString().upper()!=collector.EXPECTED_UUID:raise Guard('exact_module_required')
        image_base=module.GetObjectFileHeaderAddress().GetLoadAddress(target)
        sites={'post_start':runtime.hardware(debugger,target,module_path,0x4279edc)}
        trial.drain_process_events(listener,process,lldb)
        if not process.Continue().Success():raise Guard('initial_continue_failed')
        owner_pointer=0;stage='wait_live_owner_then_unlock'
        while time.monotonic()<deadline-40:
            state=trial.process_event(listener,process,lldb)
            if state is None or state==lldb.eStateRunning:continue
            if state==lldb.eStateExited:raise Guard('temporary_child_exited')
            if state!=lldb.eStateStopped:raise Guard('unexpected_process_state')
            event=trial.selected_site(target,process,sites,lldb)
            frame=process.GetSelectedThread().GetFrameAtIndex(0)
            if event=='post_start':
                owner_pointer=frame.FindRegister('x19').GetValueAsUnsigned()
                vtable=int.from_bytes(runtime.bounded_memory(process,owner_pointer,8),'little')
                address=target.ResolveLoadAddress(vtable)
                if address.GetModule().GetUUIDString().upper()!=collector.EXPECTED_UUID or address.GetFileAddress()!=0x98c2c00:
                    raise Guard('owner_class_mismatch')
                sites[event].SetEnabled(False)
                sites['after_unlock']=runtime.hardware(debugger,target,module_path,0x427a258)
                if not process.Continue().Success():raise Guard('unlock_continue_failed')
                continue
            if frame.FindRegister('x19').GetValueAsUnsigned()!=owner_pointer:raise Guard('owner_identity_changed')
            sites[event].SetEnabled(False);stage='load_owned_adapter_after_unlock'
            # Remove bootstrap hardware breakpoints from every thread before
            # a new enqueue worker can inherit any debug state. Merely disabling
            # a site is not sufficient evidence that no worker can stop there.
            for breakpoint in sites.values():
                if not target.BreakpointDelete(breakpoint.GetID()):raise Guard('bootstrap_breakpoint_delete_failed')
            if target.GetNumBreakpoints():raise Guard('bootstrap_breakpoints_remain')
            result['bootstrapBreakpointsDeleted']=True
            runtime.check_library(library,library_hash)
            out=lldb.SBCommandReturnObject()
            debugger.GetCommandInterpreter().HandleCommand('settings set target.process.utility-expression-timeout 5',out)
            if not out.Succeeded():raise Guard('utility_timeout_required')
            token=process.LoadImage(lldb.SBFileSpec(str(library)),error)
            if not error.Success() or token==lldb.LLDB_INVALID_IMAGE_TOKEN:raise Guard('adapter_load_failed')
            bind=runtime.call_export(target,process,library,'wx_native_bind','int(*)(unsigned long long,unsigned long long)',str(image_base)+','+str(owner_pointer))
            if bind:raise Guard('adapter_bind_refused')
            stage='start_private_native_rpc'
            authority=os.urandom(32)
            started=runtime.call_export(target,process,library,'wx_native_rpc_start_with_authority','int(*)(const char*,const char*)',
                json.dumps(str(socket_path))+','+json.dumps(authority.hex()))
            if started:raise Guard('private_rpc_start_failed')
            trial.drain_process_events(listener,process,lldb)
            if not process.Continue().Success():raise Guard('rpc_resume_failed')
            client=Client(socket_path);info=socket_path.lstat();socket_identity=(info.st_dev,info.st_ino)
            result['rpcCapabilities']=json_command(client,{'op':'capabilities'})
            if result['rpcCapabilities']['oneApprovedHandleArmed']:raise Guard('fresh_unarmed_server_required')
            stage='check_native_account_presence'
            result['nativeCurrentAccountPresent']=client.current_account_present()
            if not result['nativeCurrentAccountPresent']:
                print(json.dumps({'type':'native_login_needed','enqueueCalled':False,'nativeCurrentAccountPresent':False}),flush=True)
                consecutive=0
                while time.monotonic()<deadline-35:
                    if place_window is not None:place_window(child.pid)
                    event=trial.process_event(listener,process,lldb,timeout=0)
                    if event==lldb.eStateStopped:raise Guard('unexpected_stop_before_login')
                    if event==lldb.eStateExited:raise Guard('temporary_child_exited')
                    consecutive=consecutive+1 if client.current_account_present() else 0
                    if consecutive>=3:
                        result['nativeCurrentAccountPresent']=True;break
                    time.sleep(.3)
            if not result['nativeCurrentAccountPresent']:raise Guard('native_account_not_ready_login_required')
            print(json.dumps({'type':'native_current_account_present','enqueueCalled':False}),flush=True)
            stage='prepare_approved_message'
            if place_window is not None:place_window(child.pid)
            handle=json_command(client,{'op':'prepare','recipient':proposal['recipient'],'text':proposal['text'],
                'timestamp':proposal['timestamp'],'client_message_id':proposal['client_message_id']})['handle']
            fresh=load_approved_proposal(proposal_path,approval_path)
            if proposal_digest(fresh)!=proposal_digest(proposal):raise Guard('approved_proposal_changed')
            claim_once(claim_path,proposal);stage='arm_one_approved_handle'
            client.arm_from_controller(handle,authority)
            stage='enqueue_once'
            result['enqueueCalled']=True
            result['enqueueAccepted']=json_command(client,{'op':'enqueue','handle':handle})['enqueueAccepted']
            print(json.dumps({'type':'native_message_queued','guiUsed':False,'deliveryConfirmed':False}),flush=True)
            stage='wait_native_completion'
            while time.monotonic()<deadline-10:
                time.sleep(1)
                event=trial.process_event(listener,process,lldb,timeout=0)
                if event==lldb.eStateExited or process.GetState()==lldb.eStateExited:raise Guard('temporary_child_exited')
                if event==lldb.eStateStopped:
                    result['stopSummary']=trial.safe_stop_summary(process,lldb)
                    result['stoppedCodeFrames']=runtime.stopped_code_frames(process)
                    raise Guard('unexpected_sender_stop')
                result['status']=json_command(client,{'op':'status','handle':handle})
                status=result['status']
                if status['phase']==4 or (status['phase']==3 and status['destroyed']):break
            status=result.get('status',{})
            result['nativeAcknowledgementSucceeded']=status.get('nativeAcknowledgementSucceeded') is True
            if status.get('phase')==3 and status.get('destroyed'):json_command(client,{'op':'release','handle':handle})
            client.request(7);until=time.monotonic()+3
            while socket_path.exists() and time.monotonic()<until:time.sleep(.05)
            result['rpcStoppedCleanly']=not socket_path.exists()
            break
        else:raise Guard('live_owner_observation_timeout')
    except Exception as error:
        result['failureStage']=stage;result['errorType']=type(error).__name__
        if isinstance(error,(Guard,trial.TrialError)):result['failureCode']=str(error)
        elif isinstance(error,RpcError):
            result['failureCode']=error.code;result['nativeCode']=error.native_code;result['uncertain']=error.uncertain
        if process is not None and process.IsValid() and process.GetState()==lldb.eStateStopped:
            result.setdefault('stopSummary',trial.safe_stop_summary(process,lldb))
            result.setdefault('stoppedCodeFrames',runtime.stopped_code_frames(process))
    finally:
        signal.alarm(0);signal.signal(signal.SIGALRM,previous)
        if prepared:
            try:
                if owned_identity is not None:
                    result['originalIntegrityPreserved']=trial.cleanup(process,owned_identity,root,original_hash,lldb,True,restore_original=False)
                else:
                    if child and child.poll() is None:child.terminate();child.wait(timeout=5)
                    if trial.exact_processes_under(root/trial.APP_NAME):raise Guard('temporary_quit_required')
                    trial.check_original(root,original_hash);trial.unregister_copy(root/trial.APP_NAME)
                    (root/trial.APP_NAME).rename(root/trial.DISABLED_NAME);result['originalIntegrityPreserved']=True
            except Exception:result['cleanupRequired']=True
        if debugger is not None:lldb.SBDebugger.Destroy(debugger)
        if child is not None and child.poll() is None:
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:result['cleanupRequired']=True
        (root/'attach-preparation.json').unlink(missing_ok=True)
        result['copyDisabled']=(root/trial.DISABLED_NAME).is_dir()
        if socket_path.exists() and socket_identity:
            info=socket_path.lstat()
            if (info.st_dev,info.st_ino)==socket_identity and stat.S_ISSOCK(info.st_mode):socket_path.unlink()
        try:socket_directory.rmdir()
        except OSError:result['privateSocketCleanupRequired']=True
        runtime.private_result(root/report_name,result)
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ('root','library','proposal','approval','claim'):parser.add_argument('--'+flag,type=Path,required=True)
    parser.add_argument('--report-name',default='native-self-send-result.json')
    args=parser.parse_args()
    try:print(json.dumps(execute(args.root,args.library,args.proposal,args.approval,args.claim,report_name=args.report_name)),flush=True)
    except Guard as error:
        print(json.dumps({'error':'native_sender_refused','failureCode':str(error),'enqueueCalled':False}),flush=True)
        raise SystemExit(1)
