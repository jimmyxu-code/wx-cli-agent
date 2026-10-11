// Offline real socket/ABI fixture; never loads or sends through WeChat.
#include "core.cpp"
#include "rpc.cpp"
#include <cassert>
#include <chrono>
#include <iostream>
#include <thread>

alignas(8) std::array<uint8_t,160> rpc_fake_owner{};
void forbidden_start(void *,Task *){std::abort();}
std::atomic<unsigned> fake_dispatches{0};
void fixture_completion(void *,Task *task){
 fake_dispatches++;task->transport_id=123;
 auto callback=task->callback_ptr;int32_t error_type=0,error_code=0;
 auto invoke=reinterpret_cast<int(*)(Callback*,Task**,const int32_t*,const int32_t*)>(callback->table[6]);
 assert(invoke(callback,&task,&error_type,&error_code)==1);
 auto destroy=reinterpret_cast<void(*)(Task*)>(task->table[1]);destroy(task);
}
int main(int argc,char **argv){
 if(argc!=2&&argc!=3)return 2;
 base=4096;owner=rpc_fake_owner.data();rpc_fake_owner[0x18]=1;api.start=forbidden_start;
 bool authorized=argc==3&&std::string(argv[2])=="authority-fixture";
 if(authorized){api.start=fixture_completion;
  assert(wx_native_rpc_start_with_authority(argv[1],"0101010101010101010101010101010101010101010101010101010101010101")==0);
 }else assert(wx_native_rpc_start(argv[1])==0);
 assert(wx_native_rpc_start(argv[1])==-3);
 std::cout<<"{\"offlineRpcFixtureReady\":true,\"realWeChatSend\":false}"<<std::endl;
 for(unsigned i=0;i<4500&&!rpc::server->finished.load();i++)std::this_thread::sleep_for(std::chrono::milliseconds(10));
 assert(wx_native_rpc_stop()==0);assert(registry.empty());
 assert(fake_dispatches.load()==unsigned(authorized));
 std::cout<<"{\"privateRpcStopped\":true,\"allPreparedHandlesCancelled\":true,\"realWeChatSend\":false}"<<std::endl;
}
