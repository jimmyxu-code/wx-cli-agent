"""Bounded read-only attach to the exact normally launched temporary copy.
No signing, original attach, request mutation, expressions or message sending.
"""
import argparse, json, os, signal, time
from pathlib import Path
import metadata_trial as trial


def prepare(root):
 import lldb_metadata_collector as collector
 root=trial.canonical_root(root)
 original_hash,plan=trial.preflight(root,collector)
 (root/trial.DISABLED_NAME).rename(root/trial.APP_NAME)
 try:
  main,module=trial.validate_enabled_copy(root,plan,collector)
  record={'schema':1,'originalHash':original_hash,'expires':time.time()+300}
  path=root/'attach-preparation.json'
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'w') as stream:json.dump(record,stream)
 except BaseException:
  if not trial.exact_processes_under(root/trial.APP_NAME):
   (root/trial.APP_NAME).rename(root/trial.DISABLED_NAME)
  raise
 return {'prepared':True,'sendingEnabled':False}


def execute(root,pid,seconds=120):
 import lldb
 import lldb_metadata_collector as collector
 root=trial.canonical_root(root)
 saved=json.loads(trial.read_private(root/'attach-preparation.json'))
 if saved.get('schema')!=1 or not time.time()<saved.get('expires',0) or not 1<=seconds<=120:
  raise trial.TrialError('fresh_attach_preparation_required')
 original_hash=trial.check_original(root,saved['originalHash'])
 if trial.exact_processes_under(trial.ORIGINAL):raise trial.TrialError('original_running_refused')
 plan=json.loads(trial.read_private(root/'signing-plan.json'))
 main,module=trial.validate_enabled_copy(root,plan,collector)
 # Inventory is kernel executable metadata; verify exact expected PID before attach.
 if pid not in trial.exact_processes_under(root/trial.APP_NAME):raise trial.TrialError('exact_temporary_pid_required')
 if (root/collector.TICKET_NAME).exists() or (root/collector.OUTPUT_NAME).exists():raise trial.TrialError('fresh_observation_files_required')
 debugger=process=owner=None;stage='attach_exact_temporary_copy'
 result={'type':'isolated_attach_trial','counts':{n:0 for n in trial.SITES},'sendingEnabled':False,'originalLaunchRequested':False}
 previous=os.environ.get('WX_ABI_PRIVATE_TICKET');handlers={}
 def interrupted(signum,frame):raise trial.TrialError('metadata_trial_interrupted')
 try:
  for signum in (signal.SIGINT,signal.SIGTERM,signal.SIGALRM):handlers[signum]=signal.signal(signum,interrupted)
  signal.alarm(seconds);started=time.monotonic()
  lldb.SBDebugger.Initialize();debugger=lldb.SBDebugger.Create()
  debugger.SkipLLDBInitFiles(True);debugger.SkipAppInitFiles(True);debugger.SetAsync(True)
  target=debugger.CreateTarget(str(main))
  listener=lldb.SBListener('wx-isolated-copy-metadata')
  error=lldb.SBError();process=target.AttachToProcessWithID(listener,pid,error)
  if not error.Success() or not process.IsValid() or process.GetProcessID()!=pid or process.GetState()!=lldb.eStateStopped:
   raise trial.TrialError('owned_attach_failed')
  owner=(pid,process.GetUniqueID())
  ticket=trial.write_ticket(root,process,module,collector)
  os.environ['WX_ABI_PRIVATE_TICKET']=str(root/collector.TICKET_NAME)
  collector._bind_process(target,process,ticket,main,os.geteuid())
  loaded=target.FindModule(lldb.SBFileSpec(str(module)))
  if not loaded.IsValid() or loaded.GetUUIDString().upper()!=collector.EXPECTED_UUID:
   raise trial.TrialError('module_uuid_mismatch')
  stage='install_exact_hardware_sites'
  sites=trial.hardware_sites(debugger,target,module,lldb)
  trial.drain_process_events(listener,process,lldb)
  if not process.Continue().Success():raise trial.TrialError('initial_continue_failed')
  stage='observe_task_metadata'
  print(json.dumps({'type':'isolated_metadata_observation_started','sendingEnabled':False}),flush=True)
  result['counts']=trial.observe(debugger,target,process,sites,collector,lldb,started+max(1,seconds-6),counts=result['counts'],listener=listener)
 except Exception as error:
  result['failureStage']=stage;result['error']='observation_stopped_safely'
  if isinstance(error,trial.TrialError):
   code=str(error)
   if code in trial.SAFE_FAILURE_CODES or code=='owned_attach_failed':result['failureCode']=code
   if error.stop_summary is not None:result['startupStop']=error.stop_summary
 finally:
  signal.alarm(0)
  (root/collector.TICKET_NAME).unlink(missing_ok=True)
  try:
   if owner is None and trial.exact_processes_under(root/trial.APP_NAME):
    result['error']='manual_copy_quit_required'
   else:
    result['originalIntegrityPreserved']=trial.cleanup(process,owner,root,original_hash,lldb,True,restore_original=False)
  except Exception:result['error']='manual_restore_required'
  if debugger is not None:lldb.SBDebugger.Destroy(debugger)
  if previous is None:os.environ.pop('WX_ABI_PRIVATE_TICKET',None)
  else:os.environ['WX_ABI_PRIVATE_TICKET']=previous
  for signum,handler in handlers.items():signal.signal(signum,handler)
  (root/'attach-preparation.json').unlink(missing_ok=True)
 return result


if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('action',choices=['prepare','observe']);parser.add_argument('--root',type=Path,required=True)
 parser.add_argument('--pid',type=int);parser.add_argument('--seconds',type=int,default=120)
 args=parser.parse_args()
 try:
  if args.action=='prepare':result=prepare(args.root)
  elif args.pid is not None:result=execute(args.root,args.pid,args.seconds)
  else:parser.error('--pid required for observe')
  if args.action=='observe':
   report=args.root/'metadata-trial-result-20261010-04.json'
   report.write_text(json.dumps(result));report.chmod(0o600)
  print(json.dumps(result),flush=True)
 except Exception as error:
  print(json.dumps({'error':'isolated_trial_guarded','errorType':type(error).__name__,'detailsWithheld':True}),flush=True)
  raise SystemExit(1)
