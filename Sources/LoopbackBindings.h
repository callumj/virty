#include <stdbool.h>
#include <stdint.h>

void VirtyEnableLoopbackBindings(void);
bool VirtyValidateLoopbackListener(uint16_t port);
bool VirtyAllowConsoleClient(int fd);
void VirtyRevokeConsoleClient(int fd);
bool VirtyValidateConsoleClient(int fd);
