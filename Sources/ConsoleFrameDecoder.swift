import Foundation
import ImageIO
import CoreGraphics

struct ConsoleFrame {
    let png: Data
    let width: Int
    let height: Int
    let cached: Bool
    let updatedAt: Date
}

protocol ConsoleByteStream {
    func read(_ count: Int) throws -> Data
    func write(_ data: Data) throws
    func messageByte(allowIdle: Bool) throws -> UInt8?
}

extension ConsoleByteStream {
    func messageByte(allowIdle: Bool) throws -> UInt8? { try read(1)[0] }
    func u16() throws -> UInt16 { let bytes = try read(2); return UInt16(bytes[0]) << 8 | UInt16(bytes[1]) }
    func u32() throws -> UInt32 { let bytes = try read(4); return UInt32(bytes[0]) << 24 | UInt32(bytes[1]) << 16 | UInt32(bytes[2]) << 8 | UInt32(bytes[3]) }
}

// Parse only negotiated raw framebuffer updates, with independent size, byte,
// rectangle and message limits. Require full pixel coverage before returning.
enum ConsoleFrameDecoder {
    static func read(connection: ConsoleByteStream, width: Int, height: Int,
                     challengeResponse: (Data) -> Data?) throws -> ConsoleFrame {
        let session = try ConsoleFrameSession(connection: connection, challengeResponse: challengeResponse)
        let frame = try session.capture()
        try validate(frame: frame, width: width, height: height)
        return frame
    }
    static func validate(frame: ConsoleFrame, width: Int, height: Int) throws {
        guard frame.width == width && frame.height == height else {
            throw Failure("Private console dimensions differ from the configured guest display (received \(frame.width)x\(frame.height), expected \(width)x\(height)); retry after guest boot or mode change.")
        }
    }
}

// Keep one authenticated connection alive across captures and display modes.
// Apple's private observer lifecycle is fragile when the last client reconnects.
final class ConsoleFrameSession {
    private let connection: ConsoleByteStream
    private var width: Int
    private var height: Int
    private var pixels = Data()
    private var coverage = [UInt8]()
    private var remaining = 0
    private var updatedAt: Date?

    init(connection: ConsoleByteStream, challengeResponse: (Data) -> Data?) throws {
        self.connection = connection
        width = 0; height = 0
        guard try connection.read(12) == Data("RFB 003.008\n".utf8) else { throw Failure("Unsupported private console protocol.") }
        try connection.write(Data("RFB 003.008\n".utf8))
        let count = Int(try connection.read(1)[0])
        guard count > 0, try connection.read(count).contains(2) else { throw Failure("Private console requires VNC authentication support.") }
        try connection.write(Data([2]))
        guard let response = challengeResponse(try connection.read(16)), response.count == 16 else { throw Failure("Cannot authenticate the private console.") }
        try connection.write(response)
        guard try connection.u32() == 0 else { throw Failure("Private console authentication failed.") }
        try connection.write(Data([1])) // Shared connection; preserve the local console.
        let header = try connection.read(24)
        width = Self.u16(header, 0); height = Self.u16(header, 2)
        let nameSize = Int(Self.u32(header, 20))
        guard nameSize <= 1024 * 1024 else { throw Failure("Private console name exceeds the protocol limit.") }
        _ = try connection.read(nameSize)
        try Self.validateSize(width: width, height: height)
        resetPixels()
        // 32-bit little-endian true colour, with R/G/B at byte offsets 0/1/2.
        try connection.write(Data([0,0,0,0, 32,24,0,1, 0,255,0,255,0,255, 0,8,16,0,0,0]))
        var encodings = Data([2,0,0,3])
        for value: UInt32 in [0, 0xffff_ff21, 0xffff_ff20] { Self.append32(value, to: &encodings) }
        try connection.write(encodings)
    }

    private func requestFrame() throws {
        var request = Data([3,0,0,0,0,0])
        Self.append16(width, to: &request); Self.append16(height, to: &request)
        try connection.write(request)
    }

    func capture() throws -> ConsoleFrame {
        try requestFrame()
        var totalBytes = 0
        var receivedPixels = false
        for _ in 0..<64 {
            let kind: UInt8
            do {
                guard let next = try connection.messageByte(allowIdle: remaining == 0) else {
                    return try encode(cached: !receivedPixels)
                }
                kind = next
            }
            catch { throw Failure("Private console update unavailable (\(remaining) pixels still unknown): \(error.localizedDescription)") }
            if kind == 2 { continue } // Bell.
            if kind == 3 {
                _ = try connection.read(3)
                let size = Int(try connection.u32())
                guard size <= 1024 * 1024 else { throw Failure("Private console clipboard message exceeds the protocol limit.") }
                _ = try connection.read(size) // Discard without touching either clipboard.
                continue
            }
            guard kind == 0 else { throw Failure("Unsupported private console message.") }
            _ = try connection.read(1)
            let rectangles = Int(try connection.u16())
            guard rectangles <= 4096 else { throw Failure("Private console update exceeds the rectangle limit.") }
            var resized = false
            for _ in 0..<rectangles {
                let rectangle = try connection.read(12)
                let x = Self.u16(rectangle, 0), y = Self.u16(rectangle, 2)
                let w = Self.u16(rectangle, 4), h = Self.u16(rectangle, 6)
                let encoding = Int32(bitPattern: Self.u32(rectangle, 8))
                if encoding == -224 { break }
                if encoding == -223 {
                    try Self.validateSize(width: w, height: h)
                    if w != width || h != height {
                        width = w; height = h
                        resetPixels()
                        totalBytes = 0; resized = true
                    }
                    continue
                }
                guard encoding == 0, w > 0, h > 0, x + w <= width, y + h <= height else {
                    throw Failure("Unsupported or out-of-bounds private console rectangle.")
                }
                totalBytes += w * h * 4
                guard totalBytes <= width * height * 4 * 4 else { throw Failure("Private console frame exceeds the byte limit.") }
                let data = try connection.read(w * h * 4)
                receivedPixels = true; updatedAt = Date()
                for row in 0..<h {
                    let start = ((y + row) * width + x) * 4
                    pixels.replaceSubrange(start..<(start + w * 4), with: data[(row * w * 4)..<((row + 1) * w * 4)])
                    for column in x..<(x + w) {
                        let index = (y + row) * width + column
                        if coverage[index] == 0 { coverage[index] = 1; remaining -= 1 }
                    }
                }
            }
            if resized && remaining != 0 { try requestFrame() }
            if remaining == 0 {
                return try encode(cached: !receivedPixels)
            }
        }
        throw Failure("Private console did not deliver a complete guest frame.")
    }

    private func encode(cached: Bool) throws -> ConsoleFrame {
        guard remaining == 0, let updatedAt else { throw Failure("Private console has no complete guest frame.") }
        guard let provider = CGDataProvider(data: pixels as CFData),
              let image = CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32,
                                  bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGBitmapInfo.byteOrder32Big.union(CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue)),
                                  provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent) else {
            throw Failure("Cannot construct the guest framebuffer image.")
        }
        let png = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(png, "public.png" as CFString, 1, nil) else { throw Failure("Cannot create guest PNG encoder.") }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { throw Failure("Cannot encode guest framebuffer PNG.") }
        return ConsoleFrame(png: png as Data, width: width, height: height, cached: cached, updatedAt: updatedAt)
    }

    private func resetPixels() {
        pixels = Data(count: width * height * 4)
        coverage = [UInt8](repeating: 0, count: width * height)
        remaining = width * height
        updatedAt = nil
    }

    private static func validateSize(width: Int, height: Int) throws {
        guard width > 0, height > 0, width <= 8192, height <= 8192,
              width * height <= 16 * 1024 * 1024 else { throw Failure("Private console dimensions exceed the protocol limit.") }
    }
    private static func u16(_ data: Data, _ offset: Int) -> Int { Int(data[offset]) << 8 | Int(data[offset + 1]) }
    private static func u32(_ data: Data, _ offset: Int) -> UInt32 {
        UInt32(data[offset]) << 24 | UInt32(data[offset + 1]) << 16 | UInt32(data[offset + 2]) << 8 | UInt32(data[offset + 3])
    }
    private static func append16(_ value: Int, to data: inout Data) { data.append(UInt8(value >> 8)); data.append(UInt8(value & 255)) }
    private static func append32(_ value: UInt32, to data: inout Data) {
        for shift in [24,16,8,0] { data.append(UInt8((value >> shift) & 255)) }
    }
}
