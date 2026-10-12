#import "PrivateConsole.h"
#import "LoopbackBindings.h"
#import <Security/Security.h>
#import <CommonCrypto/CommonCryptor.h>
#import <sys/socket.h>
#import <netinet/in.h>
#import <unistd.h>
#import <errno.h>

static BOOL verifyBindRestriction(void) {
    int fd = socket(AF_INET6, SOCK_STREAM, 0);
    if (fd < 0) return NO;
    struct sockaddr_in6 address = {0};
    address.sin6_len = sizeof(address); address.sin6_family = AF_INET6;
    socklen_t size = sizeof(address);
    BOOL valid = bind(fd, (const struct sockaddr *)&address, size) == 0 &&
        getsockname(fd, (struct sockaddr *)&address, &size) == 0 && IN6_IS_ADDR_LOOPBACK(&address.sin6_addr);
    if (valid) {
        valid = listen(fd, 1) == 0;
        int client = socket(AF_INET6, SOCK_STREAM, 0);
        valid = valid && client >= 0 && connect(client, (const struct sockaddr *)&address, size) == 0;
        if (valid) {
            int rejected = accept(fd, NULL, NULL);
            valid = rejected == -1 && errno == EAGAIN;
            if (rejected >= 0) close(rejected);
        }
        if (client >= 0) close(client);
    }
    close(fd);
    return valid;
}
static void clearBytes(uint8_t *buffer, size_t length) {
    volatile uint8_t *bytes = buffer;
    while (length--) *bytes++ = 0;
}

// Isolate private selectors and their exact ABI here. No private framebuffer
// screenshot selector or host display API is used.
@interface VirtyPrivateVNCServer : NSObject
- (id)initWithPort:(uint16_t)port queue:(dispatch_queue_t)queue securityConfiguration:(id)security;
- (uint16_t)port;
- (void)setVirtualMachine:(VZVirtualMachine *)machine;
- (void)start;
- (void)stop;
@end
@interface VirtyPrivateVNCSecurity : NSObject
- (id)initWithPassword:(NSString *)password;
@end

@implementation VirtyVNCConsole {
    VirtyPrivateVNCServer *_server;
    NSData *_key;
}
- (instancetype)initWithVirtualMachine:(VZVirtualMachine *)machine error:(NSError **)error {
    self = [super init];
    if (!self) return nil;
    Class serverClass = NSClassFromString(@"_VZVNCServer");
    Class securityClass = NSClassFromString(@"_VZVNCAuthenticationSecurityConfiguration");
    if (!serverClass || !securityClass ||
        ![serverClass instancesRespondToSelector:@selector(initWithPort:queue:securityConfiguration:)] ||
        ![serverClass instancesRespondToSelector:@selector(setVirtualMachine:)] ||
        ![serverClass instancesRespondToSelector:@selector(start)] ||
        ![serverClass instancesRespondToSelector:@selector(stop)] ||
        ![serverClass instancesRespondToSelector:@selector(port)] ||
        ![securityClass instancesRespondToSelector:@selector(initWithPassword:)]) {
        if (error) *error = [NSError errorWithDomain:@"VirtyConsole" code:1 userInfo:@{NSLocalizedDescriptionKey:@"Private VM console capability unavailable on this host."}];
        return nil;
    }
    uint8_t random[8], password[8], key[8];
    if (SecRandomCopyBytes(kSecRandomDefault, sizeof(random), random) != errSecSuccess) {
        if (error) *error = [NSError errorWithDomain:@"VirtyConsole" code:2 userInfo:@{NSLocalizedDescriptionKey:@"Cannot generate a private console credential."}];
        return nil;
    }
    for (size_t i = 0; i < sizeof(password); i++) {
        password[i] = 33 + random[i] % 94;
        uint8_t value = password[i], reversed = 0;
        for (int bit = 0; bit < 8; bit++) { reversed = (reversed << 1) | (value & 1); value >>= 1; }
        key[i] = reversed;
    }
    NSString *text = [[NSString alloc] initWithBytes:password length:sizeof(password) encoding:NSASCIIStringEncoding];
    _key = [NSData dataWithBytes:key length:sizeof(key)];
    id security = [(VirtyPrivateVNCSecurity *)[securityClass alloc] initWithPassword:text];
    VirtyEnableLoopbackBindings();
    if (!verifyBindRestriction()) {
        if (error) *error = [NSError errorWithDomain:@"VirtyConsole" code:3 userInfo:@{NSLocalizedDescriptionKey:@"Private console bind restriction is unavailable; refusing to start its listener."}];
        clearBytes(random, sizeof(random)); clearBytes(password, sizeof(password)); clearBytes(key, sizeof(key));
        return nil;
    }
    // Its accessor callbacks must run on the VM's queue. A separate queue can
    // trip an Apple assertion during client connect/disconnect transitions.
    _server = [(VirtyPrivateVNCServer *)[serverClass alloc] initWithPort:0 queue:machine.queue securityConfiguration:security];
    [_server setVirtualMachine:machine];
    [_server start];
    clearBytes(random, sizeof(random)); clearBytes(password, sizeof(password)); clearBytes(key, sizeof(key));
    return self;
}
- (uint16_t)port { return _server.port; }
- (BOOL)validateListener { return VirtyValidateLoopbackListener(_server.port); }
- (NSData *)responseForChallenge:(NSData *)challenge {
    if (challenge.length != 16) return nil;
    uint8_t response[16]; size_t written = 0;
    CCCryptorStatus status = CCCrypt(kCCEncrypt, kCCAlgorithmDES, kCCOptionECBMode,
        _key.bytes, _key.length, NULL, challenge.bytes, challenge.length, response, sizeof(response), &written);
    if (status != kCCSuccess || written != sizeof(response)) return nil;
    return [NSData dataWithBytes:response length:written];
}
- (void)stop { [_server stop]; }
- (void)dealloc { [_server stop]; }
@end
