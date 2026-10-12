#include "../Sources/LoopbackBindings.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <netinet/in.h>

int main(void) {
    VirtyEnableLoopbackBindings();
    for (int familyIndex = 0; familyIndex < 2; familyIndex++) {
        int family = familyIndex == 0 ? AF_INET : AF_INET6;
        int fd = socket(family, SOCK_STREAM, 0);
        assert(fd >= 0);
        struct sockaddr_storage storage = {0};
        socklen_t size;
        if (family == AF_INET) {
            struct sockaddr_in *address = (struct sockaddr_in *)&storage;
            address->sin_len = sizeof(*address); address->sin_family = AF_INET;
            size = sizeof(*address);
        } else {
            struct sockaddr_in6 *address = (struct sockaddr_in6 *)&storage;
            address->sin6_len = sizeof(*address); address->sin6_family = AF_INET6;
            size = sizeof(*address);
        }
        assert(bind(fd, (const struct sockaddr *)&storage, size) == 0);
        assert(getsockname(fd, (struct sockaddr *)&storage, &size) == 0);
        uint16_t port;
        if (family == AF_INET) {
            struct sockaddr_in *address = (struct sockaddr_in *)&storage;
            assert(address->sin_addr.s_addr == htonl(INADDR_LOOPBACK));
            port = ntohs(address->sin_port);
        } else {
            struct sockaddr_in6 *address = (struct sockaddr_in6 *)&storage;
            assert(IN6_IS_ADDR_LOOPBACK(&address->sin6_addr));
            port = ntohs(address->sin6_port);
        }
        assert(!VirtyValidateLoopbackListener(port)); // Bound is not listening.
        assert(listen(fd, 1) == 0);
        assert(VirtyValidateLoopbackListener(port));
        close(fd);
        assert(!VirtyValidateLoopbackListener(port));
    }
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    assert(fd >= 0);
    struct sockaddr_in external = {0};
    external.sin_len = sizeof(external); external.sin_family = AF_INET;
    external.sin_addr.s_addr = htonl(0xcb007101); // Documentation-only 203.0.113.1.
    errno = 0;
    assert(bind(fd, (const struct sockaddr *)&external, sizeof(external)) == -1 && errno == EACCES);
    close(fd);
    assert(!VirtyValidateLoopbackListener(0));

    int listener = socket(AF_INET6, SOCK_STREAM, 0);
    struct sockaddr_in6 local = {0};
    local.sin6_len = sizeof(local); local.sin6_family = AF_INET6; local.sin6_addr = in6addr_loopback;
    socklen_t size = sizeof(local);
    assert(bind(listener, (const struct sockaddr *)&local, size) == 0);
    assert(getsockname(listener, (struct sockaddr *)&local, &size) == 0 && listen(listener, 2) == 0);
    int outsider = socket(AF_INET6, SOCK_STREAM, 0);
    assert(connect(outsider, (const struct sockaddr *)&local, size) == 0);
    errno = 0;
    assert(accept(listener, NULL, NULL) == -1 && errno == EAGAIN);
    char byte;
    assert(recv(outsider, &byte, 1, 0) == 0);
    close(outsider);
    int ownClient = socket(AF_INET6, SOCK_STREAM, 0);
    assert(VirtyAllowConsoleClient(ownClient));
    assert(!VirtyValidateConsoleClient(ownClient));
    assert(connect(ownClient, (const struct sockaddr *)&local, size) == 0);
    int accepted = accept(listener, NULL, NULL);
    assert(accepted >= 0 && VirtyValidateConsoleClient(ownClient));
    VirtyRevokeConsoleClient(ownClient);
    assert(!VirtyValidateConsoleClient(ownClient));
    close(ownClient); close(accepted); close(listener);
    assert(!VirtyAllowConsoleClient(-1));

    // Historical bind count must not exhaust tracking. Repeatedly reuse one
    // descriptor, then occupy it with a file so the next listener uses another.
    for (int i = 0; i < 256; i++) {
        fd = socket(AF_INET6, SOCK_STREAM, 0);
        local.sin6_port = 0;
        assert(bind(fd, (const struct sockaddr *)&local, sizeof(local)) == 0);
        close(fd);
    }
    int held = open("/dev/null", O_RDONLY);
    assert(held >= 0);
    listener = socket(AF_INET6, SOCK_STREAM, 0);
    assert(bind(listener, (const struct sockaddr *)&local, sizeof(local)) == 0);
    size = sizeof(local);
    assert(getsockname(listener, (struct sockaddr *)&local, &size) == 0);
    assert(listen(listener, 1) == 0 && VirtyValidateLoopbackListener(ntohs(local.sin6_port)));
    close(listener); close(held);

    // Capacity applies to simultaneous bound sockets and refuses before bind.
    // Freeing a descriptor (including reuse via dup2) makes space again.
    int bound[64];
    for (int i = 0; i < 64; i++) {
        bound[i] = socket(AF_INET6, SOCK_STREAM, 0); local.sin6_port = 0;
        assert(bind(bound[i], (const struct sockaddr *)&local, sizeof(local)) == 0);
    }
    fd = socket(AF_INET6, SOCK_STREAM, 0); local.sin6_port = 0;
    errno = 0;
    assert(bind(fd, (const struct sockaddr *)&local, sizeof(local)) == -1 && errno == ENOBUFS);
    size = sizeof(local);
    assert(getsockname(fd, (struct sockaddr *)&local, &size) == 0 && local.sin6_port == 0);
    held = open("/dev/null", O_RDONLY); assert(held >= 0);
    assert(dup2(held, bound[0]) == bound[0]);
    assert(bind(fd, (const struct sockaddr *)&local, sizeof(local)) == 0);
    assert(getsockname(fd, (struct sockaddr *)&local, &size) == 0);
    assert(listen(fd, 1) == 0 && VirtyValidateLoopbackListener(ntohs(local.sin6_port)));
    close(fd); close(held);
    for (int i = 0; i < 64; i++) close(bound[i]);
    puts("Loopback checks passed: confinement, listener verification, client admission, descriptor churn/reuse and capacity recovery.");
}
