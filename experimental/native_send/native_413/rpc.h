#pragma once
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
// Private, same-UID Unix socket. Starts unarmed; RPC cannot grant send authority.
int wx_native_rpc_start(const char *path);
// Private controller authority, never exposed as an ordinary JSON command.
int wx_native_rpc_start_with_authority(const char *path,const char *authority_hex);
// Controller-only: one exact already-approved prepared handle, consumed once.
int wx_native_rpc_arm_one(uint64_t handle);
int wx_native_rpc_stop(void);
#ifdef __cplusplus
}
#endif
