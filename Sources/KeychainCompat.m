#import "KeychainCompat.h"

#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
CFStringRef VirtyLegacyAuthenticationUIFail(void) { return kSecUseAuthenticationUIFail; }
OSStatus VirtyDisableLegacyAuthenticationUI(void) { return SecKeychainSetUserInteractionAllowed(false); }
#pragma clang diagnostic pop
