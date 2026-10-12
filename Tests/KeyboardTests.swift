import Foundation
import AppKit

@main
struct KeyboardTests {
    static func main() throws {
        let printable = String((32...126).map { Character(UnicodeScalar($0)!) })
        let allPrintable = try USKeyboard.strokes(printable)
        precondition(allPrintable.count == 95)
        let examples = try USKeyboard.strokes("aA![]{}/\\0 ")
        let codes: [UInt16] = [0, 0, 18, 33, 30, 33, 30, 44, 42, 29, 49]
        precondition(examples.map(\.code) == codes)
        precondition(examples.map(\.shift) == [false,true,true,false,false,true,true,false,false,false,false])
        precondition(USKeyboard.eventFlags([.shift]).rawValue == NSEvent.ModifierFlags.shift.rawValue | 0x2)
        precondition(USKeyboard.eventFlags([.control, .command]).rawValue == (NSEvent.ModifierFlags.control.rawValue | NSEvent.ModifierFlags.command.rawValue | 0x9))
        precondition(USKeyboard.eventFlags([]).rawValue == 0)
        precondition(["home", "end", "pageup", "pagedown"].map { USKeyboard.special[$0]! } == [115,119,116,121])
        for y in [3, -2, 10, -10] {
            let scroll = try GuestScroll.event(deltaY: y)
            precondition(scroll.type == .scrollWheel && scroll.deltaX == 0 && scroll.deltaY == Double(-y))
            precondition(scroll.modifierFlags.intersection(.deviceIndependentFlagsMask).isEmpty)
        }
        for y in [0, 11, -11, Int.max] {
            do { _ = try GuestScroll.event(deltaY: y); fatalError("Invalid scroll accepted") }
            catch is Failure { }
        }
        for text in ["🔐", "é", "a\u{8}"] {
            do { _ = try USKeyboard.strokes(text); fatalError("Unsupported input accepted") }
            catch is Failure { }
        }
        do { _ = try USKeyboard.strokes("abc\n", allowControl: false); fatalError("Secret newline accepted") }
        catch is Failure { }
        print("Guest input checks passed: ANSI US typing, modifiers, navigation keys, bounded scroll directions and unsupported-input rejection.")
    }
}
