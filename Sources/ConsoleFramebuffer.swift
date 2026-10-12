import Foundation
import Darwin

// A bounded, read-only RFB client connected exclusively to the worker's verified
// IPv6 loopback listener. No clipboard messages are sent or exposed.
// Socket work is confined to queue; the console credential is immutable and its
// port is provided by the private server's synchronized accessor.
final class ConsoleFramebuffer: @unchecked Sendable {
    private let console: VirtyVNCConsole
    private let queue = DispatchQueue(label: "com.virty.console.capture")
    private var connection: RFBConnection?
    private var session: ConsoleFrameSession?
    private var failed = false

    init(console: VirtyVNCConsole) { self.console = console }

    func validate() async throws {
        for _ in 0..<100 {
            if console.port != 0 { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        guard console.validateListener() else {
            console.stop()
            throw Failure("Private console listener could not be verified as loopback-only; it was stopped.")
        }
    }

    func capture(width: Int, height: Int) async throws -> ConsoleFrame {
        try await validate()
        return try await withCheckedThrowingContinuation { continuation in
            queue.async {
                do { continuation.resume(returning: try self.readFrame(width: width, height: height)) }
                catch { continuation.resume(throwing: error) }
            }
        }
    }

    private func readFrame(width: Int, height: Int) throws -> ConsoleFrame {
        guard !failed else { throw Failure("Private console connection failed; restart this VM's worker to recover.") }
        let frame: ConsoleFrame
        do {
            if session == nil {
                connection = try RFBConnection(port: console.port)
                session = try ConsoleFrameSession(connection: connection!) { self.console.response(forChallenge: $0) }
            }
            connection!.renewDeadline()
            frame = try session!.capture()
        } catch {
            failed = true
            throw error
        }
        // Keep the authenticated connection during temporary guest boot modes.
        try ConsoleFrameDecoder.validate(frame: frame, width: width, height: height)
        return frame
    }

    @MainActor func stop() { console.stop() }
}

private final class RFBConnection: ConsoleByteStream {
    private let descriptor: Int32
    private var deadline = ProcessInfo.processInfo.systemUptime + 10
    func renewDeadline() { deadline = ProcessInfo.processInfo.systemUptime + 10 }
    init(port: UInt16) throws {
        descriptor = socket(AF_INET6, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw Failure("Cannot create the private console connection.") }
        guard VirtyAllowConsoleClient(descriptor) else {
            close(descriptor); throw Failure("Cannot reserve the manager's private console client.")
        }
        var timeout = timeval(tv_sec: 5, tv_usec: 0)
        var enabled: Int32 = 1
        _ = setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        _ = setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        _ = setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &enabled, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_in6()
        address.sin6_len = UInt8(MemoryLayout<sockaddr_in6>.size)
        address.sin6_family = sa_family_t(AF_INET6)
        address.sin6_port = port.bigEndian
        address.sin6_addr = in6addr_loopback
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in6>.size)) }
        }
        if result != 0 {
            VirtyRevokeConsoleClient(descriptor); close(descriptor)
            throw Failure("Cannot connect to the private loopback console.")
        }
    }
    deinit { VirtyRevokeConsoleClient(descriptor); close(descriptor) }
    func messageByte(allowIdle: Bool) throws -> UInt8? {
        if allowIdle {
            var event = pollfd(fd: descriptor, events: Int16(POLLIN), revents: 0)
            let waitUntil = ProcessInfo.processInfo.systemUptime + 0.3
            while true {
                let remaining = waitUntil - ProcessInfo.processInfo.systemUptime
                if remaining <= 0 { return nil }
                let result = poll(&event, 1, Int32(max(1, Int(remaining * 1000))))
                if result < 0, errno == EINTR { continue }
                if result == 0 { return nil } // RFB may wait indefinitely for changed pixels.
                guard result > 0 else { throw Failure("Cannot inspect the private console connection.") }
                break
            }
            // POLLHUP/POLLERR are read below and reported as errors, never idle.
        }
        return try read(1)[0]
    }
    func read(_ count: Int) throws -> Data {
        guard count >= 0, count <= 64 * 1024 * 1024 else { throw Failure("Private console read exceeds the protocol limit.") }
        var result = Data(count: count)
        try result.withUnsafeMutableBytes { bytes in
            var offset = 0
            while offset < count {
                try setRemainingTimeout(SO_RCVTIMEO)
                let amount = recv(descriptor, bytes.baseAddress!.advanced(by: offset), count - offset, 0)
                if amount < 0, errno == EINTR { continue }
                guard amount > 0 else { throw Failure("Private console frame unavailable or timed out.") }
                offset += amount
            }
        }
        guard VirtyValidateConsoleClient(descriptor) else { throw Failure("Private console client admission could not be verified.") }
        return result
    }
    func write(_ data: Data) throws {
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < data.count {
                try setRemainingTimeout(SO_SNDTIMEO)
                let amount = send(descriptor, bytes.baseAddress!.advanced(by: offset), data.count - offset, 0)
                if amount < 0, errno == EINTR { continue }
                guard amount > 0 else { throw Failure("Cannot send the private console request.") }
                offset += amount
            }
        }
    }
    private func setRemainingTimeout(_ option: Int32) throws {
        let remaining = deadline - ProcessInfo.processInfo.systemUptime
        guard remaining > 0 else { throw Failure("Private console frame timed out.") }
        var timeout = timeval(tv_sec: Int(remaining), tv_usec: Int32(max(1, Int(remaining.truncatingRemainder(dividingBy: 1) * 1_000_000))))
        guard setsockopt(descriptor, SOL_SOCKET, option, &timeout, socklen_t(MemoryLayout<timeval>.size)) == 0 else {
            throw Failure("Cannot enforce the private console frame timeout.")
        }
    }
}
