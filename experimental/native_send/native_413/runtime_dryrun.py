"""Owned temporary-client load/bind/prepare/cancel trial; never calls submit.

Runs via normal subprocess launch followed by attach, without GUI/SpaceO.
It intentionally remains separate from the read-only metadata observer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import struct
import sys
import tempfile
import threading
import time
from client import Client,RpcError,json_command

SEND_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SEND_ROOT))
import metadata_trial as trial
import isolated_attach_trial as attached
import lldb_metadata_collector as collector
import lldb

class Guard(RuntimeError):
    pass

def private_result(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(value,stream)

def bounded_memory(process,address,size):
    if not address or not 0<size<=256:raise Guard('metadata_range')
    error=lldb.SBError();raw=process.ReadMemory(address,size,error)
    if not error.Success() or len(raw)!=size:raise Guard('metadata_unreadable')
    return raw

def stopped_code_frames(process):
    """Only static code locations; never frame arguments, descriptions or data."""
    result=[]
    for index in range(min(process.GetNumThreads(),128)):
        thread=process.GetThreadAtIndex(index)
        reason=thread.GetStopReason()
        if reason in (lldb.eStopReasonNone,lldb.eStopReasonInvalid):continue
        frames=[]
        for n in range(min(thread.GetNumFrames(),8)):
            address=thread.GetFrameAtIndex(n).GetPCAddress();module=address.GetModule()
            if not address.IsValid() or not module.IsValid():continue
            pc=address.GetFileAddress()
            if pc==lldb.LLDB_INVALID_ADDRESS:continue
            frames.append({'module':module.GetFileSpec().GetFilename(),'uuid':module.GetUUIDString(),'fileCodeAddress':hex(pc)})
        result.append({'threadIndex':index,'stopReason':reason,'frames':frames})
        if len(result)>=4:break
    return result

def hardware(debugger,target,module,rva):
    quoted='"'+str(module).replace('\\','\\\\').replace('"','\\"')+'"'
    out=lldb.SBCommandReturnObject();before=target.GetNumBreakpoints()
    debugger.GetCommandInterpreter().HandleCommand('breakpoint set --hardware --shlib '+quoted+' --address '+hex(rva),out)
    if not out.Succeeded() or target.GetNumBreakpoints()!=before+1:raise Guard('hardware_setup')
    bp=target.GetBreakpointAtIndex(before)
    if not bp.IsHardware() or bp.GetNumLocations()!=1:raise Guard('hardware_required')
    address=bp.GetLocationAtIndex(0).GetAddress()
    if address.GetFileAddress()!=rva or Path(address.GetModule().GetFileSpec().fullpath)!=module:
        raise Guard('exact_site_required')
    return bp

def call_export(target,process,library,name,signature,arguments,allow_other_threads=False):
    module=target.FindModule(lldb.SBFileSpec(str(library)))
    if not module.IsValid():raise Guard('adapter_module_missing')
    matches=module.FindSymbols(name,lldb.eSymbolTypeCode)
    if matches.GetSize()!=1:raise Guard('exact_export_required')
    address=matches.GetContextAtIndex(0).GetSymbol().GetStartAddress().GetLoadAddress(target)
    options=lldb.SBExpressionOptions();options.SetLanguage(lldb.eLanguageTypeC_plus_plus)
    options.SetIgnoreBreakpoints(True);options.SetUnwindOnError(True)
    options.SetTimeoutInMicroSeconds(3000000);options.SetTryAllThreads(False)
    options.SetStopOthers(not allow_other_threads);options.SetSuppressPersistentResult(True)
    expression='(('+signature+')'+hex(address)+')('+arguments+')'
    value=process.GetSelectedThread().GetFrameAtIndex(0).EvaluateExpression(expression,options)
    if not value.IsValid() or not value.GetError().Success():raise Guard('adapter_call_failed')
    return value.GetValueAsSigned()

def check_library(path,expected):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or info.st_mode&0o022:
        raise Guard('owned_adapter_required')
    if path.resolve()!=path or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        raise Guard('adapter_identity_changed')
    trial.command(['/usr/bin/codesign','--verify','--strict',str(path)])

def transport_gate_probe(debugger,target,process,listener,module_path,library,client,handle,authority,result,deadline,probe_worker=False):
    """Stop before the exact transport call. Never continue after this probe."""
    if target.GetNumBreakpoints():raise Guard('bootstrap_breakpoints_must_be_deleted')
    trial.drain_process_events(listener,process,lldb)
    if not process.Stop().Success():raise Guard('gate_pause_failed')
    until=min(deadline-10,time.monotonic()+4)
    while time.monotonic()<until:
        state=trial.process_event(listener,process,lldb,timeout=0)
        if state==lldb.eStateStopped:break
        if state not in (None,lldb.eStateRunning):raise Guard('gate_pause_unexpected_state')
        time.sleep(.03)
    else:raise Guard('gate_pause_timeout')
    if not trial.controlled_startup_stop(process,lldb):raise Guard('gate_pause_unexpected_stop')
    gate=hardware(debugger,target,module_path,0x4279ecc)
    address=gate.GetLocationAtIndex(0).GetAddress().GetLoadAddress(target)
    if bounded_memory(process,address,4)!=struct.pack('<I',0xd63f0100):raise Guard('transport_instruction_mismatch')
    result['transportGate']={'hardware':True,'instructionRva':'0x4279ecc','verifiedInstruction':'blr x8',
        'noContinueAfterProbe':True,'bootstrapBreakpointsDeleted':True}
    trial.drain_process_events(listener,process,lldb)
    if not process.Continue().Success():raise Guard('gate_resume_failed')
    done=threading.Event();reply={}
    def issue():
        try:
            client.request(13 if probe_worker else 12,struct.pack('<Q',handle)+authority)
            reply['workerStarted' if probe_worker else 'unexpectedReturn']=True
        except RpcError as error:reply.update({'code':error.code,'nativeCode':error.native_code})
        finally:done.set()
    worker=threading.Thread(target=issue,daemon=True);worker.start()
    until=min(deadline-10,time.monotonic()+8)
    while time.monotonic()<until:
        state=trial.process_event(listener,process,lldb,timeout=0)
        if state==lldb.eStateStopped:
            result['stopSummary']=trial.safe_stop_summary(process,lldb)
            result['stoppedCodeFrames']=stopped_code_frames(process)
            try:event=trial.selected_site(target,process,{'transport_gate':gate},lldb)
            except trial.TrialError:result['wrapperStoppedBeforeGate']=True;return
            if event!='transport_gate':raise Guard('unexpected_probe_site')
            frame=process.GetSelectedThread().GetFrameAtIndex(0)
            task=frame.FindRegister('x20').GetValueAsUnsigned()
            data=bounded_memory(process,task,20)
            table,transport,command,channel=struct.unpack('<QIII',data)
            table_address=target.ResolveLoadAddress(table)
            own=target.FindModule(lldb.SBFileSpec(str(library)))
            own_table=(table_address.GetModule().IsValid() and
                table_address.GetModule().GetUUIDString()==own.GetUUIDString())
            result['taskAtGate']={'ownAdapterVtable':own_table,'transportId':transport,'commandId':command,'channel':channel}
            result['ownWrapperReachedTransportGate']=own_table and command==522 and transport==0
            # This process is intentionally left stopped for finally/cleanup.
            # Do not inspect request/response bytes or resume any stopped task.
            return
        if state==lldb.eStateExited:raise Guard('gate_child_exited')
        if done.is_set():
            result['probeReply']=reply
            if not probe_worker or not reply.get('workerStarted'):raise Guard('wrapper_probe_refused_or_returned')
            if process.GetState()==lldb.eStateRunning:
                phase,rc=struct.unpack('<Ii',client.request(14))
                result['probeWorkerState']={'phase':phase,'result':rc}
                if phase==2:raise Guard('wrapper_probe_worker_refused_or_returned')
        time.sleep(.03)
    result['probeReply']=reply
    raise Guard('wrapper_probe_timeout')

def execute(root,library,seconds=70,diagnostics=False,transport_gate=False,probe_worker=False,wait_account=False):
    if not 1<=seconds<=120:raise Guard('bounded_seconds_required')
    if diagnostics and transport_gate:raise Guard('one_diagnostic_mode_required')
    if probe_worker and not transport_gate:raise Guard('worker_probe_requires_transport_gate')
    root=trial.canonical_root(root);library=library.resolve(strict=True)
    expected=hashlib.sha256(library.read_bytes()).hexdigest();check_library(library,expected)
    debugger=process=child=owned_identity=None;prepared=False;original_hash=None
    socket_directory=Path(tempfile.mkdtemp(prefix='wxn-')).resolve();socket_path=socket_directory/'rpc.sock'
    socket_identity=None
    result={'type':'native_adapter_dryrun','guiUsed':False,'sendingEnabled':False,'submitCalled':False}
    stage='prepare_exact_copy';deadline=time.monotonic()+seconds
    def interrupted(signum,frame):raise Guard('bounded_trial_interrupted')
    old=signal.signal(signal.SIGALRM,interrupted);signal.alarm(seconds)
    try:
        attached.prepare(root);prepared=True
        original_hash=json.loads(trial.read_private(root/'attach-preparation.json'))['originalHash']
        app=root/trial.APP_NAME;main=app/trial.MAIN_REL;module_path=app/trial.MODULE_REL
        child=subprocess.Popen([str(main)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            close_fds=True,env={k:os.environ[k] for k in ('HOME','USER','LOGNAME','TMPDIR','PATH','LANG') if k in os.environ})
        time.sleep(2)
        if child.poll() is not None:raise Guard('temporary_child_exited')
        if child.pid not in trial.exact_processes_under(app):raise Guard('exact_child_required')
        stage='attach_exact_child'
        lldb.SBDebugger.Initialize();debugger=lldb.SBDebugger.Create()
        debugger.SkipLLDBInitFiles(True);debugger.SkipAppInitFiles(True);debugger.SetAsync(True)
        target=debugger.CreateTarget(str(main));listener=lldb.SBListener('native-adapter-dryrun')
        error=lldb.SBError();process=target.AttachToProcessWithID(listener,child.pid,error)
        if not error.Success() or not process.IsValid() or process.GetProcessID()!=child.pid or process.GetState()!=lldb.eStateStopped:
            raise Guard('owned_attach_failed')
        owned_identity=(process.GetProcessID(),process.GetUniqueID())
        module=target.FindModule(lldb.SBFileSpec(str(module_path)))
        if not module.IsValid() or module.GetUUIDString().upper()!=collector.EXPECTED_UUID:raise Guard('exact_module_required')
        image_base=module.GetObjectFileHeaderAddress().GetLoadAddress(target)
        sites={'post_start':hardware(debugger,target,module_path,0x4279edc)}
        trial.drain_process_events(listener,process,lldb)
        if not process.Continue().Success():raise Guard('initial_continue_failed')
        owner_pointer=0;stage='wait_live_owner_then_unlock'
        while time.monotonic()<deadline-15:
            state=trial.process_event(listener,process,lldb)
            if state is None or state==lldb.eStateRunning:continue
            if state==lldb.eStateExited:raise Guard('temporary_child_exited')
            if state!=lldb.eStateStopped:raise Guard('unexpected_process_state')
            event=trial.selected_site(target,process,sites,lldb)
            frame=process.GetSelectedThread().GetFrameAtIndex(0)
            if event=='post_start':
                owner_pointer=frame.FindRegister('x19').GetValueAsUnsigned()
                owner_vtable=int.from_bytes(bounded_memory(process,owner_pointer,8),'little')
                vtable_address=target.ResolveLoadAddress(owner_vtable)
                if (vtable_address.GetModule().GetUUIDString().upper()!=collector.EXPECTED_UUID
                    or vtable_address.GetFileAddress()!=0x98c2c00):raise Guard('owner_class_mismatch')
                result['ownerVtableRva']='0x98c2c00'
                result['ownerActive']=bool(bounded_memory(process,owner_pointer+0x18,1)[0]&1)
                sites[event].SetEnabled(False)
                sites['after_unlock']=hardware(debugger,target,module_path,0x427a258)
                if not process.Continue().Success():raise Guard('unlock_continue_failed')
                continue
            if frame.FindRegister('x19').GetValueAsUnsigned()!=owner_pointer:raise Guard('owner_identity_changed')
            sites[event].SetEnabled(False)
            if transport_gate:
                for breakpoint in sites.values():
                    if not target.BreakpointDelete(breakpoint.GetID()):raise Guard('bootstrap_breakpoint_delete_failed')
                if target.GetNumBreakpoints():raise Guard('bootstrap_breakpoints_remain')
            stage='load_owned_adapter_after_unlock';check_library(library,expected)
            out=lldb.SBCommandReturnObject()
            debugger.GetCommandInterpreter().HandleCommand('settings set target.process.utility-expression-timeout 5',out)
            if not out.Succeeded():raise Guard('utility_timeout_required')
            error=lldb.SBError();token=process.LoadImage(lldb.SBFileSpec(str(library)),error)
            result['adapterLoaded']=error.Success() and token!=lldb.LLDB_INVALID_IMAGE_TOKEN
            if not result['adapterLoaded']:raise Guard('adapter_load_failed')
            stage='bind_exact_owner'
            bind=call_export(target,process,library,'wx_native_bind','int(*)(unsigned long long,unsigned long long)',
                str(image_base)+','+str(owner_pointer))
            result['bindResult']=bind
            if bind:raise Guard('adapter_bind_refused')
            stage='prepare_without_submit'
            handle=call_export(target,process,library,'wx_native_prepare_text',
                'unsigned long long(*)(const char*,const char*,unsigned long long,unsigned long long)',
                '"filehelper","adapter dry-run; never submitted",1,1')
            result['prepareSucceeded']=handle>0
            if handle<=0:raise Guard('preparation_refused')
            cancel=call_export(target,process,library,'wx_native_cancel_prepared','int(*)(unsigned long long)',str(handle))
            result['cancelResult']=cancel
            if cancel:raise Guard('cancel_failed')
            stage='start_unarmed_private_rpc'
            authority=os.urandom(32) if transport_gate else None
            if transport_gate:
                started=call_export(target,process,library,'wx_native_rpc_start_with_authority','int(*)(const char*,const char*)',
                    json.dumps(str(socket_path))+','+json.dumps(authority.hex()))
            else:started=call_export(target,process,library,'wx_native_rpc_start','int(*)(const char*)',json.dumps(str(socket_path)))
            if started:raise Guard('private_rpc_start_failed')
            trial.drain_process_events(listener,process,lldb)
            if not process.Continue().Success():raise Guard('rpc_continue_failed')
            client=Client(socket_path)
            for _ in range(30):
                if socket_path.exists():break
                time.sleep(.05)
            info=socket_path.lstat();socket_identity=(info.st_dev,info.st_ino)
            result['rpcCapabilities']=json_command(client,{'op':'capabilities'})
            if result['rpcCapabilities']['oneApprovedHandleArmed']:raise Guard('dryrun_must_be_unarmed')
            result['nativeCurrentAccountPresent']=client.current_account_present()
            if wait_account and not result['nativeCurrentAccountPresent']:
                stage='wait_human_login_read_only'
                print(json.dumps({'type':'native_login_needed','sendingEnabled':False,'nativeCurrentAccountPresent':False}),flush=True)
                while time.monotonic()<deadline-15:
                    event=trial.process_event(listener,process,lldb,timeout=0)
                    if event==lldb.eStateStopped:raise Guard('unexpected_stop_during_login')
                    if event==lldb.eStateExited:raise Guard('temporary_child_exited')
                    if client.current_account_present():
                        result['nativeCurrentAccountPresent']=True;break
                    time.sleep(.3)
                if not result['nativeCurrentAccountPresent']:raise Guard('human_login_not_completed_within_bound')
                print(json.dumps({'type':'native_current_account_present','sendingEnabled':False}),flush=True)
            stage='json_prepare_status_cancel'
            prepared_task=json_command(client,{'op':'prepare','recipient':'filehelper','text':'structured RPC dry-run; never sent',
                'timestamp':1,'client_message_id':2})
            h=prepared_task['handle'];status=json_command(client,{'op':'status','handle':h})
            result['rpcPreparedStatus']={k:status[k] for k in ('phase','transportId','serialized','parsed')}
            if status['phase']!=1 or status['transportId'] or status['serialized']:raise Guard('dryrun_state_invalid')
            if transport_gate:
                stage='wrapper_probe_with_transport_gate'
                result['gatedProbeOnNewWorker']=probe_worker
                transport_gate_probe(debugger,target,process,listener,module_path,library,client,h,authority,result,deadline,probe_worker)
                result['probeStoppedWithoutContinue']=process.GetState()==lldb.eStateStopped
                result['noNativeTransportCall']=result.get('probeStoppedWithoutContinue') is True
                break
            if diagnostics:
                stage='local_native_buffer_diagnostic'
                result['nativeBufferDiagnostic']=client.validate_local_buffer(h)
                status=client.status(h)
                if status['phase']!=1 or status['serialized'] or status['destroyed']:raise Guard('diagnostic_changed_prepared_task')
                stage='local_thread_diagnostic';client.probe_thread()
                result['threadDiagnosticStates']=[]
                until=time.monotonic()+4
                while time.monotonic()<until:
                    event=trial.process_event(listener,process,lldb,timeout=0)
                    state=process.GetState();phase=client.probe_thread_state()
                    result['threadDiagnosticStates'].append({'publicState':state,'eventState':event,'probePhase':phase})
                    if event==lldb.eStateStopped:
                        result['diagnosticStopSummary']=trial.safe_stop_summary(process,lldb)
                        result['diagnosticCodeFrames']=stopped_code_frames(process)
                        raise Guard('local_probe_unexpected_stop')
                    if phase==2:break
                    time.sleep(.05)
                if phase!=2:raise Guard('local_probe_timeout')
            try:json_command(client,{'op':'enqueue','handle':h})
            except RpcError as error:
                if error.native_code!=-8 or error.uncertain:raise Guard('unarmed_gate_failed')
                result['unarmedSendRefused']=True
            else:raise Guard('unexpected_unarmed_enqueue')
            result['rpcCancelSucceeded']=json_command(client,{'op':'cancel','handle':h})['cancelled']
            client.request(7)
            until=time.monotonic()+3
            while socket_path.exists() and time.monotonic()<until:time.sleep(.05)
            result['rpcStoppedCleanly']=not socket_path.exists()
            if not result['rpcStoppedCleanly']:raise Guard('private_rpc_stop_failed')
            result['dryrunPassed']=True
            break
        if not result.get('dryrunPassed') and not result.get('probeStoppedWithoutContinue'):raise Guard('live_owner_observation_timeout')
    except Exception as error:
        result['failureStage']=stage;result['errorType']=type(error).__name__
        if isinstance(error,(Guard,trial.TrialError)):result['failureCode']=str(error)
        elif isinstance(error,RpcError):result['failureCode']=error.code
        if process is not None and process.IsValid() and process.GetState()==lldb.eStateStopped:
            result['stopSummary']=trial.safe_stop_summary(process,lldb)
            result['stoppedCodeFrames']=stopped_code_frames(process)
    finally:
        signal.alarm(0);signal.signal(signal.SIGALRM,old)
        if prepared:
            try:
                if owned_identity is not None:
                    result['originalIntegrityPreserved']=trial.cleanup(process,owned_identity,root,original_hash,lldb,True,restore_original=False)
                else:
                    if child and child.poll() is None:
                        child.terminate();child.wait(timeout=5)
                    if trial.exact_processes_under(root/trial.APP_NAME):raise Guard('temporary_quit_required')
                    trial.check_original(root,original_hash)
                    trial.unregister_copy(root/trial.APP_NAME)
                    (root/trial.APP_NAME).rename(root/trial.DISABLED_NAME)
                    result['originalIntegrityPreserved']=True
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
        name=('native-login-state-result.json' if wait_account else
            'native-worker-gated-result.json' if probe_worker else 'native-wrapper-gated-result.json' if transport_gate else
            'native-local-diagnostic-result.json' if diagnostics else 'native-adapter-rpc-dryrun-result.json')
        private_result(root/name,result)
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--library',type=Path,required=True);parser.add_argument('--diagnostics',action='store_true')
    parser.add_argument('--transport-gate',action='store_true');parser.add_argument('--probe-worker',action='store_true')
    parser.add_argument('--wait-account',action='store_true');args=parser.parse_args()
    print(json.dumps(execute(args.root,args.library,seconds=120 if args.wait_account else 70,diagnostics=args.diagnostics,
        transport_gate=args.transport_gate,probe_worker=args.probe_worker,wait_account=args.wait_account)),flush=True)
