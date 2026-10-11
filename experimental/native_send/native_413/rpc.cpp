#include "rpc.h"
#include "core.h"
#include <atomic>
#include <array>
#include <cerrno>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <poll.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include <limits.h>

namespace rpc {
using Bytes=std::vector<uint8_t>;
struct Server {
 std::atomic<int> fd{-1};std::string path;pthread_t thread{};
 dev_t socket_device=0;ino_t socket_inode=0;
 std::atomic<bool> stop{false},finished{false};
 std::atomic<uint64_t> approved_handle{0};
 std::array<uint8_t,32> authority{};
 std::atomic<bool> authority_ready{false},armed_once{false};
};
std::mutex server_lock;
std::shared_ptr<Server> server;
void remove_own_socket(const std::shared_ptr<Server>&state){
 struct stat st{};
 if(!lstat(state->path.c_str(),&st)&&S_ISSOCK(st.st_mode)&&st.st_uid==geteuid()
    &&st.st_dev==state->socket_device&&st.st_ino==state->socket_inode)unlink(state->path.c_str());
}
constexpr size_t kMaxBody=16664;
uint32_t u32(const uint8_t *p){return uint32_t(p[0])|uint32_t(p[1])<<8|uint32_t(p[2])<<16|uint32_t(p[3])<<24;}
uint64_t u64(const uint8_t *p){return uint64_t(u32(p))|uint64_t(u32(p+4))<<32;}
void put32(Bytes &out,uint32_t n){for(unsigned i=0;i<4;i++)out.push_back(uint8_t(n>>(8*i)));}
void put64(Bytes &out,uint64_t n){for(unsigned i=0;i<8;i++)out.push_back(uint8_t(n>>(8*i)));}
bool receive(int fd,uint8_t *p,size_t n){
 while(n){ssize_t got=recv(fd,p,n,0);if(got<0&&errno==EINTR)continue;if(got<=0)return false;p+=got;n-=size_t(got);}return true;
}
bool transmit(int fd,const uint8_t *p,size_t n){
 while(n){ssize_t sent=send(fd,p,n,0);if(sent<0&&errno==EINTR)continue;if(sent<=0)return false;p+=sent;n-=size_t(sent);}return true;
}
int arm_handle(const std::shared_ptr<Server>&state,uint64_t handle){
 if(state->stop.load())return -1;
 wx_native_status status{};if(wx_native_get_status(handle,&status)||status.phase!=1)return -2;
 bool expected=false;if(!state->armed_once.compare_exchange_strong(expected,true))return -8;
 state->approved_handle.store(handle);return 0;
}
int32_t command(const std::shared_ptr<Server>&state,uint32_t op,const Bytes &body,Bytes &reply){
 if(op==1){
  if(body.size()<24)return -10;
  uint64_t when=u64(body.data()),id=u64(body.data()+8);
  uint32_t rn=u32(body.data()+16),tn=u32(body.data()+20);
  if(!rn||rn>128||!tn||tn>16384||body.size()!=24+size_t(rn)+tn)return -10;
  std::string recipient(body.begin()+24,body.begin()+24+rn),text(body.begin()+24+rn,body.end());
  if(recipient.find('\0')!=std::string::npos||text.find('\0')!=std::string::npos)return -10;
  uint64_t handle=wx_native_prepare_text(recipient.c_str(),text.c_str(),when,id);
  if(!handle)return -11;put64(reply,handle);return 0;
 }
 if(op>=2&&op<=5){
  if(body.size()!=8)return -10;uint64_t handle=u64(body.data());
  if(op==2){
   uint64_t expected=handle;
   if(!handle||!state->approved_handle.compare_exchange_strong(expected,0))return -8;
   return wx_native_enqueue(handle); // Approved handle consumed even if result is uncertain.
  }
  if(op==3){
   wx_native_status status{};int rc=wx_native_get_status(handle,&status);if(rc)return rc;
   put64(reply,status.handle);put64(reply,status.client_message_id);put32(reply,status.transport_id);
   put32(reply,status.phase);put32(reply,uint32_t(status.network_error_type));put32(reply,uint32_t(status.network_error_code));
   put32(reply,uint32_t(status.response_ret));put32(reply,status.response_ret_present);
   put32(reply,status.serialized);put32(reply,status.parsed);put32(reply,status.destroyed);return 0;
  }
  if(op==4)return wx_native_cancel_prepared(handle);
  return wx_native_release_completed(handle);
 }
 if(op==6){if(!body.empty())return -10;put32(reply,1);put32(reply,269602);
  put32(reply,wx_native_is_bound());put32(reply,state->approved_handle.load()?1:0);return 0;}
 if(op==7){if(!body.empty())return -10;state->stop.store(true);return 0;}
 if(op==8){
  if(body.size()!=40||!state->authority_ready.load())return -8;
  uint8_t mismatch=0;for(size_t i=0;i<32;i++)mismatch|=body[8+i]^state->authority[i];
  if(mismatch)return -8;return arm_handle(state,u64(body.data()));
 }
 if(op==9){if(body.size()!=8)return -10;return wx_native_validate_local_buffer(u64(body.data()));}
 if(op==10){if(!body.empty())return -10;return wx_native_probe_thread();}
 if(op==11){if(!body.empty())return -10;put32(reply,wx_native_probe_thread_state());return 0;}
 if(op==12||op==13){
  if(body.size()!=40||!state->authority_ready.load())return -8;
  uint8_t mismatch=0;for(size_t i=0;i<32;i++)mismatch|=body[8+i]^state->authority[i];
  if(mismatch)return -8;
  return op==12?wx_native_probe_wrapper_with_transport_gate(u64(body.data())):
    wx_native_probe_worker_with_transport_gate(u64(body.data()));
 }
 if(op==14){if(!body.empty())return -10;uint32_t phase=0;int32_t result=0;
  int rc=wx_native_probe_worker_result(&phase,&result);if(rc)return rc;
  put32(reply,phase);put32(reply,uint32_t(result));return 0;}
 if(op==15){if(!body.empty())return -10;int present=wx_native_current_account_present();
  if(present<0)return present;put32(reply,uint32_t(present));return 0;}
 return -12;
}
void connection(const std::shared_ptr<Server>&state,int fd){
 uid_t uid;gid_t gid;if(getpeereid(fd,&uid,&gid)||uid!=geteuid())return;
 timeval timeout{2,0};setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
 setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout));
 int yes=1;setsockopt(fd,SOL_SOCKET,SO_NOSIGPIPE,&yes,sizeof(yes));
 uint8_t header[12];if(!receive(fd,header,sizeof(header))||std::memcmp(header,"WXN1",4))return;
 uint32_t op=u32(header+4),size=u32(header+8);if(size>kMaxBody)return;
 Bytes body(size),reply;if(size&&!receive(fd,body.data(),size))return;
 int32_t result;
 try{result=command(state,op,body,reply);}catch(...){result=-13;}
 Bytes response{'W','X','R','1'};put32(response,uint32_t(result));put32(response,uint32_t(reply.size()));
 response.insert(response.end(),reply.begin(),reply.end());transmit(fd,response.data(),response.size());
}
void *serve(void *raw){
 std::unique_ptr<std::shared_ptr<Server>> holder(static_cast<std::shared_ptr<Server>*>(raw));auto state=*holder;
 while(!state->stop.load()){
  pollfd poller{state->fd.load(),POLLIN,0};int ready=poll(&poller,1,200);
  if(ready<0&&errno==EINTR)continue;if(ready<0)break;
  if(!ready)continue;if(!(poller.revents&POLLIN))break;
  int fd=accept(state->fd.load(),nullptr,nullptr);if(fd<0)continue;
  connection(state,fd);close(fd);
 }
 int fd=state->fd.exchange(-1);if(fd>=0)close(fd);
 remove_own_socket(state);state->finished.store(true);return nullptr;
}
bool private_path(const std::string &path){
 if(path.empty()||path.front()!='/'||path.size()>=sizeof(sockaddr_un::sun_path))return false;
 size_t slash=path.find_last_of('/');if(slash==0||slash+1==path.size())return false;
 std::string parent=path.substr(0,slash);char canonical[PATH_MAX];struct stat st{};
 if(!realpath(parent.c_str(),canonical)||parent!=canonical||lstat(parent.c_str(),&st))return false;
 if(!S_ISDIR(st.st_mode)||st.st_uid!=geteuid()||(st.st_mode&0777)!=0700)return false;
 if(!lstat(path.c_str(),&st)||errno!=ENOENT)return false;
 return true;
}
}
extern "C" int wx_native_rpc_start(const char *path){
 if(!path||!wx_native_is_bound())return -1;
 size_t n=strnlen(path,sizeof(sockaddr_un::sun_path));if(n>=sizeof(sockaddr_un::sun_path))return -2;
 std::lock_guard<std::mutex> lock(rpc::server_lock);if(rpc::server)return -3;
 std::string name(path,n);if(!rpc::private_path(name))return -4;
 auto state=std::make_shared<rpc::Server>();state->path=name;state->fd=socket(AF_UNIX,SOCK_STREAM,0);
 if(state->fd<0)return -5;
 sockaddr_un addr{};addr.sun_family=AF_UNIX;addr.sun_len=sizeof(addr);std::memcpy(addr.sun_path,name.c_str(),n+1);
 if(bind(state->fd,reinterpret_cast<sockaddr*>(&addr),sizeof(addr))){close(state->fd);return -6;}
 struct stat entry{};
 if(lstat(name.c_str(),&entry)||!S_ISSOCK(entry.st_mode)||entry.st_uid!=geteuid()){close(state->fd);return -6;}
 state->socket_device=entry.st_dev;state->socket_inode=entry.st_ino;
 if(chmod(name.c_str(),0600)||listen(state->fd,8)){
  close(state->fd);rpc::remove_own_socket(state);return -6;
 }
 auto holder=new std::shared_ptr<rpc::Server>(state);
 if(pthread_create(&state->thread,nullptr,rpc::serve,holder)){delete holder;close(state->fd);rpc::remove_own_socket(state);return -7;}
 rpc::server=state;return 0;
}
extern "C" int wx_native_rpc_arm_one(uint64_t handle){
 std::lock_guard<std::mutex> lock(rpc::server_lock);auto state=rpc::server;
 if(!state||state->stop.load()||state->approved_handle.load())return -1;
 return rpc::arm_handle(state,handle);
}
extern "C" int wx_native_rpc_start_with_authority(const char *path,const char *authority_hex){
 if(!authority_hex||strnlen(authority_hex,65)!=64)return -8;
 std::array<uint8_t,32> authority{};
 auto digit=[](char c)->int{return c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:-1;};
 for(size_t i=0;i<32;i++){int high=digit(authority_hex[i*2]),low=digit(authority_hex[i*2+1]);
  if(high<0||low<0)return -8;authority[i]=uint8_t((high<<4)|low);}
 int result=wx_native_rpc_start(path);if(result)return result;
 std::lock_guard<std::mutex> lock(rpc::server_lock);
 rpc::server->authority=authority;rpc::server->authority_ready.store(true);return 0;
}
extern "C" int wx_native_rpc_stop(void){
 std::lock_guard<std::mutex> lock(rpc::server_lock);auto state=rpc::server;if(!state)return -1;
 state->stop.store(true);int fd=state->fd.load();if(fd>=0)shutdown(fd,SHUT_RDWR);
 pthread_join(state->thread,nullptr);fd=state->fd.exchange(-1);if(fd>=0)close(fd);
 rpc::remove_own_socket(state);rpc::server.reset();return 0;
}
