#import "../Sources/KeychainCompat.h"

int VirtyDeriveBackupKey(const void *password, size_t passwordLength,
                        const void *salt, size_t saltLength, void *output);
