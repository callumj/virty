#import "CryptoCompat.h"
#import <CommonCrypto/CommonKeyDerivation.h>

int VirtyDeriveBackupKey(const void *password, size_t passwordLength,
                        const void *salt, size_t saltLength, void *output) {
    return CCKeyDerivationPBKDF(kCCPBKDF2, password, passwordLength, salt, saltLength,
                               kCCPRFHmacAlgSHA256, 600000, output, 32);
}
