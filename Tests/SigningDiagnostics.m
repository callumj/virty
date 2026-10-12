#import <Foundation/Foundation.h>
#import "SigningProbe.h"

#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
CFArrayRef VirtySigningProbePartitions(CFStringRef service, OSStatus *status) {
    NSDictionary *query = @{
        (__bridge id)kSecClass: (__bridge id)kSecClassGenericPassword,
        (__bridge id)kSecAttrService: (__bridge id)service,
        (__bridge id)kSecAttrAccount: @"vm-login",
        (__bridge id)kSecReturnRef: @YES,
        (__bridge id)kSecUseAuthenticationUI: (__bridge id)kSecUseAuthenticationUIFail
    };
    CFTypeRef item = NULL;
    SecAccessRef access = NULL;
    CFArrayRef entries = NULL;
    NSMutableArray *partitions = [NSMutableArray array];
    *status = SecItemCopyMatching((__bridge CFDictionaryRef)query, &item);
    if (*status == errSecSuccess) *status = SecKeychainItemCopyAccess((SecKeychainItemRef)item, &access);
    if (*status == errSecSuccess) *status = SecAccessCopyACLList(access, &entries);
    if (*status == errSecSuccess) {
        for (id entry in (__bridge NSArray *)entries) {
            CFArrayRef authorizations = SecACLCopyAuthorizations((__bridge SecACLRef)entry);
            if (!authorizations) {
                *status = errSecParam;
                break;
            }
            if (CFArrayContainsValue(authorizations, CFRangeMake(0, CFArrayGetCount(authorizations)),
                                    kSecACLAuthorizationPartitionID)) {
                CFArrayRef applications = NULL;
                CFStringRef description = NULL;
                SecKeychainPromptSelector prompt;
                *status = SecACLCopyContents((__bridge SecACLRef)entry, &applications, &description, &prompt);
                if (*status == errSecSuccess && !description) *status = errSecParam;
                if (*status == errSecSuccess) {
                    NSData *data = [(__bridge NSString *)description dataUsingEncoding:NSUTF8StringEncoding];
                    id plist = [NSPropertyListSerialization propertyListWithData:data options:0 format:nil error:nil];
                    if ([plist isKindOfClass:NSDictionary.class])
                        [partitions addObjectsFromArray:plist[@"Partitions"] ?: @[]];
                    else
                        [partitions addObject:(__bridge NSString *)description];
                }
                if (applications) CFRelease(applications);
                if (description) CFRelease(description);
            }
            CFRelease(authorizations);
            if (*status != errSecSuccess) break;
        }
    }
    if (entries) CFRelease(entries);
    if (access) CFRelease(access);
    if (item) CFRelease(item);
    return CFBridgingRetain(partitions);
}
#pragma clang diagnostic pop
