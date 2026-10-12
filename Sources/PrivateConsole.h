#import <Foundation/Foundation.h>
#import <Virtualization/Virtualization.h>

NS_ASSUME_NONNULL_BEGIN
// Only the manager can use the ephemeral console credential. It is never stored
// in VM metadata, arguments, environment, logs, or agent-facing responses.
@interface VirtyVNCConsole : NSObject
- (nullable instancetype)initWithVirtualMachine:(VZVirtualMachine *)machine error:(NSError **)error;
@property(readonly) uint16_t port;
- (BOOL)validateListener;
- (nullable NSData *)responseForChallenge:(NSData *)challenge;
- (void)stop;
@end
NS_ASSUME_NONNULL_END
