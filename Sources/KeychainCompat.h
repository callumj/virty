#import <Security/Security.h>

// Required only for the explicitly selected file-based login-Keychain compatibility mode.
CFStringRef VirtyLegacyAuthenticationUIFail(void);
OSStatus VirtyDisableLegacyAuthenticationUI(void);
