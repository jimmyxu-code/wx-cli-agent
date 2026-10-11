#include "core.h"
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>
#include <pthread.h>
#include <mach/mach.h>
#include <mach/arm/thread_status.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>

namespace {
// Derived from the local 4.1.13/269602 image, not a cross-version offset shift.
constexpr uintptr_t kStartWrapper=0x427995c, kBufferWrite=0x429f91c;
constexpr uintptr_t kBufferAt=0x429f980, kBufferSize=0x429f9c0;
using Bytes=std::vector<uint8_t>;
struct State {
 std::mutex lock;
 wx_native_status status{};
 Bytes payload;
};
struct Task;
using Fn=void(*)();
struct Callback { Fn *table; Task *task; uint64_t reserved; };
struct Task {
 Fn *table; uint32_t transport_id=0, command_id=522;
 uint32_t channel=3, padding=0;
 std::string cgi="/cgi-bin/micromsg-bin/newsendmsg";
 uint32_t flags=0x01010100, padding2=0;
 std::string user;
 Callback callback{};
 Callback *callback_ptr=&callback;
 uint8_t config=0; uint8_t padding3[3]{}; uint32_t limit=0;
 // Fields above 0x78 belong to this adapter, never to another native object.
 std::shared_ptr<State> state;
 explicit Task(std::shared_ptr<State> s):state(std::move(s)){}
 ~Task(){std::lock_guard<std::mutex> g(state->lock);state->status.destroyed=1;}
};
static_assert(sizeof(std::string)==24 && alignof(std::string)==8);
static_assert(offsetof(Task,transport_id)==8);
static_assert(offsetof(Task,command_id)==12);
static_assert(offsetof(Task,channel)==16);
static_assert(offsetof(Task,cgi)==24);
static_assert(offsetof(Task,flags)==48);
static_assert(offsetof(Task,user)==56);
static_assert(offsetof(Task,callback)==80);
static_assert(offsetof(Task,callback_ptr)==104);
static_assert(offsetof(Task,config)==112);
static_assert(offsetof(Task,limit)==116);
static_assert(offsetof(Task,state)==120);
static_assert(sizeof(Callback)==24);

std::mutex registry_lock;
std::unordered_map<uint64_t,std::shared_ptr<State>> registry;
std::atomic<uint64_t> next_handle{1};
std::atomic<bool> starting_task{false};
std::atomic<uint32_t> probe_thread_phase{0};
std::atomic<uint32_t> probe_wrapper_phase{0};
std::atomic<int32_t> probe_wrapper_result{0};
uintptr_t base=0; void *owner=nullptr;
struct Api {
 void (*start)(void*,Task*)=nullptr;
 void (*write)(void*,const uint8_t*,uint32_t)=nullptr;
 const uint8_t *(*at)(void*,uint32_t)=nullptr;
 uint32_t (*size)(void*)=nullptr;
 void (*construct_buffer)(void*)=nullptr;
 void (*destroy_buffer)(void*)=nullptr;
} api;

bool exact_image(uintptr_t address){
 char executable[4096];uint32_t size=sizeof(executable);
 if(_NSGetExecutablePath(executable,&size))return false;
 std::string path(executable),suffix="/WeChat-trial.app/Contents/MacOS/WeChat";
 if(path.size()<suffix.size()||path.compare(path.size()-suffix.size(),suffix.size(),suffix))return false;
 for(uint32_t i=0;i<_dyld_image_count();i++){
  const auto *h=_dyld_get_image_header(i);
  if(reinterpret_cast<uintptr_t>(h)!=address||h->magic!=MH_MAGIC_64)continue;
  const char *image=_dyld_get_image_name(i);
  if(!image||std::string(image).find("/WeChat-trial.app/Contents/Resources/wechat.dylib")==std::string::npos)return false;
  const uint8_t expected[]={0xcd,0xb8,0x10,0x58,0x0f,0xac,0x35,0x18,0x95,0xf5,0xc0,0xcc,0x78,0x60,0xf9,0xb5};
  const auto *p=reinterpret_cast<const uint8_t*>(h)+sizeof(mach_header_64);
  if(h->ncmds>256||h->sizeofcmds>65536)return false;
  const auto *end=p+h->sizeofcmds;
  for(uint32_t j=0;j<h->ncmds;j++){
   if(end-p<static_cast<ptrdiff_t>(sizeof(load_command)))return false;
   const auto *lc=reinterpret_cast<const load_command*>(p);
   if(lc->cmdsize<sizeof(load_command)||lc->cmdsize>size_t(end-p))return false;
   if(lc->cmd==LC_UUID){
    if(lc->cmdsize<sizeof(uuid_command))return false;
    return !std::memcmp(reinterpret_cast<const uuid_command*>(p)->uuid,expected,16);
   }
   p+=lc->cmdsize;
  }
 }
 return false;
}

void varint(Bytes &b,uint64_t n){while(n>=128){b.push_back((n&127)|128);n>>=7;}b.push_back(n);}
void number(Bytes &b,uint32_t field,uint64_t n){varint(b,uint64_t(field)<<3);varint(b,n);}
void blob(Bytes &b,uint32_t field,const uint8_t *p,size_t n){
 varint(b,(uint64_t(field)<<3)|2);varint(b,n);b.insert(b.end(),p,p+n);
}
void blob(Bytes &b,uint32_t f,const Bytes &s){blob(b,f,s.data(),s.size());}
void string_field(Bytes &b,uint32_t f,const std::string &s){blob(b,f,reinterpret_cast<const uint8_t*>(s.data()),s.size());}
bool utf8(const std::string &s){
 size_t i=0;
 while(i<s.size()){
  uint8_t a=s[i++];if(a<128)continue;
  unsigned n;uint32_t value,min;
  if(a>=0xc2&&a<=0xdf){n=1;value=a&31;min=0x80;}
  else if(a>=0xe0&&a<=0xef){n=2;value=a&15;min=0x800;}
  else if(a>=0xf0&&a<=0xf4){n=3;value=a&7;min=0x10000;}else return false;
  if(i+n>s.size())return false;
  while(n--){uint8_t c=s[i++];if((c&0xc0)!=0x80)return false;value=(value<<6)|(c&63);}
  if(value<min||value>0x10ffff||(value>=0xd800&&value<=0xdfff))return false;
 }
 return true;
}
bool recipient_valid(const std::string &s){
 if(s.empty()||s.size()>128)return false;
 for(unsigned char c:s)if(!((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||c=='_'||c=='-'||c=='@'||c=='.'))return false;
 return true;
}
Bytes make_text(const std::string &recipient,const std::string &text,uint64_t when,uint64_t id){
 // Verified in one 4.1.13/build 269602 File Helper text delivery. Other message
 // types, recipients and builds still require their own runtime verification.
 Bytes name,body,outer;string_field(name,1,recipient);blob(body,1,name);
 string_field(body,2,text);number(body,3,1);number(body,4,when);number(body,5,id);
 string_field(body,6,"<msgsource><alnode><fr>1</fr></alnode></msgsource>");
 number(outer,1,1);blob(outer,2,body);return outer;
}
bool read_varint(const uint8_t *p,size_t n,size_t &at,uint64_t &v){
 v=0;for(unsigned i=0;i<10&&at<n;i++){uint8_t c=p[at++];if(i==9&&c>1)return false;v|=uint64_t(c&127)<<(7*i);if(!(c&128))return true;}return false;
}
// Missing/malformed BaseResponse is unknown, never implicitly success.
bool find_ret(const uint8_t *p,size_t n,int32_t &ret,bool nested=false){
 size_t at=0;bool found=false;
 while(at<n){
  uint64_t tag;if(!read_varint(p,n,at,tag)||!(tag>>3))return false;
  uint64_t field=tag>>3,wire=tag&7,value=0;
  if(wire==0){if(!read_varint(p,n,at,value))return false;if(nested&&field==1){if(found)return false;ret=int32_t(uint32_t(value));found=true;}}
  else if(wire==2){
   uint64_t len;if(!read_varint(p,n,at,len)||len>n-at)return false;
   if(!nested&&field==1){if(found)return false;found=find_ret(p+at,size_t(len),ret,true);if(!found)return false;}
   at+=size_t(len);
  }else if(wire==1){if(n-at<8)return false;at+=8;}
  else if(wire==5){if(n-at<4)return false;at+=4;}
  else return false;
 }
 return found;
}
std::shared_ptr<State> lookup(uint64_t h){
 std::lock_guard<std::mutex> g(registry_lock);auto it=registry.find(h);return it==registry.end()?nullptr:it->second;
}
void destroy(Task *t){t->~Task();}
void destroy_free(Task *t){delete t;}
bool serialize(Task *t,void *buffer,uint32_t *,uint8_t *flag,int32_t *){
 if(flag)*flag=0;
 auto state=t->state;
 api.write(buffer,state->payload.data(),uint32_t(state->payload.size()));
 std::lock_guard<std::mutex> g(state->lock);state->status.serialized=1;return true;
}
bool parse(Task *t,void *buffer){
 auto p=api.at(buffer,0);
 uint32_t size=api.size(buffer);
 int32_t ret=0;bool present=p&&size<=65536&&find_ret(p,size,ret);
 std::lock_guard<std::mutex> g(t->state->lock);
 t->state->status.parsed=1;t->state->status.response_ret_present=present;
 if(present)t->state->status.response_ret=ret;return true;
}
void *empty_proto(Task *){return nullptr;}
void callback_noop(Callback *){}
void *callback_clone(Callback *c){return c;}
void callback_copy(Callback *c,void *out){std::memcpy(out,c,sizeof(*c));}
int callback_invoke(Callback *c,Task **task,const int32_t *type,const int32_t *code){
 if(!c||!task||*task!=c->task||!type||!code)return 0;
 auto state=c->task->state;
 {std::lock_guard<std::mutex> g(state->lock);state->status.transport_id=c->task->transport_id;
  state->status.network_error_type=*type;state->status.network_error_code=*code;state->status.phase=3;}
 return 1; // Native owner calls our deleting destructor after this returns.
}
Fn task_table[]={reinterpret_cast<Fn>(destroy),reinterpret_cast<Fn>(destroy_free),
 reinterpret_cast<Fn>(serialize),reinterpret_cast<Fn>(parse),reinterpret_cast<Fn>(empty_proto),reinterpret_cast<Fn>(empty_proto)};
Fn callback_table[]={reinterpret_cast<Fn>(callback_noop),reinterpret_cast<Fn>(callback_noop),
 reinterpret_cast<Fn>(callback_clone),reinterpret_cast<Fn>(callback_copy),
 reinterpret_cast<Fn>(callback_noop),reinterpret_cast<Fn>(callback_noop),reinterpret_cast<Fn>(callback_invoke)};

int dispatch(uint64_t h,uint32_t expected_phase){
 auto state=lookup(h);if(!state||!base||!owner)return -1;
 {std::lock_guard<std::mutex> g(state->lock);if(state->status.phase!=expected_phase)return -2;
  if(!(reinterpret_cast<const volatile uint8_t*>(owner)[0x18]&1)){state->status.phase=4;return -3;}
  state->status.phase=2;}
 auto task=new Task(state);task->table=task_table;task->callback.table=callback_table;task->callback.task=task;
 // This wrapper is void. Its final x0 comes from mutex_unlock; interpreting
 // that register as success/task ID is incorrect. The callback proves progress.
 api.start(owner,task);return 0;
}
struct Job {uint64_t handle;std::shared_ptr<State> state;};
void *dispatch_job(void *raw){
 std::unique_ptr<Job> job(static_cast<Job*>(raw));
 try{dispatch(job->handle,5);}
 catch(...){
  // The caller must treat an interrupted native call as unknown, never resend.
  std::lock_guard<std::mutex> g(job->state->lock);
  if(job->state->status.phase!=3){job->state->status.phase=4;job->state->status.network_error_code=-1000;}
 }
 starting_task.store(false);return nullptr;
}
}

extern "C" int wx_native_bind(uint64_t image_base,uint64_t task_owner){
 if(!image_base||!task_owner||image_base%4096||task_owner%8)return -1;
 if(base||owner)return -2;
 if(!exact_image(image_base))return -4;
 // Controller also validates readable memory and live process identity before
 // passing this pointer. Do not bind a different private class or subobject.
 if(*reinterpret_cast<const uintptr_t*>(task_owner)!=image_base+0x98c2c00)return -5;
 base=image_base;owner=reinterpret_cast<void*>(task_owner);
 api={reinterpret_cast<void(*)(void*,Task*)>(base+kStartWrapper),
  reinterpret_cast<void(*)(void*,const uint8_t*,uint32_t)>(base+kBufferWrite),
  reinterpret_cast<const uint8_t*(*)(void*,uint32_t)>(base+kBufferAt),
  reinterpret_cast<uint32_t(*)(void*)>(base+kBufferSize),
  reinterpret_cast<void(*)(void*)>(base+0x429f760),
  reinterpret_cast<void(*)(void*)>(base+0x429f7e0)};
 return 0;
}
extern "C" int wx_native_is_bound(void){return base&&owner?1:0;}
extern "C" int wx_native_current_account_present(void){
 if(!base||!owner||!exact_image(base))return -1;
 if(!*reinterpret_cast<const uintptr_t*>(base+0x9e66158))return 0;
 // This exact getter is used by 0x427995c to obtain the native current-account
 // string. It returns std::string via ARM64 x8, not a raw text pointer.
 auto get=reinterpret_cast<std::string(*)(void*)>(base+0x42b6254);
 return get(nullptr).empty()?0:1;
}
extern "C" int wx_native_validate_local_buffer(uint64_t handle){
 auto original=lookup(handle);
 if(!original||!base||!api.construct_buffer||!api.destroy_buffer)return -1;
 auto copy=std::make_shared<State>();
 {std::lock_guard<std::mutex> guard(original->lock);copy->payload=original->payload;}
 // Use a separate State: this diagnostic must not mark the original prepared
 // handle serialized/destroyed. No native owner or transport start is invoked.
 Task task(copy);void *buffer=nullptr;api.construct_buffer(&buffer);
 uint32_t auxiliary=0;uint8_t flag=255;int32_t error=0;
 bool encoded=serialize(&task,&buffer,&auxiliary,&flag,&error);
 uint32_t size=api.size(&buffer);const uint8_t *data=api.at(&buffer,0);
 bool equal=encoded&&flag==0&&data&&size==copy->payload.size()&&!std::memcmp(data,copy->payload.data(),size);
 api.destroy_buffer(&buffer);return equal?0:-9;
}
extern "C" int wx_native_probe_thread(void){
 uint32_t expected=0;if(!probe_thread_phase.compare_exchange_strong(expected,1))return -6;
 pthread_t thread;int rc=pthread_create(&thread,nullptr,[](void *)->void *{probe_thread_phase.store(2);return nullptr;},nullptr);
 if(rc){probe_thread_phase.store(0);return -7;}pthread_detach(thread);return 0;
}
extern "C" uint32_t wx_native_probe_thread_state(void){return probe_thread_phase.load();}
extern "C" int wx_native_probe_wrapper_with_transport_gate(uint64_t handle){
 if(!base||!owner)return -1;
 arm_debug_state64_t debug{};mach_msg_type_number_t count=ARM_DEBUG_STATE64_COUNT;
 mach_port_t thread=mach_thread_self();
 kern_return_t read=thread_get_state(thread,ARM_DEBUG_STATE64,reinterpret_cast<thread_state_t>(&debug),&count);
 mach_port_deallocate(mach_task_self(),thread);
 bool gated=false;
 if(read==KERN_SUCCESS&&count==ARM_DEBUG_STATE64_COUNT)
  for(unsigned n=0;n<16;n++)if(debug.__bvr[n]==base+0x4279ecc&&(debug.__bcr[n]&1))gated=true;
 if(!gated)return -14; // Never invoke the native wrapper without the actual gate.
 auto original=lookup(handle);if(!original)return -1;
 auto copy=std::make_shared<State>();
 {std::lock_guard<std::mutex> guard(original->lock);
  if(original->status.phase!=1)return -2;copy->payload=original->payload;}
 auto task=new Task(copy);task->table=task_table;task->callback.table=callback_table;task->callback.task=task;
 // This must stop before native transport, or stop at an earlier fault. There
 // is no Continue path in the controller. If inactive owner returns early,
 // no ownership transfer occurred; free this diagnostic task locally.
 if(!(reinterpret_cast<const volatile uint8_t*>(owner)[0x18]&1)){delete task;return -3;}
 api.start(owner,task);
 return -15; // Unexpected return, never interpreted as a send or acknowledgement.
}
extern "C" int wx_native_probe_worker_with_transport_gate(uint64_t handle){
 if(!lookup(handle)||!base||!owner)return -1;
 uint32_t expected=0;if(!probe_wrapper_phase.compare_exchange_strong(expected,1))return -6;
 auto argument=new uint64_t(handle);pthread_t thread;
 int rc=pthread_create(&thread,nullptr,[](void *raw)->void *{
  std::unique_ptr<uint64_t> h(static_cast<uint64_t*>(raw));
  // The new thread itself verifies its exact hardware gate before invoking
  // any native start. Absence of inherited debug registers refuses safely.
  probe_wrapper_result.store(wx_native_probe_wrapper_with_transport_gate(*h));
  probe_wrapper_phase.store(2);return nullptr;
 },argument);
 if(rc){delete argument;probe_wrapper_phase.store(0);return -7;}
 pthread_detach(thread);return 0;
}
extern "C" int wx_native_probe_worker_result(uint32_t *phase,int32_t *result){
 if(!phase||!result)return -1;*phase=probe_wrapper_phase.load();*result=probe_wrapper_result.load();return 0;
}
extern "C" uint64_t wx_native_prepare_text(const char *recipient,const char *text,uint64_t timestamp,uint64_t client_id){
 if(!recipient||!text||!timestamp||!client_id)return 0;
 size_t rn=strnlen(recipient,129),tn=strnlen(text,16385);
 if(rn>128||!tn||tn>16384)return 0;
 std::string r(recipient,rn),t(text,tn);if(!recipient_valid(r)||!utf8(t))return 0;
 auto state=std::make_shared<State>();state->payload=make_text(r,t,timestamp,client_id);
 auto handle=next_handle.fetch_add(1);state->status.handle=handle;
 state->status.client_message_id=client_id;state->status.phase=1;
 std::lock_guard<std::mutex> g(registry_lock);if(registry.size()>=128)return 0;
 registry.emplace(handle,state);return handle;
}
extern "C" int wx_native_submit(uint64_t h){
 return dispatch(h,1);
}
extern "C" int wx_native_enqueue(uint64_t h){
 auto state=lookup(h);if(!state||!base||!owner)return -1;
 bool expected=false;if(!starting_task.compare_exchange_strong(expected,true))return -6;
 {std::lock_guard<std::mutex> g(state->lock);
  if(state->status.phase!=1){starting_task.store(false);return -2;}
  state->status.phase=5;}
 auto job=new Job{h,state};pthread_t thread;
 int created=pthread_create(&thread,nullptr,dispatch_job,job);
 if(created){delete job;std::lock_guard<std::mutex> g(state->lock);state->status.phase=1;starting_task.store(false);return -7;}
 // The worker owns Job and is finite. No persistent thread is left inside the
 // client, and no library unload is permitted while a task is outstanding.
 pthread_detach(thread);return 0;
}
extern "C" int wx_native_get_status(uint64_t h,wx_native_status *out){
 auto state=lookup(h);if(!state||!out)return -1;std::lock_guard<std::mutex> g(state->lock);*out=state->status;return 0;
}
extern "C" int wx_native_cancel_prepared(uint64_t h){
 std::lock_guard<std::mutex> g(registry_lock);auto it=registry.find(h);if(it==registry.end())return -1;
 auto state=it->second;std::lock_guard<std::mutex> state_guard(state->lock);
 if(state->status.phase!=1 && !(state->status.phase==4&&!state->status.serialized&&!state->status.network_error_code))return -2;
 registry.erase(it);return 0;
}
extern "C" int wx_native_release_completed(uint64_t h){
 std::lock_guard<std::mutex> g(registry_lock);auto it=registry.find(h);if(it==registry.end())return -1;
 auto state=it->second;std::lock_guard<std::mutex> state_guard(state->lock);
 if(state->status.phase!=3||!state->status.destroyed)return -2;
 registry.erase(it);return 0;
}
