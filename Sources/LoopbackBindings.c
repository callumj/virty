// Loaded as a separate dylib: dyld does not apply this interpose from the main executable.
// The private VZ console has no public bind-address setting. Restrict this worker's
// explicit IP binds before starting it, and verify its actual listening socket.
#include "LoopbackBindings.h"
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <netinet/tcp_fsm.h>
#include <string.h>
#include <errno.h>
#include <stdatomic.h>
#include <pthread.h>
#include <unistd.h>

static atomic_bool enabled = false;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static int sockets[64];
static size_t socketCount = 0;
static struct { int fd; uint16_t port; bool accepted; } clients[16];
static size_t clientCount = 0;

void VirtyEnableLoopbackBindings(void) { atomic_store(&enabled, true); }

// Reclaim closed descriptors, non-socket reuse and sockets not yet bound. A
// descriptor number is not a permanent identity. Actual listener validation
// below still checks the current kernel address and TCP state on every call.
static void pruneSockets(void) {
    for (size_t i = 0; i < socketCount;) {
        struct sockaddr_storage address = {0};
        socklen_t size = sizeof(address);
        bool bound = getsockname(sockets[i], (struct sockaddr *)&address, &size) == 0 &&
            ((address.ss_family == AF_INET && ((struct sockaddr_in *)&address)->sin_port != 0) ||
             (address.ss_family == AF_INET6 && ((struct sockaddr_in6 *)&address)->sin6_port != 0));
        if (!bound) sockets[i] = sockets[--socketCount];
        else i++;
    }
}

static int loopbackBind(int fd, const struct sockaddr *address, socklen_t size) {
    if (!atomic_load(&enabled) || !address || (address->sa_family != AF_INET && address->sa_family != AF_INET6))
        return bind(fd, address, size);
    struct sockaddr_storage storage = {0};
    if (size > sizeof(storage)) { errno = EINVAL; return -1; }
    memcpy(&storage, address, size);
    if (address->sa_family == AF_INET6) {
        if (size != sizeof(struct sockaddr_in6)) { errno = EINVAL; return -1; }
        struct sockaddr_in6 *local = (struct sockaddr_in6 *)&storage;
        if (IN6_IS_ADDR_UNSPECIFIED(&local->sin6_addr)) local->sin6_addr = in6addr_loopback;
        if (!IN6_IS_ADDR_LOOPBACK(&local->sin6_addr)) { errno = EACCES; return -1; }
    } else {
        if (size != sizeof(struct sockaddr_in)) { errno = EINVAL; return -1; }
        struct sockaddr_in *local = (struct sockaddr_in *)&storage;
        if (local->sin_addr.s_addr == INADDR_ANY) local->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        if (local->sin_addr.s_addr != htonl(INADDR_LOOPBACK)) { errno = EACCES; return -1; }
    }
    pthread_mutex_lock(&lock);
    pruneSockets();
    bool tracked = false;
    for (size_t i = 0; i < socketCount; i++) tracked |= sockets[i] == fd;
    // Bound simultaneous usage, not the number of binds over the worker's life.
    // Refuse before bind rather than create a socket we cannot verify later.
    if (!tracked && socketCount == sizeof(sockets) / sizeof(sockets[0])) {
        pthread_mutex_unlock(&lock); errno = ENOBUFS; return -1;
    }
    int result = bind(fd, (const struct sockaddr *)&storage, size);
    int savedErrno = errno;
    if (result == 0) {
        if (!tracked) sockets[socketCount++] = fd;
    }
    pthread_mutex_unlock(&lock);
    errno = savedErrno;
    return result;
}

bool VirtyValidateLoopbackListener(uint16_t port) {
    bool valid = false;
    pthread_mutex_lock(&lock);
    pruneSockets();
    for (size_t i = 0; i < socketCount; i++) {
        struct sockaddr_storage address = {0};
        socklen_t size = sizeof(address);
        struct tcp_connection_info info = {0};
        socklen_t optionSize = sizeof(info);
        if (getsockname(sockets[i], (struct sockaddr *)&address, &size) != 0 ||
            getsockopt(sockets[i], IPPROTO_TCP, TCP_CONNECTION_INFO, &info, &optionSize) != 0 || info.tcpi_state != TCPS_LISTEN) continue;
        if (address.ss_family == AF_INET6) {
            const struct sockaddr_in6 *local = (const struct sockaddr_in6 *)&address;
            valid |= IN6_IS_ADDR_LOOPBACK(&local->sin6_addr) && ntohs(local->sin6_port) == port;
        } else if (address.ss_family == AF_INET) {
            const struct sockaddr_in *local = (const struct sockaddr_in *)&address;
            valid |= local->sin_addr.s_addr == htonl(INADDR_LOOPBACK) && ntohs(local->sin_port) == port;
        }
    }
    pthread_mutex_unlock(&lock);
    return valid && port != 0;
}

// Reserve this process's client source port before connecting. Other local
// processes cannot claim it while the socket is open, and cannot reach the
// private server's fragile unauthenticated-client lifecycle.
bool VirtyAllowConsoleClient(int fd) {
    struct sockaddr_in6 address = {0};
    address.sin6_len = sizeof(address); address.sin6_family = AF_INET6;
    address.sin6_addr = in6addr_loopback;
    socklen_t size = sizeof(address);
    if (!atomic_load(&enabled) || bind(fd, (const struct sockaddr *)&address, size) != 0 ||
        getsockname(fd, (struct sockaddr *)&address, &size) != 0) return false;
    pthread_mutex_lock(&lock);
    bool valid = clientCount < sizeof(clients) / sizeof(clients[0]);
    if (valid) {
        clients[clientCount].fd = fd;
        clients[clientCount].port = address.sin6_port;
        clients[clientCount].accepted = false;
        clientCount++;
    }
    pthread_mutex_unlock(&lock);
    return valid;
}

void VirtyRevokeConsoleClient(int fd) {
    pthread_mutex_lock(&lock);
    for (size_t i = 0; i < clientCount; i++) {
        if (clients[i].fd == fd) { clients[i] = clients[--clientCount]; break; }
    }
    pthread_mutex_unlock(&lock);
}

bool VirtyValidateConsoleClient(int fd) {
    bool valid = false;
    pthread_mutex_lock(&lock);
    for (size_t i = 0; i < clientCount; i++) if (clients[i].fd == fd) valid = clients[i].accepted;
    pthread_mutex_unlock(&lock);
    return valid;
}

static int consoleAccept(int listener, struct sockaddr *address, socklen_t *size) {
    int fd = accept(listener, address, size);
    if (fd < 0 || !atomic_load(&enabled)) return fd;
    struct sockaddr_storage peer = {0}; socklen_t peerSize = sizeof(peer);
    if (getpeername(fd, (struct sockaddr *)&peer, &peerSize) != 0) { close(fd); errno = EAGAIN; return -1; }
    // The owner-only control socket is AF_UNIX and remains independent.
    if (peer.ss_family != AF_INET && peer.ss_family != AF_INET6) return fd;
    bool allowed = false;
    if (peer.ss_family == AF_INET6 && IN6_IS_ADDR_LOOPBACK(&((struct sockaddr_in6 *)&peer)->sin6_addr)) {
        uint16_t port = ((struct sockaddr_in6 *)&peer)->sin6_port;
        pthread_mutex_lock(&lock);
        for (size_t i = 0; i < clientCount; i++) if (clients[i].port == port) {
            allowed = true; clients[i].accepted = true; break;
        }
        pthread_mutex_unlock(&lock);
    }
    if (!allowed) { close(fd); errno = EAGAIN; return -1; }
    return fd;
}

__attribute__((used)) static const struct { const void *replacement; const void *replacee; } mapping
    __attribute__((section("__DATA,__interpose,interposing"))) = {
        (const void *)(uintptr_t)&loopbackBind, (const void *)(uintptr_t)&bind
    };
__attribute__((used)) static const struct { const void *replacement; const void *replacee; } acceptMapping
    __attribute__((section("__DATA,__interpose,interposing"))) = {
        (const void *)(uintptr_t)&consoleAccept, (const void *)(uintptr_t)&accept
    };
