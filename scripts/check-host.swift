import Foundation
import ObjectiveC
import Virtualization

// Metadata only: never instantiate private objects, create a VM, or start a server.
func hasInstanceSelector(_ className: String, _ selectorName: String) -> Bool {
    guard let runtimeClass = NSClassFromString(className) else { return false }
    return class_getInstanceMethod(runtimeClass, NSSelectorFromString(selectorName)) != nil
}

#if arch(arm64)
let architecture = "arm64"
#else
let architecture = "other"
#endif

let virtualizationSupported = VZVirtualMachine.isSupported
let result: [String: Any] = [
    "architecture": architecture,
    "hostOS": ProcessInfo.processInfo.operatingSystemVersionString,
    "virtualizationSupported": virtualizationSupported,
    "publicVMViewPresent": NSClassFromString("VZVirtualMachineView") != nil,
    "macGuestProvisioningPresent": NSClassFromString("VZMacGuestProvisioningOptions") != nil,
    "privateVNC": [
        "serverClassPresent": NSClassFromString("_VZVNCServer") != nil,
        "authenticationClassPresent": NSClassFromString("_VZVNCAuthenticationSecurityConfiguration") != nil,
        "serverInitializerPresent": hasInstanceSelector("_VZVNCServer", "initWithPort:queue:securityConfiguration:"),
        "authenticationInitializerPresent": hasInstanceSelector("_VZVNCAuthenticationSecurityConfiguration", "initWithPassword:"),
        "vmSetterPresent": hasInstanceSelector("_VZVNCServer", "setVirtualMachine:"),
        "startPresent": hasInstanceSelector("_VZVNCServer", "start"),
        "stopPresent": hasInstanceSelector("_VZVNCServer", "stop"),
        "portGetterPresent": hasInstanceSelector("_VZVNCServer", "port"),
    ],
    "limitations": [
        "No guest was started or tested.",
        "No virtualization entitlement was validated.",
        "Private symbol presence does not validate private API behavior.",
    ],
]

let json = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(json)
FileHandle.standardOutput.write(Data("\n".utf8))
if !virtualizationSupported || architecture != "arm64" {
    exit(1)
}
