#import <Foundation/Foundation.h>
#import <Security/Security.h>

// Diagnostic only. Passwords arrive on stdin; responses contain OSStatus only.
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
int main(void) {
    @autoreleasepool {
        NSDictionary *request = [NSJSONSerialization JSONObjectWithData:
            [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile] options:0 error:nil];
        OSStatus status = SecKeychainSetUserInteractionAllowed(false);
        SecKeychainRef keychain = NULL;
        NSString *action = request[@"action"];
        NSString *path = request[@"keychain"];
        NSData *password = [request[@"password"] dataUsingEncoding:NSUTF8StringEncoding];
        if (!request || !action || !path || !password) status = errSecParam;
        if (status == errSecSuccess && [action isEqualToString:@"create"]) {
            CFArrayRef original = NULL;
            status = SecKeychainCopySearchList(&original);
            if (status == errSecSuccess) {
                status = SecKeychainCreate(path.fileSystemRepresentation, (UInt32)password.length,
                    password.bytes, false, NULL, &keychain);
                OSStatus restored = SecKeychainSetSearchList(original);
                if (status == errSecSuccess) status = restored;
            }
            if (original) CFRelease(original);
        } else if (status == errSecSuccess) {
            status = SecKeychainOpen(path.fileSystemRepresentation, &keychain);
        }
        if (status == errSecSuccess && password)
            status = SecKeychainUnlock(keychain, (UInt32)password.length, password.bytes, true);
        if (status == errSecSuccess && [action isEqualToString:@"import"]) {
            SecTrustedApplicationRef codesign = NULL;
            SecAccessRef access = NULL;
            status = SecTrustedApplicationCreateFromPath("/usr/bin/codesign", &codesign);
            if (status == errSecSuccess)
                status = SecAccessCreate(CFSTR("Virty local signing key"),
                    (__bridge CFArrayRef)@[(__bridge id)codesign], &access);
            if (status == errSecSuccess) {
                NSData *data = [NSData dataWithContentsOfFile:request[@"pkcs12"]];
                SecExternalFormat format = kSecFormatPKCS12;
                SecExternalItemType type = kSecItemTypeAggregate;
                SecItemImportExportKeyParameters parameters = {0};
                parameters.passphrase = (__bridge CFStringRef)request[@"passphrase"];
                parameters.accessRef = access;
                parameters.keyAttributes = (__bridge CFArrayRef)@[(__bridge id)kSecAttrIsPermanent,
                    (__bridge id)kSecAttrIsSensitive];
                status = data ? SecItemImport((__bridge CFDataRef)data, NULL, &format, &type,
                    0, &parameters, keychain, NULL) : errSecParam;
            }
            if (access) CFRelease(access);
            if (codesign) CFRelease(codesign);
        } else if (status == errSecSuccess && [action isEqualToString:@"delete"]) {
            status = path ? SecKeychainDelete(keychain) : errSecParam;
        } else if (status == errSecSuccess && ![action isEqualToString:@"create"]) {
            status = errSecParam;
        }
        if (keychain) CFRelease(keychain);
        NSData *result = [NSJSONSerialization dataWithJSONObject:@{@"status": @(status)} options:0 error:nil];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:result];
        return status == errSecSuccess ? 0 : 1;
    }
}
#pragma clang diagnostic pop
