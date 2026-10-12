import Foundation
import Darwin

/// One bounded JSON request per connection. No TCP listener and no secret values.
final class ControlSocket {
    let path: String
    // The accept loop owns close; stop only wakes it. Guard late shutdown
    // against a descriptor that the loop already closed and the OS reused.
    private final class Listener {
        let descriptor: Int32
        private let lock = NSLock()
        private var closed = false
        init(_ descriptor: Int32) { self.descriptor = descriptor }
        func stop() {
            lock.lock(); defer { lock.unlock() }
            if !closed { shutdown(descriptor, SHUT_RDWR) }
        }
        func close() {
            lock.lock(); defer { lock.unlock() }
            if !closed { Darwin.close(descriptor); closed = true }
        }
    }
    private var listener: Listener?
    private let ioTimeout: TimeInterval
    private let clients: DispatchSemaphore
    init(path: String, ioTimeout: TimeInterval = 5, maxClients: Int = 32) {
        precondition(ioTimeout > 0 && ioTimeout.isFinite && maxClients > 0)
        self.path = path
        self.ioTimeout = ioTimeout
        clients = DispatchSemaphore(value: maxClients)
    }

    func start(handler: @escaping ([String: Any]) async -> [String: Any]) throws {
        guard path.utf8.count < MemoryLayout.size(ofValue: sockaddr_un().sun_path) else {
            throw Failure("Control socket path is too long; set a shorter VIRTY_HOME.")
        }
        guard listener == nil else { throw Failure("Control socket is already started.") }
        let descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw Failure("Cannot create control socket.") }
        _ = fcntl(descriptor, F_SETFD, FD_CLOEXEC)
        unlink(path)
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        _ = withUnsafeMutablePointer(to: &address.sun_path) { ptr in
            path.withCString { source in strcpy(UnsafeMutableRawPointer(ptr).assumingMemoryBound(to: CChar.self), source) }
        }
        let bound = withUnsafePointer(to: &address) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard bound == 0, chmod(path, 0o600) == 0, Darwin.listen(descriptor, 8) == 0 else {
            close(descriptor); unlink(path)
            throw Failure("Cannot bind private control socket.")
        }
        let listener = Listener(descriptor), clients = clients, ioTimeout = ioTimeout
        self.listener = listener
        DispatchQueue.global(qos: .userInitiated).async {
            // This loop owns the descriptor until shutdown wakes accept. Closing
            // it elsewhere could let a reused descriptor be accepted or closed.
            defer { listener.close() }
            while true {
                let client = accept(listener.descriptor, nil, nil)
                if client < 0 {
                    if errno == EINTR { continue }
                    return
                }
                _ = fcntl(client, F_SETFD, FD_CLOEXEC)
                var uid: uid_t = 0, gid: gid_t = 0
                guard getpeereid(client, &uid, &gid) == 0, uid == getuid(),
                      clients.wait(timeout: .now()) == .success else { close(client); continue }
                let deadline = ProcessInfo.processInfo.systemUptime + ioTimeout
                // Bounded independent readers keep an incomplete request from
                // holding up accept or the VM/main actor. Excess clients fail fast.
                DispatchQueue.global(qos: .userInitiated).async {
                    var noSignal: Int32 = 1
                    setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
                    guard let request = Self.readRequest(client, deadline: deadline) else {
                        close(client); clients.signal(); return
                    }
                    Task { @MainActor in
                        let response = await handler(request)
                        let data = ((try? JSONSerialization.data(withJSONObject: response, options: [.sortedKeys])) ?? Data("{}".utf8)) + Data([10])
                        // Include response writers and suspended handlers in the
                        // connection budget; slow readers cannot grow it unbounded.
                        DispatchQueue.global(qos: .userInitiated).async {
                            defer { close(client); clients.signal() }
                            let deadline = ProcessInfo.processInfo.systemUptime + ioTimeout
                            data.withUnsafeBytes { bytes in
                                var offset = 0
                                while offset < bytes.count, Self.wait(client, events: Int16(POLLOUT), deadline: deadline) {
                                    let count = send(client, bytes.baseAddress!.advanced(by: offset), bytes.count - offset, MSG_DONTWAIT)
                                    if count < 0 && [EINTR, EAGAIN, EWOULDBLOCK].contains(errno) { continue }
                                    if count <= 0 { break }
                                    offset += count
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private static func wait(_ fd: Int32, events: Int16, deadline: TimeInterval) -> Bool {
        while true {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            guard remaining > 0 else { return false }
            var event = pollfd(fd: fd, events: events, revents: 0)
            let result = poll(&event, 1, Int32(min(Double(Int32.max), max(1, ceil(remaining * 1000)))))
            if result < 0 && errno == EINTR { continue }
            return result > 0 && event.revents & (events | Int16(POLLHUP)) != 0
        }
    }

    private static func readRequest(_ client: Int32, deadline: TimeInterval) -> [String: Any]? {
        var body = Data(), buffer = [UInt8](repeating: 0, count: 4096)
        while body.count <= 65536, !body.contains(10) {
            guard wait(client, events: Int16(POLLIN), deadline: deadline) else { return nil }
            let count = recv(client, &buffer, buffer.count, MSG_DONTWAIT)
            if count < 0 && [EINTR, EAGAIN, EWOULDBLOCK].contains(errno) { continue }
            guard count > 0 else { return nil }
            body.append(contentsOf: buffer.prefix(count))
        }
        guard body.count <= 65536, let end = body.firstIndex(of: 10) else { return nil }
        return (try? JSONSerialization.jsonObject(with: body.prefix(upTo: end))) as? [String: Any]
    }

    func stop() {
        if let listener {
            listener.stop()
            self.listener = nil
            unlink(path)
        }
    }
    deinit { stop() }
}
