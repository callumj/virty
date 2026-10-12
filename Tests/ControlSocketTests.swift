import Foundation
import Darwin

@main
struct ControlSocketTests {
    static func pause(_ seconds: TimeInterval) { Thread.sleep(forTimeInterval: seconds) }
    static func connect(_ path: String) -> Int32 {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        precondition(fd >= 0)
        var noSignal: Int32 = 1, timeout = timeval(tv_sec: 2, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        _ = withUnsafeMutablePointer(to: &address.sun_path) { ptr in
            path.withCString { strcpy(UnsafeMutableRawPointer(ptr).assumingMemoryBound(to: CChar.self), $0) }
        }
        let result = withUnsafePointer(to: &address) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        precondition(result == 0)
        return fd
    }
    @discardableResult static func sendText(_ fd: Int32, _ text: String) -> Int {
        let bytes = Array(text.utf8)
        return bytes.withUnsafeBytes { send(fd, $0.baseAddress, $0.count, 0) }
    }
    static func reply(_ fd: Int32) -> String {
        var buffer = [UInt8](repeating: 0, count: 1024)
        let count = recv(fd, &buffer, buffer.count, 0)
        return count > 0 ? String(decoding: buffer.prefix(count), as: UTF8.self) : ""
    }
    static func ordinary(_ path: String) {
        let fd = connect(path); defer { close(fd) }
        precondition(sendText(fd, "{}\n") == 3)
        precondition(reply(fd).contains("\"ok\":true"))
    }
    static func main() async throws {
        let directory = URL(fileURLWithPath: "/tmp/virty-control-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let path = directory.appendingPathComponent("control.sock").path
        let server = ControlSocket(path: path, ioTimeout: 0.8, maxClients: 4)
        try server.start { request in
            if request["large"] as? Bool == true { return ["padding": String(repeating: "x", count: 4 * 1024 * 1024)] }
            return ["ok": true]
        }
        defer { server.stop() }
        await Task.detached {
            let slow = connect(path)
            sendText(slow, "{")
            pause(0.05)
            // A complete request must finish before the slow client's deadline.
            let start = ProcessInfo.processInfo.systemUptime
            ordinary(path)
            precondition(ProcessInfo.processInfo.systemUptime - start < 0.6)
            for _ in 0..<6 { pause(0.2); sendText(slow, " ") }
            let expired = ProcessInfo.processInfo.systemUptime
            precondition(reply(slow).isEmpty) // Trickle input cannot renew the deadline.
            precondition(ProcessInfo.processInfo.systemUptime - expired < 0.3)
            close(slow)
            ordinary(path)

            for input in ["{", "not-json\n", "[]\n", String(repeating: " ", count: 65537) + "\n"] {
                let fd = connect(path)
                sendText(fd, input); shutdown(fd, SHUT_WR)
                precondition(reply(fd).isEmpty)
                close(fd)
            }
            // A peer that disconnects without a request releases its slot.
            close(connect(path)); ordinary(path)

            // Admission is bounded and excess clients fail promptly. Once idle
            // connections time out, the same listener accepts normal work again.
            var idle: [Int32] = []
            for _ in 0..<4 { idle.append(connect(path)) }
            pause(0.1)
            let extra = connect(path)
            let rejected = ProcessInfo.processInfo.systemUptime
            precondition(reply(extra).isEmpty)
            precondition(ProcessInfo.processInfo.systemUptime - rejected < 0.6)
            close(extra)
            pause(1)
            for fd in idle { precondition(reply(fd).isEmpty); close(fd) }
            ordinary(path)

            // A response larger than the peer's receive buffer cannot hold a
            // slot forever; concurrent ordinary responses still complete.
            let reader = connect(path)
            var small: Int32 = 1024
            setsockopt(reader, SOL_SOCKET, SO_RCVBUF, &small, socklen_t(MemoryLayout<Int32>.size))
            sendText(reader, "{\"large\":true}\n")
            pause(0.1)
            ordinary(path)
            pause(1)
            var buffer = [UInt8](repeating: 0, count: 8192)
            var ended = false
            while true {
                let n = recv(reader, &buffer, buffer.count, 0)
                if n <= 0 { ended = n == 0; break }
            }
            precondition(ended)
            close(reader); ordinary(path)
        }.value
        server.stop()
        try server.start { _ in ["ok": true] }
        await Task.detached { ordinary(path) }.value
        print("Control socket checks passed: concurrent reads, absolute deadlines, malformed/oversized requests, bounded admission, slow response readers and listener restart.")
    }
}
