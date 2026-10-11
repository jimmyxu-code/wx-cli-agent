#pragma once
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
// This API runs only inside the exact owned temporary client. The controller
// validates binary identity, signatures, PID, module UUID and explicit proposal.
typedef struct wx_native_status {
 uint64_t handle;
 uint64_t client_message_id;
 uint32_t transport_id;
 uint32_t phase; // 1 prepared, 2 submitted, 3 callback, 4 rejected locally, 5 queued
 int32_t network_error_type;
 int32_t network_error_code;
 int32_t response_ret;
 uint32_t response_ret_present;
 uint32_t serialized;
 uint32_t parsed;
 uint32_t destroyed;
} wx_native_status;
int wx_native_bind(uint64_t image_base, uint64_t owner);
int wx_native_is_bound(void);
// Only a necessary readiness signal: a native current-account name exists.
// Returns its presence, never the actual account name or authentication data.
int wx_native_current_account_present(void);
// Local ABI diagnostics: own payload/buffer only; never call native start.
int wx_native_validate_local_buffer(uint64_t handle);
int wx_native_probe_thread(void);
uint32_t wx_native_probe_thread_state(void);
// Debug-only: refuses unless this very thread has an enabled hardware execute
// breakpoint at the exact native transport-call instruction. Never remove or
// continue that gate; the controller kills only its owned temporary process.
int wx_native_probe_wrapper_with_transport_gate(uint64_t handle);
int wx_native_probe_worker_with_transport_gate(uint64_t handle);
int wx_native_probe_worker_result(uint32_t *phase, int32_t *result);
uint64_t wx_native_prepare_text(const char *recipient, const char *text,
                              uint64_t timestamp, uint64_t client_message_id);
// Only the human-authorized controller may call submit. Preparation never
// invokes the native network stack. No GUI, original process, or public server.
int wx_native_submit(uint64_t handle);
// Bounded asynchronous dispatch avoids running the native wrapper inside a
// debugger expression while other client threads are paused. Only one worker
// may be starting a task at once; a queued handle cannot be submitted twice.
int wx_native_enqueue(uint64_t handle);
int wx_native_get_status(uint64_t handle, wx_native_status *out);
int wx_native_cancel_prepared(uint64_t handle);
int wx_native_release_completed(uint64_t handle);
#ifdef __cplusplus
}
#endif
