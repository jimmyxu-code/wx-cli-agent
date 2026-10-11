// Offline ABI/lifetime fixtures. These never load or send through WeChat.
#include "core.cpp"
#include <cassert>
#include <iostream>
#include <thread>
#include <chrono>

namespace fixture {
struct Buffer { Bytes bytes; };
std::atomic<unsigned> calls{0};
Bytes reply{0x0a,0x02,0x08,0x00};
int32_t error_type=0,error_code=0;
bool response_enabled=true;
alignas(8) std::array<uint8_t,160> fake_owner{};
void write(void *b,const uint8_t *p,uint32_t n){
 auto &bytes=static_cast<Buffer*>(b)->bytes;bytes.insert(bytes.end(),p,p+n);
}
const uint8_t *at(void *b,uint32_t offset){
 auto &bytes=static_cast<Buffer*>(b)->bytes;
 return offset<bytes.size()?bytes.data()+offset:nullptr;
}
uint32_t size(void *b){return uint32_t(static_cast<Buffer*>(b)->bytes.size());}
void start(void *o,Task *task){
 assert(o==fake_owner.data());calls++;
 assert(task->command_id==522&&task->channel==3);
 assert(task->cgi=="/cgi-bin/micromsg-bin/newsendmsg");
 assert(task->callback_ptr==&task->callback);
 Buffer request;uint32_t aux=0;uint8_t flag=255;int32_t err=0;
 auto serializer=reinterpret_cast<bool(*)(Task*,void*,uint32_t*,uint8_t*,int32_t*)>(task->table[2]);
 assert(serializer(task,&request,&aux,&flag,&err)&&flag==0);
 assert(request.bytes==task->state->payload);
 task->transport_id=71;
 if(response_enabled){
  Buffer response{reply};auto parser=reinterpret_cast<bool(*)(Task*,void*)>(task->table[3]);
  assert(parser(task,&response));
 }
 auto cb=task->callback_ptr;
 auto invoke=reinterpret_cast<int(*)(Callback*,Task**,const int32_t*,const int32_t*)>(cb->table[6]);
 Task *different=nullptr;assert(invoke(cb,&different,&error_type,&error_code)==0);
 assert(invoke(cb,&task,&error_type,&error_code)==1);
 // Mirrors the observed native completion tail. No task access follows D0.
 auto deleting_destructor=reinterpret_cast<void(*)(Task*)>(task->table[1]);
 deleting_destructor(task);
}
struct Field {unsigned number,wire;uint64_t value;Bytes bytes;};
// Separate fixture decoder, deliberately independent of the adapter decoder.
std::vector<Field> decode(const Bytes &bytes){
 std::vector<Field> result;size_t position=0;
 auto integer=[&](){uint64_t n=0;unsigned shift=0;uint8_t c;
  do{assert(position<bytes.size()&&shift<64);c=bytes[position++];n|=uint64_t(c&127)<<shift;shift+=7;}while(c&128);
  return n;};
 while(position<bytes.size()){
  uint64_t tag=integer();Field field{unsigned(tag>>3),unsigned(tag&7),0,{}};
  assert(field.number);
  if(field.wire==0)field.value=integer();
  else{assert(field.wire==2);auto len=integer();assert(len<=bytes.size()-position);
   field.bytes.assign(bytes.begin()+position,bytes.begin()+position+len);position+=len;}
  result.push_back(std::move(field));
 }
 return result;
}
std::string string(const Bytes &b){return std::string(b.begin(),b.end());}
uint64_t prepared(){return wx_native_prepare_text("filehelper",u8"结构化验证 🧪",1791646458,123456789);}
void completion_case(const Bytes &response,int32_t type,int32_t code,bool present,int32_t ret,bool enabled=true){
 reply=response;error_type=type;error_code=code;response_enabled=enabled;
 auto h=prepared();assert(h);wx_native_status before{};assert(!wx_native_get_status(h,&before));
 assert(before.phase==1&&!before.serialized&&!before.parsed);
 assert(!wx_native_submit(h));wx_native_status after{};assert(!wx_native_get_status(h,&after));
 assert(after.phase==3&&after.destroyed==1&&after.serialized==1&&after.transport_id==71);
 assert(after.network_error_type==type&&after.network_error_code==code);
 assert(after.parsed==unsigned(enabled)&&after.response_ret_present==unsigned(present));
 if(present)assert(after.response_ret==ret);
 assert(wx_native_submit(h)==-2&&wx_native_cancel_prepared(h)==-2);
 assert(!wx_native_release_completed(h)&&wx_native_get_status(h,&after)==-1);
}
}

int main(){
 using namespace fixture;
 assert(sizeof(wx_native_status)==56&&offsetof(wx_native_status,destroyed)==48);
 assert(wx_native_bind(4096,8)==-4); // This fixture is not the approved client.
 assert(!wx_native_prepare_text(nullptr,"x",1,1));
 assert(!wx_native_prepare_text("invalid recipient","x",1,1));
 assert(!wx_native_prepare_text("filehelper","",1,1));
 assert(!wx_native_prepare_text("filehelper","x",0,1));
 assert(!wx_native_prepare_text("filehelper","x",1,0));
 assert(!wx_native_prepare_text("filehelper","\xc0\x80",1,1));
 assert(!wx_native_prepare_text("filehelper","\xed\xa0\x80",1,1));
 assert(!wx_native_prepare_text("filehelper","\xf4\x90\x80\x80",1,1));
 assert(!wx_native_prepare_text("filehelper",std::string(16385,'x').c_str(),1,1));
 auto h=prepared();assert(h&&calls==0);
 auto encoded=lookup(h)->payload;auto outer=decode(encoded);assert(outer.size()==2);
 assert(outer[0].number==1&&outer[0].value==1&&outer[1].number==2);
 auto body=decode(outer[1].bytes);assert(body.size()==6);
 for(unsigned i=0;i<6;i++)assert(body[i].number==i+1);
 auto recipient=decode(body[0].bytes);assert(recipient.size()==1&&recipient[0].number==1);
 assert(string(recipient[0].bytes)=="filehelper"&&string(body[1].bytes)==u8"结构化验证 🧪");
 assert(body[2].value==1&&body[3].value==1791646458&&body[4].value==123456789);
 assert(string(body[5].bytes)=="<msgsource><alnode><fr>1</fr></alnode></msgsource>");
 assert(wx_native_submit(h)==-1&&calls==0&&!wx_native_cancel_prepared(h));
 // Registry limits and repeated cancellation exercise lifetime ownership.
 std::vector<uint64_t> handles;
 for(unsigned i=0;i<128;i++){h=prepared();assert(h);handles.push_back(h);}
 assert(!prepared());for(auto handle:handles)assert(!wx_native_cancel_prepared(handle));
 for(unsigned i=0;i<4096;i++){h=prepared();assert(h&&!wx_native_cancel_prepared(h));}
 assert(registry.empty()&&calls==0);
 base=4096;owner=fake_owner.data();api={start,fixture::write,at,size};
 h=prepared();assert(h&&wx_native_probe_wrapper_with_transport_gate(h)==-14&&calls==0);
 assert(!wx_native_probe_worker_with_transport_gate(h));
 for(unsigned i=0;i<5000&&probe_wrapper_phase.load()!=2;i++)std::this_thread::sleep_for(std::chrono::milliseconds(1));
 assert(probe_wrapper_phase.load()==2&&probe_wrapper_result.load()==-14&&calls==0);
 assert(!wx_native_cancel_prepared(h)); // No actual current-thread hardware gate.
 fake_owner[0x18]=0;h=prepared();assert(h&&wx_native_submit(h)==-3&&calls==0);
 assert(!wx_native_cancel_prepared(h));
 fake_owner[0x18]=1;
 completion_case({0x0a,0x02,0x08,0x00},0,0,true,0);
 completion_case({0x0a,0x0b,0x08,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0xff,0x01},0,0,true,-1);
 completion_case({},4,-13,false,0,false);
 completion_case({},0,0,false,0);
 completion_case({0x0a,0x03,0x08,0x00},0,0,false,0);
 completion_case({0x0a,0x04,0x08,0x00,0x08,0x00},0,0,false,0);
 completion_case({0x0a,0x02,0x08,0x00,0x0a,0x02,0x08,0x00},0,0,false,0);
 completion_case({0x0a,0x01,0x00},0,0,false,0);
 completion_case(Bytes(65537,0),0,0,false,0);
 reply={0x0a,0x02,0x08,0x00};error_type=0;error_code=0;response_enabled=true;
 h=prepared();assert(h&&!wx_native_enqueue(h));
 for(unsigned i=0;i<5000&&starting_task.load();i++)std::this_thread::sleep_for(std::chrono::milliseconds(1));
 assert(!starting_task.load());wx_native_status asynchronous{};
 assert(!wx_native_get_status(h,&asynchronous));
 assert(asynchronous.phase==3&&asynchronous.destroyed&&asynchronous.response_ret_present);
 assert(wx_native_enqueue(h)==-2&&wx_native_submit(h)==-2);
 assert(!wx_native_release_completed(h));
 // Concurrent prepare/get/cancel verifies registry and State mutex order.
 std::vector<std::thread> threads;
 for(unsigned i=0;i<8;i++)threads.emplace_back([](){
  for(unsigned j=0;j<256;j++){auto handle=prepared();assert(handle);wx_native_status status{};
   assert(!wx_native_get_status(handle,&status)&&status.phase==1);
   assert(!wx_native_cancel_prepared(handle));}
 });
 for(auto &thread:threads)thread.join();assert(registry.empty());
 std::cout<<"{\"offlineAbiFixture\":true,\"lifetimeChecks\":true,\"wireRoundTrip\":true,\"realWeChatSend\":false}"<<std::endl;
}
