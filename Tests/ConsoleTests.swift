import Foundation
import ImageIO
import CoreGraphics

private final class Fixture: ConsoleByteStream {
    var incoming: Data
    var writes: [Data] = []
    let idle: Bool
    init(_ incoming: Data, idle: Bool = false) { self.incoming = incoming; self.idle = idle }
    func read(_ count: Int) throws -> Data {
        guard count <= incoming.count else { throw Failure("Fixture ended before a complete frame.") }
        let value = Data(incoming.prefix(count)); incoming.removeFirst(count); return value
    }
    func write(_ data: Data) { writes.append(data) }
    func messageByte(allowIdle: Bool) throws -> UInt8? {
        if idle && allowIdle && incoming.isEmpty { return nil }
        return try read(1)[0]
    }
}

@main
struct ConsoleTests {
    static func bytes16(_ value: Int) -> Data { Data([UInt8((value >> 8) & 255), UInt8(value & 255)]) }
    static func bytes32(_ value: UInt32) -> Data { Data([UInt8((value >> 24) & 255), UInt8((value >> 16) & 255), UInt8((value >> 8) & 255), UInt8(value & 255)]) }
    static func handshake(width: Int = 2, height: Int = 1, result: UInt32 = 0, nameSize: UInt32 = 0) -> Data {
        Data("RFB 003.008\n".utf8) + Data([1,2]) + Data(repeating: 0, count: 16) + bytes32(result) +
            bytes16(width) + bytes16(height) + Data(repeating: 0, count: 16) + bytes32(nameSize)
    }
    static func rectangle(x: Int, width: Int = 1, encoding: UInt32 = 0, pixels: Data = Data([255,0,0,0])) -> Data {
        bytes16(x) + bytes16(0) + bytes16(width) + bytes16(1) + bytes32(encoding) + pixels
    }
    static func update(_ rectangles: [Data]) -> Data { Data([0,0]) + bytes16(rectangles.count) + rectangles.reduce(Data(), +) }
    private static func decode(_ fixture: Fixture) throws -> ConsoleFrame {
        try ConsoleFrameDecoder.read(connection: fixture, width: 2, height: 1) { challenge in
            precondition(challenge == Data(repeating: 0, count: 16))
            return Data(repeating: 7, count: 16) // Public fixture response, no credential.
        }
    }
    private static func refused(_ fixture: Fixture, containing: String) {
        do { _ = try decode(fixture); fatalError("Malformed console input accepted") }
        catch { precondition(error.localizedDescription.contains(containing), error.localizedDescription) }
    }
    static func main() throws {
        let clipboard = Data([3,0,0,0]) + bytes32(12) + Data("public-test!".utf8)
        let fixture = Fixture(handshake() + Data([2]) + clipboard + update([
            rectangle(x: 0), rectangle(x: 1, pixels: Data([0,255,0,0]))
        ]))
        let frame = try decode(fixture)
        precondition(frame.width == 2 && frame.height == 1)
        precondition(frame.png.prefix(8) == Data([137,80,78,71,13,10,26,10]))
        let source = CGImageSourceCreateWithData(frame.png as CFData, nil)!
        let image = CGImageSourceCreateImageAtIndex(source, 0, nil)!
        var pixels = [UInt8](repeating: 0, count: 8)
        pixels.withUnsafeMutableBytes { data in
            let context = CGContext(data: data.baseAddress, width: 2, height: 1, bitsPerComponent: 8,
                bytesPerRow: 8, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGBitmapInfo.byteOrder32Big.rawValue | CGImageAlphaInfo.premultipliedLast.rawValue)!
            context.draw(image, in: CGRect(x: 0, y: 0, width: 2, height: 1))
        }
        precondition(pixels == [255,0,0,255, 0,255,0,255], "RGB byte order or alpha is incorrect")
        precondition(fixture.writes.count == 7 && fixture.writes[1] == Data([2]))
        precondition(fixture.writes[2] == Data(repeating: 7, count: 16))
        precondition(fixture.writes.last == Data([3,0,0,0,0,0,0,2,0,1]))
        precondition(!fixture.writes.contains { $0.first == 6 }, "Clipboard data must not be sent")

        let persistent = Fixture(handshake() + update([rectangle(x: 0, width: 2, pixels: Data(repeating: 0, count: 8))]) +
            update([rectangle(x: 0, width: 2, pixels: Data(repeating: 255, count: 8))]))
        let session = try ConsoleFrameSession(connection: persistent) { _ in Data(repeating: 7, count: 16) }
        let first = try session.capture(), second = try session.capture()
        precondition(first.png != second.png && persistent.writes.count == 8, "Captures must share one authenticated session and return changed pixels")

        let partial = Fixture(handshake() + update([rectangle(x: 0, width: 2, pixels: Data(repeating: 0, count: 8))]) +
            update([rectangle(x: 1, pixels: Data([255,0,0,0]))]) + update([]))
        let partialSession = try ConsoleFrameSession(connection: partial) { _ in Data(repeating: 7, count: 16) }
        let baseline = try partialSession.capture(), changedPartial = try partialSession.capture(), unchanged = try partialSession.capture()
        precondition(baseline.png != changedPartial.png && changedPartial.png == unchanged.png,
                     "Partial and empty updates must preserve previously verified pixels")
        precondition(!baseline.cached && !changedPartial.cached && unchanged.cached && changedPartial.updatedAt == unchanged.updatedAt)

        let idle = Fixture(handshake() + update([rectangle(x: 0, width: 2, pixels: Data(repeating: 0, count: 8))]), idle: true)
        let idleSession = try ConsoleFrameSession(connection: idle) { _ in Data(repeating: 7, count: 16) }
        let active = try idleSession.capture(), retained = try idleSession.capture()
        precondition(!active.cached && retained.cached && active.png == retained.png && active.updatedAt == retained.updatedAt)
        // No-data is only safe between complete messages after full coverage.
        refused(Fixture(handshake(), idle: true), containing: "complete frame")
        let torn = Fixture(handshake() + update([rectangle(x: 0, width: 2, pixels: Data(repeating: 0, count: 8))]) + Data([0,0]), idle: true)
        let tornSession = try ConsoleFrameSession(connection: torn) { _ in Data(repeating: 7, count: 16) }
        _ = try tornSession.capture()
        do { _ = try tornSession.capture(); fatalError("Truncated update used the cached image") }
        catch { precondition(error.localizedDescription.contains("complete frame")) }

        // Preserve the connection across a boot display resize and request the
        // complete new frame when the old-size request cannot cover it.
        let resized = Fixture(handshake() + update([rectangle(x: 0, width: 2, pixels: Data(repeating: 0, count: 8))]) +
            update([rectangle(x: 0, width: 3, encoding: 0xffff_ff21, pixels: Data())]) +
            update([rectangle(x: 0, width: 3, pixels: Data(repeating: 1, count: 12))]))
        let resizeSession = try ConsoleFrameSession(connection: resized) { _ in Data(repeating: 7, count: 16) }
        _ = try resizeSession.capture()
        let changed = try resizeSession.capture()
        precondition(changed.width == 3 && changed.height == 1 && resized.writes.count == 9)
        precondition(resized.writes.last == Data([3,0,0,0,0,0,0,3,0,1]))

        refused(Fixture(Data("RFB 003.008\n".utf8) + Data([1,1])), containing: "authentication support")
        refused(Fixture(handshake(result: 1)), containing: "authentication failed")
        refused(Fixture(handshake(width: 3) + update([rectangle(x: 0, width: 3, pixels: Data(repeating: 0, count: 12))])), containing: "dimensions differ")
        refused(Fixture(handshake(nameSize: 1_048_577)), containing: "name exceeds")
        refused(Fixture(handshake() + update([rectangle(x: 2)])), containing: "out-of-bounds")
        refused(Fixture(handshake() + update([rectangle(x: 0, encoding: 1)])), containing: "Unsupported")
        // Overlap cannot substitute for complete coverage of every guest pixel.
        refused(Fixture(handshake() + update([rectangle(x: 0), rectangle(x: 0)])), containing: "complete frame")
        refused(Fixture(handshake() + update(Array(repeating: rectangle(x: 0), count: 9))), containing: "byte limit")
        refused(Fixture(handshake() + Data([0,0]) + bytes16(4097)), containing: "rectangle limit")
        refused(Fixture(handshake() + Data([3,0,0,0]) + bytes32(1_048_577)), containing: "clipboard message exceeds")
        refused(Fixture(handshake() + Data(repeating: 2, count: 64)), containing: "complete guest frame")
        print("Console checks passed: authenticated negotiation, PNG colour, persistent/partial/empty updates, resize coverage, clipboard discard and malformed/bounded-frame refusal.")
    }
}
